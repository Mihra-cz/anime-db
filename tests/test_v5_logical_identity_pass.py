"""Regrese pro společný V5 logical-identity pass (B3, B4, B5).

Tři potvrzené blockery byly tři různé interpretace jedné hierarchie::

    logical episode identity -> representation (variant lane) -> Media Part segment

* ``V5-CLOSURE-B3`` – potvrzená duplicita si držela autoritu nad logickým
  seskupením i poté, co pozdější zápis změnil logickou identitu secondary videa;
* ``V5-CLOSURE-B4`` – sekvenční číslování tvořilo identity z fyzických Video
  řádků, takže varianty, potvrzené duplicity i Media Parts dostaly různá čísla;
* ``V5-CLOSURE-B5`` – úplná sada Media Parts jedné epizody byla vyhodnocena jako
  canonical duplicate conflict.

Testy používají výhradně in-memory SQLite a transientní objekty.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.hierarchy_authority import activate_manual_hierarchy_snapshot
from app.hierarchy_evaluation import (
    HierarchyIssueCode,
    evaluate_collection_hierarchy,
)
from app.hierarchy_review import refresh_collection_state
from app.media_parts import media_part_total, set_media_part_number
from app.models import (
    CatalogCollection,
    CatalogTitle,
    Video,
    VideoVariantGroup,
    utc_now,
)
from app.numbering import (
    apply_sequential_numbering,
    collapses_into_duplicate_primary,
    confirmed_duplicate_identity_conflicts,
    duplicate_relation_is_current,
    logical_episode_partitions,
    preview_sequential_numbering,
    recalculate_title_numbering,
    sequential_numbering_groups,
    set_duplicate_group_primary,
    set_video_episode_number_from_input,
    summarize_title_numbering,
    unresolved_duplicate_groups,
)
from app.video_variants import assign_video_catalog_title


# ---------------------------------------------------------------------------
# Sdílené in-memory fixtures
# ---------------------------------------------------------------------------

def _collection() -> CatalogCollection:
    return CatalogCollection(
        local_title="Show",
        normalized_local_title="show",
        relative_root_path="Anime/Show",
    )


def _season(
    collection: CatalogCollection,
    *,
    name: str = "Season 1",
    season_number: int = 1,
    sort_order: int = 0,
) -> CatalogTitle:
    title = CatalogTitle(
        collection=collection,
        local_title=name,
        normalized_local_title=name.casefold(),
        relative_root_path=f"{collection.relative_root_path}/{name}",
        numbering_mode="unknown",
    )
    activate_manual_hierarchy_snapshot(
        title,
        part_type="season",
        season_number=season_number,
        part_number=None,
        season_label=f"S{season_number}",
        sort_order=sort_order,
        verified_at=utc_now(),
    )
    return title


def _video(
    collection: CatalogCollection,
    title: CatalogTitle,
    filename: str,
    size: int,
    *,
    group: VideoVariantGroup | None = None,
    media_part: int | None = None,
) -> Video:
    return Video(
        relative_path=f"{title.relative_root_path}/{filename}",
        root_folder="Anime",
        filename=filename,
        size=size,
        mtime_ns=size,
        file_type="episode",
        catalog_collection=collection,
        catalog_title=title,
        video_variant_group=group,
        media_part_number=media_part,
    )


def _identified(
    collection: CatalogCollection, title: CatalogTitle, videos: list[Video],
) -> None:
    """Přiřadí transientnímu grafu explicitní FK, jak je vyžaduje potvrzení duplicity."""
    collection.id = 1
    title.id = 10
    for index, video in enumerate(videos, start=1):
        video.id = index
        video.catalog_collection_id = collection.id
        video.catalog_title_id = title.id


def _session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return Session(engine)


def _issue_codes(collection: CatalogCollection) -> set[str]:
    return {
        issue.code.value
        for issue in evaluate_collection_hierarchy(
            collection, list(collection.videos),
        ).issues
    }


def _blocking_codes(collection: CatalogCollection) -> set[str]:
    return {
        issue.code.value
        for issue in evaluate_collection_hierarchy(
            collection, list(collection.videos),
        ).issues
        if issue.blocking
    }


def _confirmed_duplicate_pair(session: Session) -> dict[str, int]:
    """Dvě videa E1 v jedné části, potvrzená jako primary/copy."""
    collection = _collection()
    title = _season(collection)
    primary = _video(collection, title, "Show - 01.mkv", 1)
    secondary = _video(collection, title, "Show - 01 - copy.mkv", 2)
    session.add(collection)
    session.flush()
    for video in (primary, secondary):
        set_video_episode_number_from_input(video, "1")
    recalculate_title_numbering(title, [primary, secondary])
    session.flush()
    set_duplicate_group_primary([primary, secondary], primary)
    session.flush()
    refresh_collection_state(collection)
    session.commit()
    return {
        "collection": collection.id,
        "title": title.id,
        "primary": primary.id,
        "secondary": secondary.id,
    }


# ---------------------------------------------------------------------------
# B3 – platnost potvrzené duplicity vůči současné logické identitě
# ---------------------------------------------------------------------------

def test_confirmed_duplicate_collapses_only_while_the_relation_matches():
    with _session() as session:
        ids = _confirmed_duplicate_pair(session)
        session.expire_all()
        collection = session.get(CatalogCollection, ids["collection"])
        title = session.get(CatalogTitle, ids["title"])

        summary = summarize_title_numbering(list(title.videos), title)
        assert summary.logical_episode_count == 1
        assert summary.identity_inconsistent_confirmed_duplicates == 0
        assert _blocking_codes(collection) == set()
        assert collection.hierarchy_status == "verified"


def test_b3_a_stale_duplicate_after_episode_number_write_is_not_collapsed():
    with _session() as session:
        ids = _confirmed_duplicate_pair(session)
        collection = session.get(CatalogCollection, ids["collection"])
        title = session.get(CatalogTitle, ids["title"])
        secondary = session.get(Video, ids["secondary"])

        # Skutečný episode-number write, jak jej dělá /videos/{id}/episode-number.
        set_video_episode_number_from_input(secondary, "2")
        refresh_collection_state(collection)
        session.commit()

        # write -> commit -> reload -> shared evaluation
        session.expire_all()
        collection = session.get(CatalogCollection, ids["collection"])
        title = session.get(CatalogTitle, ids["title"])
        primary = session.get(Video, ids["primary"])
        secondary = session.get(Video, ids["secondary"])

        assert primary.season_episode_number == 1
        assert secondary.season_episode_number == 2
        # Ruční evidence zůstává zachovaná.
        assert secondary.duplicate_of_video_id == primary.id
        assert duplicate_relation_is_current(secondary) is False
        assert collapses_into_duplicate_primary(secondary) is False

        partitions = logical_episode_partitions(list(title.videos), catalog_title=title)
        assert [
            partition.identity.season_episode_number for partition in partitions
        ] == [1, 2]
        summary = summarize_title_numbering(list(title.videos), title)
        assert summary.logical_episode_count == 2
        assert summary.confirmed_duplicates == 1
        assert summary.identity_inconsistent_confirmed_duplicates == 1

        blocking = _blocking_codes(collection)
        assert HierarchyIssueCode.CONFIRMED_DUPLICATE_IDENTITY_CONFLICT.value in blocking
        assert HierarchyIssueCode.CONFIRMED_DUPLICATE.value in _issue_codes(collection)
        assert collection.hierarchy_status == "review_required"


def test_b3_b_stale_duplicate_after_reassignment_to_another_title():
    with _session() as session:
        ids = _confirmed_duplicate_pair(session)
        collection = session.get(CatalogCollection, ids["collection"])
        second_season = _season(
            collection, name="Season 2", season_number=2, sort_order=1,
        )
        session.add(second_season)
        session.flush()
        secondary = session.get(Video, ids["secondary"])
        assign_video_catalog_title(secondary, second_season)
        refresh_collection_state(collection)
        session.commit()

        session.expire_all()
        collection = session.get(CatalogCollection, ids["collection"])
        first = session.get(CatalogTitle, ids["title"])
        second = session.get(CatalogTitle, second_season.id)
        primary = session.get(Video, ids["primary"])
        secondary = session.get(Video, ids["secondary"])

        assert secondary.catalog_title_id == second.id
        assert secondary.duplicate_of_video_id == primary.id
        assert duplicate_relation_is_current(secondary) is False

        assert summarize_title_numbering(
            list(first.videos), first,
        ).logical_episode_count == 1
        # Přesunuté video je v nové části aktivní reprezentací, ne kopií.
        assert summarize_title_numbering(
            list(second.videos), second,
        ).logical_episode_count == 1

        blocking = _blocking_codes(collection)
        assert HierarchyIssueCode.CONFIRMED_DUPLICATE_IDENTITY_CONFLICT.value in blocking
        assert collection.hierarchy_status != "verified"


def test_b3_stale_relation_conflict_is_reported_as_one_group():
    with _session() as session:
        ids = _confirmed_duplicate_pair(session)
        collection = session.get(CatalogCollection, ids["collection"])
        secondary = session.get(Video, ids["secondary"])
        set_video_episode_number_from_input(secondary, "2")
        refresh_collection_state(collection)
        session.commit()

        conflicts = confirmed_duplicate_identity_conflicts(list(collection.videos))
        assert len(conflicts) == 1
        assert {video.id for video in conflicts[0].videos} == {
            ids["primary"], ids["secondary"],
        }


def test_b3_unknown_identity_never_invalidates_manual_evidence():
    """Nezjistitelná identita nesmí ruční rozhodnutí zneplatnit."""
    collection = _collection()
    title = _season(collection)
    primary = _video(collection, title, "Show - 01.mkv", 1)
    secondary = _video(collection, title, "Neznámý soubor.mkv", 2)
    secondary.duplicate_of = primary

    assert duplicate_relation_is_current(secondary) is True
    assert collapses_into_duplicate_primary(secondary) is True
    assert confirmed_duplicate_identity_conflicts([primary, secondary]) == ()


# ---------------------------------------------------------------------------
# B4 – sekvenční číslování nad logickými skupinami
# ---------------------------------------------------------------------------

def _sequence(videos: list[Video]) -> dict[str, int]:
    rows = preview_sequential_numbering(videos, 1)
    return {row.filename: row.proposed_episode for row in rows}


def _applied(videos: list[Video]) -> dict[str, int]:
    apply_sequential_numbering(videos, 1, confirm_manual_conflicts=True)
    return {
        video.filename: video.episode_number_manual_override for video in videos
    }


def test_b4_a_variant_lanes_stay_one_logical_episode():
    collection = _collection()
    title = _season(collection)
    bd = VideoVariantGroup(
        id=100, catalog_title=title, manual_label="BD",
        release_source="bd", content_variant=None, verified_at=utc_now(),
    )
    tv = VideoVariantGroup(
        id=200, catalog_title=title, manual_label="TV",
        release_source="tv", content_variant=None, verified_at=utc_now(),
    )
    videos = [
        _video(collection, title, "BD Show - 01.mkv", 1, group=bd),
        _video(collection, title, "BD Show - 02.mkv", 2, group=bd),
        _video(collection, title, "TV Show - 01.mkv", 3, group=tv),
        _video(collection, title, "TV Show - 02.mkv", 4, group=tv),
    ]
    for index, video in enumerate(videos, start=1):
        video.id = index
    recalculate_title_numbering(title, videos)

    assert _sequence(videos) == {
        "BD Show - 01.mkv": 1, "TV Show - 01.mkv": 1,
        "BD Show - 02.mkv": 2, "TV Show - 02.mkv": 2,
    }
    assert _applied(videos) == _sequence(videos)


def test_b4_b_confirmed_duplicate_secondary_follows_its_primary():
    collection = _collection()
    title = _season(collection)
    primary = _video(collection, title, "Show - 01.mkv", 1)
    secondary = _video(collection, title, "Show - 01 - copy.mkv", 2)
    other = _video(collection, title, "Show - 02.mkv", 3)
    _identified(collection, title, [primary, secondary, other])
    for video in (primary, secondary):
        set_video_episode_number_from_input(video, "1")
    set_video_episode_number_from_input(other, "2")
    recalculate_title_numbering(title, [primary, secondary, other])
    set_duplicate_group_primary([primary, secondary], primary)

    videos = [primary, secondary, other]
    assert _sequence(videos) == {
        "Show - 01.mkv": 1, "Show - 01 - copy.mkv": 1, "Show - 02.mkv": 2,
    }
    assert _applied(videos) == _sequence(videos)


def test_b4_c_media_parts_stay_one_logical_episode():
    collection = _collection()
    title = _season(collection)
    videos = [
        _video(collection, title, "Show - 01 - p1.mkv", 1, media_part=1),
        _video(collection, title, "Show - 01 - p2.mkv", 2, media_part=2),
        _video(collection, title, "Show - 02 - p1.mkv", 3, media_part=1),
        _video(collection, title, "Show - 02 - p2.mkv", 4, media_part=2),
    ]
    for index, video in enumerate(videos, start=1):
        video.id = index
    _project_known_episode_numbers(
        title,
        [(videos[0], 1), (videos[1], 1), (videos[2], 2), (videos[3], 2)],
    )

    assert _sequence(videos) == {
        "Show - 01 - p1.mkv": 1, "Show - 01 - p2.mkv": 1,
        "Show - 02 - p1.mkv": 2, "Show - 02 - p2.mkv": 2,
    }
    assert _applied(videos) == _sequence(videos)


def test_b4_d_variant_lanes_with_media_parts_stay_one_logical_episode():
    collection = _collection()
    title = _season(collection)
    bd = VideoVariantGroup(
        id=100, catalog_title=title, manual_label="BD",
        release_source="bd", content_variant=None, verified_at=utc_now(),
    )
    tv = VideoVariantGroup(
        id=200, catalog_title=title, manual_label="TV",
        release_source="tv", content_variant=None, verified_at=utc_now(),
    )
    videos = [
        _video(collection, title, "BD Show - 01 - p1.mkv", 1, group=bd, media_part=1),
        _video(collection, title, "BD Show - 01 - p2.mkv", 2, group=bd, media_part=2),
        _video(collection, title, "BD Show - 02 - p1.mkv", 3, group=bd, media_part=1),
        _video(collection, title, "BD Show - 02 - p2.mkv", 4, group=bd, media_part=2),
        _video(collection, title, "TV Show - 01 - p1.mkv", 5, group=tv, media_part=1),
        _video(collection, title, "TV Show - 01 - p2.mkv", 6, group=tv, media_part=2),
        _video(collection, title, "TV Show - 02 - p1.mkv", 7, group=tv, media_part=1),
        _video(collection, title, "TV Show - 02 - p2.mkv", 8, group=tv, media_part=2),
    ]
    for index, video in enumerate(videos, start=1):
        video.id = index
    _project_known_episode_numbers(title, [
        (videos[0], 1), (videos[1], 1), (videos[2], 2), (videos[3], 2),
        (videos[4], 1), (videos[5], 1), (videos[6], 2), (videos[7], 2),
    ])

    groups = sequential_numbering_groups(videos)
    assert [len(group.videos) for group in groups] == [4, 4]
    assert _sequence(videos) == {
        "BD Show - 01 - p1.mkv": 1, "BD Show - 01 - p2.mkv": 1,
        "TV Show - 01 - p1.mkv": 1, "TV Show - 01 - p2.mkv": 1,
        "BD Show - 02 - p1.mkv": 2, "BD Show - 02 - p2.mkv": 2,
        "TV Show - 02 - p1.mkv": 2, "TV Show - 02 - p2.mkv": 2,
    }
    assert _applied(videos) == _sequence(videos)


def test_b4_preview_and_apply_use_the_same_logical_groups():
    collection = _collection()
    title = _season(collection)
    bd = VideoVariantGroup(
        id=100, catalog_title=title, manual_label="BD",
        release_source="bd", content_variant=None, verified_at=utc_now(),
    )
    videos = [
        _video(collection, title, "BD Show - 01 - p1.mkv", 1, group=bd, media_part=1),
        _video(collection, title, "BD Show - 01 - p2.mkv", 2, group=bd, media_part=2),
        _video(collection, title, "BD Show - 02.mkv", 3, group=bd),
    ]
    for index, video in enumerate(videos, start=1):
        video.id = index
    _project_known_episode_numbers(
        title,
        [(videos[0], 1), (videos[1], 1), (videos[2], 2)],
    )

    preview = preview_sequential_numbering(videos, 1)
    applied = apply_sequential_numbering(videos, 1)
    assert [
        (row.video_id, row.proposed_episode, row.logical_group_index)
        for row in preview
    ] == [
        (row.video_id, row.proposed_episode, row.logical_group_index)
        for row in applied
    ]
    assert [video.episode_number_manual_override for video in videos] == [
        row.proposed_episode
        for row in sorted(applied, key=lambda item: item.video_id)
    ]


def test_b4_stale_duplicate_does_not_share_the_primary_slot():
    collection = _collection()
    title = _season(collection)
    primary = _video(collection, title, "Show - 01.mkv", 1)
    secondary = _video(collection, title, "Show - 01 - copy.mkv", 2)
    _identified(collection, title, [primary, secondary])
    for video in (primary, secondary):
        set_video_episode_number_from_input(video, "1")
    recalculate_title_numbering(title, [primary, secondary])
    set_duplicate_group_primary([primary, secondary], primary)
    set_video_episode_number_from_input(secondary, "2")
    recalculate_title_numbering(title, [primary, secondary])

    proposed = _sequence([primary, secondary])
    assert sorted(proposed.values()) == [1, 2]
    assert proposed["Show - 01.mkv"] != proposed["Show - 01 - copy.mkv"]


# ---------------------------------------------------------------------------
# B5 – Media Parts versus konkurenční reprezentace
# ---------------------------------------------------------------------------

def test_b5_a_complete_media_part_set_is_not_a_canonical_duplicate():
    collection = _collection()
    title = _season(collection)
    first = _video(collection, title, "Show - 01 - p1.mkv", 1, media_part=1)
    second = _video(collection, title, "Show - 01 - p2.mkv", 2, media_part=2)
    for video in (first, second):
        set_video_episode_number_from_input(video, "1")
    recalculate_title_numbering(title, [first, second])
    videos = [first, second]

    summary = summarize_title_numbering(videos, title)
    assert summary.logical_episode_count == 1
    assert summary.duplicate_numbers == ()
    assert unresolved_duplicate_groups(videos, catalog_title=title) == ()
    partition = logical_episode_partitions(videos, catalog_title=title)[0]
    assert partition.unresolved_video_groups == ()
    assert _blocking_codes(collection) == set()


def test_b5_b_repeated_media_part_ordinal_stays_a_conflict():
    collection = _collection()
    title = _season(collection)
    first = _video(collection, title, "Show - 01 - p1.mkv", 1, media_part=1)
    second = _video(collection, title, "Show - 01 - p1 alt.mkv", 2, media_part=1)
    for video in (first, second):
        set_video_episode_number_from_input(video, "1")
    recalculate_title_numbering(title, [first, second])
    videos = [first, second]

    summary = summarize_title_numbering(videos, title)
    assert summary.duplicate_numbers == (1,)
    assert len(unresolved_duplicate_groups(videos, catalog_title=title)) == 1
    assert HierarchyIssueCode.CANONICAL_DUPLICATE.value in _blocking_codes(collection)


def test_b5_b_incomplete_media_part_set_stays_a_conflict():
    collection = _collection()
    title = _season(collection)
    first = _video(collection, title, "Show - 01 - p1.mkv", 1, media_part=1)
    second = _video(collection, title, "Show - 01 - p3.mkv", 2, media_part=3)
    third = _video(collection, title, "Show - 01 - x.mkv", 3)
    for video in (first, second, third):
        set_video_episode_number_from_input(video, "1")
    recalculate_title_numbering(title, [first, second, third])

    assert summarize_title_numbering(
        [first, second], title,
    ).duplicate_numbers == (1,)
    # Částečně přiřazená sada (jedno video bez Media Part) je také konflikt.
    assert summarize_title_numbering(
        [first, third], title,
    ).duplicate_numbers == (1,)


def test_b5_c_variant_lanes_with_valid_media_part_sets_have_no_false_blocker():
    collection = _collection()
    title = _season(collection)
    bd = VideoVariantGroup(
        id=100, catalog_title=title, manual_label="BD",
        release_source="bd", content_variant=None, verified_at=utc_now(),
    )
    tv = VideoVariantGroup(
        id=200, catalog_title=title, manual_label="TV",
        release_source="tv", content_variant=None, verified_at=utc_now(),
    )
    videos = [
        _video(collection, title, "BD Show - 01 - p1.mkv", 1, group=bd, media_part=1),
        _video(collection, title, "BD Show - 01 - p2.mkv", 2, group=bd, media_part=2),
        _video(collection, title, "TV Show - 01 - p1.mkv", 3, group=tv, media_part=1),
        _video(collection, title, "TV Show - 01 - p2.mkv", 4, group=tv, media_part=2),
    ]
    for video in videos:
        set_video_episode_number_from_input(video, "1")
    recalculate_title_numbering(title, videos)

    summary = summarize_title_numbering(videos, title)
    assert summary.logical_episode_count == 1
    assert summary.duplicate_numbers == ()
    assert unresolved_duplicate_groups(videos, catalog_title=title) == ()
    assert _blocking_codes(collection) == set()


def test_b5_d_genuine_competing_representations_still_block():
    collection = _collection()
    title = _season(collection)
    first = _video(collection, title, "Show - 01 - a.mkv", 1)
    second = _video(collection, title, "Show - 01 - b.mkv", 2)
    for video in (first, second):
        set_video_episode_number_from_input(video, "1")
    recalculate_title_numbering(title, [first, second])
    videos = [first, second]

    summary = summarize_title_numbering(videos, title)
    assert summary.logical_episode_count == 1
    assert summary.duplicate_numbers == (1,)
    assert HierarchyIssueCode.CANONICAL_DUPLICATE.value in _blocking_codes(collection)


def test_b5_null_lane_next_to_a_known_lane_stays_visible():
    collection = _collection()
    title = _season(collection)
    bd = VideoVariantGroup(
        id=100, catalog_title=title, manual_label="BD",
        release_source="bd", content_variant=None, verified_at=utc_now(),
    )
    known = _video(collection, title, "BD Show - 01.mkv", 1, group=bd)
    unknown = _video(collection, title, "Show - 01.mkv", 2)
    for video in (known, unknown):
        set_video_episode_number_from_input(video, "1")
    recalculate_title_numbering(title, [known, unknown])
    videos = [known, unknown]

    partition = logical_episode_partitions(videos, catalog_title=title)[0]
    assert partition.unresolved_video_groups == (partition.videos,)
    assert HierarchyIssueCode.CANONICAL_DUPLICATE.value in _blocking_codes(collection)


def test_b5_media_part_write_surface_clears_a_canonical_duplicate():
    """Označení Media Parts je legitimní řešení nahlášené kolize."""
    with _session() as session:
        collection = _collection()
        title = _season(collection)
        first = _video(collection, title, "Show - 01 - p1.mkv", 1)
        second = _video(collection, title, "Show - 01 - p2.mkv", 2)
        session.add(collection)
        session.flush()
        for video in (first, second):
            set_video_episode_number_from_input(video, "1")
        refresh_collection_state(collection)
        session.commit()
        assert HierarchyIssueCode.CANONICAL_DUPLICATE.value in _blocking_codes(collection)

        set_media_part_number(first, 1)
        set_media_part_number(second, 2)
        refresh_collection_state(collection)
        session.commit()

        session.expire_all()
        collection = session.get(CatalogCollection, collection.id)
        assert _blocking_codes(collection) == set()
        assert collection.hierarchy_status == "verified"


@pytest.mark.parametrize("start", [0, -1])
def test_sequential_numbering_still_rejects_a_nonpositive_start(start):
    collection = _collection()
    title = _season(collection)
    video = _video(collection, title, "Show - 01.mkv", 1)
    video.id = 1

    with pytest.raises(ValueError, match="kladné"):
        preview_sequential_numbering([video], start)


# ---------------------------------------------------------------------------
# Lifecycle parity – význam logického seskupení se reloadem nemění
# ---------------------------------------------------------------------------

def test_b3_stale_state_is_stable_across_repeated_finalization():
    with _session() as session:
        ids = _confirmed_duplicate_pair(session)
        collection = session.get(CatalogCollection, ids["collection"])
        secondary = session.get(Video, ids["secondary"])
        set_video_episode_number_from_input(secondary, "2")
        refresh_collection_state(collection)
        session.commit()

        for _pass in range(2):
            session.expire_all()
            collection = session.get(CatalogCollection, ids["collection"])
            refresh_collection_state(collection)
            session.commit()
            session.expire_all()
            collection = session.get(CatalogCollection, ids["collection"])
            title = session.get(CatalogTitle, ids["title"])
            secondary = session.get(Video, ids["secondary"])
            assert secondary.duplicate_of_video_id == ids["primary"]
            assert secondary.season_episode_number == 2
            assert summarize_title_numbering(
                list(title.videos), title,
            ).logical_episode_count == 2
            assert collection.hierarchy_status == "review_required"
            assert (
                HierarchyIssueCode.CONFIRMED_DUPLICATE_IDENTITY_CONFLICT.value
                in _blocking_codes(collection)
            )


def test_b5_media_part_grouping_survives_reload_and_recalculation():
    with _session() as session:
        collection = _collection()
        title = _season(collection)
        first = _video(collection, title, "Show - 01 - p1.mkv", 1, media_part=1)
        second = _video(collection, title, "Show - 01 - p2.mkv", 2, media_part=2)
        third = _video(collection, title, "Show - 02.mkv", 3)
        session.add(collection)
        session.flush()
        for video in (first, second):
            set_video_episode_number_from_input(video, "1")
        set_video_episode_number_from_input(third, "2")
        refresh_collection_state(collection)
        session.commit()

        session.expire_all()
        collection = session.get(CatalogCollection, collection.id)
        title = session.get(CatalogTitle, title.id)
        summary = summarize_title_numbering(list(title.videos), title)
        assert summary.physical_video_count == 3
        assert summary.logical_episode_count == 2
        assert summary.duplicate_numbers == ()
        assert summary.gaps == ()
        assert collection.hierarchy_status == "verified"

        refresh_collection_state(collection)
        session.commit()
        session.expire_all()
        collection = session.get(CatalogCollection, collection.id)
        title = session.get(CatalogTitle, title.id)
        assert summarize_title_numbering(
            list(title.videos), title,
        ).logical_episode_count == 2
        assert collection.hierarchy_status == "verified"


def test_b4_ambiguous_lanes_are_rejected_and_survive_shared_finalization():
    """Potvrzená a NULL lane bez logical identity se nesmí pozičně sjednotit."""
    with _session() as session:
        collection = _collection()
        title = _season(collection)
        bd = VideoVariantGroup(
            catalog_title=title, manual_label="BD",
            release_source="bd", content_variant=None, verified_at=utc_now(),
        )
        videos = [
            _video(collection, title, "BD Show - a.mkv", 1, group=bd),
            _video(collection, title, "BD Show - b.mkv", 2, group=bd),
            _video(collection, title, "TV Show - a.mkv", 3),
            _video(collection, title, "TV Show - b.mkv", 4),
        ]
        session.add(collection)
        session.add(bd)
        session.flush()

        with pytest.raises(ValueError, match="variantních řad"):
            apply_sequential_numbering(list(title.videos), 1)
        refresh_collection_state(collection)
        session.commit()

        session.expire_all()
        collection = session.get(CatalogCollection, collection.id)
        title = session.get(CatalogTitle, title.id)
        numbers = {
            video.filename: video.season_episode_number for video in title.videos
        }
        assert set(numbers.values()) == {None}
        assert summarize_title_numbering(
            list(title.videos), title,
        ).logical_episode_count == 0
        assert collection.hierarchy_status == "review_required"


def test_confirmed_duplicate_inside_a_media_part_set_stays_one_logical_episode():
    """Kombinace duplicate + Media Parts se řídí existujícím kontraktem."""
    collection = _collection()
    title = _season(collection)
    first = _video(collection, title, "Show - 01 - p1.mkv", 1, media_part=1)
    second = _video(collection, title, "Show - 01 - p2.mkv", 2, media_part=2)
    copy_of_first = _video(collection, title, "Show - 01 - p1 copy.mkv", 3, media_part=1)
    _identified(collection, title, [first, second, copy_of_first])
    for video in (first, second, copy_of_first):
        set_video_episode_number_from_input(video, "1")
    recalculate_title_numbering(title, [first, second, copy_of_first])
    set_duplicate_group_primary([first, copy_of_first], first)
    videos = [first, second, copy_of_first]

    assert media_part_total(videos) == 2
    summary = summarize_title_numbering(videos, title)
    assert summary.logical_episode_count == 1
    assert summary.duplicate_numbers == ()
    assert summary.identity_inconsistent_confirmed_duplicates == 0
    assert unresolved_duplicate_groups(videos, catalog_title=title) == ()


# ---------------------------------------------------------------------------
# Closure re-audit R4/R5 – manual-first sequential grouping
# ---------------------------------------------------------------------------

def _project_known_episode_numbers(
    title: CatalogTitle,
    assignments: list[tuple[Video, int]],
) -> None:
    for video, number in assignments:
        set_video_episode_number_from_input(video, str(number))
    recalculate_title_numbering(title, [video for video, _number in assignments])


def test_r5_reversed_filename_order_keeps_complete_media_parts_in_one_slot():
    collection = _collection()
    title = _season(collection)
    mp2 = _video(collection, title, "a.mkv", 1, media_part=2)
    mp1 = _video(collection, title, "z.mkv", 2, media_part=1)
    _identified(collection, title, [mp2, mp1])
    _project_known_episode_numbers(title, [(mp2, 1), (mp1, 1)])

    rows = preview_sequential_numbering([mp2, mp1], 1)
    applied = apply_sequential_numbering(
        [mp2, mp1], 1, confirm_manual_conflicts=True,
    )

    assert {row.filename: row.proposed_episode for row in rows} == {
        "a.mkv": 1,
        "z.mkv": 1,
    }
    assert applied == rows
    assert mp2.episode_number_manual_override == 1
    assert mp1.episode_number_manual_override == 1


@pytest.mark.parametrize(
    ("parts", "filenames"),
    (
        ((1, 2), ("a.mkv", "z.mkv")),
        ((2, 1), ("a.mkv", "z.mkv")),
    ),
)
def test_r5_complete_media_parts_ignore_physical_order(parts, filenames):
    collection = _collection()
    title = _season(collection)
    videos = [
        _video(collection, title, filename, index, media_part=part)
        for index, (part, filename) in enumerate(zip(parts, filenames), 1)
    ]
    _identified(collection, title, videos)
    _project_known_episode_numbers(title, [(video, 1) for video in videos])

    groups = sequential_numbering_groups(videos)

    assert len(groups) == 1
    assert set(groups[0].videos) == set(videos)


@pytest.mark.parametrize("parts", ((1, 1), (1, 3), (1, None)))
def test_r5_invalid_media_part_set_refuses_sequential_numbering(parts):
    collection = _collection()
    title = _season(collection)
    videos = [
        _video(collection, title, f"item-{index}.mkv", index, media_part=part)
        for index, part in enumerate(parts, 1)
    ]
    _identified(collection, title, videos)
    _project_known_episode_numbers(title, [(video, 1) for video in videos])
    before = [video.episode_number_manual_override for video in videos]

    with pytest.raises(ValueError, match="Media Part"):
        preview_sequential_numbering(videos, 1)
    with pytest.raises(ValueError, match="Media Part"):
        apply_sequential_numbering(
            videos, 1, confirm_manual_conflicts=True,
        )

    assert [video.episode_number_manual_override for video in videos] == before


def test_r5_confirmed_variant_lanes_each_keep_complete_media_parts():
    collection = _collection()
    title = _season(collection)
    bd = VideoVariantGroup(
        id=100, catalog_title=title, manual_label="BD",
        release_source="bd", content_variant=None, verified_at=utc_now(),
    )
    tv = VideoVariantGroup(
        id=200, catalog_title=title, manual_label="TV",
        release_source="tv", content_variant=None, verified_at=utc_now(),
    )
    videos = [
        _video(collection, title, "BD-z.mkv", 1, group=bd, media_part=1),
        _video(collection, title, "BD-a.mkv", 2, group=bd, media_part=2),
        _video(collection, title, "TV-z.mkv", 3, group=tv, media_part=1),
        _video(collection, title, "TV-a.mkv", 4, group=tv, media_part=2),
    ]
    _identified(collection, title, videos)
    _project_known_episode_numbers(title, [(video, 1) for video in videos])

    groups = sequential_numbering_groups(videos)

    assert len(groups) == 1
    assert set(groups[0].videos) == set(videos)


def _variant_sequence(
    lane_numbers: tuple[tuple[int, ...], ...],
) -> tuple[CatalogCollection, CatalogTitle, list[Video]]:
    collection = _collection()
    title = _season(collection)
    videos = []
    for group_id, (label, numbers) in enumerate(
        zip(("BD", "TV", "WEB"), lane_numbers),
        100,
    ):
        group = VideoVariantGroup(
            id=group_id,
            catalog_title=title,
            manual_label=label,
            release_source=label.casefold(),
            content_variant=None,
            verified_at=utc_now(),
        )
        videos.extend(
            _video(
                collection,
                title,
                f"{label} Show - {number:02}.mkv",
                len(videos) + 1,
                group=group,
            )
            for number in numbers
        )
    _identified(collection, title, videos)
    recalculate_title_numbering(title, videos)
    return collection, title, videos


@pytest.mark.parametrize(
    "lane_numbers",
    (
        ((1, 2), (2,)),
        ((1,), (1, 2)),
        ((1, 3), (2, 3), (1, 2)),
    ),
)
def test_r4_incomplete_variant_lanes_refuse_positional_alignment(lane_numbers):
    _collection_row, _title, videos = _variant_sequence(lane_numbers)
    before = [video.episode_number_manual_override for video in videos]

    with pytest.raises(ValueError, match="variantních řad"):
        preview_sequential_numbering(videos, 1)
    with pytest.raises(ValueError, match="variantních řad"):
        apply_sequential_numbering(videos, 1, confirm_manual_conflicts=True)

    assert [video.episode_number_manual_override for video in videos] == before


def test_r4_single_lane_remains_sequenceable():
    collection = _collection()
    title = _season(collection)
    videos = [
        _video(collection, title, "b.mkv", 2),
        _video(collection, title, "a.mkv", 1),
    ]
    _identified(collection, title, videos)

    rows = apply_sequential_numbering(videos, 3)

    assert {row.filename: row.proposed_episode for row in rows} == {
        "a.mkv": 3,
        "b.mkv": 4,
    }


def test_r4_matching_known_logical_identities_allow_confirmed_variant_lanes():
    _collection_row, _title, videos = _variant_sequence(((1, 2), (1, 2)))

    rows = apply_sequential_numbering(videos, 3)

    assert {
        row.filename: row.proposed_episode for row in rows
    } == {
        "BD Show - 01.mkv": 3,
        "TV Show - 01.mkv": 3,
        "BD Show - 02.mkv": 4,
        "TV Show - 02.mkv": 4,
    }


def test_r4_unassigned_lane_is_not_confirmed_by_matching_episode_numbers():
    collection = _collection()
    title = _season(collection)
    bd = VideoVariantGroup(
        id=100, catalog_title=title, manual_label="BD",
        release_source="bd", content_variant=None, verified_at=utc_now(),
    )
    videos = [
        _video(collection, title, "BD Show - 01.mkv", 1, group=bd),
        _video(collection, title, "Show - 01.mkv", 2),
    ]
    _identified(collection, title, videos)
    recalculate_title_numbering(title, videos)

    with pytest.raises(ValueError, match="variantních řad"):
        preview_sequential_numbering(videos, 1)

    assert [video.episode_number_manual_override for video in videos] == [None, None]


def test_r4_incomplete_lane_does_not_change_logical_completeness_or_hierarchy():
    collection, title, videos = _variant_sequence(((1, 2), (2,)))
    refresh_collection_state(collection)
    before = {
        video.filename: video.season_episode_number for video in videos
    }

    with pytest.raises(ValueError, match="variantních řad"):
        preview_sequential_numbering(videos, 1)

    summary = summarize_title_numbering(videos, title)
    assert summary.logical_episode_count == 2
    assert summary.gaps == ()
    assert summary.duplicate_numbers == ()
    assert _blocking_codes(collection) == set()
    assert {
        video.filename: video.season_episode_number for video in videos
    } == before
