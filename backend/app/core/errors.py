class HyperCopyError(Exception):
    code = 'HYPERCOPY_ERROR'
    status_code = 400

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        if code:
            self.code = code


class Forbidden(HyperCopyError):
    code = 'FORBIDDEN'
    status_code = 403


class Conflict(HyperCopyError):
    code = 'CONFLICT'
    status_code = 409


class ExternalDependencyError(HyperCopyError):
    code = 'DEPENDENCY_UNAVAILABLE'
    status_code = 503


class AmbiguousExecution(HyperCopyError):
    code = 'AMBIGUOUS_EXECUTION'
    status_code = 503
