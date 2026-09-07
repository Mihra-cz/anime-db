from decimal import Decimal

import pytest

from app.catalog import effective_video_content_type, effective_video_content_display
from app.hierarchy_types import VIDEO_CONTENT_TYPES
from app.models import CatalogCollection
from app.numbering import (
    effective_recap_episode_number, effective_video_numbering,
    recalculate_title_numbering, summarize_title_numbering, video_numbering_identity,
)
from app.supplementary import (
    collection_supplementary_review, supplementary_inventory, supplementary_ordinal,
)
from test_supplementary_ordinals import graph, video


def collection_graph(kind="bonus"):
    collection = CatalogCollection(
        id=1, local_title="Overlord", normalized_local_title="overlord",
        relative_root_path="Overlord",
    )
    title = graph(kind)
    title.collection = collection
    title.catalog_collection_id = 1
    return collection, title


def review(items):
    return collection_supplementary_review(items)


@pytest.mark.parametrize("kind", sorted(VIDEO_CONTENT_TYPES - {"episode"}))
def test_every_canonical_non_episode_type_uses_logical_multiplicity(kind):
    _, title = collection_graph()
    a = video(title, "unidentified-a.mkv", 1, content_type_manual=kind)
    assert review([a]) == {}
    b = video(title, "unidentified-b.mkv", 2, content_type_manual=kind)
    assert review([a, b])[title.id][0].code == "missing_supplementary_ordinal"
    a.episode_number_manual_override = 1
    b.episode_number_manual_override = 2
    assert review([a, b]) == {}
    assert a.episode_number_manual_override == 1


@pytest.mark.parametrize("filename", [
    "Overlord Special Voice Drama CD Vol.2 [Visual Version].mkv",
    "Overlord Special 05.mkv",
])
def test_overlord_manual_type_moves_namespace_without_remapping_parser_number(filename):
    _, title = collection_graph()
    title.local_title = "Extras – Drama CD"
    a = video(title, filename, 1)
    assert a.file_type == "special"
    assert supplementary_ordinal(a).supplementary_type == "special"
    a.content_type_manual = "bonus"
    assert effective_video_content_type(a) == "bonus"
    assert supplementary_ordinal(a).number is None
    assert effective_video_numbering(a).supplementary_number is None
    assert video_numbering_identity(a) is None
    b = video(title, "another.mkv", 2, content_type_manual="bonus")
    issues = review([a, b])[title.id]
    assert {issue.supplementary_type for issue in issues} == {"bonus"}
    a.episode_number_manual_override = 5
    b.episode_number_manual_override = 6
    assert effective_video_content_display(a).supplementary_label == "Bonus 05"
    assert review([a, b]) == {}
    assert a.file_type == "special"
    for kind in ("ova", "special", "bonus"):
        a.content_type_manual = kind
        assert supplementary_ordinal(a).supplementary_type == kind
        assert supplementary_ordinal(a).number == 5
    a.content_type_manual = None
    assert supplementary_ordinal(a).display_label == "Special 05"
    assert a.file_type == "special"


@pytest.mark.parametrize("kind", ["season", "bonus"])
def test_episode_number_required_even_for_singleton_in_any_container(kind):
    _, title = collection_graph(kind)
    a = video(title, "untitled.mkv", content_type_manual="episode")
    recalculate_title_numbering(title, [a])
    assert summarize_title_numbering([a], title).requires_review
    a.episode_number_manual_override = 1
    recalculate_title_numbering(title, [a])
    assert effective_video_numbering(a).is_standard
    assert video_numbering_identity(a).kind == "standard"
    assert not summarize_title_numbering([a], title).requires_review


def test_collection_namespace_spans_titles_without_combining_unrelated_collections():
    collection, first = collection_graph()
    second = graph()
    second.id = 20
    second.collection = collection
    second.catalog_collection_id = 1
    a = video(first, "a.mkv", 1, content_type_manual="ova")
    b = video(second, "b.mkv", 2, content_type_manual="ova")
    assert set(review([a, b])) == {first.id, second.id}
    a.episode_number_manual_override = 1
    b.episode_number_manual_override = 2
    assert not review([a, b])
    b.episode_number_manual_override = 1
    assert all(issues[0].code == "supplementary_ordinal_collision"
               for issues in review([a, b]).values())
    # No variant lane can claim the identity of a different CatalogTitle.
    a.video_variant_group_id, b.video_variant_group_id = 1, 2
    assert review([a, b])


@pytest.mark.parametrize("number", [None, 1])
def test_complete_media_parts_and_duplicates_do_not_make_multiple_identities(number):
    _, title = collection_graph("ova")
    a = video(title, "OVA P1.mkv", 1, media_part_number=1,
              episode_number_manual_override=number)
    b = video(title, "OVA P2.mkv", 2, media_part_number=2,
              episode_number_manual_override=number)
    copy = video(title, "OVA P1 copy.mkv", 3, media_part_number=1,
                 episode_number_manual_override=number, duplicate_of_video_id=1)
    inventory = supplementary_inventory([a, b, copy], collection_scope=True)
    assert inventory.logical_identity_count == 1
    assert not inventory.invalid_duplicates
    assert not review([a, b, copy])
    c = video(title, "another OVA.mkv", 4)
    assert review([a, b, copy, c])[title.id][0].code == "missing_supplementary_ordinal"


def test_unnumbered_duplicate_does_not_require_singleton_ordinal():
    _, title = collection_graph("ova")
    a = video(title, "OVA.mkv", 1)
    b = video(title, "OVA copy.mkv", 2, duplicate_of_video_id=1)
    assert supplementary_inventory([a, b]).logical_identity_count == 1
    assert not review([a, b])


@pytest.mark.parametrize("kind", ["bonus", "film", "other", "nced"])
def test_confirmed_variants_share_one_number_but_unexplained_repetition_collides(kind):
    _, title = collection_graph()
    items = [video(title, f"item-{i}.mkv", i, content_type_manual=kind,
                   episode_number_manual_override=2, video_variant_group_id=i)
             for i in (1, 2)]
    assert supplementary_inventory(items).logical_identity_count == 1
    assert not review(items)
    for item in items:
        item.video_variant_group_id = None
    assert supplementary_inventory(items).logical_identity_count is None
    assert review(items)[title.id][0].code == "supplementary_ordinal_collision"


@pytest.mark.parametrize("position", ["5.5", "24.5", "24.9", "24.25"])
def test_fractional_recap_is_exact_chronology_not_ordinal(position):
    _, title = collection_graph("recap")
    a = video(title, f"Recap {position}.mkv")
    assert effective_recap_episode_number(a) == Decimal(position)
    assert supplementary_ordinal(a) is None
    assert effective_video_numbering(a).supplementary_number == Decimal(position)
    assert effective_video_content_display(a).noncanonical_position == position
    b = video(title, "Recap 25.5.mkv", 2)
    assert review([a, b]) == {}
    a.recap_episode_number_manual_tenths = 249
    assert effective_recap_episode_number(a) == Decimal("24.9")
    assert supplementary_ordinal(a) is None


def test_preview_pv_share_collection_namespace_and_op_ed_remain_distinct():
    _, title = collection_graph()
    a, b = video(title, "PV01.mkv"), video(title, "Preview01.mkv", 2)
    assert review([a, b])[title.id][0].supplementary_type == "preview"
    assert not review([video(title, "OP01.mkv", 3), video(title, "ED01.mkv", 4)])


def test_explicit_other_is_typed_but_unresolved_episode_evidence_stays_separate():
    _, title = collection_graph("season")
    a = video(title, "unidentified-a.mkv", 1)
    b = video(title, "unidentified-b.mkv", 2)
    a.file_type = b.file_type = "other"
    assert supplementary_ordinal(a) is None
    assert summarize_title_numbering([a], title).requires_review
    a.content_type_manual = b.content_type_manual = "other"
    assert supplementary_ordinal(a).supplementary_type == "other"
    assert not review([a])
    assert review([a, b])[title.id][0].supplementary_type == "other"
    standard = video(title, "Episode 01.mkv", 3)
    standard.file_type = "other"
    assert supplementary_ordinal(standard) is None
    assert effective_video_numbering(standard).is_standard
    fractional = video(title, "Episode 5.5.mkv", 4)
    assert supplementary_ordinal(fractional) is None
    assert effective_video_numbering(fractional).is_nonstandard


def test_raw_singleton_episode_without_number_is_review():
    _, title = collection_graph("season")
    item = video(title, "unidentified.mkv", 1)
    item.file_type = "episode"
    assert summarize_title_numbering([item], title).requires_review
    assert supplementary_ordinal(item) is None


def test_recap_manual_integer_then_fractional_authority_precede_parser_in_main_container():
    _, title = collection_graph("season")
    item = video(title, "Recap 5.5.mkv", episode_number_manual_override=24)
    assert effective_recap_episode_number(item) == Decimal(24)
    assert supplementary_ordinal(item) is None
    item.recap_episode_number_manual_tenths = 249
    assert effective_recap_episode_number(item) == Decimal("24.9")
    assert supplementary_ordinal(item) is None


def test_collection_inventory_with_unloaded_relationships_never_queries(tmp_path):
    from sqlalchemy import create_engine, select, event
    from sqlalchemy.orm import Session, raiseload
    from app.database import Base
    from app.models import CatalogTitle, Video

    engine = create_engine(f"sqlite:///{tmp_path / 'read-only-identities.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _, title = collection_graph("recap")
        first = video(title, "Recap 5.5.mkv", 1, episode_number_manual_override=24)
        second = video(title, "plain.mkv", 2, recap_episode_number_manual_tenths=249)
        session.add_all([title, first, second])
        session.commit()
    with Session(engine) as session:
        items = list(session.scalars(select(Video).options(raiseload("*"))))
        titles = list(session.scalars(select(CatalogTitle).options(raiseload("*"))))
        statements = []
        def record(conn, cursor, statement, parameters, context, many):
            statements.append(statement)
        event.listen(engine, "before_cursor_execute", record)
        try:
            assert collection_supplementary_review(items, titles) == {}
            assert not session.dirty
            assert statements == []
        finally:
            event.remove(engine, "before_cursor_execute", record)
