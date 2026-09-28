"""Physical text is human authority, independent of catalog domain writes."""
from datetime import datetime, timezone
import importlib
import importlib.util
import json

import pytest
from sqlalchemy import delete, event, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import Base, make_engine
from app import models
from app.models import CatalogCollection, CatalogTitle, ExternalTitleLink, TitleMetadata, Video


NOW = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)


def naming_api():
    assert importlib.util.find_spec("app.physical_naming_service") is not None, (
        "V6 physical naming persistence is missing"
    )
    return (
        importlib.import_module("app.physical_naming_service"),
        importlib.import_module("app.physical_naming"),
    )


@pytest.fixture
def session():
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def collection(session, name="Show"):
    result = CatalogCollection(
        local_title=name, normalized_local_title=name.casefold(), relative_root_path=name,
        manual_display_title="UI root",
    )
    session.add(result)
    session.flush()
    return result


def title(session, owner, name="Season 1", *, season=1, part=None, kind="season", metadata=True):
    result = CatalogTitle(
        collection=owner, local_title=name, normalized_local_title=name.casefold(),
        relative_root_path=f"{owner.relative_root_path}/{name}", part_type=kind,
        season_number=season, hierarchy_manual_override=True, part_type_manual=kind,
        season_number_manual=season, part_number_manual=part, hierarchy_verified_at=NOW,
    )
    session.add(result)
    session.flush()
    if metadata:
        attach_metadata(result, str(result.id))
    session.flush()
    return result


def attach_metadata(owner, external_id, romaji="Full Romaji: OVA Part 2?"):
    owner.metadata_status = "linked_manual"
    owner.external_links = [ExternalTitleLink(
        provider="anilist", external_id=external_id, is_primary=True, is_manual=True,
        verified_at=NOW, lifecycle_state="active", match_method="manual_search",
    )]
    owner.metadata_record = TitleMetadata(
        display_title="UI metadata", title_romaji=romaji, title_english="English",
        synonyms_json='["Short"]', metadata_provider="anilist", metadata_external_id=external_id,
    )


def domain_snapshot(session):
    session.flush()
    return {
        table.name: tuple(tuple(row) for row in session.execute(
            table.select().order_by(*table.primary_key.columns)
        ))
        for table in Base.metadata.sorted_tables
        if table.name != "physical_naming_choices"
    }


@pytest.mark.parametrize("scope", ["collection", "title"])
@pytest.mark.parametrize("kind", ["romaji", "english", "synonym", "current", "custom"])
def test_confirm_roundtrip_isolated_from_all_domain_rows(session, scope, kind):
    service, resolver = naming_api()
    root = collection(session)
    main = title(session, root)
    group = models.VideoVariantGroup(catalog_title=main, manual_label="BD", verified_at=NOW)
    video = Video(
        relative_path="Show/01.mkv", root_folder="Show", filename="01.mkv", size=1,
        mtime_ns=1, catalog_title=main, catalog_collection=root, video_variant_group=group,
        season_episode_number=1, episode_number_manual_override=1, media_part_number=1,
        recap_episode_number_manual_tenths=55, content_type_manual="episode",
        duplicate_status_manual="suspected",
    )
    session.add(video)
    session.flush()
    session.add(models.ManualSplitRuleVideo(catalog_title=main, video=video))
    session.commit()
    before = domain_snapshot(session)
    owner = root if scope == "collection" else main
    choice = service.confirm_physical_naming_choice(
        session, owner, "  人の名前: OVA Part 2?  ", kind, now=NOW,
    )
    session.commit()
    assert domain_snapshot(session) == before
    session.expire_all()
    stored = session.get(models.PhysicalNamingChoice, choice.id)
    assert (stored.physical_text, stored.choice_kind) == ("人の名前: OVA Part 2?", kind)
    assert stored.confirmed_at.replace(tzinfo=timezone.utc) == NOW
    context = service.load_physical_naming_context(session)
    resolved = resolver.resolve_physical_name(context, scope, owner.id)
    assert resolved.authority == "human_choice"
    assert resolved.effective_text == "人の名前: OVA Part 2?"
    assert resolved.basis_matches is True
    service.reset_physical_naming_choice(session, owner)
    session.commit()
    assert session.scalar(select(models.PhysicalNamingChoice)) is None
    assert domain_snapshot(session) == before
    reset = resolver.resolve_physical_name(service.load_physical_naming_context(session), scope, owner.id)
    assert reset.authority == "derived_default"


@pytest.mark.parametrize("value", ["", " \u2003 ", "x" * 501, "a\0b", "a\nb", "a\tb", "a\x7fb", "a\x85b", "a\ud800b", "\na\n"])
def test_invalid_text_is_rejected_without_flushing_or_mutating(session, value):
    service, _ = naming_api()
    root = collection(session)
    session.commit()
    root.local_title = "pending unrelated edit"
    with pytest.raises(ValueError):
        service.confirm_physical_naming_choice(session, root, value, "custom")
    assert not session.new
    with session.no_autoflush:
        assert session.connection().exec_driver_sql("SELECT local_title FROM catalog_collections").scalar() == "Show"


def test_logical_validation_preserves_unicode_and_punctuation():
    service, _ = naming_api()
    assert service.validate_physical_text("  e\u0301 日本語: / ?  ") == "e\u0301 日本語: / ?"
    assert service.validate_physical_text("界" * 500) == "界" * 500
    assert service.validate_physical_text("👩\u200d💻") == "👩\u200d💻"


@pytest.mark.parametrize("value", ["a\u2028b", "a\u2029b"])
def test_unicode_line_and_paragraph_separators_are_not_single_line_text(value):
    service, _ = naming_api()
    with pytest.raises(ValueError):
        service.validate_physical_text(value)


def test_pending_confirm_reconfirm_and_reset_do_not_flush_domain_edits(session):
    service, resolver = naming_api()
    root = collection(session)
    session.commit()
    root.local_title = "unflushed edit"
    first = service.confirm_physical_naming_choice(session, root, "First", "custom")
    again = service.confirm_physical_naming_choice(session, root, "Second", "current")
    assert again is first
    context = service.load_physical_naming_context(session)
    assert resolver.resolve_physical_name(context, "collection", root.id).effective_text == "Second"
    assert session.connection().exec_driver_sql("SELECT local_title FROM catalog_collections").scalar() == "Show"
    assert session.connection().exec_driver_sql("SELECT count(*) FROM physical_naming_choices").scalar() == 0
    service.reset_physical_naming_choice(session, root)
    assert not session.new
    assert resolver.resolve_physical_name(service.load_physical_naming_context(session), "collection", root.id).choice is None
    service.confirm_physical_naming_choice(session, root, "Stored", "custom")
    session.commit()
    service.reset_physical_naming_choice(session, root)
    assert resolver.resolve_physical_name(service.load_physical_naming_context(session), "collection", root.id).choice is None
    service.confirm_physical_naming_choice(session, root, "Reconfirmed", "custom")
    session.commit()
    assert len(list(session.scalars(select(models.PhysicalNamingChoice)))) == 1


def test_expired_owner_does_not_autoflush_unrelated_domain_edits(session):
    service, _ = naming_api()
    root = collection(session)
    main = title(session, root)
    session.commit()
    root.local_title = "pending edit"
    session.expire(main)
    service.confirm_physical_naming_choice(session, main, "Prefix", "custom")
    assert session.connection().exec_driver_sql("SELECT local_title FROM catalog_collections").scalar() == "Show"
    service.reset_physical_naming_choice(session, main)
    assert session.connection().exec_driver_sql("SELECT count(*) FROM physical_naming_choices").scalar() == 0


def test_title_move_retains_choice_and_root_choices_are_independent(session):
    from app.hierarchy_review import move_titles_to_collection
    service, resolver = naming_api()
    source = collection(session)
    main = title(session, source)
    target = collection(session, "Target")
    service.confirm_physical_naming_choice(session, source, "Short root", "custom")
    choice = service.confirm_physical_naming_choice(session, main, "Title prefix", "custom")
    session.commit()
    saved_basis = choice.basis_snapshot_json
    move_titles_to_collection(session, target.id, [main.id])
    session.commit()
    assert choice.catalog_title_id == main.id
    assert choice.basis_snapshot_json == saved_basis
    context = service.load_physical_naming_context(session)
    assert resolver.resolve_physical_name(context, "title", main.id).effective_text == "Title prefix"
    assert resolver.resolve_physical_name(context, "title", main.id).basis_matches is True
    assert resolver.resolve_physical_name(context, "collection", source.id).effective_text == "Short root"
    assert ("collection", target.id) not in context.choices


@pytest.mark.parametrize("loaded", [False, True])
@pytest.mark.parametrize("scope", ["collection", "title"])
def test_orm_owner_delete_cascades_with_loaded_or_unloaded_choice(session, loaded, scope):
    service, _ = naming_api()
    root = collection(session)
    owner = root if scope == "collection" else title(session, root, metadata=False)
    service.confirm_physical_naming_choice(session, owner, "Human", "custom")
    session.commit()
    session.expire(owner, ["physical_naming_choice"])
    if loaded:
        assert owner.physical_naming_choice is not None
    session.delete(owner)
    session.commit()
    assert session.scalar(select(models.PhysicalNamingChoice)) is None


@pytest.mark.parametrize("collection_owner,title_owner", [(None, None), (True, True)])
def test_database_rejects_invalid_owner_xor(session, collection_owner, title_owner):
    naming_api()
    root = collection(session)
    main = title(session, root)
    session.commit()
    session.add(models.PhysicalNamingChoice(
        catalog_collection_id=root.id if collection_owner else None,
        catalog_title_id=main.id if title_owner else None, physical_text="Choice",
        choice_kind="custom", confirmed_at=NOW, basis_snapshot_json='{"version":1}',
    ))
    with pytest.raises(IntegrityError):
        session.flush()


@pytest.mark.parametrize("scope", ["collection", "title"])
def test_unique_owner_and_bulk_delete_cascade(session, scope):
    service, _ = naming_api()
    root = collection(session)
    owner = root if scope == "collection" else title(session, root, metadata=False)
    service.confirm_physical_naming_choice(session, owner, "One", "custom")
    session.commit()
    fk = "catalog_collection_id" if scope == "collection" else "catalog_title_id"
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(models.PhysicalNamingChoice(
            **{fk: owner.id}, physical_text="Two", choice_kind="custom", confirmed_at=NOW,
            basis_snapshot_json='{"version":1}',
        ))
        session.flush()
    session.execute(delete(type(owner)).where(type(owner).id == owner.id))
    session.commit()
    assert session.scalar(select(models.PhysicalNamingChoice)) is None


@pytest.mark.parametrize("field,value", [("choice_kind", "inherited"), ("physical_text", ""), ("physical_text", "x" * 501), ("basis_snapshot_json", "not json")])
def test_database_constraints_reject_incomplete_choice(session, field, value):
    naming_api()
    root = collection(session)
    session.commit()
    fields = dict(catalog_collection_id=root.id, physical_text="Choice", choice_kind="custom",
                  confirmed_at=NOW, basis_snapshot_json='{"version":1}')
    fields[field] = value
    session.add(models.PhysicalNamingChoice(**fields))
    with pytest.raises(IntegrityError):
        session.flush()


def test_default_and_explicit_romaji_are_distinct_and_basis_is_deterministic(session):
    service, resolver = naming_api()
    root = collection(session)
    main = title(session, root)
    session.commit()
    context = service.load_physical_naming_context(session)
    result = resolver.resolve_physical_name(context, "title", main.id)
    assert result.authority == "derived_default"
    assert result.choice is None
    assert result.effective_text == "Full Romaji: OVA Part 2?"
    choice = service.confirm_physical_naming_choice(session, main, result.effective_text, "romaji", now=NOW)
    session.commit()
    basis = json.loads(choice.basis_snapshot_json)
    assert basis == {
        "version": 1, "owner": {"scope": "title", "id": main.id},
        "metadata": {"state": "confirmed", "identities": [{"provider": "anilist", "external_id": str(main.id)}]},
        "structure": None, "source_title_id": None,
    }
    assert choice.basis_snapshot_json == json.dumps(basis, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert resolver.resolve_physical_name(service.load_physical_naming_context(session), "title", main.id).authority == "human_choice"


def test_refresh_relink_unlink_and_reconfirm_preserve_human_snapshot(session):
    service, resolver = naming_api()
    root = collection(session)
    main = title(session, root)
    choice = service.confirm_physical_naming_choice(session, main, "Short", "synonym", now=NOW)
    session.commit()
    saved_basis = choice.basis_snapshot_json
    main.metadata_record.title_romaji = "Refreshed Romaji"
    main.metadata_record.synonyms_json = "[]"
    session.commit()
    result = resolver.resolve_physical_name(service.load_physical_naming_context(session), "title", main.id)
    assert (result.effective_text, result.default_candidate, result.basis_matches) == ("Short", "Refreshed Romaji", True)
    assert choice.basis_snapshot_json == saved_basis
    main.external_links[0].external_id = "999"
    main.metadata_record.metadata_external_id = "999"
    session.commit()
    result = resolver.resolve_physical_name(service.load_physical_naming_context(session), "title", main.id)
    assert result.basis_matches is False
    assert "metadata_identity_changed" in result.diagnostics
    assert result.effective_text == "Short"
    later = datetime(2026, 9, 29, tzinfo=timezone.utc)
    reconfirmed = service.confirm_physical_naming_choice(session, main, "Short", "custom", now=later)
    session.commit()
    assert reconfirmed.id == choice.id
    assert reconfirmed.confirmed_at.replace(tzinfo=timezone.utc) == later
    assert resolver.resolve_physical_name(service.load_physical_naming_context(session), "title", main.id).basis_matches is True
    main.external_links[0].is_primary = False
    main.external_links[0].lifecycle_state = "unlinked"
    main.metadata_status = "unlinked"
    session.commit()
    result = resolver.resolve_physical_name(service.load_physical_naming_context(session), "title", main.id)
    assert result.effective_text == "Short"
    assert result.basis_matches is False
    assert "metadata_identity_changed" in result.diagnostics


@pytest.mark.parametrize("name", ["Bananya", "Monogatari"])
def test_ambiguous_root_confirmation_does_not_create_metadata_anchor(session, name):
    service, resolver = naming_api()
    root = collection(session, name)
    title(session, root, season=2)
    if name == "Monogatari":
        # Production shape: manual S2 main title beside three confirmed films.
        for film in ("Kizumonogatari", "Kizumonogatari II", "Kizumonogatari III"):
            title(session, root, film, kind="film", season=None)
    session.commit()
    before = domain_snapshot(session)
    result = resolver.resolve_physical_name(service.load_physical_naming_context(session), "collection", root.id)
    assert result.authority == "ambiguous"
    assert result.default_candidate is None
    service.confirm_physical_naming_choice(session, root, name, "current")
    session.commit()
    result = resolver.resolve_physical_name(service.load_physical_naming_context(session), "collection", root.id)
    assert (result.authority, result.effective_text, result.basis_matches) == ("human_choice", name, True)
    assert domain_snapshot(session) == before


def test_film_root_and_s1_p1_adapter_never_choose_by_id(session):
    service, resolver = naming_api()
    root = collection(session)
    title(session, root, "S2", season=2)
    first = title(session, root, "S1 P1", part=1)
    title(session, root, "S1 P2", part=2)
    first.metadata_record.title_romaji = "Correct source"
    film_root = collection(session, "Film")
    title(session, film_root, "Movie", kind="film", season=None)
    session.commit()
    context = service.load_physical_naming_context(session)
    assert resolver.resolve_physical_name(context, "collection", root.id).effective_text == "Correct source"
    root_resolution = resolver.resolve_physical_name(context, "collection", root.id)
    assert root_resolution.metadata_source_title_id == first.id
    assert root_resolution.metadata_identity == ("anilist", str(first.id))
    assert resolver.resolve_physical_name(context, "collection", film_root.id).authority == "derived_default"


def test_unconfirmed_or_mismatched_metadata_cannot_supply_default(session):
    service, resolver = naming_api()
    root = collection(session)
    main = title(session, root)
    main.external_links[0].is_manual = False
    session.commit()
    assert resolver.resolve_physical_name(service.load_physical_naming_context(session), "title", main.id).authority == "unavailable"
    main.external_links[0].is_manual = True
    main.metadata_record.metadata_external_id = "wrong"
    session.commit()
    result = resolver.resolve_physical_name(service.load_physical_naming_context(session), "title", main.id)
    assert result.effective_text is None
    assert "metadata_payload_mismatch" in result.diagnostics


def test_inheritance_does_not_persist_and_split_season_choice_is_a_snapshot(session):
    service, resolver = naming_api()
    root = collection(session)
    main = title(session, root)
    child = title(session, root, "NC S1", kind="bonus", metadata=False)
    extra = title(session, root, "Root bonus", kind="bonus", season=None, metadata=False)
    p1 = title(session, root, "S2 P1", season=2, part=1)
    title(session, root, "S2 P2", season=2, part=2)
    split_child = title(session, root, "NC S2", kind="bonus", season=2, metadata=False)
    session.commit()
    context = service.load_physical_naming_context(session)
    for owner in (child, extra):
        result = resolver.resolve_physical_name(context, "title", owner.id)
        assert result.authority == "inherited"
        assert result.effective_text == "Full Romaji: OVA Part 2?"
    result = resolver.resolve_physical_name(context, "title", split_child.id)
    assert result.authority == "ambiguous"
    assert "season_only_multiple_parts" in result.diagnostics
    assert session.scalar(select(models.PhysicalNamingChoice)) is None
    service.confirm_physical_naming_choice(session, p1, "Tensura S2", "custom")
    snapshot = service.confirm_physical_naming_choice(
        session, split_child, "Tensura S2", "parent_prefix", source_title_id=p1.id,
    )
    session.commit()
    assert json.loads(snapshot.basis_snapshot_json)["source_title_id"] == p1.id
    assert (split_child.effective_season_number, split_child.effective_part_number) == (2, None)
    service.confirm_physical_naming_choice(session, p1, "New parent text", "custom")
    session.commit()
    result = resolver.resolve_physical_name(service.load_physical_naming_context(session), "title", split_child.id)
    assert (result.effective_text, result.basis_matches) == ("Tensura S2", True)
    service.reset_physical_naming_choice(session, split_child)
    session.commit()
    assert resolver.resolve_physical_name(service.load_physical_naming_context(session), "title", split_child.id).authority == "ambiguous"


def test_parent_prefix_snapshot_survives_explicit_delete_of_its_source_parent(tmp_path):
    from app.hierarchy_review import delete_empty_local_title
    service, resolver = naming_api()
    engine = make_engine(f"sqlite:///{tmp_path / 'naming.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        root = collection(session)
        title(session, root)
        p1 = title(session, root, "S2 P1", season=2, part=1)
        title(session, root, "S2 P2", season=2, part=2)
        split_child = title(session, root, "NC S2", kind="bonus", season=2, metadata=False)
        session.commit()
        choice = service.confirm_physical_naming_choice(
            session, split_child, "Full Romaji: OVA Part 2?", "parent_prefix", source_title_id=p1.id,
        )
        session.commit()
        ids = (root.id, p1.id, split_child.id, choice.id)
    with Session(engine) as session:
        delete_empty_local_title(session, ids[0], ids[1])
        session.commit()
    with Session(engine) as session:
        assert session.get(CatalogTitle, ids[1]) is None
        stored = session.get(models.PhysicalNamingChoice, ids[3])
        assert (stored.catalog_title_id, stored.physical_text, stored.choice_kind) == (
            ids[2], "Full Romaji: OVA Part 2?", "parent_prefix",
        )
        assert json.loads(stored.basis_snapshot_json)["source_title_id"] == ids[1]
        result = resolver.resolve_physical_name(service.load_physical_naming_context(session), "title", ids[2])
    engine.dispose()
    assert (result.authority, result.effective_text) == ("human_choice", "Full Romaji: OVA Part 2?")
    assert (result.basis_matches, result.diagnostics) == (False, ("structural_context_changed",))


def test_parent_basis_mismatch_propagates_dependency_without_child_authority(session):
    service, resolver = naming_api()
    root = collection(session)
    parent = title(session, root, season=2)
    child = title(session, root, "NC", kind="bonus", season=2, metadata=False)
    service.confirm_physical_naming_choice(session, parent, "Parent", "custom")
    session.commit()
    parent.external_links[0].is_primary = False
    parent.external_links[0].lifecycle_state = "unlinked"
    session.commit()
    result = resolver.resolve_physical_name(service.load_physical_naming_context(session), "title", child.id)
    assert result.authority == "inherited"
    assert result.dependency == ("title", parent.id)
    assert result.ready is False
    assert "dependency_unresolved" in result.diagnostics
    assert result.choice is None
    assert len(list(session.scalars(select(models.PhysicalNamingChoice)))) == 1


@pytest.mark.parametrize("corrupt", ["version", "owner_id"])
def test_json_valid_basis_with_coerced_values_fails_safe_to_review(tmp_path, corrupt):
    # Python equality treats JSON true as 1; a payload the service never writes
    # must not be silently accepted as a matched version-1 basis.
    service, resolver = naming_api()
    engine = make_engine(f"sqlite:///{tmp_path / 'naming.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        root = collection(session)
        title(session, root)
        choice = service.confirm_physical_naming_choice(session, root, "Human root", "custom", now=NOW)
        session.commit()
        assert root.id == 1
        basis = json.loads(choice.basis_snapshot_json)
        if corrupt == "version":
            basis["version"] = True
        else:
            basis["owner"]["id"] = True
        session.execute(update(models.PhysicalNamingChoice).where(
            models.PhysicalNamingChoice.id == choice.id,
        ).values(basis_snapshot_json=json.dumps(basis)))
        session.commit()
    with Session(engine) as session:
        result = resolver.resolve_physical_name(service.load_physical_naming_context(session), "collection", 1)
    engine.dispose()
    assert result.effective_text == "Human root"
    assert (result.basis_matches, result.diagnostics, result.ready) == (False, ("invalid_basis_snapshot",), False)


def test_resolver_is_sql_free_and_batch_loader_is_bounded(session):
    service, resolver = naming_api()
    for number in range(25):
        root = collection(session, f"Show{number}")
        title(session, root)
        title(session, root, "NC", kind="bonus", metadata=False)
    session.commit()
    statements = []
    def record(c, cursor, statement, parameters, context, many):
        statements.append(statement)
    event.listen(session.bind, "before_cursor_execute", record)
    try:
        context = service.load_physical_naming_context(session)
        load_count = len(statements)
        assert load_count <= 6
        for owner in context.titles.values():
            resolver.resolve_physical_name(context, "title", owner.id)
        assert len(statements) == load_count
        assert all(st.lstrip().upper().startswith("SELECT") for st in statements)
        assert not session.new and not session.dirty and not session.deleted
    finally:
        event.remove(session.bind, "before_cursor_execute", record)
