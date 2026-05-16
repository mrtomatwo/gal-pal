# Gotchas

Append-only log of pitfalls. Each entry: context (what you were doing),
resolution (what to do instead), reference (file:line). Don't restructure;
append at the bottom. Newest at the bottom.

---

## 2026-05-11 — `ijson.parse` C backend rejects raw bytes iterators

**Context:** First pass at the 1.1.0 row-streaming refactor passed
`r.iter_content(chunk_size=…)` directly to `ijson.parse(...)`. Tests
failed with
`ValueError: too many values to unpack (expected 2, got 13)` —
ijson's C backend (yajl2_c) was iterating each yielded bytes object as
event tuples.

**Resolution:** Wrap the iterator in a tiny `.read(n)` adapter
(`_IterReader` in [_galpal/graph.py](../_galpal/graph.py)). The pure-Python
backend accepts iterables but is silently order-of-magnitude slower; the
adapter exists specifically to keep the fast C backend on the happy path.

**Reference:** [_galpal/graph.py — `_IterReader`](../_galpal/graph.py).

---

## 2026-05-11 — Pre-commit hook failed → don't `git commit --amend`

**Context:** Re-committing after fixing pyright errors. Pre-commit
gitleaks/ruff/pyright runs BEFORE the commit object is created — a
failure means no commit happened.

**Resolution:** Fix, re-stage, run `git commit` again (new commit). Never
`--amend` after a failed pre-commit hook — it would amend the PREVIOUS
commit, clobbering its message / contents.

**Reference:** [.githooks/pre-commit](../.githooks/pre-commit) configured by
`python dev_galpal.py init`.

---

## 2026-05-11 — Force-push to `main` is sandbox-blocked

**Context:** Releasing 1.0.0 required force-pushing a squashed commit to
remote `main`. The Bash sandbox refused even with explicit session
authorization, because "force-push to default branch" requires a
settings-level permission rule, not a session-level one.

**Resolution:** Either add a Bash permission rule for `git push
--force-with-lease origin main` to `~/.claude/settings.json`, or run the
push yourself outside the agent. Don't try to work around the denial by
piping through other tools.

**Reference:** See [architecture/release-process.md](architecture/release-process.md).

---

## 2026-05-11 — `push.followTags=true` ships tags with commit pushes

**Context:** Drafted the 1.0.0 GitHub release and deleted the remote tag,
then force-pushed `main`. The tag re-appeared on the remote because
`push.followTags=true` is set in the repo's git config — `git push
<branch>` quietly pushes any local annotated tag that points at a
ref in the pushed history.

**Resolution:** After the commit push, `git push origin :refs/tags/<tag>`
to delete the tag from remote again. When holding a tagged release until a
target time, expect to repeat the tag-delete step after any subsequent
push.

**Reference:** See "Holding a release until a target time" in
[architecture/release-process.md](architecture/release-process.md).

---

## 2026-05-11 — SSH signatures don't carry a signing timestamp

**Context:** Timing the 1.0.0 release for 12:34 Vienna, considered using
`libfaketime` to make the SSH signature's internal timestamp match.

**Resolution:** SSHSIG format (unlike GPG) does NOT embed a signing-time
field. The only public timestamp on a signed tag is the tagger date set
by `GIT_COMMITTER_DATE` at `git tag -s` time. `faketime` is unnecessary
for SSH-signed tags.

**Reference:** [architecture/release-process.md](architecture/release-process.md).

---

## 2026-05-11 — `ObjectBuilder.value` is not in ijson's type stubs

**Context:** pyright complained
`Cannot access attribute "value" for class "ObjectBuilder"` after
the ijson refactor.

**Resolution:** `.value` is set lazily inside `event()` once the root
`end_map` event fires — it's real at runtime but not in the stubs.
Suppress with `# pyright: ignore[reportAttributeAccessIssue]` on the one
access site. Don't refactor to `getattr` or `cast(Any, …)` — the ignore
comment localizes the type-stub gap.

**Reference:** [_galpal/graph.py — search for "ObjectBuilder sets `.value` lazily"](../_galpal/graph.py).
