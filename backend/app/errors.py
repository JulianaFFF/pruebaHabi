class AppError(Exception):
    status_code = 400
    code = "error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class NotFound(AppError):
    status_code = 404
    code = "not_found"


class InsufficientFunds(AppError):
    status_code = 409
    code = "insufficient_funds"


class IdempotencyConflict(AppError):
    status_code = 409
    code = "idempotency_key_reused"


class PoolNotOpen(AppError):
    status_code = 409
    code = "pool_not_open"


class Forbidden(AppError):
    status_code = 403
    code = "forbidden"


class InvalidOperation(AppError):
    status_code = 422
    code = "invalid_operation"
