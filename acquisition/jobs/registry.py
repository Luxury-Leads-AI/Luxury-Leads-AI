"""Job type -> the function that does it.

Handlers are plain functions taking (job, payload). The same function runs
from a button today and from a worker later, which is the whole point.
"""
_HANDLERS = {}


def register(job_type, handler):
    _HANDLERS[job_type] = handler
    return handler


def handler(job_type):
    """Decorator form: @registry.handler('research')."""
    def wrap(fn):
        register(job_type, fn)
        return fn
    return wrap


def handler_for(job_type):
    return _HANDLERS.get(job_type)


def known_types():
    return sorted(_HANDLERS)


def clear():
    """Only for tests."""
    _HANDLERS.clear()
