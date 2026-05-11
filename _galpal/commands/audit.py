"""`galpal audit` — read-only GAL inspection.

Streams the GAL through the active filters and reports email collisions,
azure-id collisions, and entries missing a mail field. Never writes to Graph.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from _galpal.commands import PREVIEW_LIMIT
from _galpal.graph import fetch_gal
from _galpal.reporter import default_reporter

if TYPE_CHECKING:
    from _galpal.filters import FilterConfig
    from _galpal.reporter import Reporter


def run_audit(token: str, filters: FilterConfig, *, reporter: Reporter | None = None) -> None:
    """Stream the GAL through the active filters and report key-quality issues."""
    rep = reporter or default_reporter()
    by_email: dict[str, list[tuple[str, str]]] = {}
    by_id: dict[str, list[tuple[str, str]]] = {}
    no_mail: list[tuple[str, str]] = []
    total = 0

    with rep.progress(total=None, unit="entry", desc="Auditing GAL") as bar:
        for u in fetch_gal(token, filters):
            total += 1
            name = u.get("displayName") or "(no name)"
            aid = u.get("id") or ""
            mail = (u.get("mail") or "").lower()
            by_id.setdefault(aid, []).append((name, mail or "—"))
            if mail:
                by_email.setdefault(mail, []).append((name, aid))
            else:
                no_mail.append((name, aid))
            bar.update(1)

    dup_emails = {m: v for m, v in by_email.items() if len(v) > 1}
    dup_ids = {i: v for i, v in by_id.items() if len(v) > 1}

    rep.info(f"\nGAL entries scanned: {total}")
    rep.info(f"  unique mail addresses: {len(by_email)}")
    rep.info(f"  entries without a mail field: {len(no_mail)}")
    rep.info(f"  email collisions (same mail on >1 entry): {len(dup_emails)}")
    rep.info(f"  azure-id collisions (should always be 0): {len(dup_ids)}")

    if dup_emails:
        rep.info("\nEmail collisions:")
        for mail, entries in sorted(dup_emails.items()):
            rep.entry("audit.email_collision", mail=mail, count=len(entries))
            for name, aid in entries:
                rep.entry("audit.email_collision_entry", name=name, id=aid)
    if dup_ids:
        rep.info("\nAzure-id collisions:")
        for aid, entries in dup_ids.items():
            rep.entry("audit.id_collision", id=aid, count=len(entries))
            for name, mail in entries:
                rep.entry("audit.id_collision_entry", name=name, mail=mail)
    if no_mail and len(no_mail) <= PREVIEW_LIMIT:
        rep.info("\nEntries with no mail field:")
        for name, aid in no_mail:
            rep.entry("audit.no_mail_entry", name=name, id=aid)

    rep.summary(
        scanned=total,
        unique_emails=len(by_email),
        no_mail=len(no_mail),
        email_collisions=len(dup_emails),
        id_collisions=len(dup_ids),
    )
