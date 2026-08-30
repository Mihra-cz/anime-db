from app.catalog import detect_episode_number
from app.hierarchy_evaluation import (
    HierarchyIssueCode,
    evaluate_collection_hierarchy,
)
from app.models import CatalogCollection, CatalogTitle, Video, VideoVariantGroup, utc_now
from app.numbering import (
    confirmed_duplicate_variant_conflicts,
    logical_episode_identity,
    logical_episode_partitions,
    recalculate_title_numbering,
    summarize_title_numbering,
    unresolved_duplicate_groups,
)


def _graph():
    collection = CatalogCollection(
        id=1,
        local_title="Show",
        normalized_local_title="show",
        relative_root_path="Anime/Show",
    )
    title = CatalogTitle(
        id=10,
        collection=collection,
        local_title="Season 1",
        normalized_local_title="season 1",
        relative_root_path="Anime/Show/Season 1",
        part_type="season",
        season_number=1,
        season_label="S1",
    )
    first = VideoVariantGroup(
        id=100,
        catalog_title=title,
        manual_label="A",
        release_source=None,
        content_variant=None,
        verified_at=utc_now(),
    )
    second = VideoVariantGroup(
        id=200,
        catalog_title=title,
        manual_label="TV",
        release_source="tv",
        content_variant=None,
        verified_at=utc_now(),
    )
    return collection, title, first, second


def _video(
    collection,
    title,
    video_id,
    episode,
    *,
    group=None,
    filename=None,
    duplicate_of=None,
):
    filename = filename or f"Show - {episode:02d} - {video_id}.mkv"
    return Video(
        id=video_id,
        relative_path=f"{title.relative_root_path}/{filename}",
        root_folder="Anime",
        filename=filename,
        size=video_id,
        mtime_ns=video_id,
        local_episode_number=episode,
        season_episode_number=episode,
        absolute_episode_number=episode,
        catalog_collection=collection,
        catalog_collection_id=collection.id,
        catalog_title=title,
        catalog_title_id=title.id,
        video_variant_group=group,
        video_variant_group_id=group.id if group is not None else None,
        duplicate_of=duplicate_of,
        duplicate_of_video_id=duplicate_of.id if duplicate_of is not None else None,
    )


def _codes(result):
    return {issue.code for issue in result.issues}


def test_logical_identity_excludes_variant_group_and_partition_keeps_lanes():
    collection, title, group_a, group_b = _graph()
    first = _video(collection, title, 1, 1, group=group_a)
    second = _video(collection, title, 2, 1, group=group_b)

    assert logical_episode_identity(first) == logical_episode_identity(second)
    partition = logical_episode_partitions([first, second])[0]
    assert partition.identity.season_episode_number == 1
    assert [item.video_variant_group_id for item in partition.confirmed_variants] == [
        group_a.id,
        group_b.id,
    ]
    assert partition.unassigned_videos == ()
    assert unresolved_duplicate_groups([first, second]) == ()


def test_logical_identity_is_scoped_to_catalog_title():
    collection, title, group_a, _group_b = _graph()
    other_title = CatalogTitle(
        id=11,
        collection=collection,
        local_title="Season 2",
        normalized_local_title="season 2",
        relative_root_path="Anime/Show/Season 2",
        part_type="season",
        season_number=2,
        season_label="S2",
    )
    first = _video(collection, title, 1, 1, group=group_a)
    second = _video(collection, other_title, 2, 1)

    assert logical_episode_identity(first) != logical_episode_identity(second)


def test_same_group_is_collision_but_distinct_groups_are_not():
    collection, title, group_a, group_b = _graph()
    first = _video(collection, title, 1, 1, group=group_a)
    same_lane_copy = _video(collection, title, 2, 1, group=group_a)
    other_lane = _video(collection, title, 3, 1, group=group_b)

    groups = unresolved_duplicate_groups([first, other_lane, same_lane_copy])

    assert len(groups) == 1
    assert groups[0].video_variant_group_id == group_a.id
    assert groups[0].video_variant_label == "A"
    assert groups[0].has_unassigned_variant is False
    assert {video.id for video in groups[0].videos} == {first.id, same_lane_copy.id}


def test_two_colliding_lanes_of_one_episode_remain_two_workflow_groups():
    collection, title, group_a, group_b = _graph()
    videos = [
        _video(collection, title, 1, 1, group=group_a),
        _video(collection, title, 2, 1, group=group_a),
        _video(collection, title, 3, 1, group=group_b),
        _video(collection, title, 4, 1, group=group_b),
    ]

    groups = unresolved_duplicate_groups(videos)

    assert len(groups) == 2
    assert [group.video_variant_group_id for group in groups] == [
        group_a.id,
        group_b.id,
    ]
    assert [{video.id for video in group.videos} for group in groups] == [
        {1, 2},
        {3, 4},
    ]


def test_null_collisions_and_known_plus_null_remain_visible_for_review():
    collection, title, group_a, _group_b = _graph()
    first_null = _video(collection, title, 1, 1)
    second_null = _video(collection, title, 2, 1)
    null_group = unresolved_duplicate_groups([first_null, second_null])

    assert len(null_group) == 1
    assert null_group[0].has_unassigned_variant is True
    assert {video.id for video in null_group[0].videos} == {1, 2}

    known = _video(collection, title, 3, 1, group=group_a)
    known_and_null = unresolved_duplicate_groups([known, first_null])
    assert len(known_and_null) == 1
    assert known_and_null[0].has_unassigned_variant is True
    assert {video.id for video in known_and_null[0].videos} == {1, 3}


def test_physical_logical_variant_and_confirmed_duplicate_counts_are_independent():
    collection, title, group_a, group_b = _graph()
    primary = _video(collection, title, 1, 1, group=group_a)
    secondary = _video(
        collection,
        title,
        2,
        1,
        group=group_a,
        duplicate_of=primary,
    )
    other_lane = _video(collection, title, 3, 1, group=group_b)

    summary = summarize_title_numbering([primary, secondary, other_lane], title)

    assert summary.physical_video_count == summary.total == 3
    assert summary.logical_episode_count == summary.standard_total == 1
    assert summary.confirmed_variant_instance_count == 2
    assert summary.confirmed_duplicate_count == summary.confirmed_duplicates == 1
    assert summary.unassigned_variant_video_count == 0
    assert summary.duplicate_numbers == ()


def test_logical_range_and_counts_use_episode_identity_not_variant_count():
    collection, title, group_a, group_b = _graph()
    videos = []
    video_id = 1
    for episode, groups in ((1, (group_a, group_b)), (2, (group_a, group_b)), (3, (group_a,))):
        for group in groups:
            videos.append(_video(collection, title, video_id, episode, group=group))
            video_id += 1

    summary = summarize_title_numbering(videos, title)

    assert summary.physical_video_count == 5
    assert summary.logical_episode_count == summary.standard_total == 3
    assert summary.numbered == 3
    assert summary.confirmed_variant_instance_count == 5
    assert (summary.episode_min, summary.episode_max, summary.gaps) == (1, 3, ())


def test_unassigned_collision_counts_one_identity_and_no_confirmed_variants():
    collection, title, _group_a, _group_b = _graph()
    videos = [_video(collection, title, 1, 1), _video(collection, title, 2, 1)]

    summary = summarize_title_numbering(videos, title)

    assert summary.physical_video_count == 2
    assert summary.logical_episode_count == 1
    assert summary.confirmed_variant_instance_count == 0
    assert summary.unassigned_variant_video_count == 2
    assert summary.duplicate_numbers == (1,)
    assert summary.requires_review is True


def test_hierarchy_blocks_same_or_unassigned_lane_but_not_distinct_lanes():
    collection, title, group_a, group_b = _graph()
    different = [
        _video(collection, title, 1, 1, group=group_a),
        _video(collection, title, 2, 1, group=group_b),
    ]
    assert HierarchyIssueCode.CANONICAL_DUPLICATE not in _codes(
        evaluate_collection_hierarchy(collection, different)
    )

    same = [
        _video(collection, title, 3, 1, group=group_a),
        _video(collection, title, 4, 1, group=group_a),
    ]
    assert HierarchyIssueCode.CANONICAL_DUPLICATE in _codes(
        evaluate_collection_hierarchy(collection, same)
    )

    unassigned = [_video(collection, title, 5, 1), _video(collection, title, 6, 1)]
    assert HierarchyIssueCode.CANONICAL_DUPLICATE in _codes(
        evaluate_collection_hierarchy(collection, unassigned)
    )


def test_confirmed_duplicate_across_distinct_known_groups_is_explicit_blocker():
    collection, title, group_a, group_b = _graph()
    primary = _video(collection, title, 1, 1, group=group_a)
    secondary = _video(
        collection,
        title,
        2,
        1,
        group=group_b,
        duplicate_of=primary,
    )

    conflicts = confirmed_duplicate_variant_conflicts([primary, secondary])
    result = evaluate_collection_hierarchy(collection, [primary, secondary])
    summary = summarize_title_numbering([primary, secondary], title)

    assert len(conflicts) == 1
    assert summary.variant_inconsistent_confirmed_duplicates == 1
    assert HierarchyIssueCode.CONFIRMED_DUPLICATE in _codes(result)
    issue = next(
        issue for issue in result.issues
        if issue.code == HierarchyIssueCode.CONFIRMED_DUPLICATE_VARIANT_CONFLICT
    )
    assert issue.blocking is True
    assert result.status == "review_required"


def test_ver_tv_hint_never_assigns_group_but_manual_lanes_resolve_real_shape():
    collection, title, group_a, group_tv = _graph()
    videos = []
    video_id = 1
    for episode in range(1, 14):
        videos.append(_video(
            collection,
            title,
            video_id,
            episode,
            filename=f"Show - {episode:02d}.mkv",
        ))
        video_id += 1
        if episode <= 12:
            videos.append(_video(
                collection,
                title,
                video_id,
                episode,
                filename=f"Show - {episode:02d} Ver.TV.mkv",
            ))
            video_id += 1

    assert all(video.video_variant_group_id is None for video in videos)
    assert len(unresolved_duplicate_groups(videos)) == 12
    assert summarize_title_numbering(videos, title).confirmed_variant_instance_count == 0

    for video in videos:
        group = (
            group_tv
            if detect_episode_number(video.filename).version_hint == "Ver.TV"
            else group_a
        )
        video.video_variant_group = group
        video.video_variant_group_id = group.id

    summary = summarize_title_numbering(videos, title)
    result = evaluate_collection_hierarchy(collection, videos)
    assert summary.physical_video_count == 25
    assert summary.logical_episode_count == 13
    assert summary.confirmed_variant_instance_count == 25
    assert summary.duplicate_numbers == ()
    assert HierarchyIssueCode.CANONICAL_DUPLICATE not in _codes(result)


def test_structural_ab_fractional_zero_and_unknown_stay_noncanonical():
    collection, title, _group_a, _group_b = _graph()
    videos = [
        _video(collection, title, 1, 1, filename="Show - 01A.mkv"),
        _video(collection, title, 2, 1, filename="Show - 01B.mkv"),
        _video(collection, title, 3, 1, filename="Show - 04.5.mkv"),
        _video(collection, title, 4, 1, filename="Show - 00.mkv"),
        _video(collection, title, 5, 1, filename="Show unknown.mkv"),
    ]

    recalculate_title_numbering(title, videos)

    assert [video.season_episode_number for video in videos] == [None] * 5
    assert logical_episode_partitions(videos) == ()
    assert unresolved_duplicate_groups(videos) == ()
    assert all(video.video_variant_group_id is None for video in videos)
    assert [video.episode_number_source for video in videos[:2]] == [
        "structural_variant",
        "structural_variant",
    ]


def test_supplementary_duplicate_identity_ignores_variant_lanes_as_before():
    collection, title, group_a, group_b = _graph()
    first = _video(
        collection,
        title,
        1,
        1,
        group=group_a,
        filename="Show OVA 01.mkv",
    )
    second = _video(
        collection,
        title,
        2,
        1,
        group=group_b,
        filename="Show OVA 01.mkv",
    )

    groups = unresolved_duplicate_groups([first, second])

    assert len(groups) == 1
    assert groups[0].supplementary_type == "ova"
    assert {video.id for video in groups[0].videos} == {1, 2}
