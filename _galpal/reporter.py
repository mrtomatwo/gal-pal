"""Output abstraction for galpal subcommands.

Every `run_*` orchestrator emits through a `Reporter` Protocol; the four
implementations (TTY, JSON, Quiet, Recording) below are the only places
where output rendering lives. Orchestrators stay oblivious to which one is
in use, so the same code path serves humans, JSON consumers, tests, and
"exit-code-only" cron runs.

The factory at the bottom (`default_reporter`) honors `GALPAL_REPORTER` for
environments that can't pass a CLI flag.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from tqdm import tqdm

from _galpal._term import confirm_destructive, safe_for_terminal

if TYPE_CHECKING:
    from collections.abc import Iterator

# Every Graph-controlled value should be sanitized via `safe_for_terminal`
# before reaching `Reporter.entry` / `Reporter.error` so the JSON reporter
# doesn't end up writing raw control sequences into a downstream consumer's
# log either. The TTY reporter relies on the same sanitization for ANSI
# defense; this comment exists to document that the contract spans both.


@runtime_checkable
class ProgressBar(Protocol):
    """Minimal progress-bar surface — `update(n)` and `write(msg)`."""

    def update(self, n: int = 1) -> None: ...
    def write_inline(self, msg: str) -> None: ...


@runtime_checkable
class Reporter(Protocol):
    """Output sink for one galpal subcommand run."""

    # Streams of human-meaningful messages.
    def info(self, msg: str) -> None: ...
    def warning(self, msg: str) -> None: ...
    def error(self, msg: str) -> None: ...

    # Per-row structured event. Used by pull (CREATE/UPDATE/SKIP per contact),
    # dedupe (one group), prune/delete (one preview row), categories (one
    # patch). `kind` identifies the event family (e.g. "pull.row",
    # "dedupe.group"); fields carry the structured payload.
    def entry(self, kind: str, **fields: Any) -> None: ...

    # Determinate or indeterminate progress bar. Yields a ProgressBar that the
    # caller drives. `total=None` means indeterminate (e.g. streaming GAL).
    def progress(
        self, *, total: int | None, unit: str, desc: str
    ) -> contextlib.AbstractContextManager[ProgressBar]: ...

    # Destructive confirmation. Returns True iff the user typed the right
    # `DELETE N <SCOPE>` token. Reporters in non-interactive modes return False.
    def confirm(self, count: int, scope: str) -> bool: ...

    # End-of-run summary. JSON reporter prints a single object; TTY prints
    # a human line. Tests assert on `recorded.summary_kwargs`.
    def summary(self, **stats: Any) -> None: ...


# --------------------------------------------------------------------------- TTY


class _TqdmProgress:
    """Wraps a `tqdm` bar to satisfy the `ProgressBar` Protocol.

    `tqdm.write` already coordinates with the bar to avoid corrupting the line.
    """

    def __init__(self, bar: tqdm) -> None:
        self._bar = bar

    def update(self, n: int = 1) -> None:
        self._bar.update(n)

    def write_inline(self, msg: str) -> None:
        tqdm.write(msg)


_BAR_FMT = "{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]"
_STREAM_BAR_FMT = "{desc}: {n_fmt} [{elapsed}, {rate_fmt}]"


# TTY-formatter registry. Each entry maps an event-kind string to a function
# that takes the raw fields dict and returns the human-readable line to write.
# Orchestrators emit `reporter.entry(KIND, **fields)`; the JSON reporter just
# serializes the dict; the TTY reporter looks up the formatter here.
#
# This replaces an `if kind == "..."` ladder inside `TTYReporter.entry`. New
# event kinds are added by registering a formatter here (or in the command
# module that owns the kind, via `register_tty_formatter` below) — no edit
# to `TTYReporter` required.
TTYFormatter = "Callable[[dict[str, Any]], str]"
_TTY_FORMATTERS: dict[str, Any] = {}


def register_tty_formatter(kind: str, fmt) -> None:
    """Register a TTY formatter for an event kind.

    Commands that emit a custom kind register their formatter here at import
    time so `TTYReporter.entry` can look it up. Kinds without a registered
    formatter fall through to a generic `[kind] k=v ...` shape — better than
    dropping the event but visible enough that you'll notice and add a proper
    formatter.
    """
    _TTY_FORMATTERS[kind] = fmt


def _tty_format_pull_row(f: dict[str, Any]) -> str:
    action = f.get("action", "?")
    name = safe_for_terminal(f.get("name", ""))
    mail = safe_for_terminal(f.get("mail", "—"))
    phone = safe_for_terminal(f.get("phone", "—"))
    return f"  {action:6} {name:40s}  <{mail}>  ☎ {phone}"


def _tty_format_subrequest_error(f: dict[str, Any]) -> str:
    action = f.get("action", "?")
    name_raw = f.get("name") or ""
    name = safe_for_terminal(name_raw)
    status = f.get("status", "?")
    body = safe_for_terminal(f.get("body", ""))
    # Drop the trailing space when name is empty so `chunked_batch`-style
    # callers (which can't supply a per-row name) don't render as
    # "ERROR  delete : status=..." with a stray space.
    target = f" {name}" if name else ""
    return f"  ERROR  {action}{target}: status={status} body={body}"


def _tty_format_preview_row(f: dict[str, Any]) -> str:
    return "  " + "  ".join(f"{k}={safe_for_terminal(v)}" for k, v in f.items())


def _tty_format_dedupe_group(f: dict[str, Any]) -> str:
    emails = ", ".join(safe_for_terminal(e) for e in f.get("emails", []))
    size = f.get("size", "?")
    keep_name = safe_for_terminal(f.get("keep_name", ""))
    keep_score = f.get("keep_score", "?")
    delete_names = [safe_for_terminal(n) for n in f.get("delete_names", [])]
    # Multiplication sign reads as "times N" in the user-facing summary.
    head = f"  [{emails}]  ×{size}"  # noqa: RUF001
    keep_line = f"    KEEP   {keep_name:40s}  user-data={keep_score}"
    delete_lines = [f"    DELETE {n:40s}" for n in delete_names]
    return "\n".join([head, keep_line, *delete_lines])


def _tty_format_folder_entry(f: dict[str, Any]) -> str:
    name = safe_for_terminal(f.get("name", ""))
    contacts = f.get("contacts", "?")
    fid = f.get("id", "")
    suffix = f"  id={fid}" if fid else ""
    return f"  {name:40s}  contacts={contacts}{suffix}"


def _tty_format_folder_match(f: dict[str, Any]) -> str:
    name = safe_for_terminal(f.get("name", ""))
    contacts = f.get("contacts", "?")
    return f"  {name:40s}  contacts={contacts}"


def _tty_format_folder_deleted(f: dict[str, Any]) -> str:
    return f"  deleted {safe_for_terminal(f.get('name', ''))}"


def _tty_format_audit_email_collision(f: dict[str, Any]) -> str:
    return f"  {safe_for_terminal(f.get('mail', ''))}"


def _tty_format_audit_email_collision_entry(f: dict[str, Any]) -> str:
    return f"    - {safe_for_terminal(f.get('name', ''))}  (id={f.get('id', '')})"


def _tty_format_audit_id_collision(f: dict[str, Any]) -> str:
    return f"  {f.get('id', '')}"


def _tty_format_audit_id_collision_entry(f: dict[str, Any]) -> str:
    name = safe_for_terminal(f.get("name", ""))
    mail = safe_for_terminal(f.get("mail", ""))
    return f"    - {name}  <{mail}>"


def _tty_format_audit_no_mail_entry(f: dict[str, Any]) -> str:
    return f"  - {safe_for_terminal(f.get('name', ''))}  (id={f.get('id', '')})"


def _tty_format_category_update_preview(f: dict[str, Any]) -> str:
    name = safe_for_terminal(f.get("name", ""))
    kept = [safe_for_terminal(k) for k in f.get("kept", [])]
    return f"    {name:40s}  -> categories={kept}"


def _tty_format_category_master_match(f: dict[str, Any]) -> str:
    return f"    {safe_for_terminal(f.get('name', ''))}  (id={f.get('id', '')})"


# Register the built-in kinds. Commands defined later in the codebase can
# register additional kinds via `register_tty_formatter` at import time;
# the registry is the single source of truth for "how does this kind render
# on a TTY?"
for _kind, _fmt in [
    ("pull.row", _tty_format_pull_row),
    ("subrequest.error", _tty_format_subrequest_error),
    ("preview.row", _tty_format_preview_row),
    ("dedupe.group", _tty_format_dedupe_group),
    ("folder.entry", _tty_format_folder_entry),
    ("folder.match", _tty_format_folder_match),
    ("folder.deleted", _tty_format_folder_deleted),
    ("audit.email_collision", _tty_format_audit_email_collision),
    ("audit.email_collision_entry", _tty_format_audit_email_collision_entry),
    ("audit.id_collision", _tty_format_audit_id_collision),
    ("audit.id_collision_entry", _tty_format_audit_id_collision_entry),
    ("audit.no_mail_entry", _tty_format_audit_no_mail_entry),
    ("category.update_preview", _tty_format_category_update_preview),
    ("category.master_match", _tty_format_category_master_match),
]:
    register_tty_formatter(_kind, _fmt)
del _kind, _fmt


class TTYReporter:
    """Default reporter: prints to stdout/stderr; uses tqdm for progress.

    Confirmation goes through the shared `confirm_destructive` helper (TTY
    guard + scope-bound token), so the destructive-op security story is the
    same whether reached via the reporter or via direct CLI invocation.
    """

    def info(self, msg: str) -> None:
        print(msg, flush=True)

    def warning(self, msg: str) -> None:
        print(f"warn: {msg}", file=sys.stderr, flush=True)

    def error(self, msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)

    def entry(self, kind: str, **fields: Any) -> None:
        # Look up the formatter from the registry. New event kinds register
        # their formatter via `register_tty_formatter(kind, fn)` at import
        # time — the dispatch shape stays one-line so we don't accumulate
        # an `if`-ladder as more kinds are added.
        fmt = _TTY_FORMATTERS.get(kind)
        if fmt is not None:
            line = fmt(fields)
        else:
            # Unregistered kind — emit kind + sorted fields. Better than
            # dropping the event but visible enough that a human notices and
            # adds a proper formatter.
            kv = " ".join(f"{k}={safe_for_terminal(v)}" for k, v in sorted(fields.items()))
            line = f"[{kind}] {kv}"
        # `subrequest.error` and `pull.row` are emitted while a tqdm bar is
        # active; route their output through `tqdm.write` so the bar doesn't
        # corrupt the line. Other kinds run before/after progress and can
        # use plain print.
        if kind in ("pull.row", "subrequest.error"):
            tqdm.write(line)
        else:
            print(line, flush=True)

    @contextlib.contextmanager
    def progress(self, *, total: int | None, unit: str, desc: str) -> Iterator[ProgressBar]:
        bar_format = _BAR_FMT if total is not None else _STREAM_BAR_FMT
        with tqdm(total=total, unit=unit, desc=desc, bar_format=bar_format, smoothing=0.1) as bar:
            yield _TqdmProgress(bar)

    def confirm(self, count: int, scope: str) -> bool:
        # Defer to the shared helper so the TTY guard and scope-bound token
        # logic don't drift between reporter and CLI command-orchestrator.
        return confirm_destructive(count, scope)

    def summary(self, **stats: Any) -> None:
        # Sort keys for stable test snapshots; sanitize values defensively in
        # case a stat field carries a Graph-derived string in some future
        # subcommand. Numeric counters are unaffected.
        parts = [f"{k}={safe_for_terminal(v)}" for k, v in sorted(stats.items())]
        print("\nDone. " + " ".join(parts), flush=True)


# --------------------------------------------------------------------------- JSON


class _NullProgress:
    """Stub progress bar for the JSON reporter — emits nothing."""

    def update(self, n: int = 1) -> None:
        pass

    def write_inline(self, msg: str) -> None:
        # Routed through the parent reporter's error stream so error messages
        # tqdm.write would have printed inline still reach the JSON consumer.
        # We can't write raw text into a JSON-lines stream, so the JSON reporter
        # overrides write_inline (see `_JSONProgress` below).
        pass


class _JSONProgress:
    """Progress bar for the JSON reporter.

    Drops `update` (per-row progress would flood the ndjson stream); routes
    `write_inline` back to the reporter as a structured 'error' event so
    per-row errors don't silently disappear in JSON mode.
    """

    def __init__(self, reporter: JSONReporter) -> None:
        self._reporter = reporter

    def update(self, n: int = 1) -> None:
        # Per-row progress is too noisy for JSON output (would 5x the line
        # count of the GAL stream); intentionally a no-op.
        del n

    def write_inline(self, msg: str) -> None:
        # Anything tqdm.write'd inline is, by convention, an error or noteworthy
        # row. Surface it as an error event for JSON consumers — they can grep
        # for `kind == "error"`.
        self._reporter.error(msg)


class JSONReporter:
    """One JSON object per event, line-delimited (ndjson) on stdout.

    Each emission is a self-contained `{"kind": <event>, ...}` dict, with
    well-known kinds: `info`, `warning`, `error`, `entry`, `summary`. The
    consumer can grep for kind: `galpal --json pull | jq 'select(.kind ==
    "summary")'`. Per-row progress updates are intentionally dropped (would
    flood the stream); per-row entries are not.

    Confirmation is refused: a JSON consumer is by definition non-interactive,
    and we'd rather fail loudly than silently bypass a destructive op. Use
    `GALPAL_FORCE_NONINTERACTIVE=1` together with the CLI confirmation token
    if you genuinely need scripted destructive runs.
    """

    def __init__(self, stream: Any = None) -> None:
        # Resolve `sys.stdout` at construction time (not at default-value time)
        # so pytest's `capsys` (which swaps sys.stdout per-test) actually
        # captures our output. Same reason `print()` defaults to file=None.
        self._stream = stream if stream is not None else sys.stdout

    def _scrub(self, value: Any) -> Any:
        """Recursively scrub every string leaf in `value` of C0/C1 control chars.

        The TTY reporter's ANSI/OSC defense lives in `safe_for_terminal`; the
        JSON reporter has the same threat surface (its output is consumed by
        cron mail / journalctl / Slack webhooks / log shippers, all of which
        will happily render a control sequence) but values reach `_emit` as
        raw Python objects.

        Opt-out semantics, not opt-in: `int` / `float` / `bool` / `None` pass
        through (they can't carry control chars); `str` / `dict` / `list` /
        `tuple` recurse into; *everything else* (`bytes`, `set`, `Path`,
        `datetime`, custom objects) is stringified through `safe_for_terminal`.
        Without opt-out semantics, a future event payload that adds e.g. a
        `bytes` body would land in the JSON output as a raw `repr` containing
        whatever control bytes the wire response carried.
        """
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return safe_for_terminal(value)
        if isinstance(value, dict):
            return {k: self._scrub(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return type(value)(self._scrub(v) for v in value)
        # Everything else: stringify *and* scrub. Without the explicit scrub
        # here, `default=str` would pass `bytes` / `Path` / `datetime` through
        # `json.dumps` unscrubbed.
        return safe_for_terminal(str(value))

    def _emit(self, payload: dict[str, Any]) -> None:
        # `default=str` rescues the rare case where a payload field is a
        # type json.dumps doesn't natively know (e.g. a `re.Pattern` object
        # leaking through from a FilterConfig describe()).
        json.dump(self._scrub(payload), self._stream, default=str, separators=(",", ":"))
        self._stream.write("\n")
        self._stream.flush()

    def info(self, msg: str) -> None:
        self._emit({"kind": "info", "message": msg})

    def warning(self, msg: str) -> None:
        self._emit({"kind": "warning", "message": msg})

    def error(self, msg: str) -> None:
        self._emit({"kind": "error", "message": msg})

    def entry(self, kind: str, **fields: Any) -> None:
        self._emit({"kind": "entry", "type": kind, **fields})

    @contextlib.contextmanager
    def progress(self, *, total: int | None, unit: str, desc: str) -> Iterator[ProgressBar]:
        # Progress *bars* don't translate to JSON, but the start/end of the
        # phase do — emit a marker so consumers can tell which phase a later
        # entry belongs to.
        self._emit({"kind": "phase", "desc": desc, "unit": unit, "total": total})
        yield _JSONProgress(self)

    def confirm(self, count: int, scope: str) -> bool:
        # JSON mode is non-interactive by definition — a destructive op without
        # a TTY confirmation token would silently bypass the safety check.
        del count, scope
        return False

    def summary(self, **stats: Any) -> None:
        self._emit({"kind": "summary", **stats})


# --------------------------------------------------------------------------- Quiet


class QuietReporter(TTYReporter):
    """Suppress informational output; keep warnings, errors, summary, and confirmations.

    For users who want a "success / failure" exit code without progress noise.
    Inherits TTYReporter so confirmation still uses the interactive prompt.
    """

    def info(self, msg: str) -> None:
        del msg

    def entry(self, kind: str, **fields: Any) -> None:
        # Per-row entries are dropped; subrequest errors are still surfaced
        # (they're not entries to the user, they're failures).
        del fields
        if kind == "subrequest.error":
            super().entry(kind)

    @contextlib.contextmanager
    def progress(self, *, total: int | None, unit: str, desc: str) -> Iterator[ProgressBar]:
        # Suppress the bar but keep the contract; orchestrators still call
        # update() on the yielded object.
        del total, unit, desc
        yield _NullProgress()


# Recording reporter follows — used by tests instead of capsys.


class RecordingReporter:
    """In-memory reporter for tests. Records every call as a structured event.

    Tests assert on `recorder.events` (list of dicts), `.summary_kwargs`, or
    `.confirm_calls` instead of grepping captured stdout. The `confirm`
    response can be set per-test via `recorder.confirm_response = True/False`.
    """

    def __init__(self, *, confirm_response: bool = True) -> None:
        self.events: list[dict[str, Any]] = []
        self.summary_kwargs: dict[str, Any] | None = None
        self.confirm_calls: list[tuple[int, str]] = []
        self.confirm_response = confirm_response

    def _push(self, **fields: Any) -> None:
        self.events.append(dict(fields))

    def info(self, msg: str) -> None:
        self._push(kind="info", message=msg)

    def warning(self, msg: str) -> None:
        self._push(kind="warning", message=msg)

    def error(self, msg: str) -> None:
        self._push(kind="error", message=msg)

    def entry(self, kind: str, **fields: Any) -> None:
        self._push(kind="entry", type=kind, **fields)

    @contextlib.contextmanager
    def progress(self, *, total: int | None, unit: str, desc: str) -> Iterator[ProgressBar]:
        self._push(kind="phase", desc=desc, unit=unit, total=total)
        yield _NullProgress()

    def confirm(self, count: int, scope: str) -> bool:
        self.confirm_calls.append((count, scope))
        return self.confirm_response

    def summary(self, **stats: Any) -> None:
        self.summary_kwargs = dict(stats)
        self._push(kind="summary", **stats)

    # Convenience for tests
    def kinds(self) -> list[str]:
        """List of every event's `kind` in emission order, e.g. for sequence assertions."""
        return [e["kind"] for e in self.events]


# --------------------------------------------------------------------------- factory


def default_reporter() -> Reporter:
    """Return the reporter to use when none was passed in.

    Honors `GALPAL_REPORTER=json|quiet|tty` for environments where the user
    can't pass a CLI flag (a wrapper script, a CI matrix).
    """
    pref = (os.environ.get("GALPAL_REPORTER") or "tty").lower()
    if pref == "json":
        return JSONReporter()
    if pref == "quiet":
        return QuietReporter()
    return TTYReporter()
