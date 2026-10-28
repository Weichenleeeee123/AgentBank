from __future__ import annotations


class DomainError(Exception):
    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


class AmbiguousRecipient(DomainError):
    def __init__(self) -> None:
        super().__init__("AMBIGUOUS_RECIPIENT")
