"""The Super Admin screens for the engine, all under /owner/acquisition.

Everything here sits behind the same session the rest of the Super Admin
panel uses, plus a token on every form so a page on another site cannot
make your browser post here on your behalf.

The screens are deliberately plain: a queue you can run, a market registry
you can edit, prospects you can add by hand, what it costs, and a log of
what happened. Discovery, research, drafting and pilots arrive in later
phases and hang off these same pages.
"""
import json
import os
import secrets
import time
from datetime import datetime

from flask import (Blueprint, jsonify, redirect, render_template, request,
                   session, url_for)

from .. import compliance, models, settings
from ..jobs import registry, runner
from ..providers import discovery
from ..services import ai, fetch, prospects

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(os.path.dirname(HERE), 'static', 'acquisition')

# The market rows the architecture proposed, ready to add with one click and
# edit afterwards. Nothing is hard-coded in the engine's logic: these are
# just rows, and the legal status on each one is what gates outreach.
PROPOSED_MARKETS = [
    dict(country='United States', country_code='US', city='Miami', language='en',
         timezone='America/New_York', outreach_method='email', legal_status='verified',
         postal_address_needed=True,
         legal_note="CAN-SPAM: B2B email is allowed with a working opt-out, a valid "
                    "postal address, and opt-outs honoured within 10 business days."),
    dict(country='United States', country_code='US', city='Los Angeles', language='en',
         timezone='America/Los_Angeles', outreach_method='email', legal_status='verified',
         postal_address_needed=True,
         legal_note="CAN-SPAM, as Miami."),
    dict(country='France', country_code='FR', city='Paris', language='fr',
         timezone='Europe/Paris', outreach_method='email', legal_status='verified',
         legal_note="CNIL: B2B prospecting may rely on legitimate interest when the "
                    "subject relates to the recipient's job, with information and an opt-out."),
    dict(country='France', country_code='FR', city='Nice', language='fr',
         timezone='Europe/Paris', outreach_method='email', legal_status='verified',
         legal_note="CNIL, as Paris."),
    dict(country='United Kingdom', country_code='GB', city='London', language='en',
         timezone='Europe/London', outreach_method='none', legal_status='needs_verification',
         company_check_needed=True,
         legal_note="PECR allows opt-out email to corporate subscribers (limited "
                    "companies, LLPs) but not to sole traders. Confirm against ICO "
                    "guidance, and check each prospect's company type, before sending."),
    dict(country='United Kingdom', country_code='GB', city='Manchester', language='en',
         timezone='Europe/London', outreach_method='none', legal_status='needs_verification',
         company_check_needed=True, legal_note="PECR, as London."),
    dict(country='United Arab Emirates', country_code='AE', city='Dubai', language='en',
         timezone='Asia/Dubai', outreach_method='founder_led', legal_status='needs_verification',
         legal_note="DLA Piper reads the TDRA rules as consent-based, with no B2B "
                    "exemption. Founder-led contact only until that is settled."),
    dict(country='United Arab Emirates', country_code='AE', city='Abu Dhabi', language='en',
         timezone='Asia/Dubai', outreach_method='founder_led', legal_status='needs_verification',
         legal_note="TDRA, as Dubai."),
    dict(country='Portugal', country_code='PT', city='Lisbon', language='pt',
         timezone='Europe/Lisbon', outreach_method='none', legal_status='unknown',
         legal_note="Believed to allow opt-out email to companies, with a national "
                    "opt-out list, but the statute was not read. Confirm locally."),
    dict(country='Portugal', country_code='PT', city='Algarve', language='pt',
         timezone='Europe/Lisbon', outreach_method='none', legal_status='unknown',
         legal_note="As Lisbon."),
]


def build_blueprint(db):
    bp = Blueprint('acquisition', __name__,
                   url_prefix='/owner/acquisition',
                   template_folder=os.path.join(HERE, 'templates'),
                   static_folder=STATIC_DIR,
                   static_url_path='/static')

    # ── who may be here, and a token so only our own pages may post ──

    def csrf_token():
        token = session.get('acq_csrf')
        if not token:
            token = secrets.token_urlsafe(32)
            session['acq_csrf'] = token
        return token

    @bp.before_request
    def guard():
        if not session.get('super_admin'):
            if request.accept_mimetypes.best == 'application/json' or request.is_json:
                return jsonify({'error': 'Not signed in'}), 401
            return redirect('/super-admin-login?error=Please+login+first')
        if request.method == 'POST':
            supplied = (request.form.get('csrf_token')
                        or request.headers.get('X-CSRF-Token', ''))
            expected = session.get('acq_csrf', '')
            if not expected or not secrets.compare_digest(supplied, expected):
                if request.is_json or request.headers.get('X-CSRF-Token') is not None:
                    return jsonify({'error': 'Stale page - reload and try again'}), 400
                return redirect(url_for('acquisition.today',
                                        error='That page was stale. Try again.'))
        return None

    @bp.context_processor
    def shared():
        return {
            'csrf_token': csrf_token(),
            'mode': settings.mode(),
            'mode_label': settings.MODE_LABELS.get(settings.mode(), ''),
            'budget': ai.budget_state(),
            'legal_labels': models.LEGAL_STATUS_LABELS,
            'stage_labels': models.PROSPECT_STAGE_LABELS,
            'method_labels': models.OUTREACH_METHOD_LABELS,
            'queue_depth': runner.queue_depth(),
            'everything_stopped': settings.everything_stopped(),
        }

    def record(action, entity=None, entity_id=None, before=None, after=None):
        """Engine audit: every change a person makes here."""
        try:
            db.session.add(models.Audit(
                actor='super_admin', action=action, entity=entity,
                entity_id=entity_id,
                before=json.dumps(before) if before is not None else None,
                after=json.dumps(after) if after is not None else None,
                ip=(request.headers.get('X-Forwarded-For', '').split(',')[0].strip()
                    or request.remote_addr)))
            db.session.commit()
        except Exception as e:                  # noqa: BLE001
            db.session.rollback()
            print(f"⚠️ acquisition audit error: {e}")

    def back(endpoint, **kwargs):
        return redirect(url_for(f'acquisition.{endpoint}', **kwargs))

    # ── Today ──

    @bp.route('/')
    def today():
        stage_counts = dict(
            db.session.query(models.Prospect.stage, db.func.count(models.Prospect.id))
            .group_by(models.Prospect.stage).all())
        needs_review = (models.Prospect.query
                        .filter(models.Prospect.needs_human.is_(True))
                        .order_by(models.Prospect.updated_at.desc()).limit(10).all())
        markets = models.Market.query.order_by(models.Market.country,
                                               models.Market.city).all()
        unverified = [m for m in markets if m.legal_status != 'verified']
        recent_jobs = (models.Job.query.order_by(models.Job.id.desc()).limit(10).all())
        searches = (models.Job.query
                    .filter(models.Job.type == 'discover',
                            models.Job.status.in_(('done', 'problem')))
                    .order_by(models.Job.id.desc()).limit(5).all())
        return render_template('acquisition/today.html',
                               searches=[(job, json.loads(job.result or '{}'))
                                         for job in searches],
                               stage_counts=stage_counts,
                               total_prospects=sum(stage_counts.values()),
                               needs_review=needs_review,
                               markets=markets, unverified=unverified,
                               recent_jobs=recent_jobs,
                               error=request.args.get('error'),
                               notice=request.args.get('notice'))

    # ── Markets ──

    @bp.route('/markets')
    def markets():
        rows = models.Market.query.order_by(models.Market.country,
                                            models.Market.city).all()
        counts = dict(db.session.query(models.Prospect.market_id,
                                       db.func.count(models.Prospect.id))
                      .group_by(models.Prospect.market_id).all())
        return render_template('acquisition/markets.html', markets=rows,
                               counts=counts, proposed=PROPOSED_MARKETS,
                               legal_statuses=models.LEGAL_STATUSES,
                               outreach_methods=models.OUTREACH_METHODS,
                               notice=request.args.get('notice'),
                               error=request.args.get('error'))

    @bp.route('/markets/seed', methods=['POST'])
    def seed_markets():
        added = 0
        for row in PROPOSED_MARKETS:
            exists = models.Market.query.filter_by(country=row['country'],
                                                   city=row['city']).first()
            if exists:
                continue
            db.session.add(models.Market(status='proposed', **row))
            added += 1
        db.session.commit()
        record('markets_seeded', 'market', after={'added': added})
        return back('markets', notice=f"Added {added} proposed markets. "
                                      f"Read each one before switching it on.")

    @bp.route('/markets/add', methods=['POST'])
    def add_market():
        country = (request.form.get('country') or '').strip()
        city = (request.form.get('city') or '').strip()
        if not country or not city:
            return back('markets', error="A market needs a country and a city.")
        if models.Market.query.filter_by(country=country, city=city).first():
            return back('markets', error=f"{city}, {country} is already there.")
        market = models.Market(
            country=country, city=city,
            country_code=(request.form.get('country_code') or '').strip()[:4] or None,
            language=(request.form.get('language') or 'en').strip()[:12],
            timezone=(request.form.get('timezone') or '').strip() or None,
            outreach_method=request.form.get('outreach_method', 'none'),
            legal_status=request.form.get('legal_status', 'unknown'),
            legal_note=(request.form.get('legal_note') or '').strip() or None,
            status='proposed')
        db.session.add(market)
        db.session.commit()
        record('market_added', 'market', market.id, after={'name': market.name})
        return back('markets', notice=f"Added {market.name}.")

    @bp.route('/markets/<int:market_id>', methods=['POST'])
    def update_market(market_id):
        market = db.session.get(models.Market, market_id)
        if market is None:
            return back('markets', error="That market is gone.")
        before = {'legal_status': market.legal_status,
                  'outreach_method': market.outreach_method,
                  'status': market.status}
        if request.form.get('action') == 'delete':
            if models.Prospect.query.filter_by(market_id=market.id).count():
                return back('markets', error="That market still has prospects.")
            db.session.delete(market)
            db.session.commit()
            record('market_deleted', 'market', market_id, before=before)
            return back('markets', notice="Market deleted.")

        market.legal_status = request.form.get('legal_status', market.legal_status)
        market.outreach_method = request.form.get('outreach_method', market.outreach_method)
        market.status = request.form.get('status', market.status)
        note = (request.form.get('legal_note') or '').strip()
        if note:
            market.legal_note = note
        db.session.commit()
        after = {'legal_status': market.legal_status,
                 'outreach_method': market.outreach_method,
                 'status': market.status}
        record('market_updated', 'market', market.id, before=before, after=after)
        if before['legal_status'] != 'verified' and market.legal_status == 'verified':
            record('market_verified', 'market', market.id,
                   after={'note': market.legal_note or ''})
        return back('markets', notice=f"Saved {market.name}.")

    # ── Prospects ──

    @bp.route('/prospects')
    def prospect_list():
        stage = request.args.get('stage') or ''
        market_id = request.args.get('market_id', type=int)
        query = models.Prospect.query
        if stage:
            query = query.filter(models.Prospect.stage == stage)
        if market_id:
            query = query.filter(models.Prospect.market_id == market_id)
        rows = query.order_by(models.Prospect.created_at.desc()).limit(300).all()
        return render_template('acquisition/prospects.html', prospects=rows,
                               markets=models.Market.query.order_by(
                                   models.Market.country, models.Market.city).all(),
                               sources=discovery.choices(),
                               enabled_sources=discovery.enabled_names(),
                               default_limit=settings.get_int('discovery_limit_default', 25),
                               stage=stage, market_id=market_id,
                               notice=request.args.get('notice'),
                               error=request.args.get('error'))

    @bp.route('/prospects/add', methods=['POST'])
    def add_prospects():
        market_id = request.form.get('market_id', type=int)
        lines = (request.form.get('websites') or '').splitlines()
        added, duplicates, problems = prospects.add_many(lines, market_id=market_id)
        for prospect in added:
            runner.enqueue('normalize_prospect', prospect_id=prospect.id,
                           idempotency_key=f"normalize:{prospect.id}")
        record('prospects_added', 'prospect',
               after={'added': len(added), 'duplicates': len(duplicates),
                      'problems': len(problems)})
        parts = [f"Added {len(added)}"]
        if duplicates:
            parts.append(f"{len(duplicates)} already there")
        if problems:
            parts.append(f"{len(problems)} not usable: "
                         + "; ".join(f"{text} ({why})" for text, why in problems[:3]))
        return back('prospect_list', notice=". ".join(parts) + ".")

    @bp.route('/prospects/research-all', methods=['POST'])
    def research_all():
        """Queue research for every prospect that has not had any.

        One job each rather than one big job: each has to finish inside
        gunicorn's 30 seconds, and a site that hangs must not take the rest
        of the batch down with it.
        """
        limit = request.form.get('limit', type=int) or 25
        rows = (models.Prospect.query
                .filter(models.Prospect.stage == 'new',
                        models.Prospect.do_not_contact.isnot(True))
                .order_by(models.Prospect.id).limit(limit).all())
        for prospect in rows:
            runner.enqueue('research', prospect_id=prospect.id,
                           idempotency_key=f"research:{prospect.id}")
        record('research_queued_bulk', 'prospect', None,
               after={'queued': len(rows)})
        if not rows:
            return back('prospect_list',
                        notice="Nothing new to research - every prospect has "
                               "been read already.")
        return back('prospect_list',
                    notice=f"Queued research for {len(rows)} agencies. Press "
                           f"Run the queue on Today.")

    @bp.route('/prospects/discover', methods=['POST'])
    def discover_prospects():
        """Queue a search for one city. The queue does the work when you
        press Run, so a slow search never holds up the page."""
        market_id = request.form.get('market_id', type=int)
        market = db.session.get(models.Market, market_id) if market_id else None
        if market is None:
            return back('prospect_list', error="Pick a city first.")

        source = request.form.get('source') or 'osm'
        if discovery.get(source) is None:
            return back('prospect_list', error="That source does not exist.")
        if source not in discovery.enabled_names():
            return back('prospect_list',
                        error=f"The {source} source is switched off in Settings.")

        limit = request.form.get('limit', type=int) or settings.get_int(
            'discovery_limit_default', 25)
        job = runner.enqueue('discover', payload={'market_id': market.id,
                                                  'source': source,
                                                  'limit': limit})
        record('discovery_queued', 'market', market.id,
               after={'source': source, 'limit': limit, 'job': job.id})
        return back('prospect_list',
                    notice=f"Looking for up to {limit} agencies in {market.name} "
                           f"using {source}. Press Run the queue on Today.")

    @bp.route('/prospects/<int:prospect_id>')
    def prospect_detail(prospect_id):
        prospect = db.session.get(models.Prospect, prospect_id)
        if prospect is None:
            return back('prospect_list', error="That prospect is gone.")
        decision = compliance.can_contact(prospect)
        from ..jobs.handlers import FACT_LABELS
        return render_template('acquisition/prospect.html', prospect=prospect,
                               decision=decision, fact_labels=FACT_LABELS,
                               markets=models.Market.query.order_by(
                                   models.Market.country, models.Market.city).all(),
                               stages=models.PROSPECT_STAGES,
                               jobs=models.Job.query.filter_by(
                                   prospect_id=prospect.id).order_by(
                                       models.Job.id.desc()).limit(10).all(),
                               notice=request.args.get('notice'),
                               error=request.args.get('error'))

    @bp.route('/prospects/<int:prospect_id>/update', methods=['POST'])
    def update_prospect(prospect_id):
        prospect = db.session.get(models.Prospect, prospect_id)
        if prospect is None:
            return back('prospect_list', error="That prospect is gone.")
        action = request.form.get('action', 'save')

        if action == 'do_not_contact':
            prospect.do_not_contact = True
            prospect.do_not_contact_reason = (request.form.get('reason')
                                              or 'Marked by hand')
            prospect.stage = 'parked'
            db.session.commit()
            compliance.suppress(domain=prospect.canonical_domain,
                                reason='marked do not contact')
            record('prospect_do_not_contact', 'prospect', prospect.id,
                   after={'reason': prospect.do_not_contact_reason})
            return back('prospect_detail', prospect_id=prospect.id,
                        notice="Marked do not contact, and the domain is suppressed.")

        if action == 'research':
            runner.enqueue('research', prospect_id=prospect.id,
                           idempotency_key=f"research:{prospect.id}")
            record('research_queued', 'prospect', prospect.id)
            return back('prospect_detail', prospect_id=prospect.id,
                        notice="Queued. Press Run the queue on Today.")

        if action == 'queue_normalize':
            runner.enqueue('normalize_prospect', prospect_id=prospect.id,
                           idempotency_key=f"normalize:{prospect.id}:{datetime.utcnow():%Y%m%d%H%M}")
            return back('prospect_detail', prospect_id=prospect.id,
                        notice="Queued. Press Run on the Today page.")

        before = {'stage': prospect.stage, 'market_id': prospect.market_id}
        prospect.stage = request.form.get('stage', prospect.stage)
        if request.form.get('market_id'):
            prospect.market_id = request.form.get('market_id', type=int)
        prospect.name = (request.form.get('name') or '').strip() or prospect.name
        prospect.notes = (request.form.get('notes') or '').strip() or None
        prospect.needs_human = bool(request.form.get('needs_human'))
        db.session.commit()
        record('prospect_updated', 'prospect', prospect.id, before=before,
               after={'stage': prospect.stage, 'market_id': prospect.market_id})
        return back('prospect_detail', prospect_id=prospect.id, notice="Saved.")

    # ── Jobs: the queue, and the endpoint the page loop calls ──

    @bp.route('/jobs')
    def job_list():
        rows = models.Job.query.order_by(models.Job.id.desc()).limit(100).all()
        return render_template('acquisition/jobs.html', jobs=rows,
                               known_types=', '.join(registry.known_types()),
                               notice=request.args.get('notice'),
                               error=request.args.get('error'))

    @bp.route('/jobs/run-next', methods=['POST'])
    def run_next():
        """One small job per call, well inside gunicorn's 30-second limit.
        The page calls this again and again while you watch."""
        outcome = runner.run_for(seconds=runner.BUDGET_SECONDS, limit=25)
        return jsonify(outcome)

    @bp.route('/jobs/ping', methods=['POST'])
    def queue_ping():
        job = runner.enqueue('ping', payload={'echo': 'hello'})
        return back('job_list', notice=f"Queued a test job (#{job.id}). Press Run.")

    @bp.route('/jobs/<int:job_id>/retry', methods=['POST'])
    def retry_job(job_id):
        job = db.session.get(models.Job, job_id)
        if job is None:
            return back('job_list', error="That job is gone.")
        job.status = 'queued'
        job.attempts = 0
        job.run_after = datetime.utcnow()
        job.locked_until = None
        db.session.commit()
        record('job_retried', 'job', job.id)
        return back('job_list', notice=f"Job #{job.id} is queued again.")

    # ── Can this server get out? ──

    # Short enough that the page always answers inside gunicorn's 30 seconds,
    # even when several addresses have to time out.
    CHECK_TIME_BUDGET = 20

    def connection_targets():
        """What the engine needs to reach, as it is configured right now -
        so a URL changed in Settings is the one that gets tested.

        The plain internet goes first on purpose: it is the cheapest check
        and the one that tells you most, because "even this failed" and
        "only OpenStreetMap failed" are different problems."""
        targets = [
            ('The internet in general', 'https://example.com/'),
            ('OpenStreetMap: Nominatim (finds the city)',
             settings.get('osm_nominatim_url') or discovery.DEFAULT_NOMINATIM_URL),
        ]
        # Every Overpass server the engine would try, not just the first:
        # the whole point is to see which of them will talk to this server.
        for url in discovery.OSMDiscovery().overpass_urls:
            targets.append(('OpenStreetMap: Overpass (lists the agencies)', url))
        if os.getenv('OPENAI_API_KEY'):
            targets.append(('OpenAI (AI search, drafting)',
                            'https://api.openai.com/v1/models'))
        return targets

    @bp.route('/connection', methods=['GET', 'POST'])
    def connection():
        """Which of the outside services this server can actually open.

        A job can only report what its last attempt said, and "Network is
        unreachable" is the same sentence whether the service is down, the
        address family has no route, or nothing at all gets out of here.
        This tries each address on its own and shows all of it.
        """
        checks = []
        if request.method == 'POST':
            deadline = time.monotonic() + CHECK_TIME_BUDGET
            for label, url in connection_targets():
                if time.monotonic() >= deadline:
                    checks.append({'label': label, 'url': url, 'report': None})
                    continue
                checks.append({'label': label, 'url': url,
                               'report': fetch.connection_report(url, deadline=deadline)})
            record('connection_checked')
        return render_template('acquisition/connection.html', checks=checks,
                               notice=request.args.get('notice'),
                               error=request.args.get('error'))

    # ── Costs, settings, audit ──

    @bp.route('/costs')
    def costs():
        return render_template('acquisition/costs.html',
                               summary=ai.cost_summary())

    @bp.route('/settings', methods=['GET', 'POST'])
    def settings_page():
        if request.method == 'POST':
            changed = {}
            for key, _label in settings.EDITABLE:
                if key in request.form:
                    new = (request.form.get(key) or '').strip()
                    old = settings.get(key)
                    if str(old) != new:
                        settings.set(key, new)
                        changed[key] = {'from': old, 'to': new}
            if changed:
                record('settings_changed', 'setting', after=changed)
            return back('settings_page', notice="Saved.")
        return render_template('acquisition/settings.html',
                               values=settings.all_settings(),
                               editable=settings.EDITABLE,
                               modes=settings.MODES,
                               mode_labels=settings.MODE_LABELS,
                               notice=request.args.get('notice'))

    @bp.route('/audit')
    def audit():
        rows = models.Audit.query.order_by(models.Audit.id.desc()).limit(200).all()
        return render_template('acquisition/audit.html', entries=rows)

    return bp
