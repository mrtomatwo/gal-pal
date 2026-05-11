"""MSAL device-code authentication and on-disk token cache.

Pure auth plumbing — no GAL knowledge, no presentation. The rest of the package
calls `get_token(client_id)` and gets back a Graph access token.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
import webbrowser
from pathlib import Path

import msal

# Microsoft-owned first-party public clients that support device-code flow.
# Some tenants block specific ones (AADSTS50105). Try in order; override with --client-id.
KNOWN_CLIENTS = {
    # Office desktop client — broadest Graph pre-authorization (incl. Contacts.ReadWrite),
    # universally allowed in tenants that use Outlook.
    "office": "d3590ed6-52b3-4102-aeff-aad2292ab01c",
    "vs": "872cd9fa-d31f-45e0-9eab-6e460a02d1f1",
    "azure-cli": "04b07795-8ddb-461a-bbee-02f9e1bf7b46",
    "azure-ps": "1950a258-227b-4e31-a9cf-717495945fc8",
    "graph-cli": "14d82eec-204b-4c2f-b7e8-296a70dab67e",
}
DEFAULT_CLIENT_ID = KNOWN_CLIENTS["office"]
KNOWN_GUIDS = frozenset(KNOWN_CLIENTS.values())

AUTHORITY = "https://login.microsoftonline.com/common"
SCOPES = ["User.ReadBasic.All", "Contacts.ReadWrite"]

# POSIX permissions for the cache directory and the cache file itself.
# Constants instead of magic literals so the intent is searchable.
_PARENT_MODE = 0o700  # owner-only directory
_CACHE_MODE = 0o600  # owner-only file


# --------------------------------------------------------------------------- exceptions


class AuthError(Exception):
    """Base class for galpal authentication failures.

    All subclasses carry a single user-friendly message as their first arg, so
    `str(e)` produces the line the CLI passes to `sys.exit`. Programmatic
    callers (a TUI, a test harness, a future web frontend) can catch the base
    for a uniform error path or a specific subclass for targeted handling.
    """


class InvalidClientIdError(AuthError):
    """The configured client id isn't a valid alias or a vetted Microsoft UUID.

    Raised by `resolve_client_id`. Two flavors: the value didn't even look
    like a UUID, or it's a UUID we don't recognize and the caller hasn't set
    `GALPAL_ALLOW_UNKNOWN_CLIENT_ID=1` to opt in.
    """


class DeviceFlowError(AuthError):
    """Device-code flow couldn't start.

    Two shapes: refused on non-TTY stdin (cron / pipe, no `GALPAL_FORCE_DEVICE_CODE`
    override), or `MSAL.initiate_device_flow` returned a body with no `user_code`.
    Distinct from `TokenAcquisitionError` because here we never even printed
    the prompt — there's nothing the user could have done to make it succeed.
    """


class TokenAcquisitionError(AuthError):
    """The device-code flow ran but didn't return an access_token.

    Typical causes: the user let the code expire, the tenant policy denied
    the consent, or the app id was rejected by AAD. `result["error_description"]`
    from MSAL is preserved in the message so the failure mode is visible.
    """


# --------------------------------------------------------------------------- client-id resolution


def resolve_client_id(raw: str, *, source: str = "--client-id") -> str:
    """Resolve a `--client-id` / `GALPAL_CLIENT_ID` value to a Microsoft GUID.

    Defends against the illicit-consent-grant phishing pattern: an attacker
    with influence over the user's environment (poisoned dotfile, malicious
    .envrc, CI matrix variable) plants their own AAD app id, the user goes
    through device-code flow at a real microsoft.com URL, and consents to the
    attacker's app — which now holds a delegated token for the user's mailbox
    until they revoke it in Entra.

    Three layers of defense:
      1. Aliases (KNOWN_CLIENTS) resolve to known-public-client GUIDs.
      2. Anything else must look like a UUID (cheap typo guard).
      3. UUIDs not in KNOWN_GUIDS are refused unless the user opts in via
         GALPAL_ALLOW_UNKNOWN_CLIENT_ID=1. The opt-in is loud — the env var
         exists to support the unusual but legitimate case of someone who
         registered their own AAD app.
    """
    if raw in KNOWN_CLIENTS:
        return KNOWN_CLIENTS[raw]
    try:
        uuid.UUID(raw)
    except (ValueError, AttributeError, TypeError):
        # `from None` because the chained UUID-parse traceback is noise to the
        # end user — the message we craft here is the actionable bit.
        msg = f"{source}={raw!r} is neither a known alias ({', '.join(sorted(KNOWN_CLIENTS))}) nor a UUID."
        raise InvalidClientIdError(msg) from None
    if raw not in KNOWN_GUIDS and not os.environ.get("GALPAL_ALLOW_UNKNOWN_CLIENT_ID"):
        msg = (
            f"Refusing to use unknown client id {raw!r} from {source}.\n"
            f"This would consent to a non-Microsoft app, which is the canonical "
            f"shape of an illicit-consent phishing attack: an attacker plants a "
            f"client id via dotfiles / env vars / CI matrix, the user goes through "
            f"the real microsoft.com/devicelogin URL, and grants the attacker's "
            f"app delegated access to their mailbox.\n"
            f"If you registered this app yourself, set "
            f"GALPAL_ALLOW_UNKNOWN_CLIENT_ID=1 to override (the warning is the point)."
        )
        raise InvalidClientIdError(msg)
    return raw


# Token cache lives at a per-user OS data path (XDG / Application Support /
# %APPDATA%). `GALPAL_TOKEN_CACHE_PATH` overrides.


def _default_token_cache_path() -> Path:
    """Return the per-user token-cache path for this platform.

    Honors GALPAL_TOKEN_CACHE_PATH first; otherwise picks an OS-appropriate
    location:
      - macOS:                    ~/Library/Application Support/galpal/token_cache.json
      - Windows / Cygwin / MSYS:  %APPDATA%/galpal/token_cache.json (or %LOCALAPPDATA% as fallback)
      - other:                    $XDG_DATA_HOME/galpal/token_cache.json (XDG default ~/.local/share)

    Cygwin and MSYS inherit the Windows %APPDATA% env var, so a user who
    runs galpal from PowerShell *and* Git-Bash on the same box gets the
    same cache and only authenticates once. Without this, both fell through
    to the XDG branch and produced two separate caches.
    """
    override = os.environ.get("GALPAL_TOKEN_CACHE_PATH")
    if override:
        return Path(override).expanduser()
    home = Path.home()
    if sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    elif sys.platform in ("win32", "cygwin", "msys"):
        # %APPDATA% is the Roaming profile path on Windows; fall back to
        # %LOCALAPPDATA%, then ~/AppData/Roaming, then home as a last resort.
        appdata = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        base = Path(appdata) if appdata else home / "AppData" / "Roaming"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or (home / ".local" / "share"))
    return base / "galpal" / "token_cache.json"


# Resolve at import time so tests can `monkeypatch.setattr(auth, "TOKEN_CACHE", ...)`.
TOKEN_CACHE = _default_token_cache_path()


# Earlier galpal builds wrote the cache next to the dev shim, at the project
# root. The XDG-style per-user path took over; on first run after the upgrade
# we migrate the existing cache once instead of silently forcing the user
# back through device-code login.
#
# The legacy path is restricted to the *dev-shim* layout: `__file__.parent.parent`
# is only a legitimate cache location when `dev_galpal.py` lives there. For a
# pipx install, that resolves into `site-packages/` — never a real cache and a
# surprising place to write/unlink. None means "no legacy migration applies."
def _legacy_token_cache_path() -> Path | None:
    parent = Path(__file__).resolve().parent.parent
    if (parent / "dev_galpal.py").exists():
        return parent / ".token_cache.json"
    return None


LEGACY_TOKEN_CACHE: Path | None = _legacy_token_cache_path()


# Auth fires before the CLI builds a Reporter, so the migration helper can't
# call `reporter.info(...)` directly. Notes accumulate here and the CLI
# flushes them once the reporter exists. Plain stderr `print()` would corrupt
# `--json | jq` consumers that pipe stderr through their parser.
_migration_notes: list[str] = []


def take_migration_notes() -> list[str]:
    """Return and clear the deferred migration-note buffer.

    Called by `cli.py` right after the reporter is wired up; each note is
    routed through `reporter.info(...)` so it lands on the right output
    stream (TTY line, ndjson event, recorded list, …). Idempotent: subsequent
    calls return an empty list until more notes accumulate.
    """
    notes = list(_migration_notes)
    _migration_notes.clear()
    return notes


def _migrate_legacy_cache() -> None:
    """Move a project-root cache to the per-user XDG location, if needed.

    Idempotent: a no-op when the new cache already exists, or when there's no
    legacy cache to migrate (including pipx installs, where the legacy path
    would have resolved into `site-packages/`). Failure modes (permissions,
    disk full) fall through to "no migration happened", and the user
    re-authenticates normally — losing a refresh token never blocks the
    tool, it just costs one device-code login.
    """
    if TOKEN_CACHE.exists():
        return  # Already migrated, or new install. Either way: nothing to do.
    if LEGACY_TOKEN_CACHE is None or not LEGACY_TOKEN_CACHE.exists():
        return  # New install with no prior history, or no legacy path applies.
    try:
        # Re-write through the atomic-secret writer so the new cache lands at
        # 0600 from the start (the legacy file's perms might be looser if the
        # user touched it manually) and the migration survives a crash mid-move.
        TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True, mode=_PARENT_MODE)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(LEGACY_TOKEN_CACHE, flags), encoding="utf-8") as f:
            data = f.read()
        _atomic_write_secret(TOKEN_CACHE, data)
        LEGACY_TOKEN_CACHE.unlink()
        # Defer the user-visible note until the reporter exists. See
        # `take_migration_notes()` above for the flush mechanism.
        _migration_notes.append(
            f"note: migrated token cache from {LEGACY_TOKEN_CACHE} to {TOKEN_CACHE} "
            f"(per-user XDG location). The old file has been removed.",
        )
    except OSError as e:
        # Migration is best-effort. If it fails, the user re-authenticates;
        # the legacy file stays put for a future run to retry. Same deferred
        # channel as the success note — the warning eventually reaches the
        # reporter rather than corrupting the JSON stream.
        _migration_notes.append(f"warn: token-cache migration failed ({e}); will re-authenticate.")


def _atomic_write_secret(path: Path, data: str) -> None:
    """Write `data` to `path` atomically with mode 0600 from creation.

    Defends against:
      - the umask race that `Path.write_text` + post-`chmod(0o600)` exposes
        (file is world-readable for the window between open and chmod);
      - symlink TOCTOU (`O_NOFOLLOW` refuses to follow a planted symlink);
      - mid-write crashes (write to a sibling temp file, then `os.replace` for
        an atomic rename — the cache file never exists in a half-written state).

    POSIX-only caveats:
      - Parent dir 0700 is enforced even if the directory already existed —
        `Path.mkdir(mode=…, exist_ok=True)` only applies the mode on creation,
        which would silently leave a 0755 inherited dir on most real installs
        (the XDG / Application Support / %APPDATA% parents very often pre-exist).
      - Windows' `fchmod` and `mkdir(mode)` map onto a coarse subset of NTFS
        ACLs and the inherited parent ACE wins. The 0600/0700 contract is
        load-bearing on POSIX; on Windows it's best-effort — see README.
    """
    parent = path.parent
    # Ensure the per-user data directory exists (XDG cache: ~/.local/share/galpal,
    # macOS: ~/Library/Application Support/galpal, Windows: %APPDATA%/galpal).
    # Mode 0700 so other local users can't read/list secrets we put there.
    parent.mkdir(parents=True, exist_ok=True, mode=_PARENT_MODE)
    # Re-assert 0700 on POSIX in case the directory already existed at a
    # looser mode. Best-effort — failures here are not fatal to the write.
    if os.name == "posix":
        try:
            current = parent.stat().st_mode & 0o777
            if current != _PARENT_MODE:
                parent.chmod(_PARENT_MODE)
        except OSError:
            pass
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=parent)
    tmp_path = Path(tmp)
    try:
        # mkstemp creates at mode 0600 on POSIX; tighten just in case (and
        # because some platforms don't apply the umask to mkstemp).
        try:
            os.fchmod(fd, _CACHE_MODE)
        except BaseException:
            # If fchmod raises before we hand `fd` to fdopen, the fd is ours
            # to close — nobody else got it. Without this, the fd leaks until
            # process exit (single-shot CLI, but correctness-wise wrong).
            os.close(fd)
            raise
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        tmp_path.replace(path)  # atomic on POSIX
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _read_token_cache(cache: msal.SerializableTokenCache) -> None:
    """Load the on-disk cache into `cache`. Tolerates a corrupt/partial file.

    If the cache is unreadable (truncated by a crash, JSON-mangled, etc.) we
    quietly delete it — the user re-authenticates via device-code on this run
    and the next write is clean. This is preferable to crashing on a corrupt
    cache and forcing a manual `rm`.
    """
    # One-shot upgrade path: a cache at the old project-root location gets
    # moved to the new XDG location before we look for the new cache. No-op
    # on second and subsequent runs (the legacy file is gone).
    _migrate_legacy_cache()
    if not TOKEN_CACHE.exists():
        return
    try:
        # O_NOFOLLOW where supported — refuse to read through a symlink so a
        # malicious neighbor can't substitute the cache path with a link to
        # another file we'd then read into msal.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(TOKEN_CACHE, flags), encoding="utf-8") as f:
            cache.deserialize(f.read())
    except (OSError, ValueError) as e:
        print(f"warn: token cache at {TOKEN_CACHE} is unreadable ({e}); re-authenticating.", file=sys.stderr)
        with contextlib.suppress(FileNotFoundError):
            TOKEN_CACHE.unlink()


# Microsoft does NOT support `verification_uri_complete` (RFC 8628 §3.3.2) on
# any of its identity-platform endpoints — the docs explicitly call this out
# (https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-device-code).
# So there's no URL we can hand the user that has the code already prefilled;
# the user always has to paste a 9-character code at microsoft.com/devicelogin.
#
# The next-best UX is to make that paste trivial: copy the code to the system
# clipboard and open the verification URL in their default browser, both
# best-effort. Override either with the env vars below — useful on SSH
# sessions, headless boxes, or sandboxes where opening a browser does
# something unhelpful.
ENV_NO_BROWSER = "GALPAL_NO_BROWSER"
ENV_NO_CLIPBOARD = "GALPAL_NO_CLIPBOARD"


def _copy_to_clipboard(text: str) -> bool:
    """Try to copy `text` to the system clipboard. Return True on success.

    Resolves the per-OS clipboard tool via `shutil.which()` so we never shell
    out to a name on PATH that some other tool put there. macOS ships pbcopy
    by default; Linux needs xclip or wl-copy installed; Windows ships clip.
    Anything that fails or isn't available silently returns False — clipboard
    is a convenience, not a required step.
    """
    if os.environ.get(ENV_NO_CLIPBOARD):
        return False
    if sys.platform == "darwin":
        candidates = [["pbcopy"]]
    elif sys.platform == "win32":
        candidates = [["clip"]]
    else:
        # Wayland first (it's the future on most modern Linux distros), then X11.
        candidates = [["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]
    for cmd in candidates:
        path = shutil.which(cmd[0])
        if not path:
            continue
        try:
            # `text=True` + `input=` writes the code on stdin; the tools all
            # treat stdin as the clipboard payload. 2-second timeout so a
            # broken clipboard daemon doesn't stall the auth flow.
            subprocess.run([path, *cmd[1:]], input=text, text=True, check=True, timeout=2)  # noqa: S603
        except (subprocess.SubprocessError, OSError):
            continue
        else:
            return True
    return False


def _open_browser(url: str) -> bool:
    """Try to open `url` in the user's default browser. Return True on success.

    Skipped automatically when the session looks remote: SSH_CONNECTION /
    SSH_TTY indicate an SSH login (opening a browser on the remote host
    would either fail or pop a window the user can't see). The
    GALPAL_NO_BROWSER override exists for cases the SSH heuristic misses
    (tmux on a headless workstation, container with X11 forwarded but no
    desktop, etc.).
    """
    if os.environ.get(ENV_NO_BROWSER):
        return False
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return False
    try:
        # `new=2` requests a new tab in the existing browser window when
        # supported. Returns False on failure rather than raising — but we
        # also catch the rare exception path that webbrowser exposes.
        return webbrowser.open(url, new=2)
    except webbrowser.Error:
        return False


def _print_device_flow_prompt(flow: dict) -> None:
    """Pretty-print the device-code prompt + best-effort clipboard / browser helpers.

    MSAL's stock `flow["message"]` is one long line; pulling out the URL, code,
    and expires-in onto separate lines makes them harder to lose to terminal-
    width wrapping. We additionally try to copy the user code to the clipboard
    and open the verification URL in the default browser — both no-ops if the
    helpers aren't available — and tell the user which side conveniences
    succeeded so they know what's already done for them.
    """
    code = flow.get("user_code", "?")
    url = flow.get("verification_uri", "https://microsoft.com/devicelogin")
    expires = flow.get("expires_in")
    when = ""
    if isinstance(expires, int):
        # Local-time clock is what the user reads off their wall — use the
        # tz-aware now() so ruff DTZ005 stops nagging, then format in local time.
        deadline = _dt.datetime.now(_dt.UTC).astimezone() + _dt.timedelta(seconds=expires)
        when = f"  (expires at {deadline:%H:%M:%S})"

    copied = _copy_to_clipboard(code) if code != "?" else False
    opened = _open_browser(url)

    # Tag each side note so the user knows what succeeded; empty string if it
    # didn't, so a failure on either side doesn't lie about what happened.
    code_note = "  (copied to clipboard — paste at the prompt)" if copied else ""
    url_note = "  (opened in your browser)" if opened else ""

    # Stderr, not stdout: a user running `galpal --json pull | jq` shouldn't
    # have the auth prompt corrupt the JSON stream. Auth fires before the
    # reporter exists so we can't route through `Reporter.warning`; the
    # closest stable stream is sys.stderr, which jq consumers ignore.
    print(
        f"\nSign-in required.\n  Open: {url}{url_note}\n  Code: {code}{when}{code_note}\n",
        file=sys.stderr,
        flush=True,
    )


def get_token(client_id: str) -> str:
    """Acquire a Graph access token, prompting for device-code login if the cache is empty."""
    cache = msal.SerializableTokenCache()
    _read_token_cache(cache)
    app = msal.PublicClientApplication(client_id, authority=AUTHORITY, token_cache=cache)

    result = None
    # acquire_token_silent only returns a token when the cache holds one for this
    # exact client id, so switching --client-id transparently forces a re-login.
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
    if not result:
        # Device-code flow is interactive — refuse on non-TTY stdin so a cron
        # entry doesn't silently hang for 15 minutes. Override with the env
        # var when you genuinely need to pre-seed via stdin (rare; the user
        # has to copy the code out of stdout themselves anyway).
        if not sys.stdin.isatty() and not os.environ.get("GALPAL_FORCE_DEVICE_CODE"):
            msg = (
                "Refusing device-code login on non-TTY stdin (cron / pipe / "
                "captured stdout). Run interactively first to seed "
                f"{TOKEN_CACHE}, or set GALPAL_FORCE_DEVICE_CODE=1 to override."
            )
            raise DeviceFlowError(msg)
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            msg = f"Could not start device flow: {flow}"
            raise DeviceFlowError(msg)
        _print_device_flow_prompt(flow)
        # KeyboardInterrupt during the polling loop bubbles to the caller —
        # the CLI's main() turns it into a clean "cancelled" sys.exit. We
        # don't wrap it as an AuthError because Ctrl-C isn't an auth failure,
        # it's the user changing their mind, and programmatic callers may
        # want to treat the two distinctly.
        result = app.acquire_token_by_device_flow(flow)

    if cache.has_state_changed:
        _atomic_write_secret(TOKEN_CACHE, cache.serialize())
    if "access_token" not in result:
        msg = f"Auth failed: {result.get('error_description', result)}"
        raise TokenAcquisitionError(msg)
    return result["access_token"]
