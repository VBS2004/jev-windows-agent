class JevDesktopError(RuntimeError):
    pass


class StaleDesktopState(JevDesktopError):
    """The chosen target no longer means what it meant when observed."""


class InvalidDecision(JevDesktopError):
    """The policy returned an action that is not legal in the current snapshot."""


class UnsupportedDesktopAction(JevDesktopError):
    pass


class JevProviderError(JevDesktopError):
    """The decision provider rejected or failed a request. No action was executed.

    Carries the provider's own error detail: a bare status code is not enough to
    tell an oversized request from a bad key or a malformed question.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"JEV provider returned HTTP {status_code}: {detail}; no action executed")

    @property
    def context_exceeded(self) -> bool:
        """The request was over the model's context length (docs.typesafe.ai/models)."""
        return "max_tokens_exceeded" in self.detail
