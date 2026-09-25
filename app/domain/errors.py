class DomainError(Exception):
    """Invalid domain object construction."""


class InvariantViolation(DomainError):
    """An operation would break a domain invariant (e.g. closing more than is open)."""


class InvalidTransition(DomainError):
    """Illegal state transition (e.g. FILLED -> NEW)."""
