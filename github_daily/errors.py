"""Exceptions raised by the GitHub contribution watcher."""


class GitHubDailyError(Exception):
    """Base exception for expected plugin failures."""


class GitHubApiError(GitHubDailyError):
    """Represent an error returned by the GitHub API or HTTP client."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


class AccountNotFoundError(GitHubDailyError):
    """Raised when an account is not configured for the current group."""


class InvalidAccountError(GitHubDailyError):
    """Raised when a GitHub username does not pass validation."""


class InvalidRepositoryError(GitHubDailyError):
    """Raised when a repository does not pass validation."""


class PermissionDeniedError(GitHubDailyError):
    """Raised when a chat user is not allowed to manage an account."""
