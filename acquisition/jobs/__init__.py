"""The job queue: one place that knows how work gets done."""
from . import registry, runner  # noqa: F401


def init(db):
    runner.init(db)
    from . import handlers  # noqa: F401  (importing registers them)
    return registry.known_types()
