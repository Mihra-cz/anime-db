"""Layout choices survive identity lifecycle without becoming selectors."""
from dataclasses import replace

import pytest
from sqlalchemy import select

from app.hierarchy_rebuild import (
    HierarchyPlanStaleError, apply_hierarchy_rebuild_plan,
    build_hierarchy_rebuild_plan, rebuild_hierarchy,
)
from app.hierarchy_review import apply_manual_split
from app.metadata.split import apply_metadata_split
from app.migrations import migrate_schema
from app.models import CatalogTitle
from app.scanner import scan_library
from test_physical_layout import NOW, add_video, collection, layout_api, resolve, session, title
from test_manual_split_lifecycle import _definition
from test_metadata_split import attach_confirmed_metadata, supplementary_title
from test_scanner import PROBE_RESULT


def test_metadata_split_keeps_choice_only_on_original_owner(session):
    service, _ = layout_api()
    root, source, _ = supplementary_title([f"Special {n:02}.mkv" for n in range(1, 7)])
    attach_confirmed_metadata(source, 3)
    session.add(root)
    session.commit()
    choice = service.confirm_physical_layout_choice(session, source, "own_folder", now=NOW)
    session.commit()
    result = apply_metadata_split(session, source.id, confirmed=True)
    session.commit()
    assert choice.catalog_title_id == source.id
    assert resolve(session, source).basis_matches is False
    new = resolve(session, result.new_title)
    assert new.choice is None and new.requires_human_decision


def test_manual_split_keeps_existing_choice_without_copying_to_new_title(session):
    service, _ = layout_api()
    root = collection(session)
    source = title(session, root, "OVA", kind="ova", metadata=False)
    videos = [add_video(session, source, n) for n in (1, 2)]
    session.commit()
    choice = service.confirm_physical_layout_choice(session, source, "shared_ova", now=NOW)
    session.commit()
    definitions = [replace(_definition(
        title_id=source.id if index == 0 else None, name=f"OVA {index}",
        start=None, end=None, sort_order=index, season=index + 1,
        video_ids=(video.id,),
    ), part_type_manual="ova") for index, video in enumerate(videos)]
    apply_manual_split(session, root.id, definitions)
    session.commit()
    assert source.physical_layout_choice.id == choice.id
    assert resolve(session, source).basis_matches is False
    created = next(owner for owner in root.titles if owner is not source)
    assert resolve(session, created).choice is None


def test_rebuild_preserves_empty_layout_owner_and_choice_changes_stale_plan(session):
    service, _ = layout_api()
    root = collection(session, "Anime/Obsolete")
    owner = title(session, root, "OVA", kind="ova", metadata=False)
    owner.hierarchy_manual_override = False
    owner.part_type_manual = owner.season_number_manual = owner.hierarchy_verified_at = None
    session.commit()
    choice = service.confirm_physical_layout_choice(session, owner, "shared_ova", now=NOW)
    session.commit()
    session.expire_all()
    plan = build_hierarchy_rebuild_plan(session)
    item = next(item for item in plan.titles if item.relative_root_path == owner.relative_root_path)
    assert "physical_layout_choice" in item.protection_reasons
    service.confirm_physical_layout_choice(session, owner, "extras_bonus", now=NOW)
    session.commit()
    with pytest.raises(HierarchyPlanStaleError):
        apply_hierarchy_rebuild_plan(session, plan)
    rebuild_hierarchy(session, apply=True)
    session.commit()
    assert session.get(CatalogTitle, owner.id) is not None
    assert owner.physical_layout_choice.id == choice.id
    assert choice.layout_kind == "extras_bonus"
    assert owner.hierarchy_manual_override is False


def test_layout_only_protection_does_not_preserve_video_membership(session):
    service, _ = layout_api()
    root = collection(session, "Anime/Show")
    old = title(session, root, "Obsolete OVA", kind="ova", metadata=False)
    old.hierarchy_manual_override = False
    old.part_type_manual = old.season_number_manual = old.hierarchy_verified_at = None
    video = add_video(session, old, 1)
    video.relative_path = "Anime/Show/Season 1/OVA/OVA 01.mkv"
    session.commit()
    choice = service.confirm_physical_layout_choice(session, old, "shared_ova", now=NOW)
    session.commit()
    rebuild_hierarchy(session, apply=True)
    session.commit()
    assert video.catalog_title_id != old.id
    assert old.physical_layout_choice.id == choice.id
    assert old.hierarchy_manual_override is False


def test_detached_rebuild_projection_carries_independent_choice(session):
    from app.hierarchy_rebuild import _clone_title, _load_state, _TitleSpec
    service, _ = layout_api()
    root = collection(session)
    owner = title(session, root, "OVA", kind="ova", metadata=False)
    session.commit()
    service.confirm_physical_layout_choice(session, owner, "shared_ova", now=NOW)
    session.commit()
    _load_state(session)
    clone = _clone_title(_TitleSpec(original=owner, identity=None,
        collection_path=root.relative_root_path, protected=True,
        protection_reasons=("physical_layout_choice",)), -1)
    assert clone.physical_layout_choice.layout_kind == "shared_ova"
    assert clone.physical_layout_choice is not owner.physical_layout_choice
    assert not session.new and not session.dirty and not session.deleted


def test_rebuild_apply_rolls_back_lost_layout_authority(session, monkeypatch):
    import app.hierarchy_rebuild as rebuild
    service, _ = layout_api()
    root = collection(session, "Anime/Show")
    owner = title(session, root, "OVA", kind="ova", metadata=False)
    add_video(session, owner, 1)
    session.commit()
    choice = service.confirm_physical_layout_choice(session, owner, "shared_ova", now=NOW)
    session.commit()
    session.expire_all()
    plan = build_hierarchy_rebuild_plan(session)
    original = rebuild.finalize_collection_hierarchy
    def lose_layout(*args, **kwargs):
        original(*args, **kwargs)
        session.delete(choice)
    monkeypatch.setattr(rebuild, "finalize_collection_hierarchy", lose_layout)
    with pytest.raises(rebuild.HierarchyRebuildError, match="layout"):
        apply_hierarchy_rebuild_plan(session, plan)
    assert session.get(type(choice), choice.id).layout_kind == "shared_ova"


def test_scanner_and_maintenance_preserve_choices_without_backfill(session, tmp_path, monkeypatch):
    service, _ = layout_api()
    from app.models import PhysicalLayoutChoice
    folder = tmp_path / "Show" / "Season 1" / "OVA"
    folder.mkdir(parents=True)
    (folder / "OVA 01.mkv").write_bytes(b"fixture")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda *a, **k: PROBE_RESULT)
    scan_library(session, tmp_path)
    owner = session.scalar(select(CatalogTitle).where(CatalogTitle.part_type == "ova"))
    assert session.scalar(select(PhysicalLayoutChoice)) is None
    choice = service.confirm_physical_layout_choice(session, owner, "shared_ova", now=NOW)
    session.commit()
    before = (choice.id, choice.layout_kind, choice.confirmed_at.replace(tzinfo=NOW.tzinfo), choice.basis_snapshot_json)
    for _ in range(2):
        scan_library(session, tmp_path)
        session.commit()
        assert (choice.id, choice.layout_kind, choice.confirmed_at.replace(tzinfo=NOW.tzinfo), choice.basis_snapshot_json) == before
        assert len(list(session.scalars(select(PhysicalLayoutChoice)))) == 1
    # A retained choice also protects an empty owner during the explicit legacy
    # maintenance reconstruction. It does not pin the video's assignment.
    empty = title(session, owner.collection, "Obsolete", kind="ova", metadata=False)
    empty.hierarchy_manual_override = False
    empty.part_type_manual = empty.season_number_manual = empty.hierarchy_verified_at = None
    session.commit()
    service.confirm_physical_layout_choice(session, empty, "shared_ova", now=NOW)
    session.commit()
    migrate_schema(session.get_bind())
    session.expire_all()
    assert session.get(CatalogTitle, empty.id).physical_layout_choice is not None
