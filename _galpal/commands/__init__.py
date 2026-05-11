"""Subcommand orchestrators.

Each module exposes one `run_<command>` function. Presentation tweakables live
here; the heavier shared helpers (`safe_for_terminal`, `confirm_destructive`)
live in `_galpal/_term.py` so the reporter can import them without producing
a circular dependency through this package init.
"""

from __future__ import annotations

from _galpal._term import confirm_destructive, safe_for_terminal

__all__ = ["PREVIEW_LIMIT", "PRUNE_PREVIEW_LIMIT", "confirm_destructive", "safe_for_terminal"]

PREVIEW_LIMIT = 25
PRUNE_PREVIEW_LIMIT = 50
