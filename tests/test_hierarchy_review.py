import json
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app.catalog import (
    catalog_title_display_title, catalog_title_series_label, classify_video,
    video_matches_filter,
)
from app.hierarchy_evaluation import HierarchyIssueCode, evaluate_collection_hierarchy
from app.hierarchy_review import (
    FILENAME_SEASON_CONFLICT_REVIEW_REASON,
    MISSING_PART_NUMBER_REVIEW_REASON, PERIOD_HINT_REVIEW_REASON,
    ManualTitleDefinition, apply_manual_split,
    apply_single_title_confirmation,
    classify_videos_in_place, clear_confirmed_duplicate_videos,
    collection_requires_review, confirm_duplicate_groups, confirm_duplicate_videos,
    confirm_effective_collection_hierarchy,
    catalog_title_hierarchy_is_verified, manual_hierarchy_snapshot_issue,
    create_title_from_videos,
    delete_empty_local_title, extract_local_period_hint, merge_title_into,
    move_videos_to_title, parse_manual_definitions, parse_simple_definitions,
    definitions_as_json, preview_assignments, refresh_collection_state,
    complementary_season_part_proposal_for_videos,
    confirm_existing_split_season_parts,
    create_title_with_complementary_season_part,
    existing_split_season_part_proposals,
    separate_nonstandard_videos,
    set_manual_duplicate_status, set_manual_title_hierarchy,
    simple_definition_rows,
    single_title_confirmation_suggestion,
    supplementary_assignment_recommendations,
    supplementary_video_suggestions,
)
from app.hierarchy_types import PART_TYPE_CHOICES
from app.models import (
    Artwork, CatalogCollection, CatalogTitle, ExternalTitleLink, InternalSubtitle,
    ManualSplitRuleVideo, MetadataCandidate, TitleMetadata, Video, utc_now,
)
from app.migrations import migrate_schema
from app.numbering import (
    apply_sequential_numbering,
    collection_requires_numbering_review as numbering_requires_review,
    effective_video_numbering,
    recalculate_title_numbering,
    set_video_episode_override,
    summarize_title_numbering, unresolved_duplicate_groups,
)
from app.scanner import scan_library
from app.structural_inference import (
    GENERIC_TITLE_REVIEW_REASON, LONG_FLAT_SEQUENCE_REVIEW_REASON,
)


PROBE_RESULT = {
    "duration": 60.0, "video_codec": "h264", "width": 1920, "height": 1080,
    "audio": [], "subtitles": [],
}


def seeded_collection(count: int = 26):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="Anime title (Z18-L20)",
            normalized_local_title="anime title z18 l20",
            relative_root_path="Anime/Anime title (Z18-L20)",
        )
        title = CatalogTitle(
            collection=collection, local_title=collection.local_title,
            normalized_local_title=collection.normalized_local_title,
            relative_root_path=collection.relative_root_path,
        )
        session.add_all([collection, title])
        session.flush()
        for number in range(1, count + 1):
            session.add(Video(
                relative_path=f"{collection.relative_root_path}/Episode {number:02}.mkv",
                root_folder="Anime", filename=f"Episode {number:02}.mkv",
                size=1, mtime_ns=1, local_episode_number=number,
                catalog_title=title, catalog_collection=collection,
            ))
        session.commit()
        return engine, collection.id, title.id


def definitions(first_title_id: int):
    return parse_manual_definitions(f"""[
      {{"title_id": {first_title_id}, "local_title": "Part 1", "season_number_manual": 1,
       "season_label_manual": "S1", "part_number_manual": 1, "part_type_manual": "part",
       "episode_start": 1, "episode_end": 13, "episode_start_offset": 0,
       "numbering_mode": "absolute", "sort_order": 1}},
      {{"local_title": "Part 2", "season_number_manual": 1,
       "season_label_manual": "S1", "part_number_manual": 2, "part_type_manual": "part",
       "episode_start": 14, "episode_end": 26, "episode_start_offset": 13,
       "numbering_mode": "absolute", "sort_order": 2}}
    ]""")


def explicit_definition(
    title_id: int,
    video_ids: tuple[int, ...],
) -> ManualTitleDefinition:
    return ManualTitleDefinition(
        title_id=title_id,
        local_title="Season 1",
        manual_display_title=None,
        season_number_manual=1,
        season_label_manual="S1",
        part_number_manual=None,
        part_type_manual="season",
        episode_start=None,
        episode_end=None,
        episode_start_offset=None,
        numbering_mode="season_local",
        sort_order=1,
        filename_pattern=None,
        video_ids=video_ids,
    )


def manual_split_authority_pairs(session: Session) -> set[tuple[int, int]]:
    return {
        (row.catalog_title_id, row.video_id)
        for row in session.scalars(select(ManualSplitRuleVideo)).all()
    }


def simple_collection(*, part_type="title", status="review_required", with_video=True):
    collection = CatalogCollection(
        id=1, local_title="Akame ga Kill! (L14)",
        normalized_local_title="akame ga kill l14",
        relative_root_path="Anime/Akame ga Kill! (L14)", hierarchy_status=status,
    )
    title = CatalogTitle(
        id=1, collection=collection, local_title=collection.local_title,
        normalized_local_title=collection.normalized_local_title,
        relative_root_path=collection.relative_root_path, part_type=part_type,
    )
    if with_video:
        Video(
            id=1, relative_path=f"{collection.relative_root_path}/Episode 01.mkv",
            root_folder="Anime", filename="Episode 01.mkv", size=1, mtime_ns=1,
            local_episode_number=1, season_episode_number=1,
            absolute_episode_number=1, catalog_title=title,
            catalog_collection=collection,
        )
    return collection, title


def test_flat_collection_with_episodes_1_to_26_requires_review_for_possible_parts():
    engine, collection_id, _ = seeded_collection()
    with Session(engine) as session:
        collection = session.get(CatalogCollection, collection_id)
        reason = collection_requires_review(collection, list(collection.videos))
        assert reason == LONG_FLAT_SEQUENCE_REVIEW_REASON


def explicit_season_collection(local_title: str) -> CatalogCollection:
    collection = CatalogCollection(
        id=1, local_title=local_title, normalized_local_title="anime",
        relative_root_path=f"Anime/{local_title}",
    )
    for season_number in (1, 2):
        title = CatalogTitle(
            id=season_number, collection=collection,
            local_title=f"Serie{season_number}",
            normalized_local_title=f"serie{season_number}",
            relative_root_path=f"Anime/{local_title}/Serie{season_number}",
            part_type="season", season_number=season_number,
            season_label=f"S{season_number}",
        )
        Video(
            id=season_number,
            relative_path=f"{title.relative_root_path}/E01.mkv",
            root_folder="Anime", filename="E01.mkv", size=1, mtime_ns=1,
            local_episode_number=1, season_episode_number=1,
            absolute_episode_number=1, catalog_title=title,
            catalog_collection=collection,
        )
    return collection


def test_bare_legacy_period_range_does_not_require_review_for_explicit_seasons():
    collection = explicit_season_collection("Anime L20-P23")

    assert extract_local_period_hint(collection.local_title) == "L20-P23"
    assert all(not title.hierarchy_manual_override for title in collection.titles)
    assert collection_requires_review(collection, list(collection.videos)) is None


@pytest.mark.parametrize("local_title", ["Anime (L20-P23)", "Anime ( L20-P23 )"])
def test_parenthesized_legacy_period_range_does_not_require_hierarchy_review(
    local_title,
):
    collection = explicit_season_collection(local_title)

    assert extract_local_period_hint(collection.local_title) == "L20-P23"
    assert collection_requires_review(collection, list(collection.videos)) is None


def test_single_legacy_period_hint_is_not_a_hierarchy_reason():
    collection = CatalogCollection(
        id=1, local_title="Anime P21", normalized_local_title="anime p21",
        relative_root_path="Anime/Anime P21",
    )
    title = CatalogTitle(
        id=1, collection=collection, local_title="Anime P21",
        normalized_local_title="anime p21", relative_root_path="Anime/Anime P21",
    )
    Video(
        id=1, relative_path="Anime/Anime P21/E01.mkv", root_folder="Anime",
        filename="E01.mkv", size=1, mtime_ns=1, local_episode_number=1,
        season_episode_number=1, absolute_episode_number=1,
        catalog_title=title, catalog_collection=collection,
    )

    assert extract_local_period_hint(collection.local_title) == "P21"
    assert collection_requires_review(collection, list(collection.videos)) is None


def test_legacy_period_hint_does_not_hide_real_hierarchy_problem():
    collection = _season_filename_collection("S02E03-Whatever.mkv")
    collection.local_title = "Anime P21"

    assert collection_requires_review(
        collection, list(collection.videos)
    ) == FILENAME_SEASON_CONFLICT_REVIEW_REASON


def _season_filename_collection(filename: str, *, manual=False):
    collection = CatalogCollection(
        id=1, local_title="Show", normalized_local_title="show",
        relative_root_path="Anime/Show",
    )
    title = CatalogTitle(
        id=1, collection=collection, local_title="Serie 1",
        normalized_local_title="serie 1", relative_root_path="Anime/Show/Serie 1",
        part_type="season", season_number=1, season_label="S1",
        hierarchy_manual_override=manual,
        part_type_manual="season" if manual else None,
        season_number_manual=1 if manual else None,
        season_label_manual="S1" if manual else None,
    )
    Video(
        id=1, relative_path=f"{title.relative_root_path}/{filename}",
        root_folder="Anime", filename=filename, size=1, mtime_ns=1,
        catalog_title=title, catalog_collection=collection,
    )
    return collection


def test_filename_season_conflict_requires_review_but_matching_season_does_not():
    conflicting = _season_filename_collection("S02E03-Whatever.mkv")
    matching = _season_filename_collection("S01E03-Influenza.mkv")

    assert collection_requires_review(
        conflicting, list(conflicting.videos)
    ) == FILENAME_SEASON_CONFLICT_REVIEW_REASON
    assert collection_requires_review(matching, list(matching.videos)) is None


def test_confirmed_manual_season_remains_authoritative_over_filename_hint():
    collection = _season_filename_collection("S02E03-Whatever.mkv", manual=True)

    assert collection_requires_review(collection, list(collection.videos)) is None


@pytest.mark.parametrize("filename", [
    "ED.mkv",
    "NCOP.mkv",
    "NCED.mkv",
    "S01E14 [SP]-The Common Cold.mkv",
])
def test_known_unnumbered_supplementary_does_not_require_canonical_number(filename):
    collection = _season_filename_collection(filename)

    result = evaluate_collection_hierarchy(
        collection, list(collection.videos), include_legacy_fallback=False,
    )

    assert result.issues == ()


def test_numbered_op_uses_only_supplementary_sequence_without_canonical_warning():
    collection = _season_filename_collection("OP 01.mkv")
    title = collection.titles[0]
    second = Video(
        id=2, relative_path=f"{title.relative_root_path}/OP 02.mkv",
        root_folder="Anime", filename="OP 02.mkv", size=1, mtime_ns=2,
        catalog_title=title, catalog_collection=collection,
    )

    result = evaluate_collection_hierarchy(
        collection, list(collection.videos), include_legacy_fallback=False,
    )
    states = [effective_video_numbering(video, title) for video in collection.videos]

    assert result.issues == ()
    assert [state.supplementary_type for state in states] == ["op", "op"]
    assert [state.supplementary_number for state in states] == [1, 2]
    assert [state.season_episode_number for state in states] == [None, None]


def test_genuinely_unknown_supplementary_looking_filename_still_requires_review():
    collection = _season_filename_collection("Title [IV01][codec].mkv")

    result = evaluate_collection_hierarchy(
        collection, list(collection.videos), include_legacy_fallback=False,
    )

    assert [issue.code for issue in result.blocking_issues] == [
        HierarchyIssueCode.UNKNOWN_OR_MISSING_NUMBERING,
    ]


@pytest.mark.parametrize("ova_filename", [
    "High School DxD Born OVA.mkv",
    "High School DxD New OVA.mkv",
])
def test_complete_season_with_unnumbered_ova_has_no_numbering_blocker(ova_filename):
    collection = CatalogCollection(
        id=1, local_title="High School DxD", normalized_local_title="high school dxd",
        relative_root_path="Anime/High School DxD",
    )
    title = CatalogTitle(
        id=1, collection=collection, local_title="Season",
        normalized_local_title="season",
        relative_root_path="Anime/High School DxD/Season", part_type="season",
        season_number=3, season_label="S3",
    )
    regular = [
        Video(
            id=number,
            relative_path=f"{title.relative_root_path}/Title - {number:02}.mkv",
            root_folder="Anime", filename=f"Title - {number:02}.mkv",
            size=1, mtime_ns=number, catalog_title=title,
            catalog_collection=collection,
        )
        for number in range(1, 13)
    ]
    ova = Video(
        id=13, relative_path=f"{title.relative_root_path}/{ova_filename}",
        root_folder="Anime", filename=ova_filename, size=1, mtime_ns=13,
        file_type=classify_video(f"{title.relative_root_path}/{ova_filename}"),
        catalog_title=title, catalog_collection=collection,
    )

    recalculate_title_numbering(title, [*regular, ova])
    result = evaluate_collection_hierarchy(
        collection, [*regular, ova], include_legacy_fallback=False,
    )
    summary = summarize_title_numbering([*regular, ova], title)
    state = effective_video_numbering(ova, title)
    recommendations = supplementary_assignment_recommendations([*regular, ova])

    assert (summary.standard_total, summary.numbered) == (12, 12)
    assert summary.resolved_supplemental == 1
    assert summary.unknown == 0
    assert summary.gaps == ()
    assert state.is_supplementary
    assert state.supplementary_type == "ova"
    assert state.supplementary_number is None
    assert state.season_episode_number is None
    assert not any(ova in issue.videos for issue in result.blocking_issues)
    assert len(recommendations) == 1
    assert recommendations[0].supplementary_type == "ova"
    assert recommendations[0].season_number == 3
    assert recommendations[0].items[0].supplementary_number is None


def test_unnumbered_ova_does_not_hide_unconfirmed_title_structure():
    collection = CatalogCollection(
        id=1, local_title="High School DxD", normalized_local_title="high school dxd",
        relative_root_path="Anime/High School DxD",
    )
    title = CatalogTitle(
        id=1, collection=collection, local_title="High School DxD New",
        normalized_local_title="high school dxd new",
        relative_root_path="Anime/High School DxD/High School DxD New",
        part_type="title",
    )
    ova = Video(
        id=1, relative_path=f"{title.relative_root_path}/High School DxD New OVA.mkv",
        root_folder="Anime", filename="High School DxD New OVA.mkv",
        size=1, mtime_ns=1, file_type="ova", catalog_title=title,
        catalog_collection=collection,
    )

    result = evaluate_collection_hierarchy(
        collection, [ova], include_legacy_fallback=False,
    )
    state = effective_video_numbering(ova, title)
    codes = {issue.code for issue in result.blocking_issues}
    recommendations = supplementary_assignment_recommendations([ova])

    assert state.is_supplementary
    assert state.supplementary_type == "ova"
    assert state.supplementary_number is None
    assert HierarchyIssueCode.UNKNOWN_OR_MISSING_NUMBERING not in codes
    assert HierarchyIssueCode.GENERIC_STRUCTURAL_TYPE in codes
    assert recommendations == ()


def test_manual_episode_override_resolves_nonstandard_zero_review_reason():
    collection = CatalogCollection(
        local_title="Show", normalized_local_title="show",
        relative_root_path="Anime/Show", hierarchy_status="review_required",
    )
    title = CatalogTitle(
        collection=collection, local_title="Season 1",
        normalized_local_title="season 1",
        relative_root_path="Anime/Show/Season 1",
        part_type_manual="season", season_number_manual=1,
        hierarchy_manual_override=True,
    )
    zero = Video(
        relative_path="Anime/Show/Season 1/Show 00.mkv", root_folder="Anime",
        filename="Show 00.mkv", size=1, mtime_ns=1,
        catalog_title=title, catalog_collection=collection,
    )
    recalculate_title_numbering(title, [zero])

    assert collection_requires_review(collection, [zero]) == (
        "Nestandardní číslování vyžaduje ruční zařazení."
    )

    set_video_episode_override(zero, 5)
    refresh_collection_state(collection)

    assert zero.season_episode_number == 5
    assert collection_requires_review(collection, [zero]) is None
    assert collection.hierarchy_status == "verified"
    assert collection.hierarchy_note is None


def test_manual_duplicate_suspicion_is_nullable_and_independent_of_other_video_data():
    collection, title = simple_collection()
    video = title.videos[0]
    video.content_type_manual = "recap"
    title.metadata_record = TitleMetadata(
        catalog_title_id=title.id, display_title="Metadata title",
        metadata_provider="anilist", metadata_external_id="123",
    )
    original_hierarchy = (
        video.catalog_collection, video.catalog_title,
        video.catalog_collection_id, video.catalog_title_id,
        title.part_type, title.season_number, title.season_label,
    )
    original_metadata = (
        title.metadata_record, title.metadata_record.display_title,
        title.metadata_record.metadata_external_id,
    )

    assert video.duplicate_status_manual is None
    assert video.duplicate_of_video_id is None
    assert video_matches_filter(video, "manual-duplicate-suspected") is False

    set_manual_duplicate_status(video, "suspected")

    assert video.duplicate_status_manual == "suspected"
    assert video_matches_filter(video, "manual-duplicate-suspected") is True
    assert video.duplicate_of_video_id is None
    assert video.duplicate_of is None
    assert video.content_type_manual == "recap"
    assert original_hierarchy == (
        video.catalog_collection, video.catalog_title,
        video.catalog_collection_id, video.catalog_title_id,
        title.part_type, title.season_number, title.season_label,
    )
    assert original_metadata == (
        title.metadata_record, title.metadata_record.display_title,
        title.metadata_record.metadata_external_id,
    )

    set_manual_duplicate_status(video, None)

    assert video.duplicate_status_manual is None
    assert video_matches_filter(video, "manual-duplicate-suspected") is False
    assert video.duplicate_of_video_id is None


def test_manual_duplicate_suspicion_does_not_change_automatic_duplicate_detection():
    collection, title = simple_collection()
    first = title.videos[0]
    second = Video(
        id=2, relative_path=f"{collection.relative_root_path}/Episode 01 copy.mkv",
        root_folder="Anime", filename="Episode 01 copy.mkv", size=2, mtime_ns=2,
        season_episode_number=1, catalog_title=title, catalog_collection=collection,
    )

    assert len(unresolved_duplicate_groups([first, second])) == 1

    set_manual_duplicate_status(second, "suspected")

    groups = unresolved_duplicate_groups([first, second])
    assert len(groups) == 1
    assert {video.id for video in groups[0].videos} == {first.id, second.id}


def test_manual_duplicate_status_rejects_explicit_not_duplicate_state():
    _, title = simple_collection()

    with pytest.raises(ValueError, match="Neplatný stav"):
        set_manual_duplicate_status(title.videos[0], "not_duplicate")

    assert title.videos[0].duplicate_status_manual is None


def test_internal_period_hint_does_not_infer_season_count_or_change_identity():
    engine, collection_id, title_id = seeded_collection()
    with Session(engine) as session:
        collection = session.get(CatalogCollection, collection_id)
        title = session.get(CatalogTitle, title_id)
        assert extract_local_period_hint(collection.local_title) == "Z18-L20"
        assert title.season_number is None
        assert collection.local_title == "Anime title (Z18-L20)"
        assert collection.relative_root_path == "Anime/Anime title (Z18-L20)"


def test_single_generic_title_gets_editable_part_confirmation_suggestion():
    collection, title = simple_collection()
    title.metadata_record = TitleMetadata(
        catalog_title_id=title.id, display_title="Akame ga Kill!",
        format="TV", episode_count=24,
    )

    suggestion = single_title_confirmation_suggestion(collection)

    assert suggestion is not None
    assert suggestion.title is title
    assert suggestion.metadata_supports_tv is True
    assert suggestion.proposed_part_type == "season"
    assert suggestion.proposed_season_number == 1
    assert suggestion.proposed_season_label is None
    assert suggestion.display_label == "Season 1 (S1)"
    assert title.season_number_manual is None
    assert title.season_label_manual is None
    assert title.part_type_manual is None
    assert not title.hierarchy_manual_override


@pytest.mark.parametrize(("part_type", "expected"), [
    ("part", "Part"), ("cour", "Cour"),
    ("film", "Film"), ("ova", "OVA"), ("special", "Special"),
    ("preview", "Preview"), ("recap", "Recap"), ("bonus", "Bonus"),
    ("other", "Other"),
])
def test_confirmation_proposal_has_human_label_for_supported_part_types(
    part_type, expected,
):
    collection, _ = simple_collection()
    suggestion = single_title_confirmation_suggestion(collection)

    displayed = replace(
        suggestion, proposed_part_type=part_type,
        proposed_season_number=None, proposed_season_label=None,
    )

    assert displayed.display_label == expected


@pytest.mark.parametrize("season_number", [1, 2, 3])
def test_confirmed_season_number_resolves_period_hint_review(season_number):
    collection, title = simple_collection()
    collection.hierarchy_note = PERIOD_HINT_REVIEW_REASON
    video = title.videos[0]
    original_title = video.catalog_title
    episode_values = (
        video.local_episode_number, video.season_episode_number,
        video.absolute_episode_number, video.external_episode_number,
    )
    metadata = TitleMetadata(
        catalog_title_id=title.id, display_title="Akame ga Kill!",
        metadata_provider="anilist", metadata_external_id="20613", format="TV",
    )
    link = ExternalTitleLink(
        catalog_title_id=title.id, provider="anilist", external_id="20613",
        match_method="manual_search", is_primary=True, is_manual=True,
    )
    title.metadata_record = metadata
    title.external_links.append(link)
    display_title = catalog_title_display_title(title)
    filename = video.filename
    relative_path = video.relative_path

    changed = apply_single_title_confirmation(
        collection, part_type="season", season_number=season_number, season_label=None,
    )

    assert changed is title
    assert title.season_number_manual == season_number
    assert title.season_label_manual == f"S{season_number}"
    assert title.part_type_manual == "season"
    assert title.hierarchy_manual_override is True
    assert title.hierarchy_verified_at is not None
    assert collection.hierarchy_status == "verified"
    assert collection.hierarchy_verified_at is not None
    assert collection.hierarchy_note is None
    assert video.catalog_title is original_title
    assert (
        video.local_episode_number, video.season_episode_number,
        video.absolute_episode_number, video.external_episode_number,
    ) == episode_values
    assert title.metadata_record is metadata
    assert title.external_links == [link]
    assert catalog_title_display_title(title) == display_title
    assert video.filename == filename
    assert video.relative_path == relative_path


def test_confirmed_season_can_keep_number_and_label_unspecified():
    collection, title = simple_collection()
    title.part_type = "season"
    title.season_number = 2
    title.season_label = "S2"

    apply_single_title_confirmation(
        collection, part_type="season", season_number=None, season_label=None,
    )

    assert title.part_type_manual == "season"
    assert title.season_number_manual is None
    assert title.season_label_manual is None
    assert title.effective_season_number is None
    assert title.effective_season_label is None
    assert collection.hierarchy_status == "verified"


def test_single_title_confirmation_snapshots_season_scope_and_part_ordinal():
    collection, title = simple_collection(part_type="season")
    title.season_number = 1
    title.season_label = "S1"

    apply_single_title_confirmation(
        collection, part_type="part", season_number=1, season_label=None,
        part_number=2,
    )

    assert title.part_type_manual == "part"
    assert title.season_number_manual == 1
    assert title.part_number_manual == 2
    assert title.season_label_manual == "S1"
    assert title.hierarchy_manual_override is True
    assert title.effective_season_number == 1
    assert title.effective_part_number == 2


def test_collection_confirmation_snapshots_part_ordinal_as_manual_authority():
    collection, title = simple_collection(part_type="part", status="automatic")
    title.season_number = 1
    title.part_number = 2
    title.season_label = "S1"

    confirm_effective_collection_hierarchy(collection)

    assert title.part_type_manual == "part"
    assert title.season_number_manual == 1
    assert title.part_number_manual == 2
    assert title.season_label_manual == "S1"
    assert title.hierarchy_manual_override is True


def test_manual_part_requires_ordinal_and_stores_season_and_part_independently():
    collection, title = simple_collection(part_type="season", status="automatic")
    title.part_number = 2

    with pytest.raises(ValueError, match="číslo Part"):
        set_manual_title_hierarchy(
            title, season_number=1, season_label="S1", part_type="part",
            sort_order=1, hierarchy_verified=True,
        )

    set_manual_title_hierarchy(
        title, season_number=1, season_label=None, part_type="part",
        part_number=2, sort_order=1, hierarchy_verified=True,
    )

    assert title.season_number_manual == 1
    assert title.part_number_manual == 2
    assert title.season_label_manual == "S1"
    assert collection.hierarchy_status == "verified"


def _season_sibling(
    collection: CatalogCollection,
    *,
    title_id: int,
    season_number: int,
    part_number: int | None = None,
    manual: bool = False,
) -> CatalogTitle:
    return CatalogTitle(
        id=title_id,
        collection=collection,
        local_title=f"Season {season_number} part {title_id}",
        normalized_local_title=f"season {season_number} part {title_id}",
        relative_root_path=f"Anime/Show/.catalog-part-{title_id}",
        part_type="season",
        season_number=season_number,
        season_label=f"S{season_number}",
        part_type_manual="season" if manual else None,
        season_number_manual=season_number if manual else None,
        season_label_manual=f"S{season_number}" if manual else None,
        part_number_manual=part_number if manual else None,
        hierarchy_manual_override=manual,
        hierarchy_verified_at=utc_now() if manual else None,
    )


def test_manual_season_accepts_part_number_as_independent_axis():
    collection = CatalogCollection(
        id=1, local_title="Show", normalized_local_title="show",
        relative_root_path="Anime/Show",
    )
    first = _season_sibling(
        collection, title_id=1, season_number=1, part_number=1, manual=True,
    )
    second = _season_sibling(collection, title_id=2, season_number=2)

    set_manual_title_hierarchy(
        second,
        season_number=1,
        season_label="S1",
        part_type="season",
        part_number=2,
        sort_order=None,
        hierarchy_verified=True,
    )

    assert (
        first.effective_season_number,
        first.effective_part_number,
        second.effective_season_number,
        second.effective_part_number,
    ) == (1, 1, 1, 2)
    assert second.part_type_manual == "season"
    assert second.part_number_manual == 2
    assert catalog_title_series_label(second) == "S1 · Part 2"
    assert collection.hierarchy_status == "verified"


@pytest.mark.parametrize(
    ("existing_part", "submitted_part", "message"),
    [
        (None, None, "všem jejím částem"),
        (None, 2, "všem jejím částem"),
        (1, 1, "Part 1 je použito vícekrát"),
    ],
)
def test_manual_duplicate_season_requires_complete_unique_part_numbers(
    existing_part,
    submitted_part,
    message,
):
    collection = CatalogCollection(
        id=1, local_title="Show", normalized_local_title="show",
        relative_root_path="Anime/Show",
    )
    _season_sibling(
        collection,
        title_id=1,
        season_number=1,
        part_number=existing_part,
        manual=True,
    )
    second = _season_sibling(collection, title_id=2, season_number=2)

    with pytest.raises(ValueError, match=message):
        set_manual_title_hierarchy(
            second,
            season_number=1,
            season_label="S1",
            part_type="season",
            part_number=submitted_part,
            sort_order=None,
            hierarchy_verified=True,
        )

    assert second.part_type_manual is None
    assert second.season_number_manual is None
    assert second.part_number_manual is None


def test_manual_different_seasons_do_not_require_part_numbers():
    collection = CatalogCollection(
        id=1, local_title="Show", normalized_local_title="show",
        relative_root_path="Anime/Show",
    )
    first = _season_sibling(
        collection, title_id=1, season_number=1, manual=True,
    )
    second = _season_sibling(collection, title_id=2, season_number=2)

    set_manual_title_hierarchy(
        second,
        season_number=2,
        season_label="S2",
        part_type="season",
        part_number=None,
        sort_order=None,
        hierarchy_verified=True,
    )

    assert first.effective_part_number is None
    assert second.effective_part_number is None
    assert collection.hierarchy_status == "verified"


def test_part_without_ordinal_remains_review_required():
    collection, title = simple_collection(part_type="part", status="automatic")
    title.season_number = 1
    title.season_label = "S1"

    refresh_collection_state(collection)

    assert collection.hierarchy_status == "review_required"
    assert collection.hierarchy_note == MISSING_PART_NUMBER_REVIEW_REASON


def test_historical_incomplete_part_snapshot_is_not_currently_verified():
    collection, title = simple_collection(part_type="title", status="verified")
    historical_timestamp = utc_now()
    title.part_number = 2
    title.part_type_manual = "part"
    title.season_number_manual = 1
    title.season_label_manual = "Part 2"
    title.part_number_manual = None
    title.hierarchy_manual_override = True
    title.hierarchy_verified_at = historical_timestamp

    refresh_collection_state(collection)

    assert collection.hierarchy_status == "review_required"
    assert collection.hierarchy_note.startswith(
        "Historické ruční zařazení není úplné."
    )
    assert catalog_title_hierarchy_is_verified(title) is False
    assert manual_hierarchy_snapshot_issue(title) == (
        "Pro typ Part potvrďte číslo Part."
    )
    assert title.effective_season_number is None
    assert title.effective_part_number is None
    assert title.effective_part_type == "title"
    assert catalog_title_series_label(title) == "—"
    assert title.part_number_manual is None
    assert title.hierarchy_manual_override is True
    assert title.hierarchy_verified_at == historical_timestamp

    set_manual_title_hierarchy(
        title, part_type="part", season_number=1, part_number=2,
        season_label=None, sort_order=1, hierarchy_verified=True,
    )

    assert title.part_number_manual == 2
    assert catalog_title_hierarchy_is_verified(title) is True
    assert collection.hierarchy_status == "verified"


@pytest.mark.parametrize(("season_number", "season_label", "sort_order", "verified"), [
    (2, "S2", 1, False),
    (2, "S2", 1, True),
    (None, None, None, True),
])
def test_manual_title_hierarchy_rejects_snapshot_without_concrete_type(
    season_number, season_label, sort_order, verified,
):
    collection, title = simple_collection(part_type="season", status="automatic")
    original = (
        title.season_number_manual, title.season_label_manual,
        title.part_type_manual, title.sort_order_manual,
        title.hierarchy_manual_override, title.hierarchy_verified_at,
    )

    with pytest.raises(ValueError, match="zvolte konkrétní typ části"):
        set_manual_title_hierarchy(
            title, season_number=season_number, season_label=season_label,
            part_type=None, sort_order=sort_order,
            hierarchy_verified=verified,
        )

    assert (
        title.season_number_manual, title.season_label_manual,
        title.part_type_manual, title.sort_order_manual,
        title.hierarchy_manual_override, title.hierarchy_verified_at,
    ) == original


def test_manual_title_hierarchy_stores_authoritative_season_two_snapshot():
    collection, title = simple_collection(part_type="season", status="automatic")

    set_manual_title_hierarchy(
        title, season_number=2, season_label="S2", part_type="season",
        sort_order=1, hierarchy_verified=True,
    )

    assert title.part_type_manual == "season"
    assert title.season_number_manual == 2
    assert title.part_number_manual is None
    assert title.season_label_manual == "S2"
    assert title.sort_order_manual == 1
    assert title.hierarchy_manual_override is True
    assert title.hierarchy_verified_at is not None
    assert collection.hierarchy_status == "verified"


def test_refresh_preserves_historical_incomplete_manual_override_for_review():
    collection, title = simple_collection(status="verified")
    verified_at = utc_now()
    title.season_number_manual = 2
    title.season_label_manual = "S2"
    title.sort_order_manual = 1
    title.hierarchy_manual_override = True
    title.hierarchy_verified_at = verified_at

    refresh_collection_state(collection)

    assert title.part_type_manual is None
    assert title.season_number_manual == 2
    assert title.season_label_manual == "S2"
    assert title.sort_order_manual == 1
    assert title.hierarchy_manual_override is True
    assert title.hierarchy_verified_at == verified_at
    assert collection.hierarchy_status == "review_required"
    assert collection.hierarchy_note.startswith(
        "Historické ruční zařazení není úplné."
    )


def test_new_unknown_reopens_confirmed_season_and_manual_numbering_resolves_it():
    collection, title = simple_collection()
    apply_single_title_confirmation(
        collection, part_type="season", season_number=1, season_label=None,
    )
    unknown = Video(
        id=2, relative_path=f"{collection.relative_root_path}/new-unknown.mkv",
        root_folder="Anime", filename="new-unknown.mkv", size=1, mtime_ns=1,
        catalog_title=title, catalog_collection=collection,
    )

    refresh_collection_state(collection)

    assert collection.hierarchy_status == "review_required"
    assert collection.hierarchy_note != PERIOD_HINT_REVIEW_REASON
    assert collection.hierarchy_verified_at is None

    set_video_episode_override(unknown, 2)
    refresh_collection_state(collection)

    assert collection.hierarchy_status == "verified"
    assert collection.hierarchy_note is None
    assert collection.hierarchy_verified_at is not None


def test_nonblocking_period_hint_collection_reopens_for_new_scan_problem(
    tmp_path: Path, monkeypatch,
):
    folder = tmp_path / "Asobi Asobase (L18)"
    folder.mkdir()
    episode_paths = [
        folder / f"Asobi Asobase - {number:02}.mkv" for number in range(1, 13)
    ]
    for path in episode_paths:
        path.write_bytes(b"video")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)

    with sessions() as session:
        scan_library(session, tmp_path)
        collection = session.scalar(select(CatalogCollection))
        assert collection.hierarchy_status == "automatic"
        assert collection.hierarchy_note is None

        title = apply_single_title_confirmation(
            collection, part_type="season", season_number=2, season_label=None,
        )
        session.commit()
        collection_id, title_id = collection.id, title.id
        assert collection.hierarchy_status == "verified"
        assert collection.hierarchy_note is None
        assert title.season_number_manual == 2
        assert title.season_label_manual == "S2"

        unknown_path = folder / "Asobi Asobase new material.mkv"
        unknown_path.write_bytes(b"video")
        scan_library(session, tmp_path)
        unknown = session.scalar(select(Video).where(Video.filename == unknown_path.name))
        assert collection.hierarchy_status == "review_required"
        assert collection.hierarchy_note == (
            "Číslování nebo nezařazený obsah stále vyžaduje kontrolu."
        )
        assert unknown.catalog_title_id == title_id
        issue_codes = {
            issue.code for issue in evaluate_collection_hierarchy(
                collection,
                list(collection.videos),
                include_legacy_fallback=False,
            ).issues
        }
        assert HierarchyIssueCode.UNKNOWN_OR_MISSING_NUMBERING in issue_codes
        assert HierarchyIssueCode.UNASSIGNED_VIDEO not in issue_codes
        assert HierarchyIssueCode.MANUAL_SPLIT_UNMATCHED not in issue_codes
        assert unknown.catalog_title_id == title_id

        move_videos_to_title(session, collection_id, [unknown.id], title_id)
        set_video_episode_override(unknown, 13)
        refresh_collection_state(collection)
        session.commit()
        unknown_id = unknown.id

    with sessions() as session:
        collection = session.get(CatalogCollection, collection_id)
        unknown = session.get(Video, unknown_id)
        assert collection.hierarchy_status == "verified"
        assert collection.hierarchy_note is None
        assert collection.local_period_hint == "L18"
        title = session.get(CatalogTitle, title_id)
        assert title.season_number_manual == 2
        assert title.season_label_manual == "S2"
        assert title.part_type_manual == "season"
        assert unknown.catalog_title_id == title_id
        assert unknown.season_episode_number == 13
        assert [path.read_bytes() for path in episode_paths] == [b"video"] * 12
        assert unknown_path.read_bytes() == b"video"


def test_confirmed_duplicate_persists_can_change_primary_and_can_be_cleared():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions() as session:
        collection = CatalogCollection(
            local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show", hierarchy_status="verified",
        )
        title = CatalogTitle(
            collection=collection, local_title="Season 1",
            normalized_local_title="season 1", relative_root_path="Anime/Show/Season 1",
            part_type_manual="season", hierarchy_manual_override=True,
        )
        first = Video(
            relative_path="Anime/Show/Season 1/Show - 01.mkv", root_folder="Anime",
            filename="Show - 01.mkv", size=100, mtime_ns=1,
            catalog_title=title, catalog_collection=collection,
        )
        second = Video(
            relative_path="Anime/Show/Season 1/Show 01.mp4", root_folder="Anime",
            filename="Show 01.mp4", size=200, mtime_ns=1,
            catalog_title=title, catalog_collection=collection,
        )
        session.add(collection)
        session.flush()
        refresh_collection_state(collection)
        assert summarize_title_numbering(list(title.videos), title).duplicate_numbers == (1,)
        set_manual_duplicate_status(second, "suspected")
        paths = [(video.filename, video.relative_path) for video in title.videos]

        confirm_duplicate_groups(session, collection.id, [([first.id, second.id], first.id)])
        session.commit()
        collection_id, first_id, second_id = collection.id, first.id, second.id
        assert collection.hierarchy_status == "verified"
        assert collection.hierarchy_note is None

    with sessions() as session:
        collection = session.get(CatalogCollection, collection_id)
        first, second = session.get(Video, first_id), session.get(Video, second_id)
        assert second.duplicate_of_video_id == first.id
        assert second.duplicate_status_manual == "suspected"
        summary = summarize_title_numbering(list(first.catalog_title.videos), first.catalog_title)
        assert (summary.total, summary.standard_total, summary.numbered) == (2, 1, 1)
        assert summary.duplicate_numbers == ()
        assert summary.confirmed_duplicates == 1
        assert [(video.filename, video.relative_path) for video in first.catalog_title.videos] == paths

        confirm_duplicate_videos(
            session, collection.id, [first.id, second.id], second.id,
        )
        session.commit()

    with sessions() as session:
        collection = session.get(CatalogCollection, collection_id)
        first, second = session.get(Video, first_id), session.get(Video, second_id)
        assert first.duplicate_of_video_id == second.id
        assert second.duplicate_of_video_id is None
        assert second.duplicate_status_manual == "suspected"

        clear_confirmed_duplicate_videos(
            session, collection.id, [first.id, second.id],
        )
        session.commit()
        summary = summarize_title_numbering(list(first.catalog_title.videos), first.catalog_title)
        assert summary.confirmed_duplicates == 0
        assert summary.duplicate_numbers == (1,)
        assert collection.hierarchy_status == "review_required"
        assert collection.hierarchy_note == (
            "Číslování nebo nezařazený obsah stále vyžaduje kontrolu."
        )
        assert [(video.filename, video.relative_path) for video in first.catalog_title.videos] == paths


@pytest.mark.parametrize("part_type", ["film", "ova"])
def test_non_series_manual_type_does_not_get_part_confirmation_suggestion(part_type):
    collection, title = simple_collection()
    title.part_type_manual = part_type
    title.hierarchy_manual_override = True

    assert single_title_confirmation_suggestion(collection) is None


def test_collection_with_two_titles_does_not_get_part_confirmation_suggestion():
    collection, _ = simple_collection()
    CatalogTitle(
        id=2, collection=collection, local_title="Second title",
        normalized_local_title="second title",
        relative_root_path=f"{collection.relative_root_path}/Second title",
        part_type="title",
    )

    assert single_title_confirmation_suggestion(collection) is None


def test_manual_season_two_does_not_get_duplicate_part_confirmation_suggestion():
    collection, title = simple_collection()
    title.season_number_manual = 2
    title.season_label_manual = "S2"
    title.part_type_manual = "season"
    title.hierarchy_manual_override = True

    assert single_title_confirmation_suggestion(collection) is None


def test_conflicting_hierarchy_does_not_get_part_confirmation_suggestion():
    collection, _ = simple_collection(status="conflict")

    assert single_title_confirmation_suggestion(collection) is None


def test_title_without_videos_does_not_get_part_confirmation_suggestion():
    collection, _ = simple_collection(with_video=False)

    assert single_title_confirmation_suggestion(collection) is None


def test_automatic_folder_season_is_only_editable_confirmation_proposal():
    collection, title = simple_collection()
    title.season_number = 2
    title.season_label = "S2"
    title.part_type = "season"

    suggestion = single_title_confirmation_suggestion(collection)

    assert suggestion is not None
    assert suggestion.proposed_season_number == 2
    assert suggestion.proposed_season_label == "S2"
    assert suggestion.display_label == "Season 2 (S2)"
    assert title.season_number_manual is None
    assert title.season_label_manual is None
    assert title.part_type_manual is None


def test_previously_verified_title_structure_does_not_get_suggestion():
    collection, title = simple_collection()
    title.hierarchy_verified_at = utc_now()

    assert single_title_confirmation_suggestion(collection) is None


def test_suggestion_and_simple_preview_data_are_read_only():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection, _ = simple_collection()
        session.add(collection)
        session.commit()

        assert single_title_confirmation_suggestion(collection) is not None
        rows = simple_definition_rows(collection)
        definitions = parse_simple_definitions(rows)
        preview_assignments(list(collection.videos), definitions)

        assert not session.dirty
        assert not session.new
        assert not session.deleted


def test_simple_definition_form_uses_existing_backend_semantics():
    rows = [
        {
            "title_id": "1", "local_title": "Part 1",
            "season_number_manual": "1", "season_label_manual": "S1",
            "part_number_manual": "1", "part_type_manual": "part",
            "episode_start": "1", "episode_end": "12",
            "episode_start_offset": "0", "numbering_mode": "absolute",
            "sort_order": "1", "filename_pattern": "", "video_ids": "1, 2 3",
        },
        {
            "title_id": "", "local_title": "", "numbering_mode": "unknown",
        },
    ]

    parsed = parse_simple_definitions(rows)

    assert len(parsed) == 1
    assert parsed[0] == ManualTitleDefinition(
        title_id=1, local_title="Part 1", manual_display_title=None,
        season_number_manual=1, season_label_manual="S1", part_number_manual=1,
        part_type_manual="part", episode_start=1, episode_end=12,
        episode_start_offset=0, numbering_mode="absolute", sort_order=1,
        filename_pattern=None, video_ids=(1, 2, 3),
    )


def test_manual_split_creates_two_titles_and_assigns_ranges():
    engine, collection_id, title_id = seeded_collection()
    with Session(engine) as session:
        preview = apply_manual_split(session, collection_id, definitions(title_id))
        session.commit()
        assert not preview.conflicts
        assert not preview.unmatched_video_ids
        titles = session.scalars(select(CatalogTitle).order_by(CatalogTitle.sort_order_manual)).all()
        assert [title.local_title for title in titles] == ["Part 1", "Part 2"]
        assert [title.season_number_manual for title in titles] == [1, 1]
        assert [title.part_number_manual for title in titles] == [1, 2]
        assert [title.effective_part_number for title in titles] == [1, 2]
        assert [len(title.videos) for title in titles] == [13, 13]
        assert titles[1].videos[0].season_episode_number == 1
        collection = session.get(CatalogCollection, collection_id)
        assert collection.hierarchy_status == "verified"
        assert collection.hierarchy_verified_at is not None


def test_manual_split_can_repair_existing_duplicate_seasons_atomically():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show", hierarchy_status="verified",
        )
        titles = []
        for ordinal, episode in ((1, 1), (2, 14)):
            title = CatalogTitle(
                collection=collection,
                local_title=f"Half {ordinal}",
                normalized_local_title=f"half {ordinal}",
                relative_root_path=f"Anime/Show/.catalog-part-{ordinal}",
                part_type="season",
                season_number=1,
                season_label="S1",
                part_type_manual="season",
                season_number_manual=1,
                season_label_manual="S1",
                hierarchy_manual_override=True,
                hierarchy_verified_at=utc_now(),
            )
            Video(
                relative_path=f"Anime/Show/Half {ordinal}/Episode {episode:02}.mkv",
                root_folder="Anime",
                filename=f"Episode {episode:02}.mkv",
                size=1,
                mtime_ns=episode,
                catalog_title=title,
                catalog_collection=collection,
            )
            titles.append(title)
        session.add(collection)
        session.commit()

        definitions = [
            ManualTitleDefinition(
                title_id=title.id,
                local_title=title.local_title,
                manual_display_title=None,
                season_number_manual=1,
                season_label_manual="S1",
                part_number_manual=ordinal,
                part_type_manual="season",
                episode_start=start,
                episode_end=end,
                episode_start_offset=None,
                numbering_mode="unknown",
                sort_order=None,
                filename_pattern=None,
                video_ids=(),
            )
            for title, ordinal, start, end in (
                (titles[0], 1, 1, 13),
                (titles[1], 2, 14, 26),
            )
        ]

        apply_manual_split(session, collection.id, definitions)
        session.flush()

        assert [title.part_type_manual for title in titles] == ["season", "season"]
        assert [title.season_number_manual for title in titles] == [1, 1]
        assert [title.part_number_manual for title in titles] == [1, 2]
        assert collection.hierarchy_status == "verified"
        assert [title.effective_part_number for title in titles] == [1, 2]
        assert [len(title.videos) for title in titles] == [1, 1]
        assert [title.videos[0].season_episode_number for title in titles] == [1, 14]


def _confirm_seeded_collection_as_unsplit_season(
    session: Session, collection_id: int, title_id: int,
) -> CatalogTitle:
    title = session.get(CatalogTitle, title_id)
    set_manual_title_hierarchy(
        title,
        season_number=1,
        season_label="S1",
        part_type="season",
        part_number=None,
        sort_order=None,
        hierarchy_verified=True,
    )
    session.flush()
    return title


@pytest.mark.parametrize(
    ("requested_part", "selected_numbers", "complementary_part"),
    (
        (2, range(13, 25), 1),
        (1, range(1, 13), 2),
    ),
)
def test_first_individual_video_split_requires_and_atomically_applies_complementary_part(
    requested_part, selected_numbers, complementary_part,
):
    engine, collection_id, title_id = seeded_collection(24)
    selected_numbers = set(selected_numbers)
    with Session(engine) as session:
        source = _confirm_seeded_collection_as_unsplit_season(
            session, collection_id, title_id,
        )
        selected_ids = [
            video.id for video in source.videos
            if video.local_episode_number in selected_numbers
        ]
        original_membership = {
            video.id: video.catalog_title_id for video in source.videos
        }
        original_numbering = {
            video.id: video.season_episode_number for video in source.videos
        }

        proposal = complementary_season_part_proposal_for_videos(
            session,
            collection_id,
            selected_ids,
            part_type="season",
            season_number=1,
            part_number=requested_part,
            local_title=f"Part {requested_part}",
            season_label="S1",
        )

        assert proposal is not None
        assert proposal.complementary_part_number == complementary_part
        assert proposal.selected.label == (
            "E01–E12" if requested_part == 1 else "E13–E24"
        )
        assert {
            video.id: video.catalog_title_id for video in source.videos
        } == original_membership
        created = create_title_with_complementary_season_part(
            session,
            collection_id,
            selected_ids,
            local_title=f"Part {requested_part}",
            season_number=1,
            season_label="S1",
            part_number=requested_part,
            expected_source_title_id=proposal.source_title_id,
            expected_complementary_part_number=complementary_part,
        )
        session.flush()

        titles = session.scalars(
            select(CatalogTitle).order_by(CatalogTitle.part_number_manual)
        ).all()
        assert [title.id for title in titles if title.part_number_manual == complementary_part] == [title_id]
        assert [(title.season_number_manual, title.part_number_manual) for title in titles] == [
            (1, 1), (1, 2),
        ]
        assert {
            video.local_episode_number for video in created.videos
        } == selected_numbers
        assert {
            video.local_episode_number for video in source.videos
        } == set(range(1, 25)) - selected_numbers
        assert {
            video.id: video.season_episode_number
            for title in titles for video in title.videos
        } == original_numbering
        assert session.get(CatalogCollection, collection_id).hierarchy_status == "verified"


def test_rejected_or_stale_complementary_part_proposal_writes_nothing():
    engine, collection_id, title_id = seeded_collection(24)
    with Session(engine) as session:
        source = _confirm_seeded_collection_as_unsplit_season(
            session, collection_id, title_id,
        )
        selected_ids = [
            video.id for video in source.videos
            if video.local_episode_number >= 13
        ]
        before = {
            video.id: video.catalog_title_id for video in source.videos
        }
        proposal = complementary_season_part_proposal_for_videos(
            session, collection_id, selected_ids,
            part_type="season", season_number=1, part_number=2,
        )
        assert proposal is not None
        assert len(session.get(CatalogCollection, collection_id).titles) == 1
        assert {video.id: video.catalog_title_id for video in source.videos} == before

        with pytest.raises(ValueError, match="neodpovídá aktuálním datům"):
            create_title_with_complementary_season_part(
                session, collection_id, selected_ids,
                local_title="Part 2", season_number=1, season_label="S1",
                part_number=2, expected_source_title_id=title_id,
                expected_complementary_part_number=2,
            )
        assert source.part_number_manual is None
        assert len(session.get(CatalogCollection, collection_id).titles) == 1
        assert {video.id: video.catalog_title_id for video in source.videos} == before


def test_unsplit_season_part_three_has_no_complementary_inference_and_is_rejected():
    engine, collection_id, title_id = seeded_collection(24)
    with Session(engine) as session:
        source = _confirm_seeded_collection_as_unsplit_season(
            session, collection_id, title_id,
        )
        selected_ids = [
            video.id for video in source.videos
            if video.local_episode_number >= 13
        ]
        assert complementary_season_part_proposal_for_videos(
            session, collection_id, selected_ids,
            part_type="season", season_number=1, part_number=3,
        ) is None
        with pytest.raises(ValueError, match="Season 1 už"):
            create_title_from_videos(
                session, collection_id, selected_ids,
                local_title="Part 3", part_type="season",
                season_number=1, part_number=3,
            )
        session.rollback()
        collection = session.get(CatalogCollection, collection_id)
        assert len(collection.titles) == 1
        assert collection.titles[0].part_number_manual is None
        assert len(collection.titles[0].videos) == 24


def test_existing_parts_accept_third_part_without_complementary_proposal():
    engine, collection_id, title_id = seeded_collection(24)
    with Session(engine) as session:
        apply_manual_split(session, collection_id, [
            ManualTitleDefinition(
                title_id=title_id if part == 1 else None,
                local_title=f"Part {part}", manual_display_title=None,
                season_number_manual=1, season_label_manual="S1",
                part_number_manual=part, part_type_manual="season",
                episode_start=start, episode_end=end,
                episode_start_offset=None, numbering_mode="season_local",
                sort_order=None, filename_pattern=None, video_ids=(),
            )
            for part, start, end in ((1, 1, 12), (2, 13, 24))
        ])
        session.flush()
        second = next(
            title for title in session.get(CatalogCollection, collection_id).titles
            if title.part_number_manual == 2
        )
        selected_ids = [
            video.id for video in second.videos
            if video.local_episode_number >= 21
        ]
        assert complementary_season_part_proposal_for_videos(
            session, collection_id, selected_ids,
            part_type="season", season_number=1, part_number=3,
        ) is None
        created = create_title_from_videos(
            session, collection_id, selected_ids,
            local_title="Part 3", part_type="season",
            season_number=1, season_label="S1", part_number=3,
        )
        session.flush()
        assert created.part_number_manual == 3
        assert sorted(
            title.part_number_manual
            for title in session.get(CatalogCollection, collection_id).titles
        ) == [1, 2, 3]


def _seed_existing_split_candidate(
    session: Session,
    ranges: tuple[tuple[int, int], tuple[int, int]],
) -> tuple[CatalogCollection, tuple[CatalogTitle, CatalogTitle]]:
    collection = CatalogCollection(
        local_title="Genjitsu", normalized_local_title="genjitsu",
        relative_root_path="Anime/Genjitsu",
    )
    titles = []
    for index, (start, end) in enumerate(ranges, 1):
        title = CatalogTitle(
            collection=collection, local_title=f"Half {index}",
            normalized_local_title=f"half {index}",
            relative_root_path=f"Anime/Genjitsu/Half {index}",
            part_type="season", season_number=1, season_label="S1",
            part_type_manual="season", season_number_manual=1,
            season_label_manual="S1", hierarchy_manual_override=True,
            hierarchy_verified_at=utc_now(),
        )
        for episode in range(start, end + 1):
            Video(
                relative_path=f"Anime/Genjitsu/Half {index}/Episode {episode:02}.mkv",
                root_folder="Anime", filename=f"Episode {episode:02}.mkv",
                size=1, mtime_ns=episode, local_episode_number=episode,
                season_episode_number=episode,
                catalog_title=title, catalog_collection=collection,
            )
        titles.append(title)
    session.add(collection)
    session.flush()
    refresh_collection_state(collection, recalculate=False)
    return collection, (titles[0], titles[1])


def test_existing_contiguous_split_proposal_changes_only_manual_part_identity():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection, titles = _seed_existing_split_candidate(
            session, ((1, 13), (14, 26)),
        )
        metadata = TitleMetadata(
            catalog_title_id=titles[0].id,
            display_title="Genjitsu",
            metadata_provider="anilist",
        )
        session.add(metadata)
        session.flush()
        title_ids = tuple(title.id for title in titles)
        membership = {
            video.id: video.catalog_title_id for video in collection.videos
        }
        numbering = {
            video.id: video.season_episode_number for video in collection.videos
        }
        proposals = existing_split_season_part_proposals(collection)

        assert len(proposals) == 1
        assert proposals[0].title_ids == title_ids
        assert [item.episode_range.label for item in proposals[0].items] == [
            "E01–E13", "E14–E26",
        ]
        assert [item.proposed_part_number for item in proposals[0].items] == [1, 2]
        confirm_existing_split_season_parts(
            session, collection.id, season_number=1,
            title_ids=list(proposals[0].title_ids),
        )
        session.flush()

        assert tuple(title.id for title in titles) == title_ids
        assert [title.part_number_manual for title in titles] == [1, 2]
        assert {video.id: video.catalog_title_id for video in collection.videos} == membership
        assert {video.id: video.season_episode_number for video in collection.videos} == numbering
        assert metadata.catalog_title_id == titles[0].id
        assert collection.hierarchy_status == "verified"
        assert all(
            issue.code != HierarchyIssueCode.AMBIGUOUS_SPLIT_SEASON
            for issue in evaluate_collection_hierarchy(collection).issues
        )


@pytest.mark.parametrize("ranges", (((1, 12), (1, 12)), ((1, 12), (7, 18)), ((1, 12), (14, 24))))
def test_existing_split_proposal_rejects_identical_overlap_and_gap(ranges):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection, _ = _seed_existing_split_candidate(session, ranges)
        assert existing_split_season_part_proposals(collection) == ()
        assert any(
            issue.code == HierarchyIssueCode.AMBIGUOUS_SPLIT_SEASON
            for issue in evaluate_collection_hierarchy(collection).blocking_issues
        )


def test_advanced_split_preview_rejects_partial_season_parts_and_accepts_complete_plan():
    engine, collection_id, title_id = seeded_collection(24)
    with Session(engine) as session:
        collection = session.get(CatalogCollection, collection_id)
        partial = [
            ManualTitleDefinition(
                title_id=title_id if part_number is None else None,
                local_title="First half" if part_number is None else "Part 2",
                manual_display_title=None,
                season_number_manual=1,
                season_label_manual="S1",
                part_number_manual=part_number,
                part_type_manual="season",
                episode_start=start,
                episode_end=end,
                episode_start_offset=None,
                numbering_mode="season_local",
                sort_order=None,
                filename_pattern=None,
                video_ids=(),
            )
            for part_number, start, end in ((None, 1, 12), (2, 13, 24))
        ]
        with pytest.raises(ValueError, match="všem jejím částem"):
            preview_assignments(
                list(collection.videos), partial, collection=collection,
            )

        complete = [
            replace(definition, part_number_manual=index)
            for index, definition in enumerate(partial, 1)
        ]
        preview = preview_assignments(
            list(collection.videos), complete, collection=collection,
        )
        assert not preview.conflicts
        assert not preview.unmatched_video_ids


def test_overlapping_ranges_require_explicit_confirmation():
    engine, collection_id, title_id = seeded_collection()
    overlap = definitions(title_id)
    overlap[1] = ManualTitleDefinition(
        **{**overlap[1].__dict__, "episode_start": 13}
    )
    with Session(engine) as session:
        preview = preview_assignments(list(session.get(CatalogCollection, collection_id).videos), overlap)
        assert preview.conflicts
        with pytest.raises(ValueError, match="explicitní potvrzení"):
            apply_manual_split(session, collection_id, overlap)


def test_video_outside_ranges_stays_unassigned():
    engine, collection_id, title_id = seeded_collection(27)
    with Session(engine) as session:
        preview = apply_manual_split(session, collection_id, definitions(title_id))
        session.commit()
        last = session.scalar(select(Video).where(Video.filename == "Episode 27.mkv"))
        assert last.id in preview.unmatched_video_ids
        assert last.catalog_title_id is None
        assert last.catalog_collection_id == collection_id
        assert session.get(CatalogCollection, collection_id).hierarchy_status == "review_required"


def test_individual_video_selection_and_filename_rule_are_previewed():
    engine, collection_id, title_id = seeded_collection(2)
    with Session(engine) as session:
        videos = list(session.get(CatalogCollection, collection_id).videos)
        selected = ManualTitleDefinition(
            title_id=title_id, local_title="Vybrané", manual_display_title=None,
            season_number_manual=None, season_label_manual=None, part_number_manual=None,
            part_type_manual="special", episode_start=None, episode_end=None,
            episode_start_offset=None, numbering_mode="unknown", sort_order=1,
            video_ids=(videos[0].id,),
        )
        pattern = ManualTitleDefinition(
            title_id=None, local_title="Podle názvu", manual_display_title=None,
            season_number_manual=None, season_label_manual=None, part_number_manual=None,
            part_type_manual="special", episode_start=None, episode_end=None,
            episode_start_offset=None, numbering_mode="unknown", sort_order=2,
            filename_pattern=r"Episode 02\.mkv$",
        )
        preview = preview_assignments(videos, [selected, pattern])
        assert preview.assignments == {videos[0].id: 0, videos[1].id: 1}


def test_manual_split_edit_and_remove_synchronize_explicit_authority_exactly():
    engine, collection_id, title_id = seeded_collection(3)
    with Session(engine) as session:
        videos = session.scalars(select(Video).order_by(Video.id)).all()
        first_ids = (videos[0].id, videos[1].id)
        apply_manual_split(
            session,
            collection_id,
            [explicit_definition(title_id, first_ids)],
        )
        session.flush()
        assert manual_split_authority_pairs(session) == {
            (title_id, videos[0].id),
            (title_id, videos[1].id),
        }

        edited_ids = (videos[1].id, videos[2].id)
        apply_manual_split(
            session,
            collection_id,
            [explicit_definition(title_id, edited_ids)],
        )
        session.flush()
        assert manual_split_authority_pairs(session) == {
            (title_id, videos[1].id),
            (title_id, videos[2].id),
        }
        assert videos[0].catalog_title_id is None

        apply_manual_split(
            session,
            collection_id,
            [explicit_definition(title_id, (videos[2].id,))],
        )
        remaining_video_id = videos[2].id
        session.commit()

    with Session(engine) as session:
        assert manual_split_authority_pairs(session) == {
            (title_id, remaining_video_id),
        }
        stored = session.scalars(select(Video).order_by(Video.id)).all()
        assert [video.catalog_title_id for video in stored] == [None, None, title_id]


def test_manual_split_forms_serialize_authority_without_assignment_backfill():
    engine, collection_id, title_id = seeded_collection(3)
    with Session(engine) as session:
        title = session.get(CatalogTitle, title_id)
        videos = session.scalars(select(Video).order_by(Video.id)).all()
        explicitly_selected = videos[2]
        explicitly_selected.catalog_title = None
        title.hierarchy_manual_override = True
        title.part_type_manual = "season"
        title.season_number_manual = 1
        title.manual_split_rule_videos.append(ManualSplitRuleVideo(
            video=explicitly_selected,
        ))
        session.commit()
        selected_id = explicitly_selected.id
        automatic_assignment_ids = {videos[0].id, videos[1].id}

    with Session(engine) as session:
        collection = session.get(CatalogCollection, collection_id)
        title = session.get(CatalogTitle, title_id)
        assert {video.id for video in title.videos} == automatic_assignment_ids

        rows = simple_definition_rows(collection)
        serialized = json.loads(definitions_as_json(collection))

        assert rows[0]["video_ids"] == str(selected_id)
        assert serialized[0]["video_ids"] == [selected_id]
        assert automatic_assignment_ids.isdisjoint(serialized[0]["video_ids"])


def test_manual_split_preview_and_apply_reject_omitted_active_authority_target():
    engine, collection_id, title_id = seeded_collection(1)
    with Session(engine) as session:
        video = session.scalar(select(Video))
        apply_manual_split(
            session,
            collection_id,
            [explicit_definition(title_id, (video.id,))],
        )
        session.commit()
        replacement = replace(
            explicit_definition(title_id, (video.id,)),
            title_id=None,
            local_title="Replacement",
        )
        collection = session.get(CatalogCollection, collection_id)

        with pytest.raises(ValueError, match="nelze z definice vynechat"):
            preview_assignments(
                list(collection.videos),
                [replacement],
                collection=collection,
            )
        with pytest.raises(ValueError, match="nelze z definice vynechat"):
            apply_manual_split(session, collection_id, [replacement])

        assert manual_split_authority_pairs(session) == {(title_id, video.id)}
        assert video.catalog_title_id == title_id


def test_manual_split_rejects_duplicate_target_and_foreign_explicit_video():
    engine, collection_id, title_id = seeded_collection(1)
    with Session(engine) as session:
        collection = session.get(CatalogCollection, collection_id)
        video = session.scalar(select(Video))
        duplicate_target = [
            explicit_definition(title_id, (video.id,)),
            replace(
                explicit_definition(title_id, (video.id,)),
                local_title="Duplicate target",
                sort_order=2,
            ),
        ]
        with pytest.raises(ValueError, match="pouze jednou"):
            preview_assignments(list(collection.videos), duplicate_target)
        with pytest.raises(ValueError, match="pouze jednou"):
            apply_manual_split(session, collection_id, duplicate_target)

        foreign_video = replace(
            explicit_definition(title_id, (video.id,)),
            video_ids=(video.id + 10_000,),
        )
        with pytest.raises(ValueError, match="cizí nebo neexistující video"):
            preview_assignments(list(collection.videos), [foreign_video])
        with pytest.raises(ValueError, match="cizí nebo neexistující video"):
            apply_manual_split(session, collection_id, [foreign_video])

        assert manual_split_authority_pairs(session) == set()
        assert video.catalog_title_id == title_id


def test_deleting_video_removes_all_manual_split_authority_without_orphans():
    engine, collection_id, first_title_id = seeded_collection(1)
    with Session(engine) as session:
        collection = session.get(CatalogCollection, collection_id)
        video = session.scalar(select(Video))
        second = CatalogTitle(
            collection=collection,
            local_title="Season 2",
            normalized_local_title="season 2",
            relative_root_path=f"{collection.relative_root_path}/.catalog-part-2",
            hierarchy_manual_override=True,
            part_type_manual="season",
            season_number_manual=2,
        )
        session.add(second)
        session.flush()
        session.add_all([
            ManualSplitRuleVideo(
                catalog_title_id=first_title_id,
                video_id=video.id,
            ),
            ManualSplitRuleVideo(catalog_title_id=second.id, video_id=video.id),
        ])
        session.commit()
        second_title_id = second.id
        video_id = video.id

        session.delete(video)
        session.commit()

        assert session.get(Video, video_id) is None
        assert session.get(CatalogTitle, first_title_id) is not None
        assert session.get(CatalogTitle, second_title_id) is not None
        assert manual_split_authority_pairs(session) == set()
        assert list(session.execute(text("PRAGMA foreign_key_check"))) == []


def test_safe_title_delete_removes_only_its_manual_split_authority():
    engine, collection_id, removed_title_id = seeded_collection(1)
    with Session(engine) as session:
        collection = session.get(CatalogCollection, collection_id)
        removed = session.get(CatalogTitle, removed_title_id)
        video = session.scalar(select(Video))
        video.catalog_title = None
        removed.hierarchy_manual_override = True
        removed.part_type_manual = "season"
        removed.season_number_manual = 1
        keeper = CatalogTitle(
            collection=collection,
            local_title="Season 2",
            normalized_local_title="season 2",
            relative_root_path=f"{collection.relative_root_path}/.catalog-part-2",
            hierarchy_manual_override=True,
            part_type_manual="season",
            season_number_manual=2,
        )
        session.add(keeper)
        session.flush()
        session.add_all([
            ManualSplitRuleVideo(
                catalog_title_id=removed_title_id,
                video_id=video.id,
            ),
            ManualSplitRuleVideo(catalog_title_id=keeper.id, video_id=video.id),
        ])
        session.commit()
        keeper_id, video_id = keeper.id, video.id

        delete_empty_local_title(
            session,
            collection_id,
            removed_title_id,
            remove_from_manual_split=True,
        )
        session.commit()

        assert session.get(CatalogTitle, removed_title_id) is None
        assert session.get(CatalogTitle, keeper_id) is not None
        assert session.get(Video, video_id) is not None
        assert manual_split_authority_pairs(session) == {(keeper_id, video_id)}
        assert list(session.execute(text("PRAGMA foreign_key_check"))) == []


def test_external_link_is_only_a_hint_and_does_not_split_collection():
    engine, collection_id, title_id = seeded_collection()
    with Session(engine) as session:
        session.add(ExternalTitleLink(
            catalog_title_id=title_id, provider="anilist", external_id="123",
            match_method="manual_search", is_primary=True, is_manual=True,
        ))
        session.commit()
        preview_assignments(
            list(session.get(CatalogCollection, collection_id).videos), definitions(title_id)
        )
        assert session.scalar(select(func.count()).select_from(CatalogTitle)) == 1


def test_hierarchy_filters_include_unassigned_review_and_conflict():
    collection = CatalogCollection(
        local_title="Show", normalized_local_title="show", relative_root_path="Show",
        hierarchy_status="review_required",
    )
    video = Video(
        relative_path="Show/E01.mkv", root_folder="Show", filename="E01.mkv",
        size=1, mtime_ns=1, catalog_collection=collection,
    )
    assert video_matches_filter(video, "unassigned") is True
    assert video_matches_filter(video, "hierarchy-review") is True
    collection.hierarchy_status = "conflict"
    assert video_matches_filter(video, "hierarchy-conflict") is True


def test_verified_hierarchy_with_unknown_episode_stays_in_review_filter():
    collection = CatalogCollection(
        local_title="Show", normalized_local_title="show", relative_root_path="Show",
        hierarchy_status="verified",
    )
    title = CatalogTitle(
        local_title="Season 1", normalized_local_title="season 1",
        relative_root_path="Show/Season 1", collection=collection, season_number=1,
    )
    video = Video(
        relative_path="Show/Season 1/unknown.mkv", root_folder="Show",
        filename="unknown.mkv", size=1, mtime_ns=1,
        catalog_collection=collection, catalog_title=title,
    )

    assert video_matches_filter(video, "hierarchy-review") is True
    video.season_episode_number = 1
    assert video_matches_filter(video, "hierarchy-review") is False


def test_manual_split_preserves_metadata_subtitles_and_hardsub():
    engine, collection_id, title_id = seeded_collection()
    with Session(engine) as session:
        video = session.scalar(select(Video).where(Video.filename == "Episode 01.mkv"))
        video.manual_hardsub_cs = True
        session.add(InternalSubtitle(
            video_id=video.id, stream_index=1, language="cze", normalized_language="cs",
        ))
        session.add(ExternalTitleLink(
            catalog_title_id=title_id, provider="anilist", external_id="1",
            match_method="manual_search", is_primary=True, is_manual=True,
        ))
        apply_manual_split(session, collection_id, definitions(title_id))
        session.commit()
        video = session.get(Video, video.id)
        assert video.manual_hardsub_cs is True
        assert video.internal_subtitles[0].normalized_language == "cs"
        assert session.scalar(select(ExternalTitleLink).where(
            ExternalTitleLink.catalog_title_id == title_id
        )).external_id == "1"

    migrate_schema(engine)
    with Session(engine) as session:
        video = session.scalar(select(Video).where(Video.filename == "Episode 01.mkv"))
        assert video.manual_hardsub_cs is True
        assert video.internal_subtitles[0].normalized_language == "cs"
        assert video.catalog_title_id == title_id
        assert session.scalar(select(ExternalTitleLink).where(
            ExternalTitleLink.catalog_title_id == title_id
        )).external_id == "1"


def test_verified_manual_split_survives_scan_and_new_video_reopens_review(
    tmp_path: Path, monkeypatch,
):
    root = tmp_path
    folder = root / "Anime" / "Anime title (Z18-L20)"
    folder.mkdir(parents=True)
    for number in range(1, 27):
        (folder / f"Episode {number:02}.mkv").write_bytes(b"video")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions() as session:
        scan_library(session, root)
        collection = session.scalar(select(CatalogCollection))
        original_title = collection.titles[0]
        assert collection.hierarchy_status == "review_required"
        apply_manual_split(session, collection.id, definitions(original_title.id))
        session.commit()
        split_ids = {title.id for title in collection.titles}

        scan_library(session, root)
        collection = session.get(CatalogCollection, collection.id)
        assert collection.hierarchy_status == "verified"
        assert {video.catalog_title_id for video in collection.videos} == split_ids

        (folder / "Episode 27.mkv").write_bytes(b"video")
        scan_library(session, root)
        collection = session.get(CatalogCollection, collection.id)
        new_video = session.scalar(select(Video).where(Video.filename == "Episode 27.mkv"))
        assert collection.hierarchy_status == "review_required"
        assert collection.hierarchy_note == (
            "Video, které vyžaduje ruční rozdělení, neodpovídá žádnému pravidlu."
        )
        assert new_video.catalog_title_id is None


def test_separating_zero_persists_without_metadata_or_physical_path_change(
    tmp_path: Path, monkeypatch,
):
    folder = tmp_path / "Ansatsu Kyoushitsu (Z15-Z16)" / "Serie 1"
    folder.mkdir(parents=True)
    paths = [folder / f"Ansatsu Kyoushitsu {number:02}.mp4" for number in range(23)]
    for path in paths:
        path.write_bytes(b"video")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)

    with sessions() as session:
        scan_library(session, tmp_path)
        collection = session.scalar(select(CatalogCollection))
        zero = session.scalar(select(Video).where(Video.filename == "Ansatsu Kyoushitsu 00.mp4"))
        season = zero.catalog_title
        original_relative_path = zero.relative_path
        assert collection.hierarchy_status == "review_required"
        assert zero.local_episode_number is None
        assert zero.episode_number_source == "nonstandard_zero"

        preview = separate_nonstandard_videos(
            session, collection.id, [zero.id], local_title="Preview", part_type="preview",
        )
        session.commit()
        preview_id = preview.id
        season_id = season.id

    with sessions() as session:
        zero = session.scalar(select(Video).where(Video.filename == "Ansatsu Kyoushitsu 00.mp4"))
        preview = session.get(CatalogTitle, preview_id)
        season = session.get(CatalogTitle, season_id)
        assert zero.catalog_title_id == preview_id
        assert zero.relative_path == original_relative_path
        assert preview.effective_part_type == "preview"
        assert preview.metadata_record is None
        assert preview.external_links == []
        assert [video.season_episode_number for video in sorted(
            season.videos, key=lambda item: item.season_episode_number
        )] == list(range(1, 23))
        season_summary = summarize_title_numbering(list(season.videos), season)
        assert season_summary.numbered == season_summary.standard_total == 22
        assert season_summary.nonstandard == 0
        assert season_summary.requires_review is False
        assert numbering_requires_review(preview.collection) is False

        scan_library(session, tmp_path)

    with sessions() as session:
        zero = session.scalar(select(Video).where(Video.filename == "Ansatsu Kyoushitsu 00.mp4"))
        collection = session.scalar(select(CatalogCollection))
        assert zero.catalog_title_id == preview_id
        assert zero.relative_path == original_relative_path
        assert collection.hierarchy_status == "automatic"
        assert [path.read_bytes() for path in paths] == [b"video"] * 23


def test_fractional_recap_can_stay_in_season_and_survives_session_and_scan(
    tmp_path: Path, monkeypatch,
):
    folder = tmp_path / "Arifureta" / "Season 1"
    folder.mkdir(parents=True)
    for filename in [
        *[f"Arifureta - {number:02}.mkv" for number in range(1, 13)],
        "Arifureta - 05.5.mkv",
    ]:
        (folder / filename).write_bytes(b"video")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)

    with sessions() as session:
        scan_library(session, tmp_path)
        collection = session.scalar(select(CatalogCollection))
        recap = session.scalar(select(Video).where(Video.filename == "Arifureta - 05.5.mkv"))
        season_id = recap.catalog_title_id
        original_path = recap.relative_path
        classify_videos_in_place(session, collection.id, [recap.id], "recap")
        session.commit()
        recap_id = recap.id
        collection_id = collection.id

    with sessions() as session:
        recap = session.get(Video, recap_id)
        collection = session.get(CatalogCollection, collection_id)
        summary = summarize_title_numbering(list(recap.catalog_title.videos), recap.catalog_title)
        assert recap.catalog_title_id == season_id
        assert recap.content_type_manual == "recap"
        assert recap.relative_path == original_path
        assert summary.standard_total == 12
        assert summary.resolved_supplemental == 1
        assert summary.requires_review is False
        assert collection.hierarchy_status == "automatic"

        scan_library(session, tmp_path)
        recap = session.get(Video, recap_id)
        assert recap.catalog_title_id == season_id
        assert recap.content_type_manual == "recap"
        assert recap.relative_path == original_path


def test_manual_video_classification_can_return_to_automatic_without_other_changes():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="Arifureta", normalized_local_title="arifureta",
            relative_root_path="Anime/Arifureta", hierarchy_status="verified",
        )
        title = CatalogTitle(
            collection=collection, local_title="Season 1",
            normalized_local_title="season 1",
            relative_root_path="Anime/Arifureta/Season 1",
            part_type_manual="season", season_number_manual=1,
            season_label_manual="S1", sort_order_manual=4,
            hierarchy_manual_override=True, hierarchy_verified_at=utc_now(),
        )
        video = Video(
            relative_path="Anime/Arifureta/Season 1/Arifureta - 05.5.mkv",
            root_folder="Anime", filename="Arifureta - 05.5.mkv",
            size=123, mtime_ns=456, content_type_manual="recap",
            duplicate_status_manual="suspected", manual_hardsub_cs=True,
            catalog_title=title, catalog_collection=collection,
        )
        session.add(collection)
        session.commit()
        title_state = (
            title.part_type_manual, title.season_number_manual,
            title.season_label_manual, title.sort_order_manual,
            title.hierarchy_manual_override, title.hierarchy_verified_at,
        )
        unrelated_video_state = (
            video.duplicate_status_manual, video.manual_hardsub_cs,
            video.manual_hardsub_verified_at, video.relative_path,
            video.catalog_title_id, video.catalog_collection_id,
        )

        classify_videos_in_place(session, collection.id, [video.id], "")

        assert video.content_type_manual is None
        assert effective_video_numbering(video).is_nonstandard
        assert (
            title.part_type_manual, title.season_number_manual,
            title.season_label_manual, title.sort_order_manual,
            title.hierarchy_manual_override, title.hierarchy_verified_at,
        ) == title_state
        assert (
            video.duplicate_status_manual, video.manual_hardsub_cs,
            video.manual_hardsub_verified_at, video.relative_path,
            video.catalog_title_id, video.catalog_collection_id,
        ) == unrelated_video_state
        assert collection.hierarchy_status == "review_required"
        assert collection.hierarchy_note == (
            "Číslování nebo nezařazený obsah stále vyžaduje kontrolu."
        )


def test_move_merge_and_explicit_delete_change_only_logical_assignment():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="Arifureta", normalized_local_title="arifureta",
            relative_root_path="Anime/Arifureta",
        )
        season = CatalogTitle(
            collection=collection, local_title="Season 1", normalized_local_title="season 1",
            relative_root_path="Anime/Arifureta/Season 1", part_type_manual="season",
        )
        recap_part = CatalogTitle(
            collection=collection, local_title="Recap", normalized_local_title="recap",
            relative_root_path="Anime/Arifureta/.catalog-part-2", part_type_manual="recap",
            hierarchy_manual_override=True,
        )
        recap = Video(
            relative_path="Anime/Arifureta/Season 1/Arifureta 05.5.mkv",
            root_folder="Anime", filename="Arifureta 05.5.mkv", size=1, mtime_ns=1,
            catalog_title=recap_part, catalog_collection=collection,
        )
        regular = Video(
            relative_path="Anime/Arifureta/Season 1/Arifureta 01.mkv",
            root_folder="Anime", filename="Arifureta 01.mkv", size=1, mtime_ns=1,
            catalog_title=recap_part, catalog_collection=collection,
        )
        session.add(collection)
        session.commit()
        collection_id, season_id, source_id = collection.id, season.id, recap_part.id
        recap_id, regular_id = recap.id, regular.id
        paths = {recap.id: recap.relative_path, regular.id: regular.relative_path}

        move_videos_to_title(session, collection_id, [recap_id], season_id)
        assert recap.catalog_title_id == season_id
        assert recap.content_type_manual is None
        assert recap.relative_path == paths[recap_id]

        merge_title_into(session, collection_id, source_id, season_id)
        session.commit()
        assert session.get(Video, regular_id).catalog_title_id == season_id
        assert session.get(Video, regular_id).content_type_manual is None
        assert session.get(Video, regular_id).relative_path == paths[regular_id]
        assert session.get(CatalogTitle, source_id) is not None
        assert session.get(CatalogTitle, source_id).videos == []

        delete_empty_local_title(
            session, collection_id, source_id, remove_from_manual_split=True,
        )
        session.commit()
        assert session.get(CatalogTitle, source_id) is None


def test_merge_title_preserves_automatic_op_ed_without_creating_manual_bonus():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="Example", normalized_local_title="example",
            relative_root_path="Anime/Example",
        )
        source = CatalogTitle(
            collection=collection, local_title="NC – Example Season",
            normalized_local_title="nc example season",
            relative_root_path="Anime/Example/NC/Example Season",
            part_type="bonus", season_label="NC",
        )
        target = CatalogTitle(
            collection=collection, local_title="Example Season",
            normalized_local_title="example season",
            relative_root_path="Anime/Example/Example Season",
            part_type="title", hierarchy_manual_override=True,
            part_type_manual="season", season_number_manual=3,
            season_label_manual="S3", sort_order_manual=0,
            hierarchy_verified_at=utc_now(),
        )
        videos = [
            Video(
                relative_path=f"Anime/Example/NC/Example Season/{filename}",
                root_folder="Anime", filename=filename, size=1, mtime_ns=1,
                file_type=file_type, catalog_title=source,
                catalog_collection=collection,
            )
            for filename, file_type in (
                ("ED.mkv", "ed"),
                ("OP 01.mkv", "op"),
                ("OP 02.mkv", "op"),
            )
        ]
        session.add(collection)
        session.commit()
        target_hierarchy = (
            target.hierarchy_manual_override, target.part_type_manual,
            target.season_number_manual, target.season_label_manual,
            target.sort_order_manual, target.hierarchy_verified_at,
        )

        merge_title_into(session, collection.id, source.id, target.id)
        session.flush()

        assert source.videos == []
        assert all(video.catalog_title_id == target.id for video in videos)
        assert [video.file_type for video in videos] == ["ed", "op", "op"]
        assert all(video.content_type_manual is None for video in videos)
        numbering = [effective_video_numbering(video) for video in videos]
        assert [item.classification for item in numbering] == [
            "supplementary", "supplementary", "supplementary",
        ]
        assert [item.supplementary_type for item in numbering] == ["ed", "op", "op"]
        assert all(item.numbering_input is None for item in numbering)
        assert (
            target.hierarchy_manual_override, target.part_type_manual,
            target.season_number_manual, target.season_label_manual,
            target.sort_order_manual, target.hierarchy_verified_at,
        ) == target_hierarchy


def test_merge_title_preserves_existing_manual_content_classification():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="Example", normalized_local_title="example",
            relative_root_path="Anime/Example",
        )
        source = CatalogTitle(
            collection=collection, local_title="Extras",
            normalized_local_title="extras",
            relative_root_path="Anime/Example/Extras", part_type="bonus",
        )
        target = CatalogTitle(
            collection=collection, local_title="Example Season",
            normalized_local_title="example season",
            relative_root_path="Anime/Example/Example Season",
            part_type="season", season_number=1, season_label="S1",
        )
        video = Video(
            relative_path="Anime/Example/Extras/Explicit choice.mkv",
            root_folder="Anime", filename="Explicit choice.mkv", size=1, mtime_ns=1,
            file_type="other", content_type_manual="recap",
            catalog_title=source, catalog_collection=collection,
        )
        session.add(collection)
        session.commit()

        merge_title_into(session, collection.id, source.id, target.id)
        session.flush()

        assert video.catalog_title_id == target.id
        assert video.file_type == "other"
        assert video.content_type_manual == "recap"


def test_split_ed_and_op_into_bonus_part_keeps_automatic_content_and_no_number_issue():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="Example", normalized_local_title="example",
            relative_root_path="Anime/Example",
        )
        source = CatalogTitle(
            collection=collection, local_title="Example Season",
            normalized_local_title="example season",
            relative_root_path="Anime/Example", part_type="season",
            season_number=3, season_label="S3",
        )
        videos = [
            Video(
                relative_path=f"Anime/Example/{filename}", root_folder="Anime",
                filename=filename, size=1, mtime_ns=index, file_type=file_type,
                catalog_title=source, catalog_collection=collection,
            )
            for index, (filename, file_type) in enumerate((
                ("ED.mkv", "ed"),
                ("OP 01.mkv", "op"),
                ("OP 02.mkv", "op"),
            ), 1)
        ]
        session.add(collection)
        session.flush()

        bonus = create_title_from_videos(
            session, collection.id, [video.id for video in videos],
            local_title="NC – Example Season", part_type="bonus",
            season_number=3, season_label="S3",
        )
        result = evaluate_collection_hierarchy(
            collection, list(collection.videos), include_legacy_fallback=False,
        )
        numbering = [effective_video_numbering(video, bonus) for video in videos]

        assert bonus.effective_part_type == "bonus"
        assert bonus.effective_season_number == 3
        assert bonus.effective_season_label == "S3"
        assert bonus.hierarchy_manual_override is True
        assert bonus.hierarchy_verified_at is not None
        assert [video.file_type for video in videos] == ["ed", "op", "op"]
        assert all(video.content_type_manual is None for video in videos)
        assert [state.supplementary_type for state in numbering] == ["ed", "op", "op"]
        assert [state.supplementary_number for state in numbering] == [None, 1, 2]
        assert all(state.season_episode_number is None for state in numbering)
        assert not any(
            issue.catalog_title is bonus or any(video in videos for video in issue.videos)
            for issue in result.blocking_issues
        )

        hierarchy_state = (
            bonus.part_type_manual, bonus.season_number_manual,
            bonus.season_label_manual, bonus.hierarchy_manual_override,
            bonus.hierarchy_verified_at,
        )
        for video in videos:
            video.content_type_manual = "bonus"
        classify_videos_in_place(
            session, collection.id, [video.id for video in videos], "",
        )

        assert all(video.content_type_manual is None for video in videos)
        assert (
            bonus.part_type_manual, bonus.season_number_manual,
            bonus.season_label_manual, bonus.hierarchy_manual_override,
            bonus.hierarchy_verified_at,
        ) == hierarchy_state


def test_any_empty_title_with_owned_metadata_can_be_deleted_without_orphans(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    cached_cover = tmp_path / "cover.jpg"
    cached_cover.write_bytes(b"cover stays on disk")
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="High School DxD", normalized_local_title="high school dxd",
            relative_root_path="Anime/High School DxD",
        )
        title = CatalogTitle(
            collection=collection, local_title="High School DxD OVA",
            normalized_local_title="high school dxd ova",
            relative_root_path="Anime/High School DxD/OVA",
            metadata_status="linked_manual",
        )
        title.external_links.append(ExternalTitleLink(
            provider="anilist", external_id="1", match_method="manual_search",
            is_primary=True, is_manual=True,
        ))
        title.metadata_record = TitleMetadata(
            display_title="High School DxD OVA", metadata_provider="anilist",
            metadata_external_id="1",
        )
        title.metadata_candidates.append(MetadataCandidate(
            provider="anilist", external_id="2", candidate_title="Candidate",
        ))
        title.artwork.append(Artwork(
            provider="anilist", external_id="1", artwork_type="cover",
            remote_url="https://example.invalid/cover.jpg",
            local_path=str(cached_cover), mime_type="image/jpeg", file_size=19,
        ))
        session.add(collection)
        session.commit()
        collection_id, title_id = collection.id, title.id

        delete_empty_local_title(session, collection_id, title_id)
        session.commit()

        assert session.get(CatalogTitle, title_id) is None
        assert session.scalar(select(func.count()).select_from(ExternalTitleLink)) == 0
        assert session.scalar(select(func.count()).select_from(TitleMetadata)) == 0
        assert session.scalar(select(func.count()).select_from(MetadataCandidate)) == 0
        assert session.scalar(select(func.count()).select_from(Artwork)) == 0
        assert session.get(CatalogCollection, collection_id) is not None
        assert session.get(CatalogCollection, collection_id).titles == []
        assert cached_cover.read_bytes() == b"cover stays on disk"


def test_empty_title_delete_rechecks_video_count_and_rejects_nonempty_title():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show",
        )
        title = CatalogTitle(
            collection=collection, local_title="OVA", normalized_local_title="ova",
            relative_root_path="Anime/Show/OVA",
        )
        Video(
            catalog_title=title, catalog_collection=collection,
            relative_path="Anime/Show/OVA/OVA 01.mkv", root_folder="Anime",
            filename="OVA 01.mkv", size=1, mtime_ns=1,
        )
        session.add(collection)
        session.commit()

        with pytest.raises(ValueError, match="už není prázdná; obsahuje video"):
            delete_empty_local_title(session, collection.id, title.id)

        assert session.get(CatalogTitle, title.id) is title
        assert len(title.videos) == 1


def test_manual_split_entry_delete_is_targeted_and_survives_startup_sync():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    original_path = (
        "Anime/High School DxD/NC/High School DxD New - NCOP 01.mkv"
    )
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="High School DxD", normalized_local_title="high school dxd",
            relative_root_path="Anime/High School DxD",
        )
        video = Video(
            relative_path=original_path, root_folder="Anime",
            filename="High School DxD New - NCOP 01.mkv", size=1, mtime_ns=1,
            catalog_collection=collection, episode_number_manual_override=7,
        )
        session.add(collection)
        session.commit()
        definitions = [
            ManualTitleDefinition(
                None, "High School DxD S1", None, 1, "S1", None, "season",
                None, None, None, "unknown", 1, r".*NCOP 01\.mkv$",
            ),
            ManualTitleDefinition(
                None, "NC – High School DxD New", None, None, None, None,
                "bonus", None, None, None, "unknown", 2,
            ),
            ManualTitleDefinition(
                None, "Specials – High School DxD Born", None, 3, "S3", None,
                "special", None, None, None, "season_local", 3,
            ),
        ]
        apply_manual_split(session, collection.id, definitions)
        session.commit()
        titles_by_name = {title.local_title: title for title in collection.titles}
        keeper_id = titles_by_name["High School DxD S1"].id
        removed_id = titles_by_name["NC – High School DxD New"].id
        other_id = titles_by_name["Specials – High School DxD Born"].id
        collection_id, video_id = collection.id, video.id

    migrate_schema(engine)

    with Session(engine) as session:
        collection = session.get(CatalogCollection, collection_id)
        assert {title.id for title in collection.titles} == {
            keeper_id, removed_id, other_id,
        }
        removed_definition = delete_empty_local_title(
            session, collection_id, removed_id,
        )
        session.commit()

        assert removed_definition is False
        assert session.get(CatalogTitle, removed_id) is None
        assert {title.id for title in session.get(CatalogCollection, collection_id).titles} == {
            keeper_id, other_id,
        }

    migrate_schema(engine)

    with Session(engine) as session:
        collection = session.get(CatalogCollection, collection_id)
        video = session.get(Video, video_id)
        keeper = session.get(CatalogTitle, keeper_id)
        other = session.get(CatalogTitle, other_id)
        assert session.get(CatalogTitle, removed_id) is None
        assert {title.id for title in collection.titles} == {keeper_id, other_id}
        assert keeper.hierarchy_manual_override is True
        assert keeper.season_number_manual == 1
        assert keeper.episode_filename_pattern == r".*NCOP 01\.mkv$"
        assert other.hierarchy_manual_override is True
        assert other.part_type_manual == "special"
        assert other.season_number_manual == 3
        assert other.numbering_mode == "season_local"
        assert video.catalog_title_id == keeper_id
        assert video.episode_number_manual_override == 7
        assert video.relative_path == original_path
        assert not any(
            title.relative_root_path == "Anime/High School DxD/NC"
            for title in collection.titles
        )


def test_nonempty_manual_split_entry_cannot_be_deleted_with_explicit_flag():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show",
        )
        title = CatalogTitle(
            collection=collection, local_title="Specials",
            normalized_local_title="specials",
            relative_root_path="Anime/Show/.catalog-part-1",
            hierarchy_manual_override=True, part_type_manual="special",
        )
        video = Video(
            catalog_title=title, catalog_collection=collection,
            relative_path="Anime/Show/SP 01.mkv", root_folder="Anime",
            filename="SP 01.mkv", size=1, mtime_ns=1,
        )
        session.add_all([
            collection,
            ManualSplitRuleVideo(catalog_title=title, video=video),
        ])
        session.commit()

        with pytest.raises(ValueError, match="už není prázdná; obsahuje video"):
            delete_empty_local_title(
                session, collection.id, title.id,
                remove_from_manual_split=True,
            )

        assert session.get(CatalogTitle, title.id) is title
        assert title.hierarchy_manual_override is True
        assert len(title.videos) == 1


def test_move_to_existing_title_survives_subsequent_scan(tmp_path: Path, monkeypatch):
    first_folder = tmp_path / "Arifureta" / "Season 1"
    second_folder = tmp_path / "Arifureta" / "Season 2"
    first_folder.mkdir(parents=True)
    second_folder.mkdir(parents=True)
    moved_path = first_folder / "Arifureta 05.5.mkv"
    moved_path.write_bytes(b"video")
    (second_folder / "Arifureta S2 01.mkv").write_bytes(b"video")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)

    with sessions() as session:
        scan_library(session, tmp_path)
        collection = session.scalar(select(CatalogCollection))
        moved = session.scalar(select(Video).where(Video.filename == moved_path.name))
        target = session.scalar(select(CatalogTitle).where(CatalogTitle.local_title == "Season 2"))
        original_path = moved.relative_path
        move_videos_to_title(session, collection.id, [moved.id], target.id)
        session.commit()
        moved_id, target_id = moved.id, target.id

        scan_library(session, tmp_path)
        moved = session.get(Video, moved_id)
        assert moved.catalog_title_id == target_id
        assert moved.relative_path == original_path
        assert moved_path.read_bytes() == b"video"


def test_ova_can_be_moved_from_season_to_new_season_specific_title_and_survives_scan(
    tmp_path: Path, monkeypatch,
):
    folder = tmp_path / "High School DxD" / "Season 1"
    folder.mkdir(parents=True)
    episode_path = folder / "High School DxD - 01.mkv"
    ova_path = folder / "High School DxD - OVA 01.mkv"
    episode_path.write_bytes(b"episode")
    ova_path.write_bytes(b"ova")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda _, **__: PROBE_RESULT)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)

    with sessions() as session:
        scan_library(session, tmp_path)
        collection = session.scalar(select(CatalogCollection))
        ova = session.scalar(select(Video).where(Video.filename == ova_path.name))
        original_path = ova.relative_path
        ova_title = create_title_from_videos(
            session, collection.id, [ova.id], local_title="OVA – S1",
            part_type="ova", season_number=1,
        )
        session.commit()
        ova_id, ova_title_id = ova.id, ova_title.id

        scan_library(session, tmp_path)

        ova = session.get(Video, ova_id)
        ova_title = session.get(CatalogTitle, ova_title_id)
        assert ova.catalog_title_id == ova_title_id
        assert ova.content_type_manual is None
        assert ova.file_type == "ova"
        assert ova.season_episode_number is None
        assert ova.relative_path == original_path
        assert ova_title.effective_part_type == "ova"
        assert ova_title.effective_season_number == 1
        assert ova_title.effective_season_label == "S1"
        assert unresolved_duplicate_groups(list(collection.videos)) == ()
        assert episode_path.read_bytes() == b"episode"
        assert ova_path.read_bytes() == b"ova"


def test_duplicate_candidate_keeps_direct_reassignment_action_in_supplementary_title():
    collection = CatalogCollection(
        id=1, local_title="Show", normalized_local_title="show",
        relative_root_path="Anime/Show",
    )
    title = CatalogTitle(
        id=1, collection=collection, local_title="NC – S2",
        normalized_local_title="nc s2",
        relative_root_path="Anime/Show/NC/Season 2", part_type="bonus",
        season_number=2, season_label="S2",
    )
    videos = [
        Video(
            id=identifier,
            relative_path=f"Anime/Show/NC/Season 2/copy-{identifier}/OP 01.mkv",
            root_folder="Anime", filename="OP 01.mkv", size=1, mtime_ns=1,
            catalog_title=title, catalog_collection=collection,
        )
        for identifier in (1, 2)
    ]

    assert supplementary_video_suggestions(videos) == ()
    suggestions = supplementary_video_suggestions(videos, include_video_ids={2})

    assert [suggestion.video.id for suggestion in suggestions] == [2]
    assert suggestions[0].display_label == "OP 01"
    assert suggestions[0].context_label == "S2"
    assert suggestions[0].context_season_number == 2


def _explicit_special_assignment_videos(count=1, *, manual_hierarchy=False):
    collection = CatalogCollection(
        id=1, local_title="Hataraku Saibou", normalized_local_title="hataraku saibou",
        relative_root_path="Anime/Hataraku Saibou",
    )
    season = CatalogTitle(
        id=1, collection=collection, local_title="Serie 1",
        normalized_local_title="serie 1",
        relative_root_path="Anime/Hataraku Saibou/Serie 1",
        part_type="season", season_number=1, season_label="S1",
        hierarchy_manual_override=manual_hierarchy,
        part_type_manual="season" if manual_hierarchy else None,
        season_number_manual=1 if manual_hierarchy else None,
        season_label_manual="S1" if manual_hierarchy else None,
    )
    videos = [Video(
        id=index,
        relative_path=(
            f"Anime/Hataraku Saibou/Serie 1/"
            f"S01E{13 + index:02} [SP]-Special {index}.mkv"
        ),
        root_folder="Anime", filename=f"S01E{13 + index:02} [SP]-Special {index}.mkv",
        size=1, mtime_ns=1, catalog_title=season, catalog_collection=collection,
    ) for index in range(1, count + 1)]
    return collection, season, videos


def test_explicit_sp_recommends_existing_assignment_form_without_mutating_models():
    collection, season, videos = _explicit_special_assignment_videos(
        manual_hierarchy=True,
    )
    video = videos[0]
    before = (
        video.catalog_title, video.content_type_manual,
        video.episode_number_manual_override, season.hierarchy_manual_override,
        collection.hierarchy_status,
    )

    recommendations = supplementary_assignment_recommendations(videos)

    assert len(recommendations) == 1
    recommendation = recommendations[0]
    assert recommendation.video_ids == (video.id,)
    assert recommendation.supplementary_type == "special"
    assert recommendation.proposed_part_type == "special"
    assert recommendation.type_label == "Special"
    assert recommendation.proposed_title == "Specials"
    assert recommendation.season_number == 1
    assert recommendation.season_label == "S1"
    assert recommendation.season_display == "S01"
    assert recommendation.canonical_numbering_known is False
    assert recommendation.items[0].filename_hint_label == "S01E14"
    assert recommendation.items[0].title_candidate == "Special 1"
    assert (
        video.catalog_title, video.content_type_manual,
        video.episode_number_manual_override, season.hierarchy_manual_override,
        collection.hierarchy_status,
    ) == before


def test_two_explicit_specials_with_same_scope_share_one_safe_recommendation():
    _, _, videos = _explicit_special_assignment_videos(count=2)

    recommendations = supplementary_assignment_recommendations(videos)

    assert len(recommendations) == 1
    assert recommendations[0].video_ids == (1, 2)
    assert [item.filename_hint_label for item in recommendations[0].items] == [
        "S01E14", "S01E15",
    ]
    assert all(item.supplementary_number is None for item in recommendations[0].items)


@pytest.mark.parametrize(
    ("filename", "subtype", "type_label", "proposed_title", "ordinal"),
    [
        ("Title [CM01][codec].mkv", "cm", "CM", "CM", 1),
        ("Title [Menu03][codec].mkv", "menu", "Menu", "Menu", 3),
    ],
)
def test_bracketed_cm_and_menu_get_precise_hierarchy_recommendations(
    filename, subtype, type_label, proposed_title, ordinal,
):
    collection = CatalogCollection(
        id=1, local_title="Show", normalized_local_title="show",
        relative_root_path="Anime/Show",
    )
    title = CatalogTitle(
        id=1, collection=collection, local_title="Season 1",
        normalized_local_title="season 1",
        relative_root_path="Anime/Show/Season 1", part_type="season",
        season_number=1,
    )
    relative_path = f"{title.relative_root_path}/{filename}"
    video = Video(
        id=1, relative_path=relative_path, root_folder="Anime", filename=filename,
        size=1, mtime_ns=1, catalog_title=title, catalog_collection=collection,
        file_type=classify_video(relative_path),
    )

    recommendations = supplementary_assignment_recommendations([video])

    assert len(recommendations) == 1
    recommendation = recommendations[0]
    assert recommendation.supplementary_type == subtype
    assert recommendation.proposed_part_type == "bonus"
    assert recommendation.type_label == type_label
    assert recommendation.proposed_title == proposed_title
    assert recommendation.items[0].supplementary_number == ordinal


def test_unknown_iv_marker_gets_no_invented_hierarchy_recommendation():
    collection = CatalogCollection(
        id=1, local_title="Show", normalized_local_title="show",
        relative_root_path="Anime/Show",
    )
    title = CatalogTitle(
        id=1, collection=collection, local_title="Season 1",
        normalized_local_title="season 1",
        relative_root_path="Anime/Show/Season 1", part_type="season",
        season_number=1,
    )
    filename = "Title [IV01][codec].mkv"
    relative_path = f"{title.relative_root_path}/{filename}"
    video = Video(
        id=1, relative_path=relative_path, root_folder="Anime", filename=filename,
        size=1, mtime_ns=1, catalog_title=title, catalog_collection=collection,
        file_type=classify_video(relative_path),
    )

    assert video.file_type == "other"
    assert supplementary_assignment_recommendations([video]) == ()


def test_standard_sxxexx_and_manually_classified_video_get_no_recommendation():
    _, season, videos = _explicit_special_assignment_videos()
    standard = Video(
        id=2, relative_path="Anime/Hataraku Saibou/Serie 1/S01E03-Influenza.mkv",
        root_folder="Anime", filename="S01E03-Influenza.mkv", size=1, mtime_ns=1,
        catalog_title=season, catalog_collection=season.collection,
    )
    videos[0].content_type_manual = "special"

    assert supplementary_assignment_recommendations([standard]) == ()
    assert supplementary_assignment_recommendations(videos) == ()


def test_create_special_does_not_require_canonical_number_or_content_override():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection, _, videos = _explicit_special_assignment_videos()
        session.add(collection)
        session.flush()
        special = videos[0]
        original_path = special.relative_path

        title = create_title_from_videos(
            session, collection.id, [special.id], local_title="Specials",
            part_type="special", season_number=1, season_label="S1",
        )

        assert title.effective_part_type == "special"
        assert title.effective_season_number == 1
        assert special.catalog_title is title
        assert special.season_episode_number is None
        assert special.episode_number_manual_override is None
        assert special.content_type_manual is None
        assert effective_video_numbering(special, title).supplementary_type == "special"
        assert collection.hierarchy_status == "automatic"
        assert special.relative_path == original_path


def test_create_title_from_videos_accepts_all_part_types_without_inventing_video_types():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show", hierarchy_status="review_required",
        )
        source = CatalogTitle(
            collection=collection, local_title="Source", normalized_local_title="source",
            relative_root_path="Anime/Show/Source",
        )
        videos = [Video(
            relative_path=f"Anime/Show/Source/Item {index:02}.mkv",
            root_folder="Anime", filename=f"Item {index:02}.mkv", size=index,
            mtime_ns=index, catalog_title=source, catalog_collection=collection,
        ) for index, _ in enumerate(PART_TYPE_CHOICES, 1)]
        session.add(collection)
        session.flush()
        videos[0].content_type_manual = "recap"
        original_paths = {video.id: (video.filename, video.relative_path) for video in videos}
        original_content_types = {
            video.id: video.content_type_manual for video in videos
        }

        created = []
        for video, (part_type, label) in zip(videos, PART_TYPE_CHOICES, strict=True):
            created.append(create_title_from_videos(
                session, collection.id, [video.id], local_title=label,
                part_type=part_type,
                part_number=1 if part_type == "part" else None,
            ))

        assert [title.effective_part_type for title in created] == [
            value for value, _ in PART_TYPE_CHOICES
        ]
        for video, (part_type, _) in zip(videos, PART_TYPE_CHOICES, strict=True):
            assert (video.filename, video.relative_path) == original_paths[video.id]
            assert video.content_type_manual == original_content_types[video.id]
        film_video = videos[[value for value, _ in PART_TYPE_CHOICES].index("film")]
        assert film_video.catalog_title.effective_part_type == "film"
        assert film_video.content_type_manual is None


def test_standard_and_ova_part_videos_can_be_split_into_two_local_ova_titles():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="Arifureta", normalized_local_title="arifureta",
            relative_root_path="Anime/Arifureta",
        )
        source = CatalogTitle(
            collection=collection, local_title="OVA", normalized_local_title="ova",
            relative_root_path="Anime/Arifureta/OVA",
        )
        names = [
            "Arifureta OVA Episode 01 Yue's Diary.mkv",
            "Arifureta OVA Episode 02 Hot Love Springs Eternal.mkv",
            "Arifureta S2 - OVA P1.mkv", "Arifureta S2 - OVA P2.mkv",
        ]
        items = [Video(
            relative_path=f"Anime/Arifureta/OVA/{name}", root_folder="Anime",
            filename=name, size=1, mtime_ns=1, catalog_title=source,
            catalog_collection=collection,
        ) for name in names]
        session.add(collection)
        session.flush()
        from app.numbering import recalculate_title_numbering
        recalculate_title_numbering(source, items)
        first = create_title_from_videos(
            session, collection.id, [items[0].id, items[1].id],
            local_title="OVA – Serie 1", part_type="ova", season_number=1,
        )
        second = create_title_from_videos(
            session, collection.id, [items[2].id, items[3].id],
            local_title="OVA – Serie 2", part_type="ova", season_number=2,
        )
        session.commit()

        assert [video.season_episode_number for video in first.videos] == [None, None]
        assert [video.season_episode_number for video in second.videos] == [None, None]
        assert first.effective_season_number == 1
        assert second.effective_season_number == 2
        assert first.effective_part_type == second.effective_part_type == "ova"
        assert first.metadata_record is second.metadata_record is None


def test_selected_bulk_numbering_is_deterministic_and_preserves_paths():
    items = [Video(
        id=index, relative_path=f"Anime/OVA/OVA P{name}.mkv", root_folder="Anime",
        filename=f"OVA P{name}.mkv", size=1, mtime_ns=1,
    ) for index, name in [(2, "2"), (1, "1")]]
    paths = [video.relative_path for video in items]

    rows = apply_sequential_numbering(items, 1)

    assert [(row.filename, row.proposed_episode) for row in rows] == [
        ("OVA P1.mkv", 1), ("OVA P2.mkv", 2),
    ]
    assert [video.relative_path for video in items] == paths
