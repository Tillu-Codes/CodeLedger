"""CodeLedger - "git blame for AI-written code".

CodeLedger records *why* AI-written code exists: the prompt, model and tool
behind every change, stored in plain files inside the target repo's
``.ledger/`` directory.  Fully offline, deterministic and dependency-light.
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
