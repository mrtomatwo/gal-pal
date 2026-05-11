"""Terminal / TTY presentation helpers.

`safe_for_terminal` and `confirm_destructive` are used by both the Reporter
implementations and the orchestrator commands. They live in their own
underscore-prefixed leaf module so neither dependency direction has to
import from a sibling that imports back — `reporter.py` consumes them, every
`commands/*.py` indirectly does too via the reporter, and nothing here
imports anything from `_galpal/`.
"""

from __future__ import annotations

import os
import re
import sys

# C0 control characters except \t and \n; plus DEL and the C1 control range.
# Plus the Unicode bidi / format characters that let a hostile displayName
# visually rearrange itself on the TTY:
#   U+200E / U+200F   LRM / RLM
#   U+202A-U+202E     LRE / RLE / PDF / LRO / RLO  (incl. RIGHT-TO-LEFT OVERRIDE)
#   U+2066-U+2069     LRI / RLI / FSI / PDI        (isolate marks)
#   U+FEFF            ZERO-WIDTH NO-BREAK SPACE / BOM
#
# Without these, a directory admin can store a `displayName` containing U+202E
# followed by the visible text reversed; the TTY renders the bytes mirrored
# but the underlying string is unchanged. A user reviewing a `dedupe` /
# `prune` preview then visually mis-attributes the contact. Use \u escapes
# (not raw codepoints) so the source itself stays safe to read.
# Build the regex programmatically from explicit codepoints — keeping bidi /
# format characters out of the source bytes themselves so the file stays
# safe to open in any editor and so ruff's PLE2502 (obfuscated-code check)
# doesn't fire on this very defense.
_C0 = "\x00-\x08\x0b\x0c\x0e-\x1f"
_C1 = "\x7f-\x9f"
_BIDI = (
    f"{chr(0x200E)}{chr(0x200F)}"  # LRM / RLM
    f"{chr(0x202A)}-{chr(0x202E)}"  # LRE / RLE / PDF / LRO / RLO
    f"{chr(0x2066)}-{chr(0x2069)}"  # LRI / RLI / FSI / PDI
    f"{chr(0xFEFF)}"  # ZERO-WIDTH NO-BREAK SPACE / BOM
)
_CONTROL_CHARS = re.compile(f"[{_C0}{_C1}{_BIDI}]")


def safe_for_terminal(value: object, *, max_len: int = 500) -> str:
    """Make `value` safe to print to a terminal — strip ANSI/OSC injection vectors.

    Used for any field that crossed the network from Graph: error bodies, GAL
    displayName, contact emails (in error contexts), folder/category names.
    Truncates at `max_len` to keep a single bad record from filling the
    scrollback buffer.
    """
    s = value if isinstance(value, str) else str(value)
    s = _CONTROL_CHARS.sub("?", s)
    if len(s) > max_len:
        s = s[:max_len] + "...<truncated>"
    return s


def confirm_destructive(count: int, scope: str) -> bool:
    """Prompt for `DELETE <count> <scope>`; return True iff the user types it exactly.

    Three load-bearing properties:

    1. Refuses non-TTY stdin (cron, captured stdout, piped script) unless
       `GALPAL_FORCE_NONINTERACTIVE=1` — `echo "DELETE 47 ALL" | galpal delete`
       must not silently bypass the safety check.
    2. The phrase is bound to scope (`PRUNE` / `ALL` / `UNSTAMPED` / `DEDUPE`),
       so a typo on the count can't accidentally take a more drastic path.
    3. Returns False on EOF or any mismatch — the caller prints "Aborted"
       and returns without writing.
    """
    if not sys.stdin.isatty() and not os.environ.get("GALPAL_FORCE_NONINTERACTIVE"):
        print(
            "Refusing destructive op on non-TTY stdin (cron / pipe / captured "
            "stdout). Run interactively, or set GALPAL_FORCE_NONINTERACTIVE=1 "
            "to override.",
            file=sys.stderr,
        )
        return False
    expected = f"DELETE {count} {scope}"
    try:
        confirm = input(f"Type {expected!r} to confirm: ")
    except EOFError:
        return False
    return confirm.strip() == expected
