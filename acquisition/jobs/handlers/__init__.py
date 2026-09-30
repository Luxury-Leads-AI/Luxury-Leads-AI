"""The handlers that exist so far.

Phase 1 ships the two that prove the machinery end to end: a health check
and a prospect tidy-up. Discovery, research, scoring, drafting and reply
classification arrive in phases 2 to 6, each as one more function here.
"""
from datetime import datetime

from ... import models
from .. import registry, runner


@registry.handler('ping')
def ping(job, payload):
    """Does the queue work? Costs nothing, touches nothing."""
    return {'pong': True, 'at': datetime.utcnow().isoformat(timespec='seconds'),
            'echo': payload.get('echo')}


@registry.handler('normalize_prospect')
def normalize_prospect(job, payload):
    """Tidy one prospect: fill in a missing name and website from its
    domain, and make sure it has a stage. Cheap, and it gives the Phase 1
    screens something real to run."""
    prospect_id = job.prospect_id or payload.get('prospect_id')
    prospect = runner.session().get(models.Prospect, prospect_id)
    if prospect is None:
        return {'skipped': 'prospect is gone'}

    changed = []
    if not prospect.website and prospect.canonical_domain:
        prospect.website = f"https://{prospect.canonical_domain}"
        changed.append('website')
    if not prospect.name and prospect.canonical_domain:
        stem = prospect.canonical_domain.split('.')[0].replace('-', ' ')
        prospect.name = stem.title()
        changed.append('name')
    if not prospect.stage:
        prospect.stage = 'new'
        changed.append('stage')
    if changed:
        runner.session().commit()
    return {'prospect_id': prospect.id, 'changed': changed}
