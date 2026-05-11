"""Tests for `_galpal.reporter`.

The TTYReporter / JSONReporter / QuietReporter / RecordingReporter are pure
output sinks — every test here drives them in isolation and asserts on the
shape they emit. The integration check (a full run_pull through a
RecordingReporter) lives further down: it's the highest-leverage test of the
"orchestrators don't print anymore, they emit events" contract.
"""

from __future__ import annotations

import io
import json

import pytest

from _galpal import reporter as rep_mod
from _galpal.commands.pull import run_pull
from _galpal.filters import FilterConfig
from _galpal.reporter import (
    JSONReporter,
    QuietReporter,
    RecordingReporter,
    Reporter,
    TTYReporter,
    default_reporter,
)

from .conftest import make_user

# --------------------------------------------------------------------------- Protocol shape


def test_all_reporters_satisfy_protocol():
    """Smoke-check: every concrete reporter is recognized by `isinstance(x, Reporter)`."""
    assert isinstance(TTYReporter(), Reporter)
    assert isinstance(QuietReporter(), Reporter)
    assert isinstance(JSONReporter(), Reporter)
    assert isinstance(RecordingReporter(), Reporter)


def test_default_reporter_honors_env(monkeypatch):
    monkeypatch.setenv("GALPAL_REPORTER", "json")
    assert isinstance(default_reporter(), JSONReporter)
    monkeypatch.setenv("GALPAL_REPORTER", "quiet")
    assert isinstance(default_reporter(), QuietReporter)
    monkeypatch.setenv("GALPAL_REPORTER", "tty")
    assert isinstance(default_reporter(), TTYReporter)
    monkeypatch.delenv("GALPAL_REPORTER", raising=False)
    assert isinstance(default_reporter(), TTYReporter)


# --------------------------------------------------------------------------- TTY


def test_tty_reporter_prints_info_to_stdout(capsys):
    r = TTYReporter()
    r.info("hello")
    out, err = capsys.readouterr()
    assert "hello" in out
    assert err == ""


def test_tty_reporter_prints_error_to_stderr(capsys):
    r = TTYReporter()
    r.error("oh no")
    out, err = capsys.readouterr()
    assert "oh no" in err
    assert out == ""


def test_tty_reporter_pull_row_format(capsys):
    """A `pull.row` event renders to a single human-readable line containing
    action, name, mail, and phone — the shape downstream tooling and humans
    grep for."""
    r = TTYReporter()
    r.entry("pull.row", action="CREATE", name="Doe, Jane", mail="j@x.com", phone="+1-555")
    out, _ = capsys.readouterr()
    assert "CREATE" in out
    assert "Doe, Jane" in out
    assert "<j@x.com>" in out


def test_tty_reporter_summary_includes_all_fields(capsys):
    r = TTYReporter()
    r.summary(created=2, updated=1, errors=0)
    out, _ = capsys.readouterr()
    assert "Done." in out
    # Fields are sorted for stable test snapshots.
    assert "created=2" in out
    assert "updated=1" in out
    assert "errors=0" in out


def test_tty_reporter_strips_control_characters_from_entries(capsys):
    """Graph-controlled fields might contain ANSI/OSC escapes; the reporter
    sanitizes them before printing."""
    r = TTYReporter()
    r.entry("pull.row", action="UPDATE", name="\x1b]52;c;HACKED\x07", mail="x@y.com", phone="—")
    out, _ = capsys.readouterr()
    assert "\x1b" not in out
    assert "HACKED" in out  # the visible text remains; only the OSC envelope is stripped


# --------------------------------------------------------------------------- JSON


def test_json_reporter_emits_one_object_per_event():
    """Each emit is a self-contained JSON object on its own line (ndjson)."""
    buf = io.StringIO()
    r = JSONReporter(stream=buf)
    r.info("loading")
    r.entry("pull.row", action="CREATE", name="x", mail="x@y.com", phone="555")
    r.summary(created=1, errors=0)

    lines = buf.getvalue().strip().split("\n")
    assert len(lines) == 3
    info, row, summary = (json.loads(line) for line in lines)
    assert info == {"kind": "info", "message": "loading"}
    assert row["kind"] == "entry"
    assert row["type"] == "pull.row"
    assert row["action"] == "CREATE"
    assert summary == {"kind": "summary", "created": 1, "errors": 0}


def test_json_reporter_phase_events_bracket_progress():
    """Each `progress(...)` block emits a phase marker so consumers can group entries."""
    buf = io.StringIO()
    r = JSONReporter(stream=buf)
    with r.progress(total=10, unit="contact", desc="Pulling"):
        pass
    parsed = [json.loads(line) for line in buf.getvalue().strip().split("\n")]
    assert parsed == [{"kind": "phase", "desc": "Pulling", "unit": "contact", "total": 10}]


def test_json_reporter_confirm_returns_false_unconditionally():
    """JSON mode is non-interactive — refuse destructive ops by default."""
    buf = io.StringIO()
    r = JSONReporter(stream=buf)
    assert r.confirm(47, "ALL") is False


def test_json_reporter_handles_unjsonable_field_via_default_str():
    """A re.Pattern (or any `default=str`-coercible object) leaking through
    a payload field doesn't crash the emitter."""
    import re

    buf = io.StringIO()
    r = JSONReporter(stream=buf)
    r.entry("filter.rejected", pattern=re.compile(r"^Test"))
    obj = json.loads(buf.getvalue().strip())
    assert obj["pattern"].startswith("re.compile")


def test_json_reporter_strips_control_chars_from_string_leaves():
    """A hostile Graph response containing OSC-52 / cursor-manipulation
    sequences must not reach a JSON consumer (cron mail, journalctl, log
    shipper) verbatim. The reporter scrubs every string leaf in the payload
    recursively — top-level, dict values, list elements, all of it."""
    buf = io.StringIO()
    r = JSONReporter(stream=buf)
    # Embedded OSC-52 (clipboard hijack), C1 cursor-control, and CSI escape
    # in three different payload positions.
    r.entry(
        "subrequest.error",
        action="UPDATE",
        name="Doe\x1b]52;c;HACKED\x07Jane",  # top-level string
        body={"nested": "leak\x1b[2J", "list": ["fine", "bad\x9e"]},  # nested dict + list
    )
    obj = json.loads(buf.getvalue().strip())
    # No raw control bytes anywhere.
    raw = json.dumps(obj)
    assert "\x1b" not in raw
    assert "\x07" not in raw
    assert "\x9e" not in raw
    # But the visible text the user actually wrote remains.
    assert "Doe" in obj["name"]
    assert "Jane" in obj["name"]
    assert "HACKED" in obj["name"]


def test_logging_bridge_routes_levels_to_reporter():
    """`_galpal.graph` emits structured retry / throttle events via
    `logging.getLogger("galpal.graph")`. The CLI wires those records through
    the active reporter so cron operators see them as ndjson and TTY users
    see them inline. Pin the level mapping: INFO → info, WARNING → warning,
    ERROR → error."""
    import logging

    from _galpal.cli import _wire_logging

    rec = RecordingReporter()
    _wire_logging(rec)

    log = logging.getLogger("galpal.graph")
    log.info("retry attempt 1")
    log.warning("budget exhausted")
    log.error("synthesized 500")

    kinds_msgs = [(e["kind"], e.get("message")) for e in rec.events]
    assert ("info", "retry attempt 1") in kinds_msgs
    assert ("warning", "budget exhausted") in kinds_msgs
    assert ("error", "synthesized 500") in kinds_msgs


def test_logging_bridge_is_idempotent_across_main_calls():
    """`_wire_logging` clears existing handlers before adding its own — without
    this, in-process re-invocations (tests, REPL, embeds) would accumulate
    duplicate handlers and emit each event N times."""
    import logging

    from _galpal.cli import _wire_logging

    _wire_logging(RecordingReporter())
    _wire_logging(RecordingReporter())
    _wire_logging(RecordingReporter())

    log = logging.getLogger("galpal")
    assert len(log.handlers) == 1


def test_json_reporter_summary_strings_are_scrubbed():
    """Stats can carry strings on some subcommands (e.g. an aborted reason)
    — they're scrubbed too, not just `entry` payloads."""
    buf = io.StringIO()
    r = JSONReporter(stream=buf)
    r.summary(reason="user said \x1b]0;evil\x07no", count=1)
    obj = json.loads(buf.getvalue().strip())
    assert "\x1b" not in obj["reason"]
    assert obj["count"] == 1


# --------------------------------------------------------------------------- Quiet


def test_quiet_reporter_drops_info_keeps_summary_and_errors(capsys):
    r = QuietReporter()
    r.info("loading")  # dropped
    r.entry("pull.row", action="CREATE", name="x", mail="x@y.com", phone="—")  # dropped
    r.error("something failed")  # kept
    r.summary(created=0, errors=1)  # kept
    out, err = capsys.readouterr()
    assert "loading" not in out
    assert "CREATE" not in out
    assert "something failed" in err
    assert "errors=1" in out


def test_quiet_reporter_keeps_subrequest_errors_visible(capsys):
    """Subrequest errors aren't routine entries — they're failures and must
    survive --quiet so the user sees what went wrong."""
    r = QuietReporter()
    r.entry("subrequest.error", action="UPDATE", name="x", status=500, body={"e": "boom"})
    out, _ = capsys.readouterr()
    assert "ERROR" in out


# --------------------------------------------------------------------------- Recording (tests)


def test_recording_reporter_captures_kind_and_fields():
    r = RecordingReporter()
    r.info("hi")
    r.entry("pull.row", action="CREATE", name="x")
    r.summary(created=1)

    assert r.kinds() == ["info", "entry", "summary"]
    assert r.events[1]["type"] == "pull.row"
    assert r.events[1]["action"] == "CREATE"
    assert r.summary_kwargs == {"created": 1}


def test_recording_reporter_confirm_response_is_configurable():
    r_yes = RecordingReporter(confirm_response=True)
    r_no = RecordingReporter(confirm_response=False)
    assert r_yes.confirm(5, "ALL") is True
    assert r_no.confirm(5, "ALL") is False
    assert r_yes.confirm_calls == [(5, "ALL")]


# --------------------------------------------------------------------------- Integration


def test_run_pull_drives_recording_reporter_end_to_end(graph, monkeypatch):
    """The highest-leverage test in this file: run_pull operates entirely
    through the reporter's API, so a RecordingReporter sees every event the
    orchestrator emitted — without a single `capsys` substring grep."""
    monkeypatch.setenv("GALPAL_FORCE_NONINTERACTIVE", "1")
    graph.users.append(make_user("u1"))

    rec = RecordingReporter()
    errors = run_pull(
        "tok",
        FilterConfig(),
        dry_run=True,
        limit=0,
        batch_size=20,
        reporter=rec,
    )

    assert errors == 0
    # Sequence sanity: status info before progress; one row event; summary at end.
    assert "info" in rec.kinds()
    assert any(e["kind"] == "phase" and e["desc"] == "Fetching GAL" for e in rec.events)
    pull_rows = [e for e in rec.events if e.get("type") == "pull.row"]
    assert len(pull_rows) == 1
    assert pull_rows[0]["action"] == "CREATE"
    assert rec.summary_kwargs is not None
    assert rec.summary_kwargs["created"] == 1
    assert rec.summary_kwargs["dry_run"] is True


def test_cli_json_flag_swaps_reporter(run_cli, graph, monkeypatch):
    """End-to-end: --json swaps in the JSONReporter; stdout is parseable ndjson."""
    monkeypatch.setenv("GALPAL_FORCE_NONINTERACTIVE", "1")
    graph.users.append(make_user("u1"))
    code, out, _ = run_cli("--json", "pull", "--dry-run")
    assert code == 0
    # Every non-empty line should parse as a JSON object.
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines, "expected at least one JSON event"
    parsed = [json.loads(line) for line in lines]
    kinds = [obj["kind"] for obj in parsed]
    assert "summary" in kinds
    summary = next(obj for obj in parsed if obj["kind"] == "summary")
    assert summary["created"] == 1
    assert summary["dry_run"] is True


# Ensure tests import the actual module (catches dead imports, missing exports).
def test_reporter_module_exposes_protocol_at_runtime():
    assert hasattr(rep_mod, "Reporter")
    # `isinstance` against a runtime_checkable Protocol works only for the
    # methods present at runtime.
    assert isinstance(TTYReporter(), rep_mod.Reporter)


# pytest visibility: ensure test_reporter.py is registered with the right path.
@pytest.fixture(autouse=True)
def _ensure_clean_env(monkeypatch):
    """Remove any GALPAL_REPORTER env that might leak across tests in this file."""
    monkeypatch.delenv("GALPAL_REPORTER", raising=False)
