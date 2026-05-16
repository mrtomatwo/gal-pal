# External references

## Microsoft Graph

- **Graph REST API v1.0:** https://learn.microsoft.com/en-us/graph/api/overview?view=graph-rest-1.0
- **`$batch` reference:** https://learn.microsoft.com/en-us/graph/json-batching
- **Throttle guidance (tenant-wide ceilings):** https://learn.microsoft.com/en-us/graph/throttling
- **Extended properties on contacts:**
  https://learn.microsoft.com/en-us/graph/api/resources/extendedproperty

galpal-relevant constants:

- PSETID_PublicStrings GUID for the Azure-id stamp:
  `00020329-0000-0000-c000-000000000046` (lowercased — see
  [domain/graph-quirks.md](domain/graph-quirks.md)).
- $batch hard limit: 20 sub-requests per envelope. Encoded as
  `MAX_BATCH_SIZE` in [_galpal/graph.py](../_galpal/graph.py).

## MSAL Python (device-code auth)

- **Repo:** https://github.com/AzureAD/microsoft-authentication-library-for-python
- **Public-client flow:** https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-device-code

galpal uses Microsoft's first-party public client IDs (Office, Teams, etc.)
so end users don't need an app registration. UUID-validated `--client-id`
with allowlist + `GALPAL_ALLOW_UNKNOWN_CLIENT_ID` opt-in
(illicit-consent-grant defense). See [_galpal/auth.py](../_galpal/auth.py).

## ijson

- **Docs:** https://github.com/ICRAR/ijson
- **C backend:** `ijson.backends.yajl2_c` (auto-selected when available).
  Reads via `.read(n)` — see
  [gotchas.md](gotchas.md) entry on `_IterReader`.

## requests

- **Streaming responses:** https://requests.readthedocs.io/en/latest/user/advanced/#body-content-workflow
- galpal uses `stream=True` + `iter_content(chunk_size=65536)` for the GET
  path. Connection-lifetime hygiene enforced via `try/finally: r.close()`
  per page.

## Keep a Changelog

- **Spec:** https://keepachangelog.com/en/1.1.0/

CHANGELOG sections we use: per-version block with `Subcommands`,
`Output modes`, `Authentication`, `Resilience`, `Memory`, `Hardened`,
`Architecture`, `Install`. Not every release touches every section — only
the ones with real changes.

## Tooling

- **ruff:** https://docs.astral.sh/ruff/ — `select = ["ALL"]` with
  pragmatic per-file ignores in
  [pyproject.toml](../pyproject.toml).
- **pyright:** `basic` mode. Type-checks `_galpal/` and `tests/`.
- **pytest:** 220 tests; runs in ~0.3s. No network. Suite-wide
  `filterwarnings = ["ignore::DeprecationWarning"]` quiets transitive
  msal/requests deprecation noise.
- **gitleaks:** runs in the pre-commit hook to block accidental secret
  commits.
