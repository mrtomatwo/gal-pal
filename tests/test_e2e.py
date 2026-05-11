"""End-to-end tests: drive `galpal.main()` with a real argv, observe what the
fake Graph state looks like afterwards. Exercises both happy and error paths for
every subcommand."""

from __future__ import annotations

from .conftest import make_contact, make_user

# ============================================================================
# pull — happy paths
# ============================================================================


def test_pull_creates_all_contacts_when_mailbox_empty(run_cli, graph):
    graph.users.extend([make_user("u1"), make_user("u2", mail="bob@x.com")])
    code, out, _ = run_cli("pull")
    assert code == 0
    assert len(graph.contacts) == 2
    # Each created contact gets stamped with its azure id.
    stamps = {c["singleValueExtendedProperties"][0]["value"] for c in graph.contacts}
    assert stamps == {"u1", "u2"}
    assert "created=2" in out and "updated=0" in out


def test_pull_is_idempotent_on_second_run(run_cli, graph):
    graph.users.append(make_user("u1"))
    run_cli("pull")
    # Second run with no changes should skip everything.
    code, out, _ = run_cli("pull")
    assert code == 0
    assert "skipped=1" in out
    assert "created=0" in out and "updated=0" in out


def test_pull_dry_run_writes_nothing(run_cli, graph):
    graph.users.append(make_user("u1"))
    code, out, _ = run_cli("pull", "--dry-run")
    assert code == 0
    assert graph.contacts == []
    assert "dry_run=True" in out
    # No $batch was sent (only GETs).
    assert all(method == "GET" for method, *_ in graph.calls)


def test_pull_summary_via_recording_reporter(run_cli_recorded, graph):
    """Use the structured `RecordingReporter` surface instead of grepping
    TTY substrings — assertions on `rec.summary_kwargs` survive cosmetic
    rendering changes that would otherwise break a `"created=2" in out`
    check. New e2e tests should prefer this shape; the substring greps
    above remain for tests of the TTY rendering itself."""
    graph.users.extend([make_user("u1"), make_user("u2", mail="bob@x.com")])
    code, rec = run_cli_recorded("pull")
    assert code == 0
    assert rec.summary_kwargs == {
        "created": 2,
        "updated": 0,
        "skipped": 0,
        "errors": 0,
        "dry_run": False,
    }


def test_dedupe_aborts_via_recording_reporter(run_cli_recorded, graph):
    """A typed `confirm_response=False` lets the test exercise the abort
    path without piping a stdin string — much cleaner than the current
    `run_cli(..., stdin="DELETE 1 DEDUPE\\n")` shape."""
    graph.contacts.extend(
        [
            make_contact("c1", emails=[{"address": "a@x.com"}]),
            make_contact("c2", emails=[{"address": "a@x.com"}]),
        ]
    )
    code, rec = run_cli_recorded("dedupe", "--apply", confirm_response=False)
    assert code == 0
    assert rec.summary_kwargs is not None
    assert rec.summary_kwargs.get("aborted") is True
    # Both contacts survive.
    assert {c["id"] for c in graph.contacts} == {"c1", "c2"}


def test_pull_preserves_user_email_casing_through_full_run(run_cli, graph):
    """End-to-end: a re-pull that goes through merge_emails AND the actual
    PATCH path must preserve the user's email casing.

    Pins the merge_emails ↔ flush_batch contract that the existing unit test
    covers in isolation but no e2e drives. A regression that overwrites user
    casing (a pure model-layer mistake) would slip through if only the unit
    test caught it.
    """
    # GAL row uses lowercase. The user has the same address with mixed casing
    # already in their contacts; a phone difference forces an UPDATE.
    graph.users.append(make_user("u1", mail="jane@corp.example", phone="+1-555-9999"))
    graph.contacts.append(
        make_contact(
            "c1",
            emails=[{"address": "Jane@Corp.Example", "name": "Custom"}],
            azure_id="u1",
            givenName="Jane",
            surname="Doe",
            mobilePhone="+1-555-0000",  # different from GAL → triggers UPDATE
        )
    )
    code, _, _ = run_cli("pull")
    assert code == 0
    # User's casing survived the round-trip.
    assert graph.contacts[0]["emailAddresses"][0]["address"] == "Jane@Corp.Example"


def test_pull_matches_existing_contact_by_email_and_stamps_it(run_cli, graph):
    """A contact added manually before first pull must be matched by email and stamped."""
    graph.users.append(make_user("u1", mail="jane@corp.example"))
    graph.contacts.append(make_contact("c1", emails=[{"address": "jane@corp.example"}]))
    code, out, _ = run_cli("pull")
    assert code == 0
    assert "updated=1" in out
    assert len(graph.contacts) == 1
    assert graph.contacts[0]["singleValueExtendedProperties"][0]["value"] == "u1"


# ============================================================================
# pull — filters
# ============================================================================


def test_pull_default_requires_email(run_cli, graph):
    """--require-email defaults on; UPN-only entries are skipped."""
    graph.users.extend([make_user("u1", mail="x@x.com"), make_user("u2", mail=None, upn="upn@x.com")])
    run_cli("pull")
    assert {c["singleValueExtendedProperties"][0]["value"] for c in graph.contacts} == {"u1"}


def test_pull_no_require_email_disables_default(run_cli, graph):
    graph.users.extend([make_user("u1", mail="x@x.com"), make_user("u2", mail=None, upn="upn@x.com")])
    run_cli("pull", "--no-require-email")
    assert len(graph.contacts) == 2


def test_pull_require_full_name_skips_no_name_entries(run_cli, graph):
    graph.users.extend([make_user("u1"), make_user("u2", given="", surname="OnlyLast")])
    run_cli("pull", "--require-full-name")
    assert [c["singleValueExtendedProperties"][0]["value"] for c in graph.contacts] == ["u1"]


def test_pull_exclude_regex_skips_matches(run_cli, graph):
    graph.users.extend([make_user("u1", name="Real Person"), make_user("u2", name="Test Account")])
    run_cli("pull", "--exclude", "^Test")
    assert [c["singleValueExtendedProperties"][0]["value"] for c in graph.contacts] == ["u1"]


def test_pull_limit_caps_processing(run_cli, graph):
    for i in range(5):
        graph.users.append(make_user(f"u{i}", mail=f"u{i}@x.com"))
    run_cli("pull", "--limit", "2")
    assert len(graph.contacts) == 2


# ============================================================================
# pull — error / edge paths
# ============================================================================


def test_pull_retries_on_top_level_429(run_cli, graph):
    """Server-level 429 retries transparently and the pull still completes."""
    graph.users.append(make_user("u1"))
    graph.queue_429(lambda m, u: u.endswith("/$batch"))
    run_cli("pull")
    assert len(graph.contacts) == 1


def test_pull_invalid_batch_size_rejected(run_cli):
    code, _, err = run_cli("pull", "--batch-size", "999")
    assert code != 0
    assert "batch-size" in err or "batch-size" in (err or "")


def test_pull_per_request_batch_error_is_reported(run_cli, graph, monkeypatch):
    graph.users.extend([make_user("u1"), make_user("u2", mail="b@x.com")])

    def busted_batch(body):
        # Mark every other sub-request as 500 to verify error counting.
        from .conftest import FakeResponse

        responses = []
        for i, req in enumerate(body["requests"]):
            if i % 2 == 0:
                responses.append(
                    {
                        "id": req["id"],
                        "status": 500,
                        "body": {"error": {"code": "ServerError"}},
                    }
                )
            else:
                _, body_ = graph._dispatch(req["method"], req["url"], req.get("body") or {})
                responses.append({"id": req["id"], "status": 201, "body": body_})
        return FakeResponse(200, {"responses": responses})

    monkeypatch.setattr(graph, "_handle_batch", busted_batch)
    code, out, _ = run_cli("pull")
    # Exit 2 = "succeeded with errors" (distinct from 0 = clean and 1 = preflight
    # failure). m8: pull surfaces stats["errors"] > 0 as a non-zero exit so cron
    # / CI wrappers can branch on it.
    assert code == 2
    assert "errors=1" in out
    assert "created=1" in out


# ============================================================================
# audit
# ============================================================================


def test_audit_reports_email_collisions(run_cli, graph):
    graph.users.extend(
        [
            make_user("u1", mail="dup@x.com", name="Alice"),
            make_user("u2", mail="dup@x.com", name="Bob"),
            make_user("u3", mail="unique@x.com", name="Carol"),
        ]
    )
    code, out, _ = run_cli("audit")
    assert code == 0
    assert "Email collisions" in out
    assert "dup@x.com" in out
    assert "Alice" in out and "Bob" in out


def test_audit_is_read_only(run_cli, graph):
    graph.users.append(make_user("u1"))
    run_cli("audit")
    assert graph.contacts == []
    assert all(m == "GET" for m, *_ in graph.calls)


# ============================================================================
# dedupe
# ============================================================================


def test_dedupe_dry_run_does_not_delete(run_cli, graph):
    graph.contacts.extend(
        [
            make_contact("c1", emails=[{"address": "a@x.com"}]),
            make_contact("c2", emails=[{"address": "a@x.com"}]),
        ]
    )
    code, out, _ = run_cli("dedupe")
    assert code == 0
    assert len(graph.contacts) == 2
    assert "Dry run" in out


def test_dedupe_apply_keeps_higher_user_data(run_cli, graph):
    """Of two contacts sharing an email, the one with more user-added data wins."""
    graph.contacts.extend(
        [
            # c1: bare
            make_contact("c1", emails=[{"address": "a@x.com"}]),
            # c2: has personal notes and categories → higher score → kept
            make_contact(
                "c2",
                emails=[{"address": "a@x.com"}],
                personalNotes="met at conf",
                categories=["vip"],
            ),
        ]
    )
    code, _, _ = run_cli("dedupe", "--apply", stdin="DELETE 1 DEDUPE\n")
    assert code == 0
    assert {c["id"] for c in graph.contacts} == {"c2"}


def test_dedupe_no_duplicates_short_circuits(run_cli, graph):
    graph.contacts.append(make_contact("c1", emails=[{"address": "a@x.com"}]))
    code, out, _ = run_cli("dedupe", "--apply")
    assert code == 0
    assert "No duplicate groups" in out


def test_dedupe_collapses_transitive_email_chain(run_cli, graph):
    """Four contacts forming an A↔B↔C↔D email-overlap chain must end up in
    one group, with the highest-score node winning.

    No pair shares ALL emails — only a transitive walk via Union-Find reaches
    the full group. A regression that drops `union-by-rank` (or breaks
    `find()`'s pointer compression) would silently produce wrong results on
    longer chains, and the existing two-contact tests would still pass.
    """
    graph.contacts.extend(
        [
            make_contact("c1", emails=[{"address": "a@x.com"}, {"address": "b@x.com"}]),
            make_contact("c2", emails=[{"address": "b@x.com"}, {"address": "c@x.com"}]),
            # Highest user-data score → wins.
            make_contact(
                "c3",
                emails=[{"address": "c@x.com"}, {"address": "d@x.com"}],
                categories=["vip"],
                personalNotes="winner",
            ),
            make_contact("c4", emails=[{"address": "d@x.com"}]),
        ]
    )
    code, _, _ = run_cli("dedupe", "--apply", stdin="DELETE 3 DEDUPE\n")
    assert code == 0
    assert {c["id"] for c in graph.contacts} == {"c3"}


def test_dedupe_streams_one_contact_at_a_time(graph, monkeypatch):
    """Pin the streaming-RAM contract: at any moment during dedupe's iteration
    over `graph_paged`, at most a small constant number of full Graph contact
    dicts should be alive. The streaming refactor extracts metadata
    (displayName, score, createdDateTime, emails) per row and lets the dict
    drop on the next iteration; a regression to `list(graph_paged(...))` would
    keep ALL N payloads alive while the list builds.

    Test shape: replace `graph_paged` with a generator that creates each
    contact lazily and registers a weakref to it. After each yield (i.e. after
    dedupe's loop body completes for that row), count how many earlier
    contacts are still reachable. Streaming → ~1 alive (current row in
    dedupe's loop variable). List-materialization → N alive (all in the list).

    Why not use `graph.contacts` for this? FakeGraph permanently retains its
    source-of-truth list, which simulates the server. The thing we want to
    bound is the *client*'s per-contact memory — anything dedupe holds beyond
    metadata.
    """
    import gc
    import weakref

    from _galpal.commands import dedupe as dedupe_mod
    from _galpal.reporter import RecordingReporter

    class Tracked(dict):
        """dict subclass — plain dict doesn't support weakref."""

    n = 20
    weakrefs: list[weakref.ref] = []
    peak_alive = [0]

    def streaming_check_paged(*_a, **_kw):
        for i in range(n):
            c = Tracked(make_contact(f"c{i}", emails=[{"address": f"u{i}@x.com"}]))
            weakrefs.append(weakref.ref(c))
            yield c
            # After yield returns, dedupe has finished its body for this `c`
            # and is requesting the next item. At this point, `c` is still
            # bound to dedupe's loop variable (about to be reassigned), but
            # all earlier `c`s should have been reassigned away and be
            # GC-able. Force a collect so weakrefs settle deterministically.
            del c
            gc.collect()
            alive = sum(1 for ref in weakrefs if ref() is not None)
            peak_alive[0] = max(peak_alive[0], alive)

    monkeypatch.setattr(dedupe_mod, "graph_paged", streaming_check_paged)
    dedupe_mod.run_dedupe("token", apply=False, reporter=RecordingReporter())

    # Streaming: at most ~1 alive (dedupe's current loop variable). Allow up
    # to 3 to give Python's frame-locals + cycles some headroom on different
    # interpreters. List-materialization peaks at N=20, well above this gate.
    assert peak_alive[0] <= 3, (
        f"dedupe is no longer streaming; peak alive contact dicts = "
        f"{peak_alive[0]}/{n}. A regression to `list(graph_paged(...))` shape "
        f"would push this to N. The metadata-only streaming pass is the "
        f"contract."
    )


# ============================================================================
# prune
# ============================================================================


def test_prune_refuses_with_no_filters_at_all(run_cli, graph):
    """All filter defaults off → guard trips so we don't nuke everyone."""
    code, out, _ = run_cli("prune", "--no-require-email")
    assert code == 0
    assert "Refusing to prune" in out


def test_prune_dry_run_lists_but_does_not_delete(run_cli, graph):
    # Filter is evaluated against contact data — no GAL fetch needed.
    graph.contacts.extend(
        [
            make_contact("c1", azure_id="u1", givenName="Jane", surname="Doe"),  # passes
            make_contact("c2", azure_id="u2"),  # no first/last → fails --require-full-name
        ]
    )
    code, out, _ = run_cli("prune", "--require-full-name")
    assert code == 0
    assert len(graph.contacts) == 2
    assert "1 pulled contact(s) no longer pass" in out
    assert "Dry run" in out


def test_prune_does_not_fetch_gal(run_cli, graph):
    """Regression: prune must operate on contact data only, not /users."""
    graph.contacts.append(make_contact("c1", azure_id="u1"))
    run_cli("prune", "--require-full-name")
    assert not any("/users" in url for _, url, _ in graph.calls)


def test_prune_apply_with_correct_confirmation_deletes(run_cli, graph):
    graph.contacts.extend(
        [
            make_contact("c1", azure_id="u1", givenName="Jane", surname="Doe"),  # keep
            make_contact("c2", azure_id="u2"),  # prune
        ]
    )
    code, out, _ = run_cli("prune", "--require-full-name", "--apply", stdin="DELETE 1 PRUNE\n")
    assert code == 0
    assert {c["id"] for c in graph.contacts} == {"c1"}
    assert "deleted=1" in out


def test_prune_apply_with_wrong_confirmation_aborts(run_cli, graph):
    graph.contacts.extend(
        [
            make_contact("c1", azure_id="u1", givenName="Jane", surname="Doe"),
            make_contact("c2", azure_id="u2"),
        ]
    )
    code, out, _ = run_cli("prune", "--require-full-name", "--apply", stdin="yes\n")
    assert code == 0
    assert len(graph.contacts) == 2
    assert "Aborted" in out


def test_prune_only_touches_stamped_contacts(run_cli, graph):
    """A contact without the galpal stamp is invisible to prune."""
    graph.contacts.extend(
        [
            make_contact("c1", azure_id="u1"),  # stamped, no first/last → prune
            make_contact("c2"),  # unstamped → invisible
        ]
    )
    code, _, _ = run_cli("prune", "--require-full-name", "--apply", stdin="DELETE 1 PRUNE\n")
    assert code == 0
    assert {c["id"] for c in graph.contacts} == {"c2"}


def test_prune_filter_per_field(run_cli, graph):
    """Each filter independently identifies the right contact to prune."""
    # No-email contact fails --require-email
    graph.contacts.append(
        make_contact(
            "no_email",
            azure_id="u1",
            givenName="A",
            surname="B",
            emails=[],
        )
    )
    # No-phone contact fails --require-phone
    graph.contacts.append(
        make_contact(
            "no_phone",
            azure_id="u2",
            givenName="C",
            surname="D",
            emails=[{"address": "c@x.com"}],
        )
    )
    # Has phone — passes --require-phone
    graph.contacts.append(
        make_contact(
            "phone_ok",
            azure_id="u3",
            givenName="E",
            surname="F",
            emails=[{"address": "e@x.com"}],
            businessPhones=["+1-555"],
        )
    )
    code, out, _ = run_cli("prune", "--require-email", "--require-phone")
    assert code == 0
    # Both no_email and no_phone should be flagged; phone_ok kept.
    assert "2 pulled contact(s) no longer pass" in out


def test_prune_orphan_with_passing_data_survives_without_orphans_flag(run_cli, graph):
    """Without --orphans, a contact whose stored data passes every data filter is kept,
    even if its Azure source no longer exists. --orphans is the opt-in for that check."""
    graph.contacts.append(
        make_contact(
            "orphan",
            azure_id="u-deleted-from-aad",
            givenName="Jane",
            surname="Doe",
            emails=[{"address": "jane@x.com"}],
            businessPhones=["+1-555"],
        )
    )
    code, _, _ = run_cli("prune", "--require-full-name", "--require-phone", "--apply", stdin="DELETE 0\n")
    assert code == 0
    assert len(graph.contacts) == 1  # unchanged


# ----- prune --orphans ------------------------------------------------------


def test_prune_orphans_alone_deletes_only_orphans(run_cli, graph):
    """--orphans with no other filter prunes any pulled contact whose source is gone."""
    graph.users.append(make_user("u1"))  # u1 still exists
    graph.contacts.extend(
        [
            make_contact("c1", azure_id="u1", givenName="Jane", surname="Doe"),  # live source
            make_contact("c2", azure_id="u-gone", givenName="Bob", surname="Smith"),  # orphan
            make_contact("c3", azure_id="u-also", givenName="Eve", surname="Adams"),  # orphan
            make_contact("c4"),  # unstamped — invisible to prune
        ]
    )
    code, out, _ = run_cli("prune", "--no-require-email", "--orphans", "--apply", stdin="DELETE 2 PRUNE\n")
    assert code == 0
    assert {c["id"] for c in graph.contacts} == {"c1", "c4"}
    assert "deleted=2" in out


def test_prune_orphans_dry_run_lists_but_does_not_delete(run_cli, graph):
    graph.users.append(make_user("u1"))
    graph.contacts.extend(
        [
            make_contact("c1", azure_id="u1", givenName="Jane", surname="Doe"),
            make_contact("c2", azure_id="u-gone", givenName="Bob", surname="Smith"),
        ]
    )
    code, out, _ = run_cli("prune", "--no-require-email", "--orphans")
    assert code == 0
    assert len(graph.contacts) == 2
    assert "1 pulled contact(s) no longer pass" in out
    assert "Dry run" in out


def test_prune_orphans_combined_with_data_filter_is_or(run_cli, graph):
    """A contact gets pruned if it fails ANY filter — orphan check OR data check."""
    graph.users.append(make_user("u1"))
    graph.contacts.extend(
        [
            # Live source, has full name → kept
            make_contact("c1", azure_id="u1", givenName="Jane", surname="Doe"),
            # Live source, no full name → fails data filter → pruned
            make_contact("c2", azure_id="u1", givenName="", surname=""),
            # Orphan, has full name → fails orphan check → pruned
            make_contact("c3", azure_id="u-gone", givenName="Eve", surname="Adams"),
        ]
    )
    code, _, _ = run_cli(
        "prune",
        "--require-full-name",
        "--orphans",
        "--apply",
        stdin="DELETE 2 PRUNE\n",
    )
    assert code == 0
    assert {c["id"] for c in graph.contacts} == {"c1"}


def test_prune_orphans_uses_unfiltered_user_query(run_cli, graph):
    """Orphan detection must enumerate ALL users, not apply pull's filters —
    a user who lost their email shouldn't be falsely flagged orphaned."""
    graph.users.append({**make_user("u1"), "mail": None, "userPrincipalName": None})
    graph.contacts.append(
        make_contact("c1", azure_id="u1", givenName="Jane", surname="Doe"),
    )
    code, out, _ = run_cli("prune", "--no-require-email", "--orphans")
    assert code == 0
    # u1 IS in /users → not orphaned, even with no email.
    assert "Nothing to prune" in out


def test_prune_orphans_does_not_touch_unstamped(run_cli, graph):
    graph.users.append(make_user("u1"))
    graph.contacts.extend(
        [
            make_contact("c1", azure_id="u-gone", givenName="Jane", surname="Doe"),  # orphan
            make_contact("c2", givenName="Bob", surname="Smith"),  # unstamped
        ]
    )
    code, _, _ = run_cli("prune", "--no-require-email", "--orphans", "--apply", stdin="DELETE 1 PRUNE\n")
    assert code == 0
    assert {c["id"] for c in graph.contacts} == {"c2"}


# ============================================================================
# delete — wholesale contact deletion (default = unstamped only)
# ============================================================================


def test_delete_no_unstamped_is_noop(run_cli, graph):
    graph.contacts.append(make_contact("c1", azure_id="u1"))
    code, out, _ = run_cli("delete", "--apply")
    assert code == 0
    assert "Nothing to delete" in out
    assert len(graph.contacts) == 1  # untouched


def test_delete_dry_run_lists_unstamped_but_does_not_delete(run_cli, graph):
    graph.contacts.extend(
        [
            make_contact("c1", azure_id="u1"),  # stamped, safe
            make_contact("c2", name="Manually Added"),  # unstamped
            make_contact("c3", name="Vendor Contact"),  # unstamped
        ]
    )
    code, out, _ = run_cli("delete")
    assert code == 0
    assert "2 unstamped" in out
    assert "Manually Added" in out
    assert "Vendor Contact" in out
    assert "Dry run" in out
    assert len(graph.contacts) == 3  # nothing deleted


def test_delete_apply_with_correct_confirmation_deletes_only_unstamped(run_cli, graph):
    graph.contacts.extend(
        [
            make_contact("c1", azure_id="u1"),  # stamped, must survive
            make_contact("c2", name="Manually Added"),  # unstamped
        ]
    )
    code, out, _ = run_cli("delete", "--apply", stdin="DELETE 1 UNSTAMPED\n")
    assert code == 0
    assert {c["id"] for c in graph.contacts} == {"c1"}
    assert "deleted=1" in out


def test_delete_apply_with_wrong_confirmation_aborts(run_cli, graph):
    graph.contacts.extend(
        [
            make_contact("c1", azure_id="u1"),
            make_contact("c2", name="Manually Added"),
        ]
    )
    code, out, _ = run_cli("delete", "--apply", stdin="yes\n")
    assert code == 0
    assert len(graph.contacts) == 2
    assert "Aborted" in out


def test_delete_all_dry_run_lists_every_contact(run_cli, graph):
    graph.contacts.extend(
        [
            make_contact("c1", azure_id="u1"),
            make_contact("c2", name="Manually Added"),
        ]
    )
    code, out, _ = run_cli("delete", "--all")
    assert code == 0
    assert "ALL marked for deletion" in out
    assert "Dry run" in out
    assert len(graph.contacts) == 2  # nothing deleted


def test_delete_all_apply_with_correct_confirmation_deletes_everything(run_cli, graph):
    graph.contacts.extend(
        [
            make_contact("c1", azure_id="u1"),  # stamped, would be safe under default delete
            make_contact("c2", name="Manually Added"),
            make_contact("c3", azure_id="u3"),
        ]
    )
    code, out, _ = run_cli("delete", "--all", "--apply", stdin="DELETE 3 ALL\n")
    assert code == 0
    assert graph.contacts == []
    assert "deleted=3" in out


def test_delete_all_on_empty_mailbox_is_noop(run_cli, graph):
    code, out, _ = run_cli("delete", "--all", "--apply")
    assert code == 0
    assert "address book is already empty" in out


# ============================================================================
# remove-category
# ============================================================================


def test_remove_category_dry_run(run_cli, graph):
    graph.contacts.append(make_contact("c1", categories=["ad export", "keep"]))
    graph.master_categories.append({"id": "m1", "displayName": "ad export"})
    code, out, _ = run_cli("remove-category", "ad export")
    assert code == 0
    assert graph.contacts[0]["categories"] == ["ad export", "keep"]  # unchanged
    assert graph.master_categories  # unchanged
    assert "Dry run" in out


def test_remove_category_apply_strips_and_deletes_master(run_cli, graph):
    graph.contacts.extend(
        [
            make_contact("c1", categories=["ad export", "keep"]),
            make_contact("c2", categories=["AD EXPORT"]),  # case-insensitive match
            make_contact("c3", categories=["keep"]),
        ]
    )
    graph.master_categories.append({"id": "m1", "displayName": "ad export"})
    code, _, _ = run_cli("remove-category", "ad export", "--apply")
    assert code == 0
    assert graph.contacts[0]["categories"] == ["keep"]
    assert graph.contacts[1]["categories"] == []
    assert graph.contacts[2]["categories"] == ["keep"]
    assert graph.master_categories == []


def test_remove_category_multiple_names_positional(run_cli, graph):
    graph.contacts.append(make_contact("c1", categories=["a", "b", "c"]))
    code, _, _ = run_cli("remove-category", "a", "b", "--apply")
    assert code == 0
    assert graph.contacts[0]["categories"] == ["c"]


def test_remove_category_dedupes_contact_appearing_in_default_and_subfolder(run_cli, graph):
    """A contact id that appears in BOTH the default folder and a subfolder
    must be PATCHed at most once. The `seen_ids` filter in
    `_galpal/commands/categories.py:run_remove_categories` exists exactly
    for this case; without a regression test, dropping the filter silently
    double-PATCHes (and on a real mailbox could trigger 409 conflicts on
    the second PATCH).
    """
    shared = make_contact("c1", categories=["x"])
    graph.contacts.append(shared)
    graph.contact_folders.append({"id": "f1", "displayName": "Sub"})
    graph.folder_contacts["f1"] = [shared]  # same id in default + subfolder
    code, _, _ = run_cli("remove-category", "x", "--apply")
    assert code == 0
    # Exactly one PATCH /me/contacts/c1 should have been issued.
    patches = []
    for method, url, params in graph.calls:
        if method == "POST" and url.endswith("/$batch"):
            patches.extend(r for r in params["requests"] if r["method"] == "PATCH")
    assert len(patches) == 1, f"expected 1 PATCH for c1, got {len(patches)}"


# ============================================================================
# remove-folder + list-folders
# ============================================================================


def test_list_folders_prints_each_folder_with_count(run_cli, graph):
    graph.contact_folders.extend(
        [
            {"id": "f1", "displayName": "Imported"},
            {"id": "f2", "displayName": "Lists"},
        ]
    )
    graph.folder_contacts["f1"] = [make_contact("x")]
    graph.folder_contacts["f2"] = [make_contact("y"), make_contact("z")]
    code, out, _ = run_cli("list-folders")
    assert code == 0
    assert "Imported" in out and "contacts=1" in out
    assert "Lists" in out and "contacts=2" in out


def test_remove_folder_dry_run(run_cli, graph):
    graph.contact_folders.append({"id": "f1", "displayName": "Junk"})
    code, out, _ = run_cli("remove-folder", "Junk")
    assert code == 0
    assert graph.contact_folders  # unchanged
    assert "Dry run" in out


def test_remove_folder_apply_deletes(run_cli, graph):
    graph.contact_folders.extend(
        [
            {"id": "f1", "displayName": "Junk"},
            {"id": "f2", "displayName": "Keep"},
        ]
    )
    code, _, _ = run_cli("remove-folder", "Junk", "--apply")
    assert code == 0
    assert [f["displayName"] for f in graph.contact_folders] == ["Keep"]


def test_remove_folder_unknown_name_warns(run_cli, graph):
    graph.contact_folders.append({"id": "f1", "displayName": "Real"})
    code, out, _ = run_cli("remove-folder", "Nonexistent")
    assert code == 0
    assert "No folder found" in out


# ============================================================================
# CLI / argparse
# ============================================================================


def test_no_args_errors_with_required_command(run_cli, graph):
    """No subcommand → argparse exits non-zero with the required-command message.

    Subcommands are required; there's no implicit default. A bare invocation should
    produce a clear error rather than silently doing something destructive-adjacent.
    """
    graph.users.append(make_user("u1"))
    code, _, err = run_cli()
    assert code == 2
    assert "the following arguments are required" in err
    assert "COMMAND" in err
    # Nothing was written.
    assert graph.contacts == []


def test_unknown_subcommand_errors_with_choices_and_full_help(run_cli):
    code, _, err = run_cli("snyc")
    assert code == 2
    assert "invalid choice: 'snyc'" in err
    # Full top-level help is dumped after the error: every subcommand listed.
    for cmd in ("pull", "audit", "dedupe", "prune", "delete", "remove-category", "remove-folder", "list-folders"):
        assert cmd in err


def test_unknown_option_on_subcommand_dumps_full_help(run_cli):
    code, _, err = run_cli("pull", "--bogus")
    assert code == 2
    assert "unrecognized arguments: --bogus" in err
    # --bogus surfaces at the top parser, so the top-level help (with COMMAND) is shown.
    assert "COMMAND" in err
    assert "audit" in err and "prune" in err


def test_remove_category_missing_name_dumps_subcommand_help(run_cli):
    code, _, err = run_cli("remove-category")
    assert code == 2
    assert "the following arguments are required: NAME" in err
    # Subcommand-scoped help: shows the subcommand's positional + flags, not other commands.
    assert "remove-category" in err
    assert "--apply" in err
    assert "list-folders" not in err  # other subcommands NOT shown


def test_top_level_help_lists_all_subcommands(run_cli):
    code, out, _ = run_cli("--help")
    assert code == 0
    for cmd in (
        "pull",
        "audit",
        "dedupe",
        "prune",
        "delete",
        "remove-category",
        "remove-folder",
        "list-folders",
    ):
        assert cmd in out


def test_subcommand_help_shows_only_that_subcommand(run_cli):
    code, out, _ = run_cli("prune", "--help")
    assert code == 0
    assert "--require-full-name" in out
    assert "--apply" in out
    assert "--batch-size" not in out  # pull-only; not in prune


def test_short_help_flag_works(run_cli):
    code, out, _ = run_cli("-h")
    assert code == 0
    assert "COMMAND" in out
