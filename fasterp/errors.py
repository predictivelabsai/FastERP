"""Domain failures that callers can present without leaking database details."""


class DomainError(ValueError):
    """A business invariant rejected an operation."""


class DocumentStateError(DomainError):
    """A document lifecycle transition is not allowed."""


class PeriodLockedError(DomainError):
    """Posting is not allowed in a locked fiscal period."""


class ImbalanceError(DomainError):
    """Accounting lines do not balance."""


class InsufficientStockError(DomainError):
    """The requested stock issue would violate negative-stock policy."""


class AllocationError(DomainError):
    """A payment or document allocation is invalid."""
