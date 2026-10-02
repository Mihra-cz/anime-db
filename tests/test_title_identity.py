"""Shared locators never collapse distinct logical owners; v8 stays UNIQUE."""
import json

import pytest
from sqlalchemy.orm import Session

import app.hierarchy_rebuild as rebuild
from app.hierarchy import derive_library_hierarchy
from app.hierarchy_assignment import automatic_assignment_title
from app.hierarchy_review import collection_grouping_authority_targets
from app.models import CatalogCollection, CatalogTitle, CollectionGroupingDecision, ManualSplitRuleVideo
from app.title_identity import ExistingTitleRef, PlannedTitleRef, TitleLocatorIndex
from test_hierarchy_rebuild import _engine, _collection, _title, _video


@pytest.fixture(params=[("ReZero", 2, None), ("SAO", 4, "preview"), ("Slime", 2, "bonus")])
def shared_graph(request):
    name, season, supplementary = request.param
    engine = _engine()
    with Session(engine) as session:
        collection = _collection(f"Anime/{name}")
        for part in (1, 2):
            title = _title(
                f"Anime/{name}/@manual/P{part}", collection, f"Part {part}",
                part_type="season", season_number=season, part_number=part,
                hierarchy_manual_override=True, part_type_manual="season",
                season_number_manual=season, part_number_manual=part,
                sort_order=part,
            )
            video = _video(
                f"Anime/{name}/Season {season:02}/episode-{part}.mkv",
                title=title, collection=collection, file_type="episode",
                episode_number_manual_override=1,
            )
            session.add(video)
            session.flush()
            session.add(ManualSplitRuleVideo(catalog_title=title, video=video))
        if supplementary:
            title = _title(
                f"Anime/{name}/Supplement", collection, "Reflection" if name == "SAO" else "NC",
                part_type=supplementary, season_number=season,
                hierarchy_manual_override=True, part_type_manual=supplementary,
                season_number_manual=season,
            )
            for filename, kind in (("Reflection.mkv", "preview"),) if name == "SAO" else (("NCOP.mkv", "ncop"), ("NCED.mkv", "nced")):
                session.add(_video(
                    f"Anime/{name}/Supplement/{filename}", title=title, collection=collection,
                    file_type="other", content_type_manual=kind,
                ))
        session.commit()
        state = rebuild._load_state(session)
        for title in state[1]:
            for link in title.manual_split_rule_videos:
                _ = link.video.manual_split_rule_videos
                _ = link.catalog_title
        for video in state[2]:
            _ = video.manual_split_rule_videos
        session.expunge_all()
        for title in state[1]:
            if title.effective_part_number is not None:
                title.relative_root_path = f"Anime/{name}/Season {season:02}"
        yield session, state
    engine.dispose()


def test_rebuild_shared_locator_keeps_both_owner_ids(shared_graph, monkeypatch):
    session, state = shared_graph
    monkeypatch.setattr(rebuild, "_load_state", lambda _: state)
    plan = rebuild.build_hierarchy_rebuild_plan(session)
    assert {item.title_id for item in plan.titles} == {title.id for title in state[1]}


def test_explicit_selectors_keep_distinct_projected_title_owners(shared_graph):
    _session, (collections, titles, videos) = shared_graph
    hierarchy = derive_library_hierarchy([v.relative_path for v in videos])
    intents, _decisions, blockers = rebuild._build_assignment_intents(collections, titles, videos, hierarchy)
    specs = rebuild._build_title_specs(titles, hierarchy, intents)
    identities = rebuild._build_collection_identities(collections, hierarchy, specs, intents)
    projection = rebuild._build_projection(collections, videos, identities, specs, intents, set())
    assert {s.original.id for s in specs.values()} == {t.id for t in titles}
    assert {v.id: v.catalog_title_id for v in projection.videos.values()} == {v.id: v.catalog_title_id for v in videos}
    assert not any(b.prevents_apply for b in blockers)
    for title in projection.titles.values():
        if title.local_title in {"Reflection", "NC"}:
            assert title.effective_part_number is None


def test_rebuild_shared_locator_owner_result_is_order_independent(shared_graph, monkeypatch):
    session, state = shared_graph
    results = []
    for titles in (state[1], list(reversed(state[1]))):
        monkeypatch.setattr(rebuild, "_load_state", lambda _, titles=titles: (state[0], titles, state[2]))
        plan = rebuild.build_hierarchy_rebuild_plan(session)
        results.append((
            {item.title_id for item in plan.titles},
            {a.video_id: a.target_title_ref for a in plan.video_assignments},
            plan.source_fingerprint,
        ))
    assert results[0] == results[1]
    assert results[0][0] == {title.id for title in state[1]}


def test_unprotected_locator_collision_blocks_without_removing_owners(shared_graph, monkeypatch):
    session, state = shared_graph
    for title in state[1]:
        title.hierarchy_manual_override = False
        title.part_type_manual = None
        title.season_number_manual = None
        title.part_number_manual = None
        title.manual_split_rule_videos.clear()
    for video in state[2]:
        video.manual_split_rule_videos.clear()
    monkeypatch.setattr(rebuild, "_load_state", lambda _: state)
    plan = rebuild.build_hierarchy_rebuild_plan(session)
    assert any(b.code == "ambiguous_title_locator" and b.prevents_apply for b in plan.blockers)
    assert all(item.action != "remove" for item in plan.titles)
    assert {a.video_id: a.target_title_ref for a in plan.video_assignments} == {
        video.id: ExistingTitleRef(video.catalog_title_id) for video in state[2]
    }


def test_ambiguous_locator_assignment_never_selects_first_owner(shared_graph):
    _session, (_collections, titles, videos) = shared_graph
    hierarchy = derive_library_hierarchy([v.relative_path for v in videos])
    identity = next(iter(hierarchy.values()))
    candidates = [t for t in titles if t.effective_part_number is not None]
    assert automatic_assignment_title(identity, {identity.title.relative_root_path: candidates}) is None


def test_grouping_ambiguous_legacy_path_never_deduplicates_distinct_owners(shared_graph):
    _session, (collections, titles, _videos) = shared_graph
    path = next(t.relative_root_path for t in titles if t.effective_part_number is not None)
    target = _collection("@manual/target", id=999)
    decision = CollectionGroupingDecision(
        id=1, decision="merged", selected_title_paths_json=json.dumps([path, path]),
        target_collection_path=target.relative_root_path,
    )
    class Rows(list):
        def all(self): return self
    class DetachedSource:
        def scalars(self, query):
            entity = query.column_descriptions[0]["entity"]
            return Rows({CatalogTitle: titles, CatalogCollection: [*collections, target], CollectionGroupingDecision: [decision]}[entity])
    assert collection_grouping_authority_targets(DetachedSource()) == {}


def test_locator_index_refresh_keeps_id_and_retires_old_evidence(shared_graph):
    _session, (_collections, titles, _videos) = shared_graph
    parts = [t for t in titles if t.effective_part_number is not None]
    index = TitleLocatorIndex(parts)
    old_path = parts[0].relative_root_path
    parts[0].relative_root_path = "@manual/changed-handle"
    index.add(parts[0])
    assert index.unique("@manual/changed-handle").id == parts[0].id
    assert [t.id for t in index.candidates(old_path)] == [parts[1].id]


def test_planned_handles_are_deterministic_and_bind_on_apply(monkeypatch):
    engine = _engine()
    with Session(engine) as session:
        session.add_all([_video(f"Anime/New/Season 1/E{n:02}.mkv") for n in (1, 2)])
        session.commit()
        first = rebuild.build_hierarchy_rebuild_plan(session)
        state = rebuild._load_state(session)
        with monkeypatch.context() as patch:
            patch.setattr(rebuild, "_load_state", lambda _: (state[0], state[1], list(reversed(state[2]))))
            second = rebuild.build_hierarchy_rebuild_plan(session)
        refs = {a.target_title_ref for a in first.video_assignments}
        assert refs == {a.target_title_ref for a in second.video_assignments}
        assert all(isinstance(ref, PlannedTitleRef) for ref in refs)
        assert first.source_fingerprint == second.source_fingerprint
        assert ExistingTitleRef(1) != PlannedTitleRef(1)
        assert rebuild.apply_hierarchy_rebuild_plan(session, first).applied
        state = rebuild._load_state(session)
        assert len(state[1]) == 1
        assert {v.catalog_title_id for v in state[2]} == {state[1][0].id}
    engine.dispose()


def test_apply_verification_checks_owner_ids_with_shared_locators(shared_graph, monkeypatch):
    session, (collections, titles, videos) = shared_graph
    monkeypatch.setattr(rebuild, "_load_state", lambda _: (collections, titles, videos))
    plan = rebuild.build_hierarchy_rebuild_plan(session)
    hierarchy = derive_library_hierarchy([v.relative_path for v in videos])
    intents, _decisions, _blockers = rebuild._build_assignment_intents(collections, titles, videos, hierarchy)
    specs = rebuild._build_title_specs(titles, hierarchy, intents)
    identities = rebuild._build_collection_identities(collections, hierarchy, specs, intents)
    projection = rebuild._build_projection(collections, videos, identities, specs, intents, set())
    projected = (list(projection.collections.values()), list(projection.titles.values()), list(projection.videos.values()))
    monkeypatch.setattr(rebuild, "_load_state", lambda _: projected)
    rebuild._verify_applied_plan(session, plan, {}, {})
    part_titles = [t for t in projected[1] if t.effective_part_number is not None]
    video = next(v for v in projected[2] if v.catalog_title_id == part_titles[0].id)
    video.catalog_title = part_titles[1]
    video.catalog_title_id = part_titles[1].id
    with pytest.raises(rebuild.HierarchyRebuildError, match="assignment"):
        rebuild._verify_applied_plan(session, plan, {}, {})


def test_apply_verification_detects_undeleted_owner_after_binding_map_cleanup(monkeypatch):
    from dataclasses import replace
    engine = _engine()
    with Session(engine) as session:
        title = _title("Obsolete/title", _collection("Obsolete"))
        session.add(title)
        session.commit()
        plan = rebuild.build_hierarchy_rebuild_plan(session)
        assert plan.titles[0].action == "remove"
        plan = replace(plan, collections=())
        monkeypatch.setattr(rebuild, "_load_state", lambda _: ([], [title], []))
        with pytest.raises(rebuild.HierarchyRebuildError, match="Obsolete title"):
            rebuild._verify_applied_plan(session, plan, {}, {}, {ExistingTitleRef(999): 999})


def test_root_creation_does_not_reclassify_unrelated_collection_owner(tmp_path):
    from test_unassigned_videos import _app, _endpoint, _manual_title
    from app.models import Video
    web_app = _app(tmp_path, "root-owner.db")
    with web_app.state.sessions() as session:
        video = _video("Root.mkv", file_type="other")
        session.add(video)
        session.flush()
        collection = _collection(f"@root/{video.id}")
        unrelated = _manual_title(collection, "Unrelated Special", "@manual/unrelated", "special")
        session.add(unrelated)
        session.commit()
        video_id, unrelated_id = video.id, unrelated.id
    _endpoint(web_app, "/root-videos/{video_id}/new-title")(
        video_id, display_title="Requested Film", part_type="film",
    )
    with web_app.state.sessions() as session:
        assert session.get(Video, video_id).catalog_title_id != unrelated_id
        unrelated = session.get(CatalogTitle, unrelated_id)
        assert unrelated.local_title == "Unrelated Special"
        assert unrelated.effective_part_type == "special"


def test_root_creation_reuses_current_owner_id_after_virtual_locator_change(tmp_path):
    from test_unassigned_videos import _app, _endpoint, _manual_title
    from app.models import Video
    web_app = _app(tmp_path, "root-locator.db")
    with web_app.state.sessions() as session:
        video = _video("Root.mkv", file_type="other")
        session.add(video)
        session.flush()
        collection = _collection(f"@root/{video.id}")
        owner = _manual_title(collection, "Current Film", "@manual/changed-locator", "film")
        video.catalog_title = owner
        video.catalog_collection = collection
        session.commit()
        video_id, owner_id = video.id, owner.id
    _endpoint(web_app, "/root-videos/{video_id}/new-title")(
        video_id, display_title="Current Film", part_type="film",
    )
    with web_app.state.sessions() as session:
        assert session.get(Video, video_id).catalog_title_id == owner_id
        assert len(session.query(CatalogTitle).all()) == 1


@pytest.mark.parametrize("selector_in_collection", [True, False])
def test_root_creation_conflicting_selector_and_current_owner_requires_review(tmp_path, selector_in_collection):
    from fastapi import HTTPException
    from test_unassigned_videos import _app, _endpoint, _manual_title
    web_app = _app(tmp_path, "root-conflict.db")
    with web_app.state.sessions() as session:
        video = _video("Root.mkv", file_type="other")
        session.add(video)
        session.flush()
        collection = _collection(f"@root/{video.id}")
        current = _manual_title(collection, "Current Film", "@manual/current", "film")
        selector_collection = collection if selector_in_collection else _collection("@manual/foreign")
        selected = _manual_title(selector_collection, "Selected Special", "@manual/selected", "special")
        video.catalog_title = current
        video.catalog_collection = collection
        session.add(ManualSplitRuleVideo(video=video, catalog_title=selected))
        session.commit()
        video_id, current_id, selected_id = video.id, current.id, selected.id
    with pytest.raises(HTTPException) as caught:
        _endpoint(web_app, "/root-videos/{video_id}/new-title")(
            video_id, display_title="Requested Film", part_type="film",
        )
    assert caught.value.status_code == 400
    with web_app.state.sessions() as session:
        video = session.get(rebuild.Video, video_id)
        assert video.catalog_title_id == current_id
        assert {link.catalog_title_id for link in video.manual_split_rule_videos} == {selected_id}
        assert video.catalog_title.local_title == "Current Film"


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("owner_locator", ["Show/Season 02", "Show/old"])
def test_startup_conflicting_inference_preserves_current_owner_for_review(monkeypatch, reverse, owner_locator, tmp_path):
    from dataclasses import replace
    from sqlalchemy import create_engine
    from app.database import Base
    import app.migrations as migrations
    engine = create_engine(f"sqlite:///{tmp_path / 'startup.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = _collection("Show")
        title = _title(owner_locator, collection, part_type="season", season_number=2)
        videos = [_video(f"Show/Season 02/E{n:02}.mkv", title=title, collection=collection) for n in (1, 2)]
        session.add_all(videos)
        session.commit()
        owner_id = title.id
        hierarchy = derive_library_hierarchy([v.relative_path for v in videos])
        original = hierarchy[videos[1].relative_path]
        hierarchy[videos[1].relative_path] = replace(original, title=replace(original.title, season_number=3))
        if reverse:
            hierarchy = dict(reversed(list(hierarchy.items())))
    monkeypatch.setattr(migrations, "derive_library_hierarchy", lambda _: hierarchy)
    migrations.migrate_schema(engine)
    with Session(engine) as session:
        state = rebuild._load_state(session)
        assert len(state[1]) == 1
        assert state[1][0].id == owner_id
        assert state[1][0].effective_season_number == 2
        assert state[0][0].hierarchy_status == "review_required"
        assert {v.catalog_title_id for v in state[2]} == {owner_id}


def test_grouping_boundary_keeps_historical_missing_reference(shared_graph):
    from app.hierarchy_review import resolve_grouping_owner_references
    _session, (collections, titles, _videos) = shared_graph
    titles[0].relative_root_path = "@manual/unique-owner"
    decision = CollectionGroupingDecision(id=1, decision="merged",
        target_collection_path=collections[0].relative_root_path,
        selected_title_paths_json=json.dumps([titles[0].relative_root_path, "@manual/historical-missing"]))
    result = resolve_grouping_owner_references(decision, TitleLocatorIndex(titles),
        {c.relative_root_path: c for c in collections})
    assert result.title_ids == (titles[0].id,)
    assert result.missing_paths == ("@manual/historical-missing",)
    assert result.ambiguous_paths == ()


def test_naming_layout_basis_does_not_use_title_locator(shared_graph):
    from app.physical_naming_service import physical_naming_context_from_models
    from app.physical_naming import create_basis_snapshot as naming_basis
    from app.physical_layout_service import physical_layout_context_from_models
    from app.physical_layout import create_basis_snapshot as layout_basis
    _session, (collections, titles, _videos) = shared_graph
    naming = physical_naming_context_from_models(collections, titles, [])
    layout = physical_layout_context_from_models(titles, [])
    before = {t.id: (naming_basis(naming, "title", t.id), layout_basis(layout, t.id)) for t in titles}
    for title in titles:
        title.relative_root_path = f"@manual/new-locator-{title.id}"
    naming = physical_naming_context_from_models(collections, titles, [])
    layout = physical_layout_context_from_models(titles, [])
    assert before == {t.id: (naming_basis(naming, "title", t.id), layout_basis(layout, t.id)) for t in titles}


def test_shared_locator_keeps_variant_media_part_and_duplicate_scopes(shared_graph):
    from app.numbering import DuplicateRelationState, duplicate_relation_state, logical_episode_partitions
    _session, (collections, titles, videos) = shared_graph
    copies = []
    for primary in list(videos):
        if primary.catalog_title.effective_part_number is None:
            continue
        primary.media_part_number = 1
        primary.video_variant_group_id = 100 + primary.catalog_title_id
        for offset, media_part, lane in ((1000, 2, 100), (2000, 1, 100), (3000, None, 200)):
            video = _video(
                primary.relative_path.replace(".mkv", f"-{offset}.mkv"),
                id=offset + primary.id, title=primary.catalog_title,
                collection=primary.catalog_collection, file_type="episode",
                catalog_title_id=primary.catalog_title_id,
                catalog_collection_id=primary.catalog_collection_id,
                episode_number_manual_override=1, media_part_number=media_part,
                video_variant_group_id=lane + primary.catalog_title_id,
            )
            if offset == 2000:
                video.duplicate_of = primary
                video.duplicate_of_video_id = primary.id
                copies.append(video)
            videos.append(video)
    hierarchy = derive_library_hierarchy([v.relative_path for v in videos])
    intents, _decisions, blockers = rebuild._build_assignment_intents(collections, titles, videos, hierarchy)
    assert not any(b.prevents_apply for b in blockers)
    specs = rebuild._build_title_specs(titles, hierarchy, intents)
    identities = rebuild._build_collection_identities(collections, hierarchy, specs, intents)
    projected = rebuild._build_projection(collections, videos, identities, specs, intents, set())
    for video in videos:
        clone = projected.videos[video.id]
        assert (clone.catalog_title_id, clone.video_variant_group_id, clone.media_part_number) == (
            video.catalog_title_id, video.video_variant_group_id, video.media_part_number,
        )
    for copy in copies:
        assert duplicate_relation_state(projected.videos[copy.id]) == DuplicateRelationState.VALID
    part_videos = [v for v in projected.videos.values() if v.catalog_title.effective_part_number is not None]
    assert len(logical_episode_partitions(part_videos)) == 2


def test_scanner_preserves_existing_owner_after_parent_move(tmp_path, monkeypatch):
    from app.scanner import scan_library
    from test_hierarchy_rebuild import PROBE_RESULT
    monkeypatch.setattr("app.scanner.service.probe_video", lambda *a, **k: PROBE_RESULT)
    engine = _engine()
    root = tmp_path / "media"
    for part in (1, 2):
        path = root / f"Show/Season 02/part-{part}.mkv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    with Session(engine) as session:
        collection = _collection("Show")
        expected = {}
        for part in (1, 2):
            title = _title(f"Show/@manual/Part-{part}", collection,
                part_type="season", season_number=2, part_number=part,
                hierarchy_manual_override=True, part_type_manual="season",
                season_number_manual=2, part_number_manual=part)
            video = _video(f"Show/Season 02/part-{part}.mkv", title=title, collection=collection,
                episode_number_manual_override=1, content_type_manual="episode")
            session.add(video)
            session.flush()
            expected[video.id] = title.id
        session.commit()
        result = scan_library(session, root)
        session.commit()
        assert result.created == 0
        state = rebuild._load_state(session)
        assert {v.id: v.catalog_title_id for v in state[2]} == expected
        assert {t.id for t in state[1]} == set(expected.values())


def test_scanner_new_file_ambiguous_locator_stays_unassigned(tmp_path, monkeypatch):
    import app.scanner.service as scanner
    from test_hierarchy_rebuild import PROBE_RESULT
    monkeypatch.setattr(scanner, "probe_video", lambda *a, **k: PROBE_RESULT)
    engine = _engine()
    root = tmp_path / "media"
    path = root / "Show/Season 02/E01.mkv"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"fixture")
    with Session(engine) as session:
        collection = _collection("Show")
        for part in (1, 2):
            session.add(_title(f"Show/@manual/{part}", collection, part_type="season",
                season_number=2, part_number=part, hierarchy_manual_override=True,
                part_type_manual="season", season_number_manual=2, part_number_manual=part))
        session.commit()
        def simulated_locator_index(owners):
            return TitleLocatorIndex(CatalogTitle(id=t.id, relative_root_path="Show/Season 02") for t in owners)
        monkeypatch.setattr(scanner, "TitleLocatorIndex", simulated_locator_index)
        scanner.scan_library(session, root)
        session.flush()
        state = rebuild._load_state(session)
        assert len(state[1]) == 2
        assert len(state[2]) == 1
        assert state[2][0].catalog_title_id is None
        assert collection.hierarchy_status == "review_required"


def test_conflicting_new_title_inference_is_blocked_instead_of_picking_first():
    from dataclasses import replace
    video_a = _video("Show/Season 02/E01.mkv", id=1)
    video_b = _video("Show/Season 02/E02.mkv", id=2)
    hierarchy = derive_library_hierarchy([video_a.relative_path, video_b.relative_path])
    original = hierarchy[video_b.relative_path]
    hierarchy[video_b.relative_path] = replace(original, title=replace(original.title, season_number=3))
    intents, _decisions, blockers = rebuild._build_assignment_intents([], [], [video_a, video_b], hierarchy)
    assert any(b.prevents_apply for b in blockers)
    assert all(i.title_ref is None for i in intents.values())


def test_rebuild_and_grouping_reads_are_bounded_and_select_only():
    from sqlalchemy import event
    engine = _engine()
    with Session(engine) as session:
        for size in (1, 20):
            for n in range(size):
                locator = f"Anime/Show-{size}-{n}"
                collection = _collection(locator)
                title = _title(f"{locator}/Season 1", collection, part_type="season",
                    season_number=1, hierarchy_manual_override=True,
                    part_type_manual="season", season_number_manual=1)
                video = _video(f"{locator}/Season 1/E01.mkv", title=title,
                    collection=collection, episode_number_manual_override=1)
                session.add_all([video, ManualSplitRuleVideo(catalog_title=title, video=video),
                    CollectionGroupingDecision(suggestion_key=locator, state_fingerprint="fixture", decision="merged",
                        selected_title_paths_json=json.dumps([title.relative_root_path]),
                        target_collection_path=locator)])
            session.commit()
            statements = []
            def record(_c, _cursor, sql, _params, _context, _many):
                assert sql.lstrip().upper().startswith("SELECT")
                statements.append(sql)
            event.listen(engine, "before_cursor_execute", record)
            try:
                rebuild.build_hierarchy_rebuild_plan(session)
                collection_grouping_authority_targets(session)
                assert not session.new and not session.dirty and not session.deleted
            finally:
                event.remove(engine, "before_cursor_execute", record)
            if size == 1:
                baseline = len(statements)
            else:
                assert len(statements) == baseline
