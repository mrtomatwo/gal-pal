"""CLI entry point: argparse subcommand registration + dispatch.

Internal — this docstring is *not* shown in `galpal --help`; that comes from
the user-facing `_TOP_LEVEL_DESCRIPTION` set on the parser below. Each
subcommand's actual behavior lives in `_galpal.commands.*`; this module only
wires arguments to the right `run_<command>` orchestrator and resolves the
auth client id.

Invoked one of two ways: as the `galpal` console script after `pipx install
galpal` (registered via `[project.scripts]` in pyproject.toml), or in dev as
`python dev_galpal.py …` from a clone of the repo. Both paths land in
`main()` below; argparse picks up the right program name from `sys.argv[0]`.
"""

import argparse
import logging
import os
import re
import sys

from _galpal.auth import (
    DEFAULT_CLIENT_ID,
    KNOWN_CLIENTS,
    AuthError,
    get_token,
    resolve_client_id,
    take_migration_notes,
)
from _galpal.commands.audit import run_audit
from _galpal.commands.categories import run_remove_categories
from _galpal.commands.dedupe import run_dedupe
from _galpal.commands.delete import run_delete
from _galpal.commands.folders import run_list_folders, run_remove_folders
from _galpal.commands.prune import run_prune
from _galpal.commands.pull import run_pull
from _galpal.filters import FilterConfig
from _galpal.graph import MAX_BATCH_SIZE
from _galpal.reporter import JSONReporter, QuietReporter, Reporter, TTYReporter

# User-facing top-level help text (shown by `galpal --help` and `python dev_galpal.py --help`).
_TOP_LEVEL_DESCRIPTION = (
    "galpal — mirror your company's Global Address List into your personal\n"
    "Outlook contacts, one-way and idempotent.\n\n"
    "Run a subcommand below; each accepts --help for its own flags.\n"
    "Destructive subcommands (prune --apply, delete --apply, dedupe --apply)\n"
    "default to dry-run and require an interactive confirmation prompt of the\n"
    "form `DELETE <count> <SCOPE>`. See README.md for the full picture."
)


class _ReporterLogHandler(logging.Handler):
    """Bridge between `logging.Logger` and the active Reporter.

    `_galpal.graph` emits structured retry / throttle / synthesis events
    through `logging.getLogger("galpal.graph")` so they're visible to a
    cron operator. `cli.py` attaches one of these handlers per run so the
    same events also surface through `--json` / `--quiet` / TTY uniformly.
    INFO maps to `reporter.info`; WARNING/ERROR to `reporter.warning` /
    `reporter.error`.
    """

    def __init__(self, reporter: Reporter) -> None:
        super().__init__(level=logging.INFO)
        self._reporter = reporter

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:  # noqa: BLE001  -- logging handlers must not raise
            return
        if record.levelno >= logging.ERROR:
            self._reporter.error(msg)
        elif record.levelno >= logging.WARNING:
            self._reporter.warning(msg)
        else:
            self._reporter.info(msg)


def _wire_logging(reporter: Reporter) -> None:
    """Attach a single _ReporterLogHandler to the `galpal.*` logger tree.

    Idempotent across `main()` calls (tests re-invoke `cli.main()` in-process)
    by clearing existing handlers first. Suppresses propagation so we don't
    double-print through the root logger.
    """
    log = logging.getLogger("galpal")
    log.handlers.clear()
    log.addHandler(_ReporterLogHandler(reporter))
    log.setLevel(logging.INFO)
    log.propagate = False


def _build_reporter(mode: str) -> Reporter:
    """Map the CLI --json / --quiet / (default) choice to a Reporter instance.

    Centralized so the dispatch logic in main() doesn't need three branches at
    every subcommand call site.
    """
    if mode == "json":
        return JSONReporter()
    if mode == "quiet":
        return QuietReporter()
    return TTYReporter()


class _HelpfulParser(argparse.ArgumentParser):
    """ArgumentParser that prints the parser's full --help text after every error.

    Users see the available commands/options right after the error message.
    """

    def error(self, message: str) -> None:
        sys.stderr.write(f"{self.prog}: error: {message}\n\n")
        self.print_help(sys.stderr)
        sys.exit(2)


def _add_filter_args(p: argparse.ArgumentParser) -> None:
    """Add the filter args shared by pull/audit/prune.

    Help text is predicate-neutral — each subcommand interprets a "passing" entry
    differently (pull keeps it, audit reports it, prune SPARES it). The per-command
    description is responsible for spelling out the consequence; here we only describe
    what the predicate matches.

    Defaults are 'sensible' — only filters almost everyone wants are on; the stricter
    ones are opt-in.
    """
    p.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="REGEX",
        # For pull/audit/prune the predicate semantically means "EXCLUDE matches"
        # (pull skips them, audit reports them, prune spares them). Using "skip"
        # here reads naturally for pull (the common case) without lying about
        # audit/prune.
        help="skip entries whose displayName matches REGEX (repeatable)",
    )
    p.add_argument(
        "--require-email",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="require an email address (mail field) [default: on]",
    )
    p.add_argument(
        "--require-full-name",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="require both givenName and surname [default: off]",
    )
    p.add_argument(
        "--require-phone",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="require a business or mobile phone [default: off]",
    )
    p.add_argument(
        "--require-comma",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="require a comma in displayName [default: off]",
    )


def main() -> None:
    """Parse argv, authenticate, and dispatch to the chosen subcommand."""
    # `prog` defaults to `os.path.basename(sys.argv[0])`, which renders as
    # `galpal` for pipx-installed users (`/path/to/.venv/bin/galpal`) and
    # `dev_galpal.py` for clone-based dev invocations. Either is correct
    # for the actual invocation; hardcoding it would lie in one mode.
    ap = _HelpfulParser(
        description=_TOP_LEVEL_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--client-id",
        default=os.environ.get("GALPAL_CLIENT_ID", DEFAULT_CLIENT_ID),
        help=(
            "Azure AD public client id to authenticate as. Accepts an alias "
            f"({', '.join(KNOWN_CLIENTS)}) or a raw GUID. "
            f"Default: office ({DEFAULT_CLIENT_ID})."
        ),
    )
    # Output mode is global because every subcommand uses the reporter the
    # same way. --json and --quiet are mutually exclusive (json is silent
    # outside its own ndjson stream; quiet is silent outside warnings/errors).
    output = ap.add_mutually_exclusive_group()
    output.add_argument(
        "--json",
        dest="output",
        action="store_const",
        const="json",
        help=(
            "emit one JSON object per event to stdout (ndjson). Cron- and "
            "pipeline-friendly. Confirmations are refused in this mode — for "
            "scripted destructive runs, set GALPAL_FORCE_NONINTERACTIVE=1 and "
            "run with --apply through a TTY-aware wrapper."
        ),
    )
    output.add_argument(
        "--quiet",
        "-q",
        dest="output",
        action="store_const",
        const="quiet",
        help="suppress informational output and per-row entries; keep warnings, errors, and the summary line",
    )
    ap.set_defaults(output="tty")
    sub = ap.add_subparsers(dest="cmd", metavar="COMMAND", required=True, parser_class=_HelpfulParser)

    sp_pull = sub.add_parser(
        "pull",
        help="pull GAL entries into your personal contacts (one-way: directory → contacts)",
        description=(
            "Pull GAL entries from the tenant directory into your personal Outlook "
            "contacts. One-way: directory → contacts; never writes back to the GAL.\n\n"
            "Each pulled contact is stamped with the source user's Azure id, so re-runs "
            "match the same person across pulls. A pre-existing contact whose email "
            "matches a GAL entry but lacks the stamp is adopted and stamped on first run. "
            "Manually-added fields (home phones, personal notes, categories, ...) on the "
            "contact are always preserved."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_filter_args(sp_pull)
    sp_pull.add_argument("--dry-run", action="store_true", help="show what would change without writing")
    sp_pull.add_argument("--limit", type=int, default=0, help="stop after N GAL entries (for testing)")
    sp_pull.add_argument(
        "--batch-size",
        type=int,
        default=int(os.environ.get("GALPAL_BATCH_SIZE", "20")),
        help=(
            "contacts per Graph $batch request (max 20). Use 1 to disable batching. "
            "Default 20; override with --batch-size or $GALPAL_BATCH_SIZE."
        ),
    )
    sp_pull.add_argument(
        "--scratch-dir",
        default=None,
        help=(
            "directory for the JSONL spool tempfile. Defaults to $GALPAL_SCRATCH_DIR, "
            "then the system temp dir. Useful when /tmp is small (a 5k-entry GAL "
            "spool is ~5 MB)."
        ),
    )

    sp_audit = sub.add_parser(
        "audit",
        help="report duplicates and quality issues in the GAL (read-only)",
        description=(
            "Stream the GAL through the active filters and report data-quality issues — "
            "duplicate emails, duplicate Azure ids (should never happen), and entries "
            "missing a mail field. Read-only: never writes anything to your mailbox or "
            "the directory."
        ),
    )
    _add_filter_args(sp_audit)

    sp_dedupe = sub.add_parser(
        "dedupe",
        help="find and (with --apply) remove duplicate personal contacts",
        description=(
            "Group personal contacts that share an email address (transitively, so a chain "
            "of overlapping addresses ends up in one group). Within each group, keep the "
            "contact with the most user-added data — categories, personal notes, home "
            "phones, birthday — and propose deleting the rest.\n\n"
            "Default is dry-run; --apply executes the deletions."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sp_dedupe.add_argument("--apply", action="store_true", help="actually delete duplicates (default is dry-run)")

    sp_prune = sub.add_parser(
        "prune",
        help="delete pulled contacts that fail the filters (data filters and/or --orphans)",
        description=(
            "Delete contacts previously pulled by galpal whose stored data fails the "
            "active filters. The filters describe which contacts to KEEP — anything "
            "not matching is pruned. Data filters (--require-*, --exclude) read the "
            "contact's stored fields. --orphans additionally requires the contact's "
            "Azure source to still exist in the GAL — that one criterion fetches "
            "/users; the data filters do not.\n\n"
            "Manually-added contacts (no galpal stamp) are always left alone.\n\n"
            "At least one filter must be active or prune refuses to run, which is why "
            "--require-email defaults on."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_filter_args(sp_prune)
    sp_prune.add_argument(
        "--orphans",
        action="store_true",
        help="also prune pulled contacts whose Azure source no longer exists in the GAL (requires fetching /users)",
    )
    sp_prune.add_argument(
        "--apply",
        action="store_true",
        help="perform the deletion. Without it, prune is a dry run. Even with --apply "
        "an interactive prompt appears: type literally `DELETE <count> PRUNE` "
        "(e.g. `DELETE 47 PRUNE`) to confirm.",
    )

    sp_delete = sub.add_parser(
        "delete",
        help="DESTRUCTIVE: wholesale-delete contacts (default: every contact NOT stamped by galpal; "
        "--all wipes everything)",
        description=(
            "DESTRUCTIVE wholesale deletion of personal contacts. By default deletes every "
            "contact NOT stamped by galpal — the manually-added entries, vendors, friends, "
            "former colleagues, anything you added by hand. With --all, deletes EVERY "
            "contact, including those pulled from the GAL.\n\n"
            "For selective deletion of pulled contacts (filters, orphan detection), use "
            "`prune` instead.\n\n"
            "Default is dry-run; --apply requires an interactive confirmation "
            "prompt. The prompt token is `DELETE <count> ALL` for `--all`, or "
            "`DELETE <count> UNSTAMPED` for the default mode. Deleted contacts go "
            "to the mailbox's Deleted Items folder, recoverable subject to your "
            "tenant's retention (typically 14-30 days)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sp_delete.add_argument(
        "--all",
        dest="all_contacts",
        action="store_true",
        help="delete EVERY contact, including those stamped by galpal",
    )
    sp_delete.add_argument(
        "--apply",
        action="store_true",
        help="actually delete (default is dry-run; --apply also requires interactive confirmation)",
    )

    sp_rmcat = sub.add_parser(
        "remove-category",
        help="strip categories from all personal contacts and delete master entries",
        description=(
            "Strip the named categories from every personal contact, then delete the "
            "matching master-category entries from the mailbox so they vanish from "
            "Outlook's category sidebar entirely.\n\n"
            "Names are matched case-insensitively. Default is dry-run; --apply executes."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sp_rmcat.add_argument("names", nargs="+", metavar="NAME", help="category name(s) to remove (case-insensitive)")
    sp_rmcat.add_argument("--apply", action="store_true", help="actually write (default is dry-run)")

    sp_rmfolder = sub.add_parser(
        "remove-folder",
        help="delete contact folder(s) (contents go to Deleted Items)",
        description=(
            "Delete the named contact folders. Folder contents (the contacts inside) are "
            "moved to the mailbox's Deleted Items folder along with the folder itself, so "
            "they're recoverable subject to your tenant's retention (typically 14-30 days).\n\n"
            "Note: in the new Outlook web UI, contact folders appear under 'Kategorien' in "
            "the sidebar, which is easy to confuse with Outlook categories. Use "
            "`list-folders` to see what folders actually exist.\n\n"
            "Names are matched case-insensitively. Default is dry-run; --apply executes."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sp_rmfolder.add_argument("names", nargs="+", metavar="NAME", help="folder name(s) to delete (case-insensitive)")
    sp_rmfolder.add_argument("--apply", action="store_true", help="actually delete (default is dry-run)")

    sub.add_parser(
        "list-folders",
        help="list your contact folders (Outlook calls these 'Kategorien' in the sidebar)",
        description=(
            "List your personal contact folders with the count of contacts in each. The "
            "Outlook web UI groups contact folders under 'Kategorien' in the sidebar, "
            "which is easy to confuse with the Outlook categories feature. This command "
            "is the disambiguator. Note: a name can be both a folder and a category at "
            "the same time — `remove-folder` only deletes the folder side; "
            "`remove-category` deletes the category side.\n\n"
            "Read-only."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    args = ap.parse_args()

    # Resolve and validate --client-id (or GALPAL_CLIENT_ID env). Refuses
    # unknown UUIDs unless GALPAL_ALLOW_UNKNOWN_CLIENT_ID=1 — see auth.py for
    # the illicit-consent-grant threat model.
    # Build the reporter once and hand it to every run_* below. JSON output
    # suppresses the human "Using client id ..." prefix (it would corrupt
    # the ndjson stream); the same fact is emitted through reporter.info on
    # the way through TTY/Quiet so it still shows up there.
    reporter = _build_reporter(args.output)
    # Route `_galpal.graph`'s structured retry / throttle / synthesis events
    # through the active reporter, so a cron operator running with --json
    # actually sees them as ndjson and a TTY user sees them inline.
    _wire_logging(reporter)
    # Flush any deferred migration notes from auth.py — those fire before
    # the reporter exists (auth is the first thing that can run) so they
    # buffer and we drain them here. JSON consumers see them as ndjson
    # `info` events; TTY users see a one-line note.
    for note in take_migration_notes():
        reporter.info(note)

    raw = args.client_id or DEFAULT_CLIENT_ID
    source = "GALPAL_CLIENT_ID" if os.environ.get("GALPAL_CLIENT_ID") else "--client-id"
    # auth.py raises typed AuthError subclasses; the CLI is the single place
    # that translates those to a user-visible exit. KeyboardInterrupt during
    # the device-code polling loop is handled separately — it's not an auth
    # failure, it's the user changing their mind.
    try:
        client_id = resolve_client_id(raw, source=source)
        if raw in KNOWN_CLIENTS:
            reporter.info(f"Using client id {client_id} (alias: {raw})")
        elif client_id == DEFAULT_CLIENT_ID:
            reporter.info(f"Using default client id {client_id} (office)")
        else:
            reporter.info(f"Using client id {client_id} (custom; from {source})")
        token = get_token(client_id)
    except KeyboardInterrupt:
        sys.exit("\nDevice-code login cancelled.")
    except AuthError as e:
        sys.exit(str(e))

    # Pull/audit/prune share the five-knob filter spec; build it once. Subcommands
    # that don't take filters (dedupe, delete, remove-category, remove-folder,
    # list-folders) just don't reach `args.exclude` etc. — argparse only stamps
    # the filter attributes onto the namespace for parsers wired via _add_filter_args.
    if args.cmd in ("pull", "audit", "prune"):
        filters = FilterConfig(
            exclude_patterns=tuple(re.compile(p) for p in args.exclude),
            require_comma=args.require_comma,
            require_email=args.require_email,
            require_phone=args.require_phone,
            require_full_name=args.require_full_name,
        )

    if args.cmd == "audit":
        run_audit(token, filters, reporter=reporter)
        return

    if args.cmd == "dedupe":
        run_dedupe(token, apply=args.apply, reporter=reporter)
        return

    if args.cmd == "prune":
        run_prune(token, filters, orphans=args.orphans, apply=args.apply, reporter=reporter)
        return

    if args.cmd == "delete":
        run_delete(token, apply=args.apply, all_contacts=args.all_contacts, reporter=reporter)
        return

    if args.cmd == "remove-category":
        run_remove_categories(token, args.names, apply=args.apply, reporter=reporter)
        return

    if args.cmd == "remove-folder":
        run_remove_folders(token, args.names, apply=args.apply, reporter=reporter)
        return

    if args.cmd == "list-folders":
        run_list_folders(token, reporter=reporter)
        return

    # args.cmd == "pull" — argparse's required=True guarantees this is the last possibility.
    if not 1 <= args.batch_size <= MAX_BATCH_SIZE:
        sys.exit(f"--batch-size must be between 1 and {MAX_BATCH_SIZE}")
    # --scratch-dir wins over the env var; the env var wins over the system default.
    scratch_dir = args.scratch_dir or os.environ.get("GALPAL_SCRATCH_DIR") or None
    errors = run_pull(
        token,
        filters,
        dry_run=args.dry_run,
        limit=args.limit,
        batch_size=args.batch_size,
        scratch_dir=scratch_dir,
        reporter=reporter,
    )
    # Distinguish "succeeded with batch errors" (exit 2) from "clean run"
    # (exit 0) and from "argparse / preflight failure" (exit 1, raised by
    # sys.exit elsewhere). CI / cron wrappers can branch on the exit code.
    if errors:
        sys.exit(2)
