# Testing patterns

220 tests; suite runs in ~0.3s. No network, no real Graph, no real MSAL.

## `FakeGraph` — in-memory Graph simulation

[tests/conftest.py](../../tests/conftest.py).

`FakeGraph` simulates the subset of Graph endpoints galpal touches:
`/users`, `/me/contacts`, `/me/contactFolders[/<id>/contacts][/$count]`,
`/me/outlook/masterCategories`, `/$batch`. State lives on the instance
(users / contacts / folders / master categories). Each request appends to
`graph.calls = [(method, url, params_or_body), …]` so tests can assert on
traffic shape ("exactly one $batch with 3 sub-requests", "page 1 carries
$select, page 2 doesn't").

Wired in via the `graph` fixture, which monkeypatches
`_galpal.graph.requests.get` and `.post`. **Always patch at the use site
(the module that owns the symbol), never on a re-import.** Re-imports
elsewhere don't see the monkeypatch.

### `_paged` honors `$top` and emits `@odata.nextLink`

Pages slice `items[start:start+top]`, set `@odata.nextLink` to a URL with
a `__cursor=N` query parameter, and `parse_qs` on that cursor in the next
call. This exercises `graph_paged`'s pagination loop and the "drop initial
params after the first call" rule.

### `queue_429(predicate, *, retry_after="0")`

Make the next matching request reply with 429. `retry_after` lets you
drive `_parse_retry_after`'s malformed / HTTP-date / negative / huge
paths.

## `FakeResponse`

Stand-in for `requests.Response` with just the surface galpal uses:
`status_code`, `json()`, `iter_content(chunk_size=…)`, `close()`,
`raise_for_status()`, `.text`, `.headers`.

`iter_content` serializes `json_data` on demand (single chunk for the
typical test body). `close()` is a no-op. Both exist so `stream=True` code
paths in `_galpal.graph` work against the fake without special-casing.

For tests that need a literal byte sequence (e.g. proving order-flipped
JSON keys parse correctly), subclass `FakeResponse` and override
`iter_content` to yield raw bytes:

```python
class ReorderedResponse(FakeResponse):
    def iter_content(self, chunk_size=65536):
        yield self._raw

r = ReorderedResponse(200, None)
r._raw = b'{"@odata.nextLink":"…","value":[…]}'
```

## `RecordingReporter`

[_galpal/reporter.py](../../_galpal/reporter.py).

Captures every `entry(kind, **fields)`, `info`, `warning`, `summary`, and
`confirm` call into structured fields. The `run_cli_recorded` fixture
returns `(exit_code, recorder)` — assert on `rec.summary_kwargs` and
`rec.events` rather than on stdout substrings. Cosmetic rendering changes
won't break assertions written this way.

Prefer `run_cli_recorded` over `run_cli`'s `(code, stdout, stderr)` tuple
for new tests. `test_reporter.py` has the cleanest examples.

## Factories: `make_user`, `make_contact`

`tests/conftest.py` exposes `make_user(uid="u1", …)` and
`make_contact(cid="c1", …)`. Many keyword args, all with sensible defaults
— pass only what the test asserts on, take the rest as-is. The factories
exist to keep test bodies focused on the property under test (filter
behavior, scoring, …) rather than on building Graph-shaped dicts by hand.

## Where each test file lives

| File | What it covers |
|------|----------------|
| [test_auth.py](../../tests/test_auth.py) | MSAL flow, token cache atomicity, XDG/AppData/Application Support path resolution, mode-bit enforcement, legacy-cache migration |
| [test_bootstrap.py](../../tests/test_bootstrap.py) | `dev_galpal.py init` preflight: Python version, broken-venv detection, missing-requirements, git-work-tree |
| [test_e2e.py](../../tests/test_e2e.py) | Full CLI flows: pull / dedupe / prune / delete / list-folders / remove-folder / remove-category, dry-run vs --apply, scope-bound confirmations |
| [test_reporter.py](../../tests/test_reporter.py) | Reporter Protocol contract, per-mode rendering (TTY/JSON/Quiet), entry-formatter registry |
| [test_resilience.py](../../tests/test_resilience.py) | Retry budgets, $batch synthesis, @odata.nextLink shapes, `Retry-After` parsing, GRAPH_HOSTS defense |
| [test_unit.py](../../tests/test_unit.py) | Filter predicates, `user_data_score`, model helpers, frozen-dataclass behaviors |

## Hooks: `GALPAL_FORCE_NONINTERACTIVE=1`

`run_cli` / `run_cli_recorded` set this so the destructive-confirmation
prompt doesn't refuse on captured stdin. Tests that drive `--apply` via
scripted stdin rely on this.
