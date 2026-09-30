"""The client acquisition engine.

It lives beside the SaaS in the same repository and the same database, but
it is a separate thing: app.py hands it what it needs and the engine never
reaches back. The rule is one line long -

    acquisition never imports app.py

- and it buys two things. There is no circular import to untangle, and
`python app.py` does not load the whole application twice. Everything the
engine needs from the SaaS arrives through init_app(): the database, and a
small bag of functions (provisioning an agency, sending mail, the OpenAI
client) that it calls but does not own.

Wiring it up is five lines at the bottom of app.py:

    import acquisition
    acquisition.init_app(app, db, saas=acquisition.SaaSServices(
        provision_agency=provision_agency,
        is_entitled=is_entitled,
        send_email=send_email_brevo,
        openai_client=client,
        public_base_url=PUBLIC_BASE_URL,
    ))
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import compliance, models, settings          # noqa: F401
from .jobs import registry, runner                  # noqa: F401
from .services import ai, prospects                 # noqa: F401

__all__ = ['SaaSServices', 'init_app', 'models', 'settings', 'compliance',
           'runner', 'registry', 'ai', 'prospects', 'services']


@dataclass
class SaaSServices:
    """What the SaaS lends the engine. Nothing here is imported; it is
    passed in, so the engine can be tested with plain fakes."""
    provision_agency: Optional[Callable] = None
    is_entitled: Optional[Callable] = None
    send_email: Optional[Callable] = None
    openai_client: Any = None
    public_base_url: str = ''
    extras: dict = field(default_factory=dict)


services = SaaSServices()
_ready = False


def init_app(app, db, saas=None):
    """Called once from app.py. Safe to call twice."""
    global services, _ready

    services = saas or SaaSServices()

    models.define(db)
    settings.init(db)
    compliance.init(db)
    prospects.init(db)
    runner.init(db)
    ai.init(db, openai_client=services.openai_client)

    from .jobs import init as init_jobs
    init_jobs(db)

    from .admin.routes import build_blueprint
    blueprint = build_blueprint(db)
    if blueprint.name not in app.blueprints:
        app.register_blueprint(blueprint)

    _ready = True
    return blueprint


def is_ready():
    return _ready


def create_tables(db):
    """Make any acq_ table that isn't there yet.

    Render runs an existing database, so a table that exists only in the
    models is a table that does not exist in production. Called from app.py's
    startup migrations; safe to run on every boot.
    """
    from sqlalchemy import inspect

    created = []
    existing = set(inspect(db.engine).get_table_names())
    for model in models.ALL:
        if model.__tablename__ not in existing:
            model.__table__.create(db.engine)
            created.append(model.__tablename__)
    return created
