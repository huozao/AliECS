class TPlusDataHubError(RuntimeError):
    """Base exception for this project."""


class ChanjetAPIError(TPlusDataHubError):
    def __init__(self, message: str, endpoint: str, status_code: int | None = None, body_preview: str = ""):
        super().__init__(message)
        self.endpoint = endpoint
        self.status_code = status_code
        self.body_preview = body_preview


class EndpointNotConfirmedError(TPlusDataHubError, NotImplementedError):
    """Raised when a module endpoint still needs official confirmation."""


class StorageError(TPlusDataHubError):
    """Raised when local output cannot be written."""
