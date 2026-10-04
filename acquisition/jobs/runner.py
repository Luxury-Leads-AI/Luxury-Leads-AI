"""Claim one job, run it, write down what happened.

The whole engine works this way because of one constraint: the app runs as
gunicorn with a 30-second request limit, so nothing may take longer than
that. Each job is small and self-contained; the dashboard tab calls
run_next() over and over, and later a worker process calls the same
function in a loop. Nothing about the jobs changes when that happens.

Two things stop work being done twice:
  - a claim marks the row running and holds a lock for LOCK_SECONDS, and
    only one caller can win that claim (on Postgres, SKIP LOCKED makes two
    tabs pick different jobs rather than fight over one);
  - a job that dies mid-run - a tab closed, an instance restarted - has its
    lock expire and comes back to the queue, and its idempotency key stops
    a duplicate being queued in the meantime.
"""
import json
import time
from datetime import datetime, timedelta

from .. import models, settings
from . import registry

# A job gets this long before we assume whoever claimed it has gone away.
LOCK_SECONDS = 90
# How long a caller may keep running jobs before handing control back, so a
# request always answers well inside gunicorn's 30-second limit.
BUDGET_SECONDS = 20
RETRY_BACKOFF_SECONDS = (30, 300, 1800)

_db = None


def init(db):
    global _db
    _db = db


def session():
    """The database session a handler should use. Handlers are given this
    rather than reaching for the app's own, so they stay testable."""
    return _db.session


def enqueue(job_type, payload=None, prospect_id=None, idempotency_key=None,
            run_after=None, max_attempts=3):
    """Add a job. With an idempotency key, asking twice is harmless: the
    job already queued is returned instead of a second copy."""
    if idempotency_key:
        existing = models.Job.query.filter_by(idempotency_key=idempotency_key).first()
        if existing is not None and existing.status in ('queued', 'running'):
            return existing
        if existing is not None:
            # The old attempt is finished; let the key be reused.
            existing.idempotency_key = f"{idempotency_key}#{existing.id}"
            _db.session.commit()

    job = models.Job(
        type=job_type,
        payload=json.dumps(payload or {}),
        prospect_id=prospect_id,
        idempotency_key=idempotency_key,
        run_after=run_after or datetime.utcnow(),
        max_attempts=max_attempts,
        status='queued',
    )
    _db.session.add(job)
    _db.session.commit()
    return job


def release_stale_locks(now=None):
    """Jobs whose owner vanished come back to the queue."""
    now = now or datetime.utcnow()
    stale = models.Job.query.filter(
        models.Job.status == 'running',
        models.Job.locked_until.isnot(None),
        models.Job.locked_until < now,
    ).all()
    for job in stale:
        job.status = 'queued'
        job.locked_until = None
    if stale:
        _db.session.commit()
    return len(stale)


def queue_depth(now=None):
    now = now or datetime.utcnow()
    return models.Job.query.filter(
        models.Job.status == 'queued',
        models.Job.run_after <= now,
    ).count()


def claim_one(now=None):
    """Take the next due job, or None. Two tabs never get the same one."""
    now = now or datetime.utcnow()
    query = (models.Job.query
             .filter(models.Job.status == 'queued', models.Job.run_after <= now)
             .order_by(models.Job.run_after.asc(), models.Job.id.asc()))
    # Postgres can hand different rows to different callers; SQLite runs one
    # writer at a time anyway, so the plain query is already safe there.
    if _db.engine.dialect.name == 'postgresql':
        query = query.with_for_update(skip_locked=True)
    job = query.first()
    if job is None:
        return None
    job.status = 'running'
    job.attempts = (job.attempts or 0) + 1
    job.locked_until = now + timedelta(seconds=LOCK_SECONDS)
    _db.session.commit()
    return job


def problem_in(result):
    """A handler that returns {'error': ...} did its work and found a
    problem - a service that would not answer, a cap that is used up. That
    is not a crash, so it is not retried, but it must not look like
    success either: a green tick over "Network is unreachable" is how a
    broken engine goes unnoticed for a week."""
    if isinstance(result, dict):
        return str(result.get('error') or '').strip()
    return ''


def _finish(job, result, started):
    problem = problem_in(result)
    job.status = 'problem' if problem else 'done'
    job.result = json.dumps(result if result is not None else {})
    job.last_error = problem[:2000] or None
    job.locked_until = None
    job.finished_at = datetime.utcnow()
    job.duration_ms = int((time.monotonic() - started) * 1000)
    _db.session.commit()


def _fail(job, error, started):
    attempts = job.attempts or 1
    job.last_error = str(error)[:2000]
    job.locked_until = None
    job.duration_ms = int((time.monotonic() - started) * 1000)
    if attempts < (job.max_attempts or 3):
        wait = RETRY_BACKOFF_SECONDS[min(attempts - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
        job.status = 'queued'
        job.run_after = datetime.utcnow() + timedelta(seconds=wait)
    else:
        job.status = 'failed'
        job.finished_at = datetime.utcnow()
    _db.session.commit()


def run_next(now=None):
    """Run at most one job. Returns a small dictionary the runner script and
    the worker both understand."""
    if settings.everything_stopped():
        return {'ran': False, 'reason': 'stopped', 'queue': queue_depth()}

    release_stale_locks(now)
    job = claim_one(now)
    if job is None:
        return {'ran': False, 'reason': 'empty', 'queue': 0}

    handler = registry.handler_for(job.type)
    started = time.monotonic()
    if handler is None:
        _fail(job, f"no handler for job type '{job.type}'", started)
        return {'ran': True, 'job_id': job.id, 'type': job.type, 'ok': False,
                'error': job.last_error, 'queue': queue_depth()}

    payload = {}
    try:
        payload = json.loads(job.payload or '{}')
    except ValueError:
        payload = {}

    try:
        result = handler(job, payload)
    except Exception as e:                      # noqa: BLE001 - a failed job
        _db.session.rollback()                  # must never take the page down
        _fail(job, e, started)
        return {'ran': True, 'job_id': job.id, 'type': job.type, 'ok': False,
                'error': job.last_error, 'requeued': job.status == 'queued',
                'queue': queue_depth()}

    _finish(job, result, started)
    problem = job.status == 'problem'
    return {'ran': True, 'job_id': job.id, 'type': job.type, 'ok': not problem,
            'problem': problem, 'error': job.last_error,
            'result': result, 'duration_ms': job.duration_ms,
            'queue': queue_depth()}


def run_for(seconds=BUDGET_SECONDS, limit=25):
    """Run jobs until the time budget is spent. The browser calls this once
    per request; the worker calls it in a loop."""
    deadline = time.monotonic() + seconds
    ran = []
    while len(ran) < limit and time.monotonic() < deadline:
        outcome = run_next()
        if not outcome.get('ran'):
            outcome['ran_count'] = len(ran)
            outcome['results'] = ran
            return outcome
        ran.append(outcome)
    return {'ran': True, 'ran_count': len(ran), 'results': ran,
            'queue': queue_depth()}
