# Streaming memory contract

galpal mirrors potentially-large GAL tenants into Outlook contacts on a
single user's machine. Peak RAM has to stay bounded regardless of mailbox
size. Two places enforce this — both have regression tests, neither is
allowed to silently degrade to list-materialization.

## `graph_paged` (HTTP layer)

[_galpal/graph.py](../../_galpal/graph.py) — search for `def graph_paged`.

- Uses `requests` `stream=True` + `ijson.parse(_IterReader(r.iter_content(...)))`
  to yield each `value[*]` row as its closing `}` arrives on the wire.
- The full page is **never** materialized into a Python dict. Peak per-page
  memory is one contact's payload plus ijson's small chunk buffer.
- `@odata.nextLink` is captured from the same single forward pass using
  `ijson.parse` SAX events with a per-row `ObjectBuilder` — Graph doesn't
  fix relative order of `value` vs `@odata.nextLink` in the response.
- Each page wrapped in `try/finally: r.close()` so an early generator close
  (caller `break`s out of the loop) releases the urllib3 connection
  deterministically.

### `_IterReader`

Tiny `.read(n)` adapter around `r.iter_content`. **Required** because
ijson's C backend (yajl2_c) only accepts file-likes via `.read(n)` — the
pure-Python backend accepts raw bytes iterators but is silently
order-of-magnitude slower. Don't replace `_IterReader(r.iter_content(...))`
with raw `r.iter_content(...)`; the C backend will throw
`ValueError: too many values to unpack` because it tries to interpret each
yielded bytes object as an event tuple.

### Regression tests

- [tests/test_resilience.py — `test_graph_paged_handles_next_link_before_value`](../../tests/test_resilience.py) drives an inverted-key-order JSON shape directly through `monkeypatch.setattr("_galpal.graph.requests.get", …)`. Catches a regression where someone refactors `graph_paged` back to reading `@odata.nextLink` only at end-of-stream.
- [tests/test_resilience.py — `test_graph_paged_follows_next_link`](../../tests/test_resilience.py) drives the normal multi-page flow through FakeGraph.

## `run_dedupe` (commands/dedupe.py)

[_galpal/commands/dedupe.py](../../_galpal/commands/dedupe.py).

- Single streaming pass over `graph_paged`. Each row's full contact dict is
  alive only for one loop iteration; after extracting metadata (display
  name, score, createdDateTime, lowered-email tuple), it goes out of scope
  and Python's GC reclaims it.
- Persistent state is bounded by `N × small constants` (id strings + email
  strings + small per-contact tuples) regardless of how big any
  individual contact's `personalNotes` / photo / etc. is.
- Union-Find with **path-halving + union-by-rank** for grouping by shared
  emails. Without rank, a long chain of email-sharing duplicates
  degenerates to O(n) per find and the inner pass becomes quadratic on
  real mailboxes.
- Winner selection is a **running max-by-key**, not a sort over full
  contacts. The sort happens over tiny metadata tuples (a few hundred
  bytes), never over kilobyte-sized contact dicts.

### Regression test

[tests/test_e2e.py — `test_dedupe_streams_one_contact_at_a_time`](../../tests/test_e2e.py) is weakref-based: every contact yielded by a stub `graph_paged` is wrapped in a `Tracked` subclass and tracked via `weakref.ref(c)`. After each yield + `gc.collect()`, the test counts how many tracked objects are still alive — peak `≤ 3` for streaming, would be `N=20` for any naive `list(graph_paged(...))` refactor.

The test design is subtle: a naive post-return check (after dedupe returns)
would pass for BOTH the streaming and list-materialized shapes, because
locals go out of scope at return. The check has to happen INSIDE the
generator after each yield, with a forced `gc.collect()`, to actually catch
the regression. If you ever refactor dedupe, run this test specifically —
it's the canary.

## Adding new commands that walk all contacts

Default to following the dedupe shape:
1. Iterate `graph_paged` directly.
2. Extract only the metadata you need per row.
3. Let the full row dict fall out of scope at the end of the iteration.

If you find yourself reaching for `list(graph_paged(...))`, stop and ask
whether you actually need random access, or whether you're about to leak
peak memory.
