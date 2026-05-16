# Changelog

All notable changes to galpal are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [SemVer](https://semver.org/spec/v2.0.0.html).

## [1.1.0] — row-streaming GET

- **ijson-based row-streaming for `graph_paged`.** `r.json()` previously materialized a whole page (`CONTACTS_PAGE_SIZE × max-per-contact-payload`) into RAM during page parse — the last remaining OOM ceiling for users with megabyte-sized `personalNotes` or large attachments. The GET path now reads the response body via `requests` `stream=True` and parses incrementally with `ijson`, yielding each `value[*]` element as soon as its closing `}` is seen on the wire. Peak per-page memory drops from `O(page_size × max_payload)` to `O(max_payload)`.
- **Forward-pass `@odata.nextLink` capture.** Switching from `r.json()` to a forward-pass parser meant `value` and `@odata.nextLink` can no longer be read in arbitrary order. Implementation uses `ijson.parse` (SAX-style events) with a per-row `ObjectBuilder`, so both keys are captured in the same single pass regardless of which Graph endpoint puts which first. Regression test (`test_graph_paged_handles_next_link_before_value`) drives the inverted order explicitly.
- **Connection-lifetime hygiene.** `graph_paged` wraps each page in `try/finally: r.close()` so an early generator close (caller `break`s out of the loop) releases the underlying urllib3 connection deterministically rather than waiting on GC. `_retrying_request` also closes the discarded response between transient-5xx retries — harmless on the default non-streamed path, required on `stream=True` GETs.
- **Test-fixture updates.** `FakeResponse` grows `iter_content(chunk_size=...)` (serializes its stored dict on demand) and a no-op `close()`; `FakeGraph.get` accepts the new `stream=` kwarg. No behavior change for the simple-response path — the batch endpoint still consumes `r.json()` and never went through `iter_content`.

## [1.0.0] — initial release

A single-user CLI that mirrors a Microsoft 365 Global Address List into personal Outlook contacts via Microsoft Graph. Idempotent re-runs via Azure-id extended-property stamping; preserves user-added fields; first-run adoption of email-matched pre-existing contacts. **219 tests pass; lint and format clean.**

### Subcommands

`pull`, `audit`, `dedupe`, `prune`, `delete`, `list-folders`, `remove-folder`, `remove-category`. Every destructive op defaults to dry-run; `--apply` requires a typed scope-bound confirmation (`DELETE 47 PRUNE`, etc.).

### Output modes

Human-readable (default), `--json` (ndjson for cron / pipelines, with control-character sanitization recursively applied to every payload string — including `bytes`, `set`, `Path`, `datetime`, and other non-JSON-native types), `--quiet` (errors + summary only). The `Reporter.entry(kind, **fields)` shape uses a registry of TTY formatters (`register_tty_formatter`) so new event kinds add a single formatter rather than extending an `if`-ladder inside `TTYReporter`.

### Authentication

Device-code login via MSAL using Microsoft's first-party public clients — no app registration. Verification URL auto-opens in the default browser; user code auto-copies to the clipboard (best-effort). UUID-validated `--client-id` with allowlist + `GALPAL_ALLOW_UNKNOWN_CLIENT_ID` opt-in (illicit-consent-grant defense). Auth failures raise typed `AuthError` subclasses (`InvalidClientIdError` / `DeviceFlowError` / `TokenAcquisitionError`); the CLI catches and translates to identical exit messages, so programmatic callers can consume `_galpal.auth` without process-exit side effects.

Token cache lives at the per-user OS path (XDG / Application Support / `%APPDATA%`, with cygwin/MSYS sharing the Windows path so PowerShell + Git-Bash on the same machine share one cache). Atomic 0600 writes from creation, parent dir 0700 enforced even on existing directories (POSIX), `O_NOFOLLOW` on read. Windows mode bits map onto a coarse subset of NTFS ACLs — README documents the caveat. `LEGACY_TOKEN_CACHE` only resolves for the dev-shim layout (no surprise `site-packages/.token_cache.json` for pipx installs).

### Resilience

Transparent retries for 429 (with `Retry-After`) and 5xx / ConnectionError / Timeout (exponential backoff with jitter; `_retrying_request` raises `HTTPError` consistently on the final 5xx attempt instead of returning the response). `Retry-After` parsing tolerates HTTP-date / float / negative / huge / garbage values with a hard upper bound (default 300s, matching Microsoft's documented tenant-wide throttle guidance).

`$batch` fan-out has three independent retry budgets (per-subrequest 429, outer-batch envelope 429, transient 5xx); sub-requests Graph drops from the response array get synthetic 500 entries so the returned list always aligns with the input. `graph_paged` enforces a per-page 429 cap and refuses `@odata.nextLink` URLs that don't point at `graph.microsoft.com` (defense-in-depth against bearer-token leak).

Structured retry / throttle / synthesis events route through `logging.getLogger("galpal.graph")`; `cli.py` wires them through the active reporter so a cron operator running `--json` sees them as ndjson `info` / `warning` events.

### Memory

`run_dedupe` is single-pass streaming: each row's full payload lives only for the duration of one loop iteration, after which Python's GC reclaims it. Persistent state is bounded by `N × small constants` (id strings + email strings + small per-contact metadata tuples) regardless of how big any individual contact's `personalNotes` / photo / etc. is. The remaining ceiling is one page's worth of contacts during `r.json()` page-parse, bounded by `CONTACTS_PAGE_SIZE` (env-tunable) × max-per-contact-payload — addressed in 1.1.0 via ijson row-streaming.

`run_pull` spools the filtered GAL stream to a JSONL temp file (auto-deleted on close) so memory stays bounded on big tenants. `--scratch-dir` / `GALPAL_SCRATCH_DIR` redirects the spool when `/tmp` is small; defense-in-depth warning if the path isn't owned by the running user.

### Hardened

Atomic 0600 token cache writes (with `os.fchmod` failure cleanup that closes the temp fd before re-raising); UUID-validated client-id; ANSI/OSC sanitization on every Graph-controlled string before printing (TTY *and* JSON); Unicode bidi/format chars (LRM / RLM / RLO / PDI / BOM / etc.) stripped so a hostile `displayName` can't visually rearrange a `dedupe`/`prune` preview row; scope-bound destructive confirmations (`DELETE N PRUNE` / `UNSTAMPED` / `ALL` / `DEDUPE`) with TTY guard; non-TTY destructive ops refused unless `GALPAL_FORCE_NONINTERACTIVE=1`; bootstrap preflight (Python version, broken-venv detection, missing-requirements, git-work-tree check before configuring hooks).

### Architecture

- `_galpal/_term.py` owns the presentation primitives (`safe_for_terminal`, `confirm_destructive`); `reporter.py` consumes them, every `commands/*.py` consumes the reporter — no circular imports.
- `FilterConfig` (frozen dataclass) carries the five data filters plus `live_user_ids` for `--orphans`; `prune` builds the config late after fetching `gal_ids` via `dataclasses.replace`. Adding a sixth filter is a one-place edit.
- `chunked_batch` accepts optional `tags` + `on_response(tag, resp) -> (ok, error_kwargs)` callback, so commands with per-row context (`pull.flush_batch`, `categories.py`'s PATCH+DELETE loops) collapse onto the same helper.
- `gal_already_pulled` tolerates empty-string / whitespace-only entries in `businessPhones` (no needless PATCH on `["+1-555", ""]` GAL rows) and returns `False` cleanly when the GAL row has no email at all (rather than relying on `fetch_gal`'s upstream invariant).

### Install

End users: `pipx install git+https://github.com/mrtomatwo/gal-pal.git@1.0.0` puts `galpal` on `PATH`. Devs: `git clone … && python dev_galpal.py init` creates a project-local `.venv/` with runtime + dev deps and configures the pre-commit hook.

[1.0.0]: https://github.com/mrtomatwo/gal-pal/releases/tag/1.0.0
