# Release process

## Version sync — three places

When bumping, all three must agree:

1. [`_galpal/_version.py`](../../_galpal/_version.py) — `__version__ = "X.Y.Z"`
2. [`pyproject.toml`](../../pyproject.toml) — `version = "X.Y.Z"` under `[project]`
3. [`CHANGELOG.md`](../../CHANGELOG.md) — promote `## [Unreleased]` to `## [X.Y.Z] — <short title>`

A pre-commit check or unit test isn't yet enforcing this — easy regression. If
you add one, the right shape is comparing `_galpal._version.__version__` to
the parsed `[project].version` in `pyproject.toml`.

`_version.py` lives in its own module (rather than `__init__.py`) so the
wrapper script's `--version` path never has to execute `__init__.py`. If
anyone adds a third-party import to `_galpal/__init__.py`, `--version`
would otherwise break on a fresh clone before `init` has run.

## Signed tags via SSH

The repo uses SSH-signed git tags (not GPG). Config:

```
git config gpg.format ssh
git config user.signingkey ~/.ssh/id_ed25519.pub
```

Active signing key: `SHA256:GCCzJtaHVZV+K9u7MH/SGnNP2vaCVW8U+QSo0v+ejW0` (Mr. Tomato).

**SSHSIG signatures do NOT embed a signing-time field.** Unlike GPG, where
the signature payload includes a timestamp, the SSH signature format is
content-only. That means:

- The only public timestamp on a signed tag is the **tagger date** (set by
  `GIT_COMMITTER_DATE` env at `git tag -s` time).
- `libfaketime` / `faketime` is NOT required to align a signature timestamp
  to a target moment. Setting `GIT_COMMITTER_DATE` is sufficient.

To timestamp a release at a precise moment:

```
TARGET="2026-05-11T12:34:00+02:00"   # ISO with tz
GIT_AUTHOR_DATE="$TARGET" GIT_COMMITTER_DATE="$TARGET" \
  git commit --amend --no-edit --date "$TARGET"
GIT_COMMITTER_DATE="$TARGET" git tag -s X.Y.Z -m "galpal X.Y.Z" HEAD
```

Verify with `git tag -v X.Y.Z` (expect `Good "git" signature with ED25519 key
SHA256:GCCzJtaHVZV+…`; the `No principal matched.` line is benign — no
allowed-signers file is configured).

## Holding a release until a target time

GitHub release publication flow:

1. **Now (prep):**
   - `gh release edit X.Y.Z --draft=true` — drafts hide from the public
     Releases listing.
   - `git push origin :refs/tags/X.Y.Z` — delete remote tag so the Tags
     page doesn't expose the tag before launch.
   - Re-sign the local tag with the target timestamp (see above).
   - `git push --force-with-lease origin main` to publish the commit. The
     commit's `committed_at` / `authored_at` shown on GitHub will be the
     stamped target time.
2. **At target time:**
   - `git push origin X.Y.Z`
   - `gh release edit X.Y.Z --draft=false`

A `push.followTags=true` git config will auto-push the tag with the commit
push — undo with `git push origin :refs/tags/X.Y.Z` if you needed to hold
the tag.

**Force-push to default branch is sandbox-blocked** even with session
authorization. Settings rule (or running it yourself outside the agent) is
needed. See [gotchas.md](../gotchas.md).

## Commit-history shape: one commit per release

1.0.0 was a single squashed commit (`1aee6a9`) representing the full initial
release; review-cleanup history was discarded via the orphan-branch
technique. 1.1.0 added one commit (`96655c8`) on top. Future minor / patch
versions are expected to add small commit ranges per release, not separate
release branches.

## Pre-commit hook

`python dev_galpal.py init` configures
`.githooks/pre-commit` (gitleaks + ruff check + ruff format check + pyright).
Pre-commit failures **block the commit** — fix and re-stage, **don't
`--amend`** (the prior commit wasn't created, so amend would clobber HEAD).
Create a NEW commit after fixing.
