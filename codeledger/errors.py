"""Shared error types.

Every user-facing failure in CodeLedger raises :class:`LedgerError` so the CLI
can print one friendly message and exit with a useful code instead of dumping
a traceback on the user.
"""
from __future__ import annotations

__all__ = ["LedgerError"]


class LedgerError(Exception):
    """A clean, user-facing error (message is shown as-is)."""

    exit_code: int = 1

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code
