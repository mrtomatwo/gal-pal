# Orientation

galpal is a single-user CLI that mirrors a Microsoft 365 Global Address List
(GAL) into personal Outlook contacts via Microsoft Graph. Idempotent re-runs
via Azure-id extended-property stamping; preserves user-added fields;
adopts pre-existing email-matched contacts on first run.

## Entry points

- **End-user install:** `pipx install git+https://github.com/mrtomatwo/gal-pal.git@<tag>` → puts `galpal` on `PATH` (script defined in [pyproject.toml:52-53](../pyproject.toml#L52)).
- **Dev:** `python dev_galpal.py init` creates `.venv/` + installs deps + configures the pre-commit hook. After init, run `python dev_galpal.py …` or activate `.venv/` and run `galpal …`. Both paths land in `_galpal.cli.main` ([dev_galpal.py](../dev_galpal.py), [_galpal/cli.py](../_galpal/cli.py)).
- **`dev_galpal.py` is NOT in the wheel** — it's a dev shim only. The wheel ships `_galpal/` and `_galpal/commands/`.

## Top-level layout

```
_galpal/
├── _term.py            ← terminal primitives (ANSI/OSC sanitization, confirm_destructive)
├── _version.py         ← single source of truth for version string
├── auth.py             ← MSAL device-code login + token cache; typed AuthError subclasses
├── cli.py              ← argparse subcommand dispatcher → commands/*
├── commands/           ← one file per subcommand (run_pull, run_audit, run_dedupe, …)
├── filters.py          ← FilterConfig (frozen dataclass) + contact/user filter predicates
├── graph.py            ← HTTP layer: pagination (ijson), $batch fan-out, retries
├── model.py            ← scoring / GAL-row to contact conversion / extended-property helpers
└── reporter.py         ← Reporter Protocol + TTY/JSON/Quiet/Recording impls + entry registry

tests/
├── conftest.py         ← FakeGraph + FakeResponse + factories + fixtures
├── test_auth.py        ← MSAL flow + token-cache atomicity + path resolution
├── test_bootstrap.py   ← dev_galpal.py init preflight
├── test_e2e.py         ← end-to-end CLI flows (pull / dedupe / prune / …)
├── test_reporter.py    ← Reporter Protocol contract + per-mode rendering
├── test_resilience.py  ← retry budgets, $batch synthesis, @odata.nextLink shapes
└── test_unit.py        ← filters, scoring, model helpers, per-piece units

CHANGELOG.md            ← Keep-a-Changelog style, per-version sections
```

## Subcommands

`pull` `audit` `dedupe` `prune` `delete` `list-folders` `remove-folder` `remove-category`.
Every destructive op defaults to dry-run; `--apply` requires a typed scope-bound
confirmation (e.g. `DELETE 47 PRUNE`, `DEDUPE`, `UNSTAMPED`, `ALL`). Non-TTY
destructive ops refuse to run unless `GALPAL_FORCE_NONINTERACTIVE=1`.

## Output modes

- **Human-readable** (default).
- **`--json`** — ndjson; control-character sanitization applies recursively to every payload string.
- **`--quiet`** — errors + summary only.

`Reporter.entry(kind, **fields)` uses a TTY-formatter registry
(`register_tty_formatter`) so new event kinds add a single formatter rather
than extending an `if`-ladder.

## Dependencies

- `msal` (auth), `requests` (HTTP), `tqdm` (progress), `ijson` (row-streaming).
- Python 3.11+. Type-checked with pyright (`basic` mode). Linted with ruff
  (`select = ["ALL"]` with pragmatic per-file ignores; see [pyproject.toml:80-131](../pyproject.toml#L80)).

## Where to start reading

1. [_galpal/cli.py](../_galpal/cli.py) — argparse dispatch table, top-level flow.
2. [_galpal/graph.py](../_galpal/graph.py) — `graph_paged`, `chunked_batch`, `send_batch` (every command goes through these).
3. [_galpal/commands/pull.py](../_galpal/commands/pull.py) — the most complex command; touches every other module.
4. [tests/conftest.py](../tests/conftest.py) — the fake-Graph harness; understanding it unblocks every other test file.
