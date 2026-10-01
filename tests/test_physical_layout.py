"""Human grouping authority stays separate from naming and library identity."""
from datetime import timedelta
import importlib
import importlib.util
import json

import pytest
from sqlalchemy import delete, event, select, update
from sqlalchemy.exc import IntegrityError

from app import models
from app.migrations import STARTUP_COMPATIBILITY_VERSION, migrate_schema_at_startup
from app.models import CatalogTitle, Video, VideoVariantGroup
from test_physical_naming import NOW, attach_metadata, collection, session, title


KINDS = {
    "own_folder", "shared_ova", "shared_specials", "direct_season",
    "extras_openings_endings", "extras_promo", "extras_bonus", "extras_menus",
}


def layout_api():
    assert importlib.util.find_spec("app.physical_layout_service") is not None, (
        "Physical Layout foundation is missing"
    )
    return (importlib.import_module("app.physical_layout_service"),
            importlib.import_module("app.physical_layout"))


def add_video(session, owner, ordinal=None, *, kind=None, filename=None, **values):
    index = len(owner.videos) + 1
    filename = filename or f"{kind or owner.effective_part_type} {index:02d}.mkv"
    video = Video(
        catalog_title=owner, catalog_collection=owner.collection,
        root_folder=owner.collection.relative_root_path,
        relative_path=f"{owner.relative_root_path}/{index}-{filename}",
        filename=filename, size=1, mtime_ns=1,
        file_type=kind or owner.effective_part_type,
        episode_number_manual_override=ordinal, **values,
    )
    session.add(video)
    session.flush()
    return video


def resolve(session, owner):
    service, resolver = layout_api()
    return resolver.resolve_physical_layout(service.load_physical_layout_context(session), owner.id)


def domain_snapshot(session):
    session.flush()
    return {table.name: tuple(tuple(row) for row in session.execute(
        table.select().order_by(*table.primary_key.columns)
    )) for table in models.Base.metadata.sorted_tables if table.name != "physical_layout_choices"}


def test_version_7_upgrade_is_empty_additive_and_second_startup_noop(session):
    assert STARTUP_COMPATIBILITY_VERSION == 8
    root = collection(session)
    owner = title(session, root, kind="ova")
    add_video(session, owner)
    session.commit()
    before = domain_snapshot(session)
    engine = session.get_bind()
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE physical_layout_choices")
        connection.exec_driver_sql("PRAGMA user_version = 7")
    assert migrate_schema_at_startup(engine) is True
    assert domain_snapshot(session) == before
    assert session.connection().exec_driver_sql("SELECT count(*) FROM physical_layout_choices").scalar() == 0
    assert session.connection().exec_driver_sql("PRAGMA user_version").scalar() == 8
    assert migrate_schema_at_startup(engine) is False
    assert not session.connection().exec_driver_sql("PRAGMA foreign_key_check").all()


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_explicit_choice_roundtrip_reset_does_not_mutate_other_domains(session, kind):
    service, _ = layout_api()
    root = collection(session)
    owner = title(session, root, kind="ova")
    add_video(session, owner)
    session.commit()
    before = domain_snapshot(session)
    choice = service.confirm_physical_layout_choice(session, owner, kind, now=NOW)
    session.commit()
    assert domain_snapshot(session) == before
    result = resolve(session, owner)
    assert result.effective_layout_kind == kind
    assert result.authority == "human_choice" and result.basis_matches is True
    assert result.choice.id == choice.id
    service.reset_physical_layout_choice(session, owner)
    session.commit()
    assert domain_snapshot(session) == before
    assert resolve(session, owner).choice is None
    assert resolve(session, owner).requires_human_decision


def test_confirming_default_is_explicit_and_pending_writes_do_not_flush(session):
    service, _ = layout_api()
    root = collection(session)
    owner = title(session, root, kind="ova", metadata=False)
    add_video(session, owner)
    session.commit()
    root.local_title = "unflushed unrelated edit"
    assert resolve(session, owner).effective_layout_kind == "shared_ova"
    first = service.confirm_physical_layout_choice(session, owner, "shared_ova", now=NOW)
    assert service.reconfirm_physical_layout_choice(session, owner, now=NOW + timedelta(days=1)) is first
    assert resolve(session, owner).authority == "human_choice"
    assert session.connection().exec_driver_sql("SELECT local_title FROM catalog_collections").scalar() == "Show"
    assert session.connection().exec_driver_sql("SELECT count(*) FROM physical_layout_choices").scalar() == 0
    service.reset_physical_layout_choice(session, owner)
    assert not session.new
    assert resolve(session, owner).authority == "derived_default"
    service.confirm_physical_layout_choice(session, owner, "shared_ova", now=NOW)
    session.commit()
    assert len(list(session.scalars(select(models.PhysicalLayoutChoice)))) == 1


@pytest.mark.parametrize("bad_kind", ["auto", "inherit", "interviews", "other", "", None])
def test_invalid_kind_rejected_without_flush(session, bad_kind):
    service, _ = layout_api()
    root = collection(session)
    owner = title(session, root, kind="ova", metadata=False)
    session.commit()
    root.local_title = "pending"
    with pytest.raises(ValueError):
        service.confirm_physical_layout_choice(session, owner, bad_kind)
    assert not session.new
    assert session.connection().exec_driver_sql("SELECT local_title FROM catalog_collections").scalar() == "Show"


@pytest.mark.parametrize("kind", ["season", "film"])
def test_main_and_film_titles_are_not_layout_owners(session, kind):
    service, _ = layout_api()
    root = collection(session)
    owner = title(session, root, kind=kind)
    session.commit()
    with pytest.raises(ValueError):
        service.confirm_physical_layout_choice(session, owner, "own_folder")
    with pytest.raises(ValueError):
        service.confirm_physical_layout_choice(session, root, "own_folder")


def test_constraints_owner_unique_kind_json_and_delete_cascade(session):
    service, _ = layout_api()
    root = collection(session)
    owner = title(session, root, kind="ova", metadata=False)
    session.commit()
    choice = service.confirm_physical_layout_choice(session, owner, "shared_ova", now=NOW)
    session.commit()
    assert set(models.PhysicalLayoutChoice.__table__.columns.keys()) == {
        "id", "catalog_title_id", "layout_kind", "confirmed_at", "basis_snapshot_json",
    }
    for changes in ({"layout_kind": "auto"}, {"basis_snapshot_json": "invalid"},
                    {"catalog_title_id": None}, {"catalog_title_id": 99999}):
        with pytest.raises(IntegrityError), session.begin_nested():
            session.execute(update(models.PhysicalLayoutChoice).where(
                models.PhysicalLayoutChoice.id == choice.id).values(**changes))
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(models.PhysicalLayoutChoice(catalog_title_id=owner.id, layout_kind="shared_ova",
                                              confirmed_at=NOW, basis_snapshot_json="{}"))
        session.flush()
    session.execute(delete(CatalogTitle).where(CatalogTitle.id == owner.id))
    session.commit()
    assert session.scalar(select(models.PhysicalLayoutChoice)) is None


@pytest.mark.parametrize("kind,filename,expected", [
    ("ova", "OVA.mkv", "shared_ova"), ("special", "Special.mkv", "shared_specials"),
    ("bonus", "NCOP 01.mkv", "extras_openings_endings"),
    ("bonus", "NCED 01.mkv", "extras_openings_endings"),
    ("bonus", "OP 01.mkv", "extras_openings_endings"),
    ("bonus", "ED 01.mkv", "extras_openings_endings"),
    ("bonus", "PV 01.mkv", "extras_promo"), ("bonus", "CM 01.mkv", "extras_promo"),
    ("bonus", "Bonus 01.mkv", "extras_bonus"), ("bonus", "Menu 01.mkv", "extras_menus"),
])
def test_safe_shared_defaults_do_not_create_rows(session, kind, filename, expected):
    root = collection(session)
    owner = title(session, root, kind=kind, metadata=False)
    add_video(session, owner, filename=filename)
    session.commit()
    result = resolve(session, owner)
    assert result.effective_layout_kind == expected
    assert result.authority == "derived_default" and not result.requires_human_decision
    assert (len(session.new), len(session.dirty), len(session.deleted)) == (0, 0, 0)
    assert session.scalar(select(models.PhysicalLayoutChoice)) is None


def test_mini_dra_and_oresuki_require_human_choice(session):
    service, _ = layout_api()
    root = collection(session, "Kobayashi")
    mini = title(session, root, "Mini Dra", kind="bonus", season=2)
    for n in range(1, 14):
        add_video(session, mini, n)
    oresuki = title(session, collection(session, "Oresuki"), "OVA", kind="ova")
    add_video(session, oresuki)
    session.commit()
    for owner, count, review_class, kind in (
        (mini, 13, "OWN_FOLDER_STRONG", "own_folder"),
        (oresuki, 1, "OWN_FOLDER_OPTIONAL", "shared_ova"),
    ):
        before = resolve(session, owner)
        assert before.logical_count == count
        assert before.own_metadata_identity is not None
        assert before.review_class == review_class and before.requires_human_decision
        assert before.effective_layout_kind is None
        service.confirm_physical_layout_choice(session, owner, kind, now=NOW)
        after = resolve(session, owner)
        assert after.effective_layout_kind == kind and after.basis_matches
    assert resolve(session, mini).attachment.season_number == 2


@pytest.mark.parametrize("representation", ["media_parts", "variants", "duplicate"])
def test_multiple_physical_representations_are_one_logical_item(session, representation):
    root = collection(session)
    owner = title(session, root, "Arifureta OVA", kind="ova", season=2)
    first = add_video(session, owner, 1)
    second = add_video(session, owner, 1)
    if representation == "media_parts":
        first.media_part_number, second.media_part_number = 1, 2
    elif representation == "variants":
        first.video_variant_group = VideoVariantGroup(catalog_title=owner, manual_label="BD", verified_at=NOW)
        second.video_variant_group = VideoVariantGroup(catalog_title=owner, manual_label="TV", verified_at=NOW)
    else:
        second.duplicate_of_video_id = first.id
    session.commit()
    result = resolve(session, owner)
    assert result.logical_count == 1
    assert result.review_class == "OWN_FOLDER_OPTIONAL"
    assert len(owner.videos) == 2


def test_interview_special_is_ambiguous_until_human_choice_without_retyping(session):
    service, _ = layout_api()
    root = collection(session, "Isekai Maou")
    owner = title(session, root, "Interview - Isekai Maou", season=None, kind="special", metadata=False)
    for n in (1, 2):
        add_video(session, owner, n, filename=f"[Anipakku] Isekai Maou [IV{n:02d}].mkv")
    session.commit()
    result = resolve(session, owner)
    assert result.review_class == "NEEDS_HUMAN_LAYOUT"
    assert result.effective_layout_kind is None and result.requires_human_decision
    before = domain_snapshot(session)
    for kind in ("shared_specials", "extras_bonus"):
        service.confirm_physical_layout_choice(session, owner, kind, now=NOW)
        assert resolve(session, owner).effective_layout_kind == kind
    assert domain_snapshot(session) == before


@pytest.mark.parametrize("name,season,metadata", [
    ("Ansatsu episode 0", 1, True), ("Arifureta Prologue", 1, True),
    ("Fate Initium Iter", 1, True), ("SAO Reflection", 4, False),
])
def test_authoritative_story_preview_stays_direct_season(session, name, season, metadata):
    root = collection(session)
    title(session, root, "Part 1", season=season, part=1, kind="part", metadata=False)
    title(session, root, "Part 2", season=season, part=2, kind="part", metadata=False)
    owner = title(session, root, name, season=season, kind="preview", metadata=metadata)
    add_video(session, owner, filename=f"{name}.mkv")
    session.commit()
    result = resolve(session, owner)
    assert result.effective_layout_kind == "direct_season"
    assert result.review_class == "DIRECT_SEASON" and not result.requires_human_decision
    assert result.attachment.kind == "season" and result.attachment.season_number == season
    assert owner.effective_part_number is None


def test_uncertain_mixed_profile_and_root_preview_are_unresolved(session):
    root = collection(session)
    mixed = title(session, root, "Unknown extras", kind="bonus", metadata=False)
    add_video(session, mixed, 1, filename="NCOP 01.mkv")
    add_video(session, mixed, 1, filename="CM 01.mkv")
    preview = title(session, root, "Preview", kind="preview", season=None, metadata=False)
    add_video(session, preview)
    session.commit()
    for owner in (mixed, preview):
        assert resolve(session, owner).requires_human_decision
        assert resolve(session, owner).effective_layout_kind is None


def test_basis_tracks_identity_attachment_profile_and_bucket_not_volatile_text(session):
    service, resolver = layout_api()
    root = collection(session)
    owner = title(session, root, kind="ova")
    video = add_video(session, owner, 1)
    session.commit()
    choice = service.confirm_physical_layout_choice(session, owner, "own_folder", now=NOW)
    session.commit()
    basis = choice.basis_snapshot_json
    assert json.loads(basis)["version"] == 1
    owner.metadata_record.title_romaji = "Renamed"
    owner.metadata_record.synonyms_json = '["New synonym"]'
    owner.metadata_record.episode_count = 16
    owner.manual_display_title = "UI name"
    from app.physical_naming_service import confirm_physical_naming_choice
    confirm_physical_naming_choice(session, owner, "New physical name", "custom", now=NOW)
    session.commit()
    assert resolve(session, owner).basis_matches
    assert resolver.create_basis_snapshot(service.load_physical_layout_context(session), owner.id) == basis
    for mutate in (
        lambda: setattr(owner, "season_number_manual", 2),
        lambda: setattr(video, "content_type_manual", "special"),
        lambda: setattr(owner.external_links[0], "external_id", "relinked"),
        lambda: setattr(owner, "metadata_status", "unlinked"),
        lambda: setattr(owner, "catalog_collection_id", collection(session, "Moved").id),
    ):
        mutate()
        session.commit()
        stale = resolve(session, owner)
        assert stale.basis_matches is False and stale.effective_layout_kind is None
        assert stale.choice.layout_kind == "own_folder"
        service.reconfirm_physical_layout_choice(session, owner, now=NOW + timedelta(days=2))
        session.commit()
        assert resolve(session, owner).basis_matches
    assert choice.confirmed_at.replace(tzinfo=NOW.tzinfo) == NOW + timedelta(days=2)


def test_count_bucket_changes_on_single_to_multi_not_new_physical_copy(session):
    service, _ = layout_api()
    root = collection(session)
    owner = title(session, root, kind="ova")
    primary = add_video(session, owner, 1)
    session.commit()
    choice = service.confirm_physical_layout_choice(session, owner, "own_folder", now=NOW)
    session.commit()
    add_video(session, owner, 1, duplicate_of_video_id=primary.id)
    session.commit()
    assert resolve(session, owner).basis_matches
    add_video(session, owner, 2)
    session.commit()
    assert resolve(session, owner).basis_matches is False
    service.reconfirm_physical_layout_choice(session, owner, now=NOW)
    session.commit()
    basis = choice.basis_snapshot_json
    add_video(session, owner, 3)
    session.commit()
    assert resolve(session, owner).basis_matches
    assert choice.basis_snapshot_json == basis


def test_candidate_link_and_invalid_snapshot_are_not_authority(session):
    service, _ = layout_api()
    root = collection(session)
    owner = title(session, root, kind="ova")
    add_video(session, owner)
    owner.external_links[0].verified_at = None
    session.commit()
    assert resolve(session, owner).own_metadata_identity is None
    assert resolve(session, owner).effective_layout_kind == "shared_ova"
    choice = service.confirm_physical_layout_choice(session, owner, "shared_ova", now=NOW)
    session.commit()
    choice.basis_snapshot_json = '{"version":true}'
    session.commit()
    assert resolve(session, owner).basis_matches is False


def test_batch_read_model_has_bounded_queries_and_zero_resolver_sql(session):
    service, resolver = layout_api()
    root = collection(session)
    for n in range(60):
        owner = title(session, root, f"OVA {n}", kind="ova", metadata=False)
        add_video(session, owner)
    session.commit()
    session.expire_all()
    statements = []
    engine = session.get_bind()
    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", record)
    try:
        context = service.load_physical_layout_context(session)
        count = len(statements)
        assert count <= 12
        results = [resolver.resolve_physical_layout(context, id) for id in context.titles]
        assert len(statements) == count
        assert all(result.effective_layout_kind == "shared_ova" for result in results)
        assert (len(session.new), len(session.dirty), len(session.deleted)) == (0, 0, 0)
        assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    finally:
        event.remove(engine, "before_cursor_execute", record)


def test_unflushed_title_move_can_be_confirmed_without_domain_flush(session):
    service, _ = layout_api()
    old_root, new_root = collection(session), collection(session, "Moved")
    owner = title(session, old_root, kind="ova", metadata=False)
    add_video(session, owner)
    session.commit()
    owner.catalog_collection_id = new_root.id
    service.confirm_physical_layout_choice(session, owner, "shared_ova", now=NOW)
    assert session.connection().exec_driver_sql("SELECT catalog_collection_id FROM catalog_titles").scalar() == old_root.id
    assert json.loads(next(iter(session.new)).basis_snapshot_json)["collection_id"] == new_root.id


def test_stale_owner_changed_to_main_can_still_reset_its_choice(session):
    service, _ = layout_api()
    root = collection(session)
    owner = title(session, root, kind="ova", metadata=False)
    session.commit()
    service.confirm_physical_layout_choice(session, owner, "shared_ova", now=NOW)
    session.commit()
    owner.part_type_manual = "season"
    session.commit()
    assert resolve(session, owner).basis_matches is False
    assert resolve(session, owner).requires_human_decision
    service.reset_physical_layout_choice(session, owner)
    session.commit()
    assert session.scalar(select(models.PhysicalLayoutChoice)) is None


@pytest.mark.parametrize("representation", ["incomplete_media_parts", "unnumbered_variants", "unknown_duplicate"])
def test_uncertain_representations_do_not_become_multi_item_work(session, representation):
    root = collection(session)
    owner = title(session, root, kind="ova")
    first = add_video(session, owner, filename="First.mkv")
    second = add_video(session, owner, filename="Second.mkv")
    if representation == "incomplete_media_parts":
        first.media_part_number, second.media_part_number = 1, 3
    elif representation == "unnumbered_variants":
        first.video_variant_group = VideoVariantGroup(catalog_title=owner, manual_label="BD", verified_at=NOW)
        second.video_variant_group = VideoVariantGroup(catalog_title=owner, manual_label="TV", verified_at=NOW)
    else:
        second.duplicate_of_video_id = first.id
    session.commit()
    result = resolve(session, owner)
    assert result.logical_count is None
    assert result.review_class == "NEEDS_HUMAN_LAYOUT"
    assert "logical_count_unresolved" in result.diagnostics


def test_explicit_unnumbered_duplicate_confirmation_is_one_logical_identity(session):
    root = collection(session)
    owner = title(session, root, kind="ova")
    first = add_video(session, owner, filename="First.mkv")
    second = add_video(session, owner, filename="Second.mkv", duplicate_of_video_id=first.id,
                       duplicate_confirmation_kind="unnumbered_supplementary_same_content")
    session.commit()
    assert resolve(session, owner).logical_count == 1


def test_metadata_unlink_service_retains_stale_layout_row(session):
    from app.metadata.service import unlink_title_metadata
    service, _ = layout_api()
    root = collection(session)
    owner = title(session, root, kind="ova")
    add_video(session, owner)
    session.commit()
    choice = service.confirm_physical_layout_choice(session, owner, "shared_ova", now=NOW)
    session.commit()
    unlink_title_metadata(session, owner)
    session.commit()
    assert resolve(session, owner).choice.id == choice.id
    assert resolve(session, owner).basis_matches is False


def test_automatic_preview_is_not_confirmed_story_preview(session):
    root = collection(session)
    owner = title(session, root, "Preview", kind="preview", metadata=False)
    owner.hierarchy_manual_override = False
    owner.part_type_manual = owner.season_number_manual = owner.hierarchy_verified_at = None
    add_video(session, owner, filename="Unidentified preview.mkv")
    session.commit()
    result = resolve(session, owner)
    assert result.effective_layout_kind is None
    assert result.review_class == "NEEDS_HUMAN_LAYOUT"


@pytest.mark.parametrize("known_ordinal", [None, 1])
def test_unnumbered_same_type_row_does_not_prove_another_logical_identity(session, known_ordinal):
    root = collection(session)
    owner = title(session, root, kind="ova")
    add_video(session, owner, known_ordinal, filename="First.mkv")
    add_video(session, owner, filename="Second.mkv")
    session.commit()
    result = resolve(session, owner)
    assert result.logical_count is None
    assert result.review_class == "NEEDS_HUMAN_LAYOUT"


def test_main_season_recap_does_not_become_a_layout_owner(session):
    from app.physical_layout import LAYOUT_TITLE_TYPES
    root = collection(session)
    owner = title(session, root, kind="season")
    add_video(session, owner, kind="episode", filename="Episode 01.mkv")
    add_video(session, owner, kind="recap", filename="Recap 01.5.mkv")
    session.commit()
    assert owner.effective_part_type not in LAYOUT_TITLE_TYPES
    result = resolve(session, owner)
    assert result.choice is None and result.review_class is None
    assert not result.requires_human_decision


def test_safe_shared_grouping_does_not_require_numbering_resolution(session):
    root = collection(session)
    owner = title(session, root, kind="special", metadata=False)
    add_video(session, owner, filename="First.mkv")
    add_video(session, owner, filename="Second.mkv")
    session.commit()
    result = resolve(session, owner)
    assert result.logical_count is None
    assert result.effective_layout_kind == "shared_specials"
    assert not result.requires_human_decision


def test_issue_279_interview_layout_survives_release_text_change_without_retyping(session):
    from app.catalog import effective_video_content_type
    service, _ = layout_api()
    root = collection(session, "Isekai Maou")
    owner = title(session, root, "Interview - Isekai Maou", season=None, kind="special", metadata=False)
    videos = [add_video(session, owner, n, kind="other",
                        filename=f"[Anipakku] Isekai Maou [IV{n:02d}][Ma10p_1080p].mkv") for n in (1, 2)]
    session.commit()
    assert resolve(session, owner).review_class == "NEEDS_HUMAN_LAYOUT"
    service.confirm_physical_layout_choice(session, owner, "extras_bonus", now=NOW)
    session.commit()
    # A later canonical rename removes the IV release marker and folder label.
    # Neither is grouping identity, so the human decision must stay effective.
    for n, video in enumerate(videos, 1):
        video.filename = f"Isekai Maou - Special {n:02d}.mkv"
    owner.local_title = "Specials"
    session.commit()
    session.expire_all()
    result = resolve(session, owner)
    assert result.basis_matches is True
    assert result.effective_layout_kind == "extras_bonus" and result.authority == "human_choice"
    assert owner.effective_part_type == "special"
    assert all(video.content_type_manual is None for video in owner.videos)
    assert {effective_video_content_type(video, owner) for video in owner.videos} == {"special"}


@pytest.mark.parametrize("primary_location", ["other_title", "other_type_same_title"])
def test_unresolved_numbered_duplicate_evidence_leaves_count_unknown(session, primary_location):
    root = collection(session)
    owner = title(session, root, "OVA", kind="ova")
    if primary_location == "other_title":
        elsewhere = title(session, root, "OVA S2", kind="ova", season=2)
        primary = add_video(session, elsewhere, 1)
    else:
        primary = add_video(session, owner, kind="special", filename="Special.mkv")
    add_video(session, owner, 1, filename="OVA 01.mkv", duplicate_of_video_id=primary.id)
    session.commit()
    session.expire_all()
    result = resolve(session, owner)
    assert result.logical_count is None
    assert result.review_class == "NEEDS_HUMAN_LAYOUT"
    assert "logical_count_unresolved" in result.diagnostics


def test_mini_dra_production_shape_own_folder_survives_naming_and_provider_count(session):
    from app.physical_naming_service import confirm_physical_naming_choice
    service, resolver = layout_api()
    root = collection(session, "Kobayashi-san Chi no Maid Dragon")
    title(session, root, "Season 2", season=2, metadata=False)
    mini = title(session, root, "Season 2 Shorts (L21)", kind="bonus", season=2)
    mini.metadata_record.episode_count = 13
    for n in range(1, 14):
        add_video(session, mini, n, kind="other",
                  filename=f"Kobayashi-san Chi no Maid Dragon S2 Shorts - {n:02d}.mkv")
    session.commit()
    before = resolve(session, mini)
    assert (before.review_class, before.recommendation, before.logical_count) == (
        "OWN_FOLDER_STRONG", "own_folder", 13)
    assert before.authority == "unresolved" and before.effective_layout_kind is None
    assert before.requires_human_decision
    assert before.attachment == resolver.LayoutAttachment("season", 2)
    choice = service.confirm_physical_layout_choice(session, mini, "own_folder", now=NOW)
    session.commit()
    confirmed = resolve(session, mini)
    assert (confirmed.effective_layout_kind, confirmed.authority, confirmed.basis_matches) == (
        "own_folder", "human_choice", True)
    assert set(json.loads(choice.basis_snapshot_json)) == {
        "version", "owner_title_id", "collection_id", "attachment", "grouping_profile",
        "own_metadata_identity", "logical_count_state",
    }
    # Provider 16 vs local 13 is completeness information, not layout identity;
    # the own folder name comes from Physical Naming and is never snapshotted.
    mini.metadata_record.episode_count = 16
    confirm_physical_naming_choice(session, mini, "Mini Dra", "custom", now=NOW)
    session.commit()
    renamed = resolve(session, mini)
    assert (renamed.effective_layout_kind, renamed.basis_matches) == ("own_folder", True)
    for text in ("Mini Dra", "Shorts", "Romaji", "Kobayashi"):
        assert text not in choice.basis_snapshot_json


def test_arifureta_ova_media_parts_are_one_optional_singleton(session):
    service, resolver = layout_api()
    root = collection(session, "Arifureta Shokugyou de Sekai Saikyou")
    owner = title(session, root, "OVA - Arifureta", kind="ova", season=2)
    for part in (1, 2):
        add_video(session, owner, 1, filename=f"Arifureta Shokugyou de Sekai Saikyou S2 - OVA P{part}.mkv",
                  media_part_number=part)
    session.commit()
    result = resolve(session, owner)
    assert (result.logical_count, result.review_class) == (1, "OWN_FOLDER_OPTIONAL")
    assert len(owner.videos) == 2
    basis = resolver.create_basis_snapshot(service.load_physical_layout_context(session), owner.id)
    assert json.loads(basis)["logical_count_state"] == "singleton"


def test_oresuki_unnumbered_singleton_is_optional_and_can_be_shared(session):
    from app.supplementary import supplementary_ordinal
    service, _ = layout_api()
    root = collection(session, "Ore wo Suki nano wa Omae dake ka yo")
    owner = title(session, root, "OVA - Ore wo Suki", kind="ova", season=1)
    video = add_video(session, owner, filename="Ore wo Suki nano wa Omae dake ka yo - OVA 13 - 15.mkv")
    session.commit()
    assert supplementary_ordinal(video, owner).number is None
    before = resolve(session, owner)
    assert (before.logical_count, before.review_class) == (1, "OWN_FOLDER_OPTIONAL")
    assert before.requires_human_decision and before.effective_layout_kind is None
    service.confirm_physical_layout_choice(session, owner, "shared_ova", now=NOW)
    session.commit()
    after = resolve(session, owner)
    assert (after.effective_layout_kind, after.authority, after.basis_matches) == (
        "shared_ova", "human_choice", True)


@pytest.mark.parametrize("start,target", [(1, None), (None, 2), (1, 2)])
def test_hierarchy_move_keeps_choice_kind_and_opens_basis_mismatch(session, start, target):
    from app.hierarchy_review import set_manual_title_hierarchy
    service, _ = layout_api()
    root = collection(session)
    for season in (1, 2):
        title(session, root, f"Season {season}", season=season, metadata=False)
    owner = title(session, root, "OVA", season=start, kind="ova", metadata=False)
    add_video(session, owner, 1)
    session.commit()
    choice = service.confirm_physical_layout_choice(session, owner, "shared_ova", now=NOW)
    session.commit()
    set_manual_title_hierarchy(owner, season_number=target, season_label=None, part_type="ova",
                               sort_order=None, hierarchy_verified=True)
    session.commit()
    session.expire_all()
    result = resolve(session, owner)
    assert result.choice.id == choice.id and result.choice.layout_kind == "shared_ova"
    assert result.basis_matches is False and result.effective_layout_kind is None
    assert result.attachment.season_number == target
    assert session.get(models.PhysicalLayoutChoice, choice.id).confirmed_at.replace(tzinfo=NOW.tzinfo) == NOW
