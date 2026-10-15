class DomainError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class NotFound(DomainError):
    pass


class Conflict(DomainError):
    pass


class InvalidExpense(DomainError):
    pass


class InvalidSettlement(DomainError):
    pass


class AccountingIntegrityError(Exception):
    """Financial facts or plans failed reconciliation; never expose financial rows in errors."""
