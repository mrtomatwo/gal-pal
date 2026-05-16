# `.agent-knowledge/` — galpal

Curated notes for coding agents (and humans skimming for context) working on
this repo. Designed to be read top-to-bottom by a fresh agent before touching
anything substantial. **Not** auto-generated documentation — every entry was
written down because it surprised someone.

## Layout

```
.agent-knowledge/
├── README.md                       ← you are here
├── 00-orientation.md               ← what galpal is, entry points, layout
├── architecture/
│   ├── modules.md                  ← how _galpal/ modules fit together
│   └── release-process.md          ← signed tags, version sync, draft-release flow
├── conventions/
│   ├── streaming.md                ← memory contract: dedupe + graph_paged + tests
│   └── testing.md                  ← FakeGraph / FakeResponse / RecordingReporter
├── domain/
│   └── graph-quirks.md             ← MS Graph oddities (lowercase GUID, $batch synthesis…)
├── decisions/
│   └── YYYY-MM-DD-<slug>.md        ← ADRs (status/context/decision/consequences)
└── gotchas.md                      ← append-only log of pitfalls
```

## Contribution checklist (after every meaningful commit)

1. **Non-obvious gotcha?** → append to [gotchas.md](gotchas.md) with a
   `## YYYY-MM-DD — title` entry. Don't restructure; append.
2. **Architectural decision?** → new file under
   [decisions/](decisions/) named `YYYY-MM-DD-<slug>.md`. Past ADRs are
   never edited — supersede them with a new ADR that references the old one.
3. **Domain knowledge?** (Graph quirk, MSAL behavior, tenant-specific
   surprise) → update or add a file in [domain/](domain/).
4. **New pattern others should follow or avoid?** →
   update or add a file in [conventions/](conventions/).
5. **Existing note invalidated?** → update inline. Stale knowledge is
   worse than missing knowledge.

## Style

- Short files (1–2 screens). If a topic outgrows that, split it.
- What + why + how-to-extend.
- Cite code with `path/to/file.py:line` — agents follow these blindly, so
  make them point at the right line.
- ISO dates (`2026-05-11`) on every dated entry.

## What doesn't go here

- Secrets, credentials, live data — this folder is committed.
- Auto-generated content. If a script can derive it, don't paste it.
- Reformulations of what code already says clearly with good names.
