"""Errors the service raises; the transports turn them into 404 / 409 / readable tool errors."""


class NotFound(LookupError):
    pass


class Conflict(RuntimeError):
    """The action can't happen in the current state (already running, no free slot, ...)."""
