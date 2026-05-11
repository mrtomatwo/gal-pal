"""`galpal pull` — one-way sync from the GAL into personal Outlook contacts.

Streams the filtered GAL through a JSONL temp-file spool (so memory stays bounded
on big tenants), then walks the spool: matches each entry against existing
stamped contacts (or by email as a fallback), skips when already in sync, batches
PATCH/POST sub-requests via Graph $batch.

All progress / per-row output goes through a `Reporter` (see `_galpal.reporter`),
so the same orchestrator drives the human-readable TTY output, the JSON-lines
output for cron / pipelines, and the `RecordingReporter` used by tests.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

from _galpal.graph import fetch_existing_contacts, fetch_gal, send_batch
from _galpal.model import build_request, gal_already_pulled
from _galpal.reporter import default_reporter

if TYPE_CHECKING:
    from _galpal.filters import FilterConfig
    from _galpal.reporter import Reporter

# Stat keys, lifted out of the dict so a typo (`stats["erorrs"] += 1`) is a
# NameError at parse time rather than a silent under-count. `Counter` then
# tolerates missing keys at read time without sprinkling `stats.get(k, 0)`.
STAT_CREATE = "CREATE"
STAT_UPDATE = "UPDATE"
STAT_SKIP = "SKIP"
STAT_ERRORS = "errors"


def flush_batch(token: str, batch: list, stats: Counter, reporter: Reporter) -> None:
    """Dispatch a buffered batch of (action, name, sub_request) tuples."""
    if not batch:
        return
    responses = send_batch(token, [req for _, _, req in batch])
    # `strict=True` because send_batch's contract is "one response per
    # request, in order" — and it now fills in synthetic 500 entries for any
    # sub-request Graph dropped, so a length mismatch here is a real bug, not
    # a Graph quirk to silently absorb.
    for (action, name, _), resp in zip(batch, responses, strict=True):
        status = resp.get("status")
        if status in (200, 201, 204):
            stats[action] += 1
        else:
            stats[STAT_ERRORS] += 1
            # Reporter handles ANSI/OSC sanitization on its rendering path; the
            # orchestrator hands over raw values + a structured kind so the
            # JSON consumer also gets the unredacted body if it wants it.
            reporter.entry(
                "subrequest.error",
                action=action,
                name=name,
                status=status,
                body=resp.get("body"),
            )
    batch.clear()


def run_pull(
    token: str,
    filters: FilterConfig,
    *,
    dry_run: bool,
    limit: int,
    batch_size: int,
    scratch_dir: str | None = None,
    reporter: Reporter | None = None,
) -> int:
    """Pull the filtered GAL into the user's personal contacts.

    Returns the number of per-subrequest errors so the caller can decide what
    exit code to surface. 0 errors → exit 0; >0 errors → exit 2 ("succeeded
    with errors", distinct from a clean failure exit 1).

    For each entry: match by Azure id stamp (preferred) or by email (fallback);
    skip when already in sync; otherwise CREATE or UPDATE via $batch.
    """
    rep = reporter or default_reporter()
    # Defense-in-depth: a hostile dotfile / CI matrix that points
    # --scratch-dir at a directory the running user doesn't own (e.g. a
    # world-writable /tmp share with attackers on the same box) would
    # deposit the GAL spool there. We can't refuse — small-/tmp environments
    # legitimately need an override — but we can warn loudly so the user
    # notices.
    if scratch_dir is not None:
        try:
            p = Path(scratch_dir).expanduser().resolve()
            if os.name == "posix":
                st = p.stat()
                if st.st_uid != os.getuid():
                    rep.warning(
                        f"--scratch-dir {p} is not owned by the running user "
                        f"(uid {st.st_uid}); the GAL spool will be written there.",
                    )
            scratch_dir = str(p)
        except OSError:
            # Path doesn't exist / can't be stat'd — let TemporaryFile produce
            # a clearer error than we'd craft here.
            pass
    rep.info("Indexing existing contacts...")
    by_azure_id, by_email = fetch_existing_contacts(token, reporter=rep)
    rep.info(f"  previously pulled: {len(by_azure_id)}; total emails indexed: {len(by_email)}")

    # Spool the filtered GAL to a JSONL scratch file (one entry per line) instead
    # of holding the whole directory in memory. tempfile.TemporaryFile is auto-deleted
    # when closed, even on exception. `dir=` lets the caller redirect away from
    # the system temp dir (see --scratch-dir / GALPAL_SCRATCH_DIR) — useful when
    # /tmp is tiny: a 5k-entry GAL spool is ~5 MB, a 50k-entry one ~50 MB.
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", dir=scratch_dir) as scratch:
        total = 0
        with rep.progress(total=None, unit="entry", desc="Fetching GAL") as fbar:
            for u in fetch_gal(token, filters):
                if limit and total >= limit:
                    break
                scratch.write(json.dumps(u))
                scratch.write("\n")
                total += 1
                fbar.update(1)
        rep.info(f"  {total} entries to process")

        scratch.seek(0)
        stats: Counter = Counter({STAT_CREATE: 0, STAT_UPDATE: 0, STAT_SKIP: 0, STAT_ERRORS: 0})
        batch: list = []
        with rep.progress(total=total, unit="contact", desc="Pulling") as pbar:
            for line in scratch:
                u = json.loads(line)
                existing = by_azure_id.get(u["id"])
                matched_by_id = existing is not None
                if not existing:
                    mail_lc = (u.get("mail") or u.get("userPrincipalName") or "").lower()
                    if mail_lc:
                        existing = by_email.get(mail_lc)

                # Skip writes when the contact already reflects all GAL fields.
                # Only safe for stamped contacts — email-only matches must still
                # write to persist the stamp on first run.
                if existing and matched_by_id and gal_already_pulled(u, existing):
                    stats[STAT_SKIP] += 1
                    pbar.update(1)
                    continue

                action = STAT_UPDATE if existing else STAT_CREATE
                mail = u.get("mail") or u.get("userPrincipalName") or "—"
                biz = (u.get("businessPhones") or [None])[0]
                mob = u.get("mobilePhone")
                phone = mob or biz or "—"
                rep.entry("pull.row", action=action, name=u.get("displayName"), mail=mail, phone=phone)

                if dry_run:
                    stats[action] += 1
                else:
                    batch.append((action, u.get("displayName"), build_request(existing, u)))
                    if len(batch) >= batch_size:
                        flush_batch(token, batch, stats, rep)
                pbar.update(1)
            flush_batch(token, batch, stats, rep)

    rep.summary(
        created=stats[STAT_CREATE],
        updated=stats[STAT_UPDATE],
        skipped=stats[STAT_SKIP],
        errors=stats[STAT_ERRORS],
        dry_run=dry_run,
    )
    return int(stats[STAT_ERRORS])
