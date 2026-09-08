from datetime import datetime, timezone

import pytest

from app.collection_presentation import build_collection_presentation
from app.models import CatalogCollection, CatalogTitle
from app.media_parts import media_part_total
from app.supplementary import (
    collection_supplementary_review, supplementary_inventory, typed_structural_contexts,
)
from test_supplementary_ordinals import video


def collection(identifier=1):
    return CatalogCollection(id=identifier, local_title="Arifureta",
                             normalized_local_title="arifureta", relative_root_path=f"Anime/{identifier}")


def part(owner, kind, season=None):
    identifier = owner.id * 100 + len(owner.titles) + 1
    return CatalogTitle(
        id=identifier, collection=owner, catalog_collection_id=owner.id,
        local_title=f"{kind} {identifier}", normalized_local_title=f"{kind} {identifier}",
        relative_root_path=f"Anime/{owner.id}/{identifier}", part_type=kind,
        season_number=season, season_label=f"S{season}" if season else None,
    )


def item(title, number=1, identifier=1, kind="ova", **kwargs):
    return video(title, f"material-{identifier}.mkv", identifier,
                 content_type_manual=kind, episode_number_manual_override=number, **kwargs)


def review(owner, items):
    return collection_supplementary_review(items, list(owner.titles))


def test_arifureta_seasons_keep_independent_ova_identities_and_complete_media_parts():
    owner = collection()
    part(owner, "season", 1)
    part(owner, "season", 2)
    s1, s2 = part(owner, "ova", 1), part(owner, "ova", 2)
    first = [item(s1, 1, 1), item(s1, 2, 2)]
    second = [item(s2, 1, 3, media_part_number=1), item(s2, 1, 4, media_part_number=2)]
    assert media_part_total(second) == 2
    assert supplementary_inventory(first, collection_scope=True).logical_identity_count == 2
    assert supplementary_inventory(second, collection_scope=True).logical_identity_count == 1
    assert not review(owner, first + second)
    first[1].episode_number_manual_override = None
    assert set(review(owner, first + second)) == {s1.id}
    first[1].episode_number_manual_override = 1
    assert review(owner, first + second)[s1.id][0].code == "supplementary_ordinal_collision"
    for row in second:
        row.episode_number_manual_override = None
    assert not review(owner, second)


@pytest.mark.parametrize("kind", ["ova", "special", "bonus", "ncop", "film"])
def test_generic_cross_context_isolation_and_same_context_across_titles(kind):
    owner = collection()
    part(owner, "season", 1)
    part(owner, "season", 2)
    a, b = part(owner, "bonus", 1), part(owner, "bonus", 2)
    rows = [item(a, 1, 1, kind), item(b, 1, 2, kind)]
    assert not review(owner, rows)
    for row in rows:
        row.episode_number_manual_override = None
    assert not review(owner, rows)
    b.season_number = 1
    assert set(review(owner, rows)) == {a.id, b.id}
    rows[0].episode_number_manual_override = 1
    rows[1].episode_number_manual_override = 2
    assert not review(owner, rows)
    rows[1].episode_number_manual_override = 1
    issues = review(owner, rows)
    assert all(value[0].code == "supplementary_ordinal_collision" for value in issues.values())
    assert "Kontext: S1" in issues[a.id][0].message


@pytest.mark.parametrize("kind", ["bonus", "special", "ova", "menu"])
@pytest.mark.parametrize("separate_titles", [False, True])
def test_generic_media_parts_share_ui_completeness_and_collapse_before_review(kind, separate_titles):
    owner = collection()
    title = part(owner, "bonus")
    second_title = part(owner, "bonus") if separate_titles else title
    rows = [item(title, 1, 1, kind, media_part_number=1),
            item(second_title, 1, 2, kind, media_part_number=2)]
    assert media_part_total(rows) == 2
    assert supplementary_inventory(rows, collection_scope=True).logical_identity_count == 1
    assert not review(owner, rows)
    copy = item(title, 1, 3, kind, media_part_number=1, duplicate_of_video_id=1)
    assert not review(owner, rows + [copy])
    for indexes in ((1,), (1, 3), (1, 1)):
        broken = [item(title, 1, 10 + i, kind, media_part_number=n) for i, n in enumerate(indexes)]
        assert media_part_total(broken) is None
        assert review(owner, broken)[title.id][0].code == "supplementary_ordinal_collision"


@pytest.mark.parametrize("kind", ["bonus", "film", "other"])
def test_root_namespaces_and_collection_boundary(kind):
    owner = collection()
    a, b = part(owner, "bonus"), part(owner, "bonus")
    rows = [item(a, None, 1, kind)]
    assert not review(owner, rows)
    rows.append(item(b, None, 2, kind))
    assert set(review(owner, rows)) == {a.id, b.id}
    rows[0].episode_number_manual_override = 1
    rows[1].episode_number_manual_override = 2
    assert not review(owner, rows)
    rows[1].episode_number_manual_override = 1
    assert review(owner, rows)
    other = collection(2)
    c = part(other, "bonus")
    assert not collection_supplementary_review(
        [rows[0], item(c, 1, 3, kind)], list(owner.titles) + list(other.titles),
    )


def test_structural_context_reuses_effective_snapshot_and_ambiguous_attachment_fallback():
    owner = collection()
    s1, s2 = part(owner, "season", 1), part(owner, "part", 2)
    s2.part_number = 1
    child = part(owner, "bonus", 1)
    row = item(child)
    child.hierarchy_manual_override = True
    child.hierarchy_verified_at = datetime.now(timezone.utc)
    child.part_type_manual = "bonus"
    child.season_number_manual = 2
    assert build_collection_presentation(owner.titles, include_videos=False).primary_part_for_title(s2.id).supplementary_parts[0].title is child
    assert typed_structural_contexts([row])[row].key[1] == ("primary", s2.id)
    child.part_type_manual = None  # incomplete snapshot cannot replace automatic S1
    assert typed_structural_contexts([row])[row].key[1] == ("primary", s1.id)
    another = part(owner, "cour", 1)
    another.part_number = 2
    assert typed_structural_contexts([row])[row].key[1] == ("root",)
    # Primary Parts/Cours remain distinct even when their season number matches.
    a, b = item(s1, identifier=20), item(another, identifier=21)
    contexts = typed_structural_contexts([a, b])
    assert contexts[a] != contexts[b]


def test_variants_and_duplicates_do_not_cross_structural_contexts():
    owner = collection()
    part(owner, "season", 1)
    part(owner, "season", 2)
    a, b = part(owner, "bonus", 1), part(owner, "bonus", 2)
    rows = [item(a, 1, 1, video_variant_group_id=1), item(a, 1, 2, video_variant_group_id=2)]
    assert supplementary_inventory(rows, collection_scope=True).logical_identity_count == 1
    assert not review(owner, rows)
    invalid = item(b, 1, 3, duplicate_of_video_id=1)
    assert review(owner, rows + [invalid])[b.id][0].code == "broken_supplementary_identity"


def test_structural_inventory_uses_explicit_loaded_titles_without_queries(tmp_path):
    from sqlalchemy import create_engine, event, select
    from sqlalchemy.orm import Session, raiseload
    from app.database import Base
    from app.models import Video

    engine = create_engine(f"sqlite:///{tmp_path / 'structural.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        owner = collection()
        s1, s2 = part(owner, "season", 1), part(owner, "season", 2)
        a, b = part(owner, "ova", 1), part(owner, "ova", 2)
        item(a, 1, 1)
        item(b, 1, 2, media_part_number=1)
        item(b, 1, 3, media_part_number=2)
        s1_id, s2_id = s1.id, s2.id
        session.add(owner)
        session.commit()
    with Session(engine) as session:
        titles = list(session.scalars(select(CatalogTitle).options(raiseload("*"))))
        rows = list(session.scalars(select(Video).options(raiseload("*"))))
        statements = []
        def record(conn, cursor, statement, parameters, context, many):
            statements.append(statement)
        event.listen(engine, "before_cursor_execute", record)
        try:
            inventory = supplementary_inventory(
                rows, collection_scope=True, titles_by_id={t.id: t for t in titles},
            )
            assert inventory.logical_identity_count == 2
            assert {p.identity.catalog_title_key[1] for p in inventory.partitions} == {
                ("primary", s1_id), ("primary", s2_id),
            }
            assert not collection_supplementary_review(rows, titles)
            assert statements == []
            assert not session.dirty
        finally:
            event.remove(engine, "before_cursor_execute", record)
