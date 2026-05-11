# galpal

Mirror your company's **G**lobal **A**ddress **L**ist into your personal Outlook contacts, one-way and idempotent. After the first run, re-runs are fast — only changed entries are touched.

## What it does

- Authenticates against Microsoft Graph via device-code login (no app registration needed — uses Microsoft's first-party clients).
- Reads the GAL via `/users` and writes personal contacts via `/me/contacts`.
- Stamps each pulled contact with an Azure AD object id in a Graph extended property, so re-runs match the same person across pulls regardless of what you renamed them to in Outlook.
- Preserves any user-added fields (home phones, personal notes, birthday, categories, …) — only the GAL-sourced fields are overwritten.
- Adopts pre-existing contacts that match a GAL entry by email and stamps them on first run, so it doesn't duplicate people you already had.

## Install

galpal needs Python 3.11+ and a Microsoft 365 work account.

```sh
pipx install git+https://github.com/mrtomatwo/gal-pal.git@1.0.0
galpal --help
```

(When/if galpal lands on PyPI, this becomes `pipx install galpal` and you'll be able to use `pipx upgrade galpal` for updates.) Pipx puts the CLI on your PATH inside an isolated venv — no virtual-env activation, no `pip install` in your global Python, no clash with other tools' dep versions.

### Updating

```sh
pipx install --force git+https://github.com/mrtomatwo/gal-pal.git@<newtag>
```

Once on PyPI: `pipx upgrade galpal`.

### Uninstalling

```sh
pipx uninstall galpal
```

### Developing on galpal (clone path)

```sh
git clone git@github.com:mrtomatwo/gal-pal.git
cd gal-pal
python dev_galpal.py init           # creates .venv/, installs deps + dev tools, sets up the pre-commit hook
.venv/bin/python dev_galpal.py --help
```

`dev_galpal.py` is the development shim — every `python dev_galpal.py …` invocation runs against your live source tree, so edits in `_galpal/` are picked up immediately with no reinstall. `init` always installs both `requirements.txt` (runtime) and `requirements-dev.txt` (ruff, pyright, pytest), then enables the pre-commit hook. Pass `--upgrade` to refresh inside the existing venv.

Both install paths use the same dependency floors and ceilings (`msal>=1.36,<2`, `requests>=2.33,<3`, `tqdm>=4.67,<5`). Trust model: PyPI's TLS plus the committed version ceilings — major-version bumps need an explicit pyproject change.

## Usage

### First run — authenticate and dry-run

```sh
galpal pull --dry-run
```

A device-code URL appears, the URL opens in your default browser, and the 9-character code is copied to your clipboard — paste it at the Microsoft prompt, sign in, come back. (Microsoft's identity platform doesn't support prefill URLs, so a paste is the best the OAuth 2.0 device-code flow can do.) Subsequent runs reuse the cached refresh token from the path in [Authentication](#authentication).

The dry run prints what would change without writing anything. Once you're happy:

```sh
galpal pull
```

`pull` is the only subcommand that writes to your contacts without an explicit `--apply` flag — its dry-run is opt-in (`--dry-run`) because the steady-state operation is "sync"; the destructive ops below default to dry-run because the steady-state operation is "no-op".

### Subcommands

| Command | What it does |
|---|---|
| `pull` | Pull GAL entries into your personal contacts (the default day-to-day op). |
| `audit` | Read-only: report duplicates and quality issues in the GAL. |
| `dedupe` | Group personal contacts by shared emails, keep the one with most user-added data, propose deleting the rest. |
| `prune` | Delete previously-pulled contacts that fail any active filter. `--orphans` adds "the GAL source must still exist" as a criterion. Only touches contacts galpal stamped — manual additions are safe. |
| `delete` | DESTRUCTIVE: delete every contact NOT stamped by galpal (manual additions, vendors, etc.), or with `--all`, every contact regardless of stamp. |
| `list-folders` | List your personal contact folders with counts. Outlook groups them under "Kategorien" in the sidebar, which is easy to confuse with the Outlook categories feature. |
| `remove-folder` | Delete contact folders by name. (A name can be both a folder and a category — running `remove-folder` only deletes the folder side; use `remove-category` for the category side.) |
| `remove-category` | Strip categories from contacts and delete the master entries from the sidebar. |

Every destructive op defaults to dry-run. `--apply` does the actual write; `prune --apply`, `delete --apply`, and `dedupe --apply` additionally require typing the literal phrase `DELETE <count> <SCOPE>` (where SCOPE is `PRUNE`, `ALL`, `UNSTAMPED`, or `DEDUPE` depending on the subcommand) — binding the confirmation to the count *and* the scope means a typo can't accidentally take a more drastic path on the same number. Deleted items go to Outlook's Deleted Items folder and are recoverable subject to your tenant's retention (typically 14-30 days).

`galpal -v` (or `--version`) prints the installed version. Both work without third-party dependencies installed — handy on a fresh clone via `python dev_galpal.py -v`.

### Common filter flags (shared by `pull`, `audit`, `prune`)

```
--exclude REGEX                       skip entries whose displayName matches (repeatable)
--require-email/--no-require-email    [default: on] require a mail field
--require-full-name                   require both givenName and surname
--require-phone                       require business or mobile phone
--require-comma                       require a comma in displayName (filters service accounts)
```

### Pull-specific flags

```
--dry-run                             show what would change without writing
--limit N                             stop after N GAL entries (for testing on big tenants)
--batch-size N                        contacts per Graph $batch request (max 20; default 20). Use 1 to disable batching.
--scratch-dir DIR                     directory for the JSONL spool tempfile (defaults to system temp)
```

Examples:

```sh
galpal pull --require-comma --require-phone
galpal pull --limit 50 --dry-run                    # quick smoke test on a giant tenant
galpal audit --exclude '^(svc|test|admin)-'
galpal prune --orphans --apply
```

### Output modes

Three top-level output modes, picked once and applied to every subcommand:

- **(default) human-readable** — formatted progress bars, previews, summary line.
- `--json` — one JSON object per event to stdout (ndjson). Cron- and pipeline-friendly. Pipe through `jq -c 'select(.kind == "summary")'` to grab the final stats. Refuses interactive confirmation by default — for scripted destructive runs use `GALPAL_FORCE_NONINTERACTIVE=1` and pipe the right `DELETE N <SCOPE>` token through stdin.
- `--quiet` / `-q` — drops informational chatter and per-row entries; keeps warnings, errors, and the summary line. For "exit-code is what I care about" runs.

### Exit codes

`galpal` differentiates exit codes so cron / CI wrappers can branch on them:

| Code | Meaning |
|---|---|
| 0 | Clean run. No errors. |
| 1 | Preflight or argparse failure (missing required args, bad client id, refused TTY guard, etc.). The error message tells you why. |
| 2 | The run completed but at least one Graph sub-request returned an error. The summary line shows `errors=N` and `--json` mode emits one `subrequest.error` event per failure. |

## How matching works

galpal stamps each pulled contact with the Azure AD user's object id via a single-value extended property. On re-run:

1. **Stamped match (fast path):** look up by Azure id → if data unchanged, skip; else PATCH the GAL fields.
2. **Email match (first-run adoption):** an existing contact whose email matches a GAL entry but lacks the stamp gets adopted: PATCH'd with GAL data + the new stamp.
3. **No match:** CREATE a new contact, stamped.

The stamp survives even if you rename the contact, change their phone, or move it between folders. (galpal currently only sees contacts in the **default folder** — see [Limitations](#limitations).)

When two of your existing contacts share an email (a state `dedupe` is meant to fix), `pull` warns you and stamps an arbitrary one — Graph's `/me/contacts` ordering is not stable across runs, so without the warning the stamp would silently wander between duplicates.

## Authentication

galpal uses Microsoft's first-party public clients via MSAL device-code flow — no app registration in your tenant. Default is the Office desktop client (`d3590ed6-…`), which is universally pre-authorized for Contacts.ReadWrite. If your tenant blocks it, override with `--client-id azure-cli` (or `vs`, `azure-ps`, `graph-cli`, or a raw GUID) or set `GALPAL_CLIENT_ID`.

Required Graph scopes: `User.ReadBasic.All` and `Contacts.ReadWrite` (delegated). The refresh token is cached per-user at:

- macOS: `~/Library/Application Support/galpal/token_cache.json`
- Windows: `%APPDATA%/galpal/token_cache.json`
- everything else (Linux, BSD, etc.): `$XDG_DATA_HOME/galpal/token_cache.json` (defaults to `~/.local/share/galpal/`)

The cache is written at mode 0600 from creation (atomic replace, `O_NOFOLLOW` on read) and the parent directory at mode 0700 — both enforced even on existing directories on POSIX. **On Windows these mode bits are largely cosmetic**: NTFS ACLs inherit from the parent directory and `os.chmod` maps onto a coarse subset of the inherited ACL. If you share a Windows account or profile directory with another user, treat the cache as readable by them (and consider running galpal under a dedicated, isolated profile). Override the path with `GALPAL_TOKEN_CACHE_PATH=/some/path`. Caches at the older project-root location (`.token_cache.json` next to `dev_galpal.py` in a clone) are migrated automatically on first run; pipx installs never had a legacy cache and skip the migration entirely.

### Threat model

The dangerous shape galpal defends against is **illicit consent grant**: an attacker who can plant an env var or dotfile (`GALPAL_CLIENT_ID`, `.envrc`, CI matrix) substitutes their own AAD app GUID. The user goes through a real `microsoft.com/devicelogin` URL, sees only a benign-looking app name, and consents — handing the attacker's app a delegated Graph token until they revoke it in Entra. Defenses:

1. `--client-id` / `GALPAL_CLIENT_ID` accepts an alias from a small allowlist (`office`, `vs`, `azure-cli`, `azure-ps`, `graph-cli`) or a UUID. Anything else is rejected.
2. UUIDs not in the allowlist of vetted Microsoft public-client GUIDs are also rejected — unless `GALPAL_ALLOW_UNKNOWN_CLIENT_ID=1` is set. The opt-in is loud; the warning is the point.
3. Every Graph-controlled string (display names, error bodies, folder names) is sanitized of ANSI/OSC control sequences before printing. A hostile or careless directory `displayName` containing OSC-52 can't hijack your clipboard, fake a clickable hyperlink, or clear your scrollback.
4. The token cache is locked-down at write time, not via post-hoc chmod (no umask race).

## Environment variables

All `GALPAL_*` env vars in one place:

| Variable | Default | Effect |
|---|---|---|
| `GALPAL_CLIENT_ID` | (unset) | Override the AAD client id. Same rules as `--client-id` (alias or UUID; UUID needs the allowlist override below). |
| `GALPAL_ALLOW_UNKNOWN_CLIENT_ID` | (unset) | Set to `1` to accept a UUID not in the vetted-Microsoft allowlist. **Read [Threat model](#threat-model) before setting.** |
| `GALPAL_TOKEN_CACHE_PATH` | (per-OS XDG path) | Override the token-cache file location. |
| `GALPAL_NO_BROWSER` | (unset) | Set to `1` to suppress auto-opening the device-code URL in a browser. SSH sessions are auto-detected; this is the override for cases the SSH heuristic misses. |
| `GALPAL_NO_CLIPBOARD` | (unset) | Set to `1` to suppress auto-copying the user code to the clipboard. |
| `GALPAL_FORCE_DEVICE_CODE` | (unset) | Set to `1` to allow the device-code flow on non-TTY stdin (cron, CI). Off by default — cron entries shouldn't silently hang for 15 minutes. |
| `GALPAL_FORCE_NONINTERACTIVE` | (unset) | Set to `1` to bypass the destructive-op TTY guard. The `DELETE N <SCOPE>` confirmation token still has to come from somewhere (e.g. piped stdin). |
| `GALPAL_REPORTER` | `tty` | One of `tty` / `json` / `quiet`. Equivalent to passing `--json` / `--quiet` on every invocation. |
| `GALPAL_SCRATCH_DIR` | (system temp) | Directory for the pull spool tempfile. Useful when `/tmp` is small. Same effect as `--scratch-dir`. |
| `GALPAL_HTTP_TIMEOUT_S` | `120` | Per-HTTP-request timeout. |
| `GALPAL_MAX_RETRY_AFTER_S` | `60` | Cap on `Retry-After` parsing — a hostile or buggy header value can't freeze the script for years. |
| `GALPAL_MAX_TRANSIENT_RETRIES` | `5` | Retry budget for 5xx / ConnectionError / Timeout (per HTTP call). |
| `GALPAL_MAX_BATCH_429_RETRIES` | `8` | Retry budget for 429s, applied both per-subrequest inside a `$batch` and to the whole batch envelope. |
| `GALPAL_GAL_PAGE_SIZE` | `200` | `$top` for the GAL `/users` fetch. |
| `GALPAL_CONTACTS_PAGE_SIZE` | `100` | `$top` for `/me/contacts` fetches. |

## Limitations

This is a personal hobby tool. Things to know before you trust it:

- **Default folder only.** `pull`, `prune`, `delete`, `dedupe` only see contacts in the Outlook default folder. Contacts in subfolders are invisible to those subcommands. `remove-category` walks every top-level contact folder, but **does not** recurse into nested subfolders — categories assigned only inside a nested folder are missed silently.
- **Single-user, interactive.** Designed for "I run this from my laptop." Destructive subcommands refuse non-TTY stdin unless `GALPAL_FORCE_NONINTERACTIVE=1`.
- **TOCTOU on `delete --all`.** Contacts added by another Outlook client between the enumeration and the apply will survive the run. The confirmation prompt says so explicitly.
- **German label in `list-folders` / `remove-folder` help text.** "Kategorien" is the new Outlook web UI's German label for the contact-folders sidebar group. The commands themselves are locale-agnostic; only the help-text hint is German.

## Development

```sh
python dev_galpal.py init           # venv + runtime + dev deps + pre-commit hook
.venv/bin/python -m pytest -q       # 199 tests, ~1s
```

Project structure:

```
dev_galpal.py              # development shim: docstring + `init` bootstrap + delegate
_galpal/                   # the package itself (this is what ships in the wheel)
  auth.py                  # MSAL device-code, XDG token cache, clipboard/browser helpers
  graph.py                 # graph_paged, send_batch, retry layer, EP_AZURE_ID, GAL_USER_FIELDS
  model.py                 # pure data: gal_to_payload, merge_emails, gal_already_pulled, …
  filters.py               # FilterConfig + GAL/contact predicates
  reporter.py              # Reporter Protocol + TTY/JSON/Quiet/Recording impls
  cli.py                   # argparse + dispatch — entry point for the `galpal` console script
  commands/
    pull.py audit.py dedupe.py prune.py delete.py folders.py categories.py
tests/
  conftest.py              # FakeGraph in-memory simulator + fixtures
  test_unit.py             # pure model layer (39 tests)
  test_e2e.py              # main() driven through FakeGraph (51 tests)
  test_auth.py             # MSAL stub + cache + clipboard/browser helpers (40 tests)
  test_bootstrap.py        # dev_galpal.py shim and init bootstrap (12 tests)
  test_reporter.py         # Reporter implementations + JSON sanitization (20 tests)
  test_resilience.py       # retry layer + send_batch hardening (37 tests)
```

The pre-commit hook (`.githooks/pre-commit`) runs ruff (format + check), pyright, the full test suite, and gitleaks. `dev_galpal.py init` configures it via `git config core.hooksPath .githooks`.

Release notes for tagged versions live in [CHANGELOG.md](CHANGELOG.md).

## License

MIT — see [LICENSE](LICENSE).
