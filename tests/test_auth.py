"""Unit tests for `_galpal/auth.py`.

The `graph` fixture in conftest.py monkeypatches `cli.get_token` to a stub, so
the live e2e tests never hit `_galpal.auth.get_token`'s body. These tests cover
that body directly with an msal stub: the silent cache hit, the device-flow
fallback, both error-exit branches, and the on-disk cache write semantics
(atomic replace, mode 0600, corrupt-cache recovery).
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _galpal import auth


@pytest.fixture
def fake_app(monkeypatch):
    """Stub `msal.PublicClientApplication` with a Mock the test can program.

    Each test sets `.get_accounts`, `.acquire_token_silent`,
    `.initiate_device_flow`, and/or `.acquire_token_by_device_flow` return
    values to drive the path through `get_token`.

    Bypasses the TTY guard via `GALPAL_FORCE_DEVICE_CODE=1` since pytest
    captures stdin and `isatty()` returns False under the test runner.

    Also disables the device-code clipboard / browser helpers — without this,
    a test that drives the device-flow path would actually launch a browser
    and shell out to `pbcopy` (or its peers) on the developer's machine.
    """
    monkeypatch.setenv("GALPAL_FORCE_DEVICE_CODE", "1")
    monkeypatch.setenv(auth.ENV_NO_BROWSER, "1")
    monkeypatch.setenv(auth.ENV_NO_CLIPBOARD, "1")
    app = mock.Mock()
    monkeypatch.setattr(auth.msal, "PublicClientApplication", lambda *a, **kw: app)
    return app


@pytest.fixture
def cache_in_tmp(monkeypatch, tmp_path):
    """Point `auth.TOKEN_CACHE` at a tempdir so tests don't disturb the user's real cache.

    Also redirects `auth.LEGACY_TOKEN_CACHE` to a tempdir-only path: without
    this, the project-root → XDG migration in `_read_token_cache` would
    notice an actual `.token_cache.json` at the project root (the developer's
    own working cache) and try to migrate it during the test run.
    """
    target = tmp_path / "token_cache.json"
    monkeypatch.setattr(auth, "TOKEN_CACHE", target)
    monkeypatch.setattr(auth, "LEGACY_TOKEN_CACHE", tmp_path / ".legacy-not-present.json")
    return target


# --------------------------------------------------------------------------- get_token paths


def test_get_token_silent_cache_hit_returns_token(fake_app, cache_in_tmp):
    fake_app.get_accounts.return_value = [{"username": "user@x.com"}]
    fake_app.acquire_token_silent.return_value = {"access_token": "T-silent"}
    assert auth.get_token("cid") == "T-silent"
    fake_app.initiate_device_flow.assert_not_called()


def test_get_token_falls_back_to_device_flow_when_no_account(fake_app, cache_in_tmp, capsys):
    fake_app.get_accounts.return_value = []
    fake_app.initiate_device_flow.return_value = {"user_code": "ABC", "message": "go to /devicelogin and enter ABC"}
    fake_app.acquire_token_by_device_flow.return_value = {"access_token": "T-dev"}
    assert auth.get_token("cid") == "T-dev"
    # The prompt goes to stderr so it doesn't corrupt --json output.
    err = capsys.readouterr().err
    assert "/devicelogin" in err


def test_get_token_falls_back_to_device_flow_when_silent_returns_none(fake_app, cache_in_tmp):
    fake_app.get_accounts.return_value = [{"username": "u"}]
    fake_app.acquire_token_silent.return_value = None  # token expired, no refresh
    fake_app.initiate_device_flow.return_value = {"user_code": "X", "message": "msg"}
    fake_app.acquire_token_by_device_flow.return_value = {"access_token": "T2"}
    assert auth.get_token("cid") == "T2"


def test_get_token_raises_when_device_flow_init_fails(fake_app, cache_in_tmp):
    fake_app.get_accounts.return_value = []
    fake_app.initiate_device_flow.return_value = {"error": "policy_blocks", "error_description": "nope"}
    with pytest.raises(auth.DeviceFlowError, match="Could not start device flow"):
        auth.get_token("cid")


def test_get_token_refuses_device_flow_on_non_tty(fake_app, cache_in_tmp, monkeypatch):
    """Cron / piped stdin must not silently hang for 15 minutes on the device-
    code prompt. Refuse instead, with a clear escape-hatch env var."""
    monkeypatch.delenv("GALPAL_FORCE_DEVICE_CODE", raising=False)
    monkeypatch.setattr("sys.stdin", mock.Mock(isatty=lambda: False))
    fake_app.get_accounts.return_value = []
    with pytest.raises(auth.DeviceFlowError) as exc:
        auth.get_token("cid")
    assert "non-TTY" in str(exc.value)
    assert "GALPAL_FORCE_DEVICE_CODE" in str(exc.value)
    fake_app.initiate_device_flow.assert_not_called()


# --------------------------------------------------------------------------- resolve_client_id
#
# Now testable without the `pytest.raises(SystemExit)` dance — typed
# exceptions let assertions read like the contract.


def test_resolve_client_id_alias_returns_known_guid():
    assert auth.resolve_client_id("office") == auth.KNOWN_CLIENTS["office"]
    assert auth.resolve_client_id("vs") == auth.KNOWN_CLIENTS["vs"]


def test_resolve_client_id_known_guid_passes_through():
    """A raw UUID that happens to be a vetted Microsoft client returns as-is."""
    office_guid = auth.KNOWN_CLIENTS["office"]
    assert auth.resolve_client_id(office_guid) == office_guid


def test_resolve_client_id_rejects_non_uuid_garbage():
    with pytest.raises(auth.InvalidClientIdError, match="neither a known alias"):
        auth.resolve_client_id("not-a-uuid", source="--client-id")


def test_resolve_client_id_rejects_unknown_uuid_without_opt_in(monkeypatch):
    """A UUID that isn't in KNOWN_GUIDS is the canonical illicit-consent shape —
    refused unless the user has set GALPAL_ALLOW_UNKNOWN_CLIENT_ID=1."""
    monkeypatch.delenv("GALPAL_ALLOW_UNKNOWN_CLIENT_ID", raising=False)
    attacker_uuid = "00000000-0000-0000-0000-000000000001"
    with pytest.raises(auth.InvalidClientIdError) as exc:
        auth.resolve_client_id(attacker_uuid, source="--client-id")
    assert "Refusing to use unknown client id" in str(exc.value)
    assert "GALPAL_ALLOW_UNKNOWN_CLIENT_ID" in str(exc.value)


def test_resolve_client_id_accepts_unknown_uuid_with_opt_in(monkeypatch):
    """The escape hatch: a self-registered AAD app id passes through when the
    user explicitly opted in. Same UUID rejected above is now accepted."""
    monkeypatch.setenv("GALPAL_ALLOW_UNKNOWN_CLIENT_ID", "1")
    attacker_uuid = "00000000-0000-0000-0000-000000000001"
    assert auth.resolve_client_id(attacker_uuid) == attacker_uuid


# --------------------------------------------------------------------------- AuthError hierarchy


def test_auth_error_subclasses_share_a_common_base():
    """All three concrete errors share `AuthError` as the base, so callers
    can `except AuthError:` for a uniform error path."""
    assert issubclass(auth.InvalidClientIdError, auth.AuthError)
    assert issubclass(auth.DeviceFlowError, auth.AuthError)
    assert issubclass(auth.TokenAcquisitionError, auth.AuthError)


def test_get_token_raises_when_device_flow_returns_no_access_token(fake_app, cache_in_tmp):
    fake_app.get_accounts.return_value = []
    fake_app.initiate_device_flow.return_value = {"user_code": "X", "message": "msg"}
    fake_app.acquire_token_by_device_flow.return_value = {"error": "expired_token", "error_description": "code expired"}
    with pytest.raises(auth.TokenAcquisitionError) as exc:
        auth.get_token("cid")
    assert "Auth failed" in str(exc.value)
    assert "expired" in str(exc.value).lower()


# --------------------------------------------------------------------------- token cache write


def test_get_token_writes_cache_when_state_changed(fake_app, cache_in_tmp, monkeypatch):
    """A successful auth that mutated the cache writes it to disk at mode 0600."""
    fake_app.get_accounts.return_value = []
    fake_app.initiate_device_flow.return_value = {"user_code": "X", "message": "msg"}
    fake_app.acquire_token_by_device_flow.return_value = {"access_token": "T"}

    # Stub the SerializableTokenCache so we can deterministically toggle
    # `has_state_changed` and observe what serialize() returned.
    fake_cache = SimpleNamespace(
        deserialize=mock.Mock(),
        serialize=mock.Mock(return_value='{"fake": "cache"}'),
        has_state_changed=True,
    )
    monkeypatch.setattr(auth.msal, "SerializableTokenCache", lambda: fake_cache)

    auth.get_token("cid")

    assert cache_in_tmp.exists()
    assert cache_in_tmp.read_text() == '{"fake": "cache"}'
    # Mode 0600 is the load-bearing security guarantee — the whole point of
    # the atomic-write helper is that this mode is set from creation, never
    # via a post-hoc chmod that races the open.
    mode = stat.S_IMODE(cache_in_tmp.stat().st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_get_token_does_not_write_cache_when_state_unchanged(fake_app, cache_in_tmp, monkeypatch):
    """Steady-state silent acquisition that didn't mutate the cache must not write."""
    fake_app.get_accounts.return_value = [{"username": "u"}]
    fake_app.acquire_token_silent.return_value = {"access_token": "T-silent"}

    fake_cache = SimpleNamespace(
        deserialize=mock.Mock(),
        serialize=mock.Mock(return_value='{"unchanged": true}'),
        has_state_changed=False,
    )
    monkeypatch.setattr(auth.msal, "SerializableTokenCache", lambda: fake_cache)

    auth.get_token("cid")
    assert not cache_in_tmp.exists()  # write skipped — no state change


def test_atomic_write_secret_creates_no_temp_residue_on_success(tmp_path):
    """The .tmp sibling created during the atomic write must be gone after success."""
    target = tmp_path / "secret.json"
    auth._atomic_write_secret(target, "payload")
    assert target.read_text() == "payload"
    # No leftover .tmp files.
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_secret_cleans_up_temp_on_write_failure(tmp_path, monkeypatch):
    """A mid-write exception must not leave a .tmp file behind."""
    target = tmp_path / "secret.json"

    err = OSError("disk full")

    def boom(*a, **kw):
        raise err

    # Force fsync to fail after the temp file was already created+written.
    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError, match="disk full"):
        auth._atomic_write_secret(target, "payload")
    assert not target.exists()
    assert list(tmp_path.glob("*.tmp")) == []


# --------------------------------------------------------------------------- token cache read


def test_read_token_cache_recovers_from_corrupt_cache(cache_in_tmp, monkeypatch, capsys):
    """A truncated/corrupt cache should be deleted and re-authenticated cleanly."""
    cache_in_tmp.write_text("not valid json {{{")

    # Stub deserialize to raise (mimicking msal's behavior on garbage).
    cache = SimpleNamespace(deserialize=mock.Mock(side_effect=ValueError("bad json")))
    auth._read_token_cache(cache)  # type: ignore[arg-type]

    err = capsys.readouterr().err
    assert "unreadable" in err
    assert not cache_in_tmp.exists()  # corrupt cache was unlinked


def test_read_token_cache_no_op_when_missing(tmp_path, monkeypatch):
    """No cache file is the steady-state on first run; must not raise."""
    monkeypatch.setattr(auth, "TOKEN_CACHE", tmp_path / "missing.json")
    monkeypatch.setattr(auth, "LEGACY_TOKEN_CACHE", tmp_path / "legacy-missing.json")
    cache = SimpleNamespace(deserialize=mock.Mock())
    auth._read_token_cache(cache)  # type: ignore[arg-type]
    cache.deserialize.assert_not_called()


@pytest.mark.skipif(sys.platform == "win32", reason="O_NOFOLLOW is POSIX-only")
def test_read_token_cache_refuses_symlink_at_cache_path(tmp_path, monkeypatch, capsys):
    """`O_NOFOLLOW` is the load-bearing symlink-TOCTOU defense in
    `_read_token_cache`. A regression that drops the flag (or switches to
    plain `open()`) would let a malicious neighbor on a multi-user POSIX box
    plant a symlink at the cache path and silently leak whatever the link
    pointed at into msal's deserializer.

    Verify the defense: when `TOKEN_CACHE` is a symlink, the open refuses
    (EMLINK on macOS / ELOOP on Linux), the cache file is unlinked, and
    msal's deserializer is never called.
    """
    real = tmp_path / "real.json"
    real.write_text('{"some": "cache"}')
    link = tmp_path / "linked.json"
    link.symlink_to(real)
    monkeypatch.setattr(auth, "TOKEN_CACHE", link)
    monkeypatch.setattr(auth, "LEGACY_TOKEN_CACHE", tmp_path / "no-legacy.json")

    cache = SimpleNamespace(deserialize=mock.Mock())
    auth._read_token_cache(cache)  # type: ignore[arg-type]

    cache.deserialize.assert_not_called()
    err = capsys.readouterr().err
    assert "unreadable" in err
    # Symlink itself was unlinked; the file it pointed at is unaffected.
    assert not link.exists()
    assert real.exists()


# --------------------------------------------------------------------------- XDG migration


def test_default_token_cache_path_macos(monkeypatch, tmp_path):
    """On macOS the cache must land under ~/Library/Application Support/galpal/."""
    monkeypatch.delenv("GALPAL_TOKEN_CACHE_PATH", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    p = auth._default_token_cache_path()
    assert p == tmp_path / "Library" / "Application Support" / "galpal" / "token_cache.json"


def test_default_token_cache_path_linux_xdg(monkeypatch, tmp_path):
    """Linux/BSD honor $XDG_DATA_HOME, falling back to ~/.local/share."""
    monkeypatch.delenv("GALPAL_TOKEN_CACHE_PATH", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert auth._default_token_cache_path() == tmp_path / ".local" / "share" / "galpal" / "token_cache.json"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "custom-xdg"))
    assert auth._default_token_cache_path() == tmp_path / "custom-xdg" / "galpal" / "token_cache.json"


def test_default_token_cache_path_windows(monkeypatch, tmp_path):
    """Windows uses %APPDATA% (Roaming) when set, else %LOCALAPPDATA%, else falls back."""
    monkeypatch.delenv("GALPAL_TOKEN_CACHE_PATH", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert auth._default_token_cache_path() == tmp_path / "Roaming" / "galpal" / "token_cache.json"


def test_default_token_cache_path_env_override(monkeypatch, tmp_path):
    """GALPAL_TOKEN_CACHE_PATH wins outright, regardless of platform."""
    custom = tmp_path / "weird" / "place" / "tc.json"
    monkeypatch.setenv("GALPAL_TOKEN_CACHE_PATH", str(custom))
    assert auth._default_token_cache_path() == custom


def test_migrate_legacy_cache_moves_file_and_emits_notice(tmp_path, monkeypatch, capsys):
    """A cache at the legacy project-root location is moved to the XDG location
    on first run, the original is unlinked, and the user gets a one-line notice."""
    legacy = tmp_path / ".token_cache.json"
    legacy.write_text("legacy-content")
    new = tmp_path / "xdg" / "galpal" / "token_cache.json"
    monkeypatch.setattr(auth, "LEGACY_TOKEN_CACHE", legacy)
    monkeypatch.setattr(auth, "TOKEN_CACHE", new)

    auth._migrate_legacy_cache()

    assert new.exists()
    assert new.read_text() == "legacy-content"
    assert not legacy.exists()
    # The migration note is buffered (auth fires before the reporter exists);
    # `take_migration_notes()` drains the buffer for the CLI to flush.
    notes = auth.take_migration_notes()
    assert any("migrated token cache" in n for n in notes)
    # Buffer is drained — second call returns empty.
    assert auth.take_migration_notes() == []


def test_migrate_legacy_cache_is_noop_when_xdg_already_present(tmp_path, monkeypatch):
    """If the XDG cache already exists we never touch the legacy file — the user
    has already moved past the migration. Re-running the tool must be idempotent."""
    legacy = tmp_path / ".token_cache.json"
    legacy.write_text("stale-legacy")
    new = tmp_path / "xdg" / "galpal" / "token_cache.json"
    new.parent.mkdir(parents=True)
    new.write_text("current-xdg")
    monkeypatch.setattr(auth, "LEGACY_TOKEN_CACHE", legacy)
    monkeypatch.setattr(auth, "TOKEN_CACHE", new)

    auth._migrate_legacy_cache()

    # Both files are unchanged.
    assert legacy.read_text() == "stale-legacy"
    assert new.read_text() == "current-xdg"


def test_migrate_legacy_cache_no_op_when_neither_exists(tmp_path, monkeypatch):
    """Fresh install: no legacy cache, no XDG cache. Migration is a clean no-op."""
    monkeypatch.setattr(auth, "LEGACY_TOKEN_CACHE", tmp_path / "no-legacy.json")
    monkeypatch.setattr(auth, "TOKEN_CACHE", tmp_path / "xdg" / "no-xdg.json")
    auth._migrate_legacy_cache()  # must not raise


def test_migrate_legacy_cache_writes_at_mode_0600(tmp_path, monkeypatch):
    """The migrated file lands at 0600 from the start (load-bearing for the
    multi-user-host threat model — same guarantee as a fresh write)."""
    legacy = tmp_path / ".token_cache.json"
    legacy.write_text("payload")
    new = tmp_path / "xdg" / "galpal" / "token_cache.json"
    monkeypatch.setattr(auth, "LEGACY_TOKEN_CACHE", legacy)
    monkeypatch.setattr(auth, "TOKEN_CACHE", new)

    auth._migrate_legacy_cache()
    mode = stat.S_IMODE(new.stat().st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


# --------------------------------------------------------------------------- device-code helpers


def test_copy_to_clipboard_routes_through_platform_tool(monkeypatch):
    """Verify the helper resolves the right tool per platform via shutil.which
    and feeds the code on stdin. We don't assert on a real clipboard — that
    would actually overwrite the developer's pasteboard during the test run."""
    calls: list[tuple[list[str], str]] = []

    def fake_which(name):
        # Pretend the macOS / Windows / Linux tool is available on PATH.
        return f"/fake/bin/{name}" if name in {"pbcopy", "clip", "wl-copy"} else None

    def fake_run(cmd, *, input, text, check, timeout):  # noqa: A002  -- mirrors subprocess.run kwarg name
        del text, check, timeout
        calls.append((cmd, input))

    monkeypatch.delenv(auth.ENV_NO_CLIPBOARD, raising=False)
    monkeypatch.setattr(auth.shutil, "which", fake_which)
    monkeypatch.setattr(auth.subprocess, "run", fake_run)

    monkeypatch.setattr(auth.sys, "platform", "darwin")
    assert auth._copy_to_clipboard("ABC123") is True
    assert calls[-1] == (["/fake/bin/pbcopy"], "ABC123")

    monkeypatch.setattr(auth.sys, "platform", "win32")
    assert auth._copy_to_clipboard("XYZ789") is True
    assert calls[-1] == (["/fake/bin/clip"], "XYZ789")

    monkeypatch.setattr(auth.sys, "platform", "linux")
    assert auth._copy_to_clipboard("LIN456") is True
    # Wayland's wl-copy is preferred over X11 tools.
    assert calls[-1] == (["/fake/bin/wl-copy"], "LIN456")


def test_copy_to_clipboard_returns_false_when_disabled_via_env(monkeypatch):
    """GALPAL_NO_CLIPBOARD=1 short-circuits before any subprocess attempt."""
    monkeypatch.setenv(auth.ENV_NO_CLIPBOARD, "1")
    # If the gate failed, this would crash because shutil.which is unmocked
    # and subprocess.run might still be available — the gate must take effect first.
    monkeypatch.setattr(auth.shutil, "which", lambda name: None)
    assert auth._copy_to_clipboard("ANY") is False


def test_copy_to_clipboard_returns_false_when_no_tool_available(monkeypatch):
    """A box with no clipboard tool installed (headless Linux without xclip /
    wl-copy) is a no-op — no warning, no crash, the auth flow continues."""
    monkeypatch.delenv(auth.ENV_NO_CLIPBOARD, raising=False)
    monkeypatch.setattr(auth.shutil, "which", lambda name: None)
    monkeypatch.setattr(auth.sys, "platform", "linux")
    assert auth._copy_to_clipboard("ANY") is False


def test_copy_to_clipboard_swallows_subprocess_failure(monkeypatch):
    """A clipboard tool that exits non-zero (broken X server, locked Wayland
    daemon, anything) must not raise — clipboard is best-effort by design."""
    import subprocess as _subprocess

    monkeypatch.delenv(auth.ENV_NO_CLIPBOARD, raising=False)
    monkeypatch.setattr(auth.sys, "platform", "darwin")
    monkeypatch.setattr(auth.shutil, "which", lambda name: f"/fake/bin/{name}")

    def boom(*a, **kw):
        raise _subprocess.CalledProcessError(1, "pbcopy")

    monkeypatch.setattr(auth.subprocess, "run", boom)
    assert auth._copy_to_clipboard("ANY") is False


def test_open_browser_skips_when_running_over_ssh(monkeypatch):
    """A browser launch on an SSH host either fails noisily or pops a window
    the user can't see. The SSH heuristic skips it without needing the
    explicit env var."""
    monkeypatch.delenv(auth.ENV_NO_BROWSER, raising=False)
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.1 22 10.0.0.2 22")

    called = {"count": 0}

    def boom(*a, **kw):
        called["count"] += 1
        return True

    monkeypatch.setattr(auth.webbrowser, "open", boom)
    assert auth._open_browser("https://example.com") is False
    assert called["count"] == 0


def test_open_browser_skips_when_disabled_via_env(monkeypatch):
    """GALPAL_NO_BROWSER=1 short-circuits even on a local TTY (tmux on a
    headless workstation, etc.)."""
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.setenv(auth.ENV_NO_BROWSER, "1")

    monkeypatch.setattr(auth.webbrowser, "open", lambda *a, **kw: True)  # would succeed if reached
    assert auth._open_browser("https://example.com") is False


def test_open_browser_calls_webbrowser_when_local_tty(monkeypatch):
    """No SSH, no env override → we delegate to `webbrowser.open(url, new=2)`."""
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.delenv(auth.ENV_NO_BROWSER, raising=False)

    captured: dict = {}

    def fake_open(url, new=0):
        captured["url"] = url
        captured["new"] = new
        return True

    monkeypatch.setattr(auth.webbrowser, "open", fake_open)
    assert auth._open_browser("https://microsoft.com/devicelogin") is True
    # `new=2` requests a new tab in the existing browser window where supported.
    assert captured == {"url": "https://microsoft.com/devicelogin", "new": 2}


def test_open_browser_swallows_webbrowser_error(monkeypatch):
    """`webbrowser.Error` from a misconfigured BROWSER env var or a missing
    binary must not crash the auth flow."""
    import webbrowser as _wb

    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.delenv(auth.ENV_NO_BROWSER, raising=False)

    err = _wb.Error("no browser")

    def boom(*a, **kw):
        raise err

    monkeypatch.setattr(auth.webbrowser, "open", boom)
    assert auth._open_browser("https://example.com") is False


def test_print_device_flow_prompt_announces_clipboard_and_browser(capsys, monkeypatch):
    """When both helpers succeed the prompt advertises that fact to the user.

    The prompt is on stderr (not stdout) so a `--json | jq` consumer doesn't
    have its parseable output corrupted by the auth message.
    """
    monkeypatch.setattr(auth, "_copy_to_clipboard", lambda code: True)
    monkeypatch.setattr(auth, "_open_browser", lambda url: True)
    auth._print_device_flow_prompt(
        {"user_code": "ABC123", "verification_uri": "https://microsoft.com/devicelogin", "expires_in": 900},
    )
    captured = capsys.readouterr()
    assert "ABC123" in captured.err
    assert "(copied to clipboard" in captured.err
    assert "(opened in your browser)" in captured.err
    # Stdout stays clean — the JSON-friendly invariant.
    assert captured.out == ""


def test_print_device_flow_prompt_silent_when_helpers_unavailable(capsys, monkeypatch):
    """If neither helper succeeded we don't lie about it — no annotation
    appears next to the URL or the code."""
    monkeypatch.setattr(auth, "_copy_to_clipboard", lambda code: False)
    monkeypatch.setattr(auth, "_open_browser", lambda url: False)
    auth._print_device_flow_prompt(
        {"user_code": "ABC123", "verification_uri": "https://microsoft.com/devicelogin", "expires_in": 900},
    )
    err = capsys.readouterr().err
    assert "ABC123" in err
    assert "copied to clipboard" not in err
    assert "opened in your browser" not in err
