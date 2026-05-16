# Module relationships

```
                           cli.py  (argparse dispatch)
                             │
       ┌─────────────────────┼─────────────────────────────┐
       │                     │                             │
       ▼                     ▼                             ▼
   auth.py            commands/*.py                    reporter.py
   (MSAL              (one per subcommand)             (Protocol +
   device-code,             │                          TTY/JSON/Quiet/
   token cache)             │                          Recording impls)
                            ▼
                       graph.py  ◄── filters.py (FilterConfig + predicates)
                       (HTTP layer:                  │
                       graph_paged,                  │
                       chunked_batch,                ▼
                       send_batch)               model.py
                            │                   (user_data_score,
                            ▼                   row→contact mapping,
                       _term.py                 EP_AZURE_ID helpers)
                       (ANSI sanitize,
                       confirm_destructive)
```

## Hard rules (enforced by tests, not just convention)

- **`_term.py` owns presentation primitives** (`safe_for_terminal`,
  `confirm_destructive`). `reporter.py` consumes them. Every
  `commands/*.py` consumes the reporter. **No circular imports.** Putting
  presentation in `reporter.py` directly would break — `_term.py` is shared
  by reporter and the destructive-confirm prompt path used by `cli.py`.

- **`auth.py` raises typed `AuthError` subclasses**
  (`InvalidClientIdError` / `DeviceFlowError` / `TokenAcquisitionError`,
  [_galpal/auth.py:58-77](../../_galpal/auth.py#L58)). The CLI catches and
  translates to exit messages — programmatic callers can consume
  `_galpal.auth` without process-exit side effects.

- **`FilterConfig` is a frozen dataclass** carrying five data filters plus
  `live_user_ids` for `--orphans`. Built late by `prune` (after fetching
  GAL ids) via `dataclasses.replace`. Adding a sixth filter is a
  one-place edit: add the field, add the predicate term in
  [filters.py](../../_galpal/filters.py), maybe add a CLI flag.

- **`chunked_batch` accepts optional `tags` + `on_response(tag, resp)
  -> (ok, error_kwargs)` callback** ([graph.py](../../_galpal/graph.py) —
  search for `def chunked_batch`). Commands with per-row context
  (`pull.flush_batch`, categories.py PATCH+DELETE loops) all collapse onto
  this single helper rather than hand-rolling chunk/send/zip/count loops.

- **`gal_already_pulled` tolerates empty / whitespace-only entries in
  `businessPhones`** — no needless PATCH on `["+1-555", ""]` GAL rows.
  Returns `False` cleanly when the GAL row has no email at all (doesn't
  rely on `fetch_gal`'s upstream invariant). See
  [_galpal/model.py](../../_galpal/model.py).

## What's intentionally *not* abstracted

- **`run_pull` / `run_audit` / `run_dedupe` / `run_prune` / `run_delete` /
  `run_remove_categories`** legitimately do many things in sequence
  (fetch, filter, render, confirm, mutate). Splitting them adds layers
  without clarifying the flow. Ruff complexity warnings are silenced for
  `commands/**` for this reason — see
  [pyproject.toml:98-103](../../pyproject.toml#L98).

- **`send_batch`'s complexity is the count of error shapes from one
  external system** (per-subrequest 429, outer envelope 429, transient
  5xx, partial response array, parse failure). All linear in the same
  loop. Don't split — silenced via
  [pyproject.toml:85-91](../../pyproject.toml#L85).

- **`main()` in cli.py** is mostly argparse subparser registration. Branch
  count tracks subcommand count, not concern count.
