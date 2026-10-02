class RelayForgeError(Exception):
    """Base class for expected service errors."""


class ValidationError(RelayForgeError):
    pass


class IdempotencyConflict(RelayForgeError):
    pass


class LeaseLost(RelayForgeError):
    pass


class NotFound(RelayForgeError):
    pass


class InvalidTransition(RelayForgeError):
    pass


class TransportError(RelayForgeError):
    """A request failed before a usable HTTP response was received."""

