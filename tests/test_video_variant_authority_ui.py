import asyncio
from urllib.parse import urlencode

import pytest
from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.database import Base, make_engine
from app.hierarchy_rebuild import apply_hierarchy_rebuild_plan, build_hierarchy_rebuild_plan
from app.main import create_app
from app.migrations import migrate_schema
from app.models import (
    CatalogCollection,
    CatalogTitle,
    ExternalSubtitle,
    TitleMetadata,
    Video,
    VideoVariantGroup,
)
from app.numbering import summarize_title_numbering, unresolved_duplicate_groups
from app.video_variants import (
    CONFIRMED_DUPLICATE_VARIANT_CONFLICT_MESSAGE,
    VariantGroupDraft,
    apply_structural_ab_confirmation,
    apply_video_variant_assignments,
    assign_video_variant_group,
    create_video_variant_group,
    create_video_variant_group_for_title,
    delete_empty_video_variant_group,
    parser_variant_suggestion,
    preview_repeated_variant_lane,
    preview_structural_ab_confirmation,
    preview_video_variant_assignments,
    repeated_variant_lane_proposal,
    structural_ab_pair_proposals,
    update_video_variant_group_for_title,
)


def _title(collection, name="Season 1", path=None):
    return CatalogTitle(
        collection=collection,
        local_title=name,
        normalized_local_title=name.casefold(),
        relative_root_path=path or f"{collection.relative_root_path}/{name}",
        part_type="season",
        season_number=1,
        season_label="S1",
    )


def _video(collection, title, filename, episode=None):
    return Video(
        relative_path=f"{title.relative_root_path}/{filename}",
        root_folder="Anime",
        filename=filename,
        size=1,
        mtime_ns=1,
        local_episode_number=episode,
        season_episode_number=episode,
        absolute_episode_number=episode,
        episode_number_source="filename" if episode is not None else "structural_variant",
        catalog_collection=collection,
        catalog_title=title,
    )


def _stored_graph(tmp_path, name, filenames):
    engine = make_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions() as session:
        collection = CatalogCollection(
            local_title="Show",
            normalized_local_title="show",
            relative_root_path="Anime/Show",
        )
        title = _title(collection)
        videos = [
            _video(collection, title, filename, episode)
            for filename, episode in filenames
        ]
        other = _title(collection, "Other", "Anime/Show/Other")
        session.add_all([collection, title, other, *videos])
        session.commit()
        return sessions, collection.id, title.id, other.id, [video.id for video in videos]


def _apply_preview(
    session,
    collection_id,
    title_id,
    assignments,
    drafts,
    *,
    workflow="manual_bulk",
    require_distinct=False,
):
    preview = preview_video_variant_assignments(
        session,
        collection_id,
        title_id,
        assignments=assignments,
        drafts=drafts,
        workflow=workflow,
        require_distinct=require_distinct,
    )
    apply_video_variant_assignments(
        session,
        collection_id,
        title_id,
        assignments=assignments,
        drafts=drafts,
        expected_fingerprint=preview.fingerprint,
        workflow=workflow,
        require_distinct=require_distinct,
    )
    return preview


def _post_request(web_app, path, items):
    body = urlencode(items).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({
        "type": "http",
        "app": web_app,
        "method": "POST",
        "path": path,
        "root_path": "",
        "scheme": "http",
        "query_string": b"",
        "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
    }, receive)


def _get_request(web_app, path):
    return Request({
        "type": "http",
        "app": web_app,
        "method": "GET",
        "path": path,
        "root_path": "",
        "scheme": "http",
        "query_string": b"",
        "headers": [],
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
    })


def test_group_management_create_edit_delete_and_reject_nonempty(tmp_path):
    sessions, collection_id, title_id, _other_id, video_ids = _stored_graph(
        tmp_path, "groups.db", [("Show - 01.mkv", 1)]
    )
    with sessions() as session:
        group = create_video_variant_group_for_title(
            session,
            collection_id,
            title_id,
            manual_label=" TV ",
            release_source="TV",
            content_variant="",
            note=" broadcast ",
        )
        session.commit()
        group_id = group.id
        assert (group.manual_label, group.release_source, group.content_variant) == (
            "TV", "tv", None,
        )

    with sessions() as session:
        updated = update_video_variant_group_for_title(
            session,
            collection_id,
            title_id,
            group_id,
            manual_label="BD-UC",
            release_source="bd",
            content_variant="uncensored",
            note="confirmed",
        )
        assert updated.id == group_id
        assign_video_variant_group(session.get(Video, video_ids[0]), updated)
        session.commit()

    with sessions() as session:
        with pytest.raises(ValueError, match="Neprázdnou"):
            delete_empty_video_variant_group(
                session, collection_id, title_id, group_id
            )
        session.rollback()
        assign_video_variant_group(session.get(Video, video_ids[0]), None)
        session.flush()
        delete_empty_video_variant_group(session, collection_id, title_id, group_id)
        session.commit()
        assert session.get(VideoVariantGroup, group_id) is None


def test_assign_reassign_clear_and_cross_title_rejection(tmp_path):
    sessions, collection_id, title_id, other_id, video_ids = _stored_graph(
        tmp_path, "transitions.db", [("Show - 01.mkv", 1)]
    )
    with sessions() as session:
        title = session.get(CatalogTitle, title_id)
        other = session.get(CatalogTitle, other_id)
        group_a = create_video_variant_group(title, manual_label="A")
        group_b = create_video_variant_group(title, manual_label="B")
        foreign = create_video_variant_group(other, manual_label="foreign")
        session.add_all([group_a, group_b, foreign])
        session.flush()
        group_a_id, group_b_id, foreign_id = group_a.id, group_b.id, foreign.id
        session.commit()

    with sessions() as session:
        _apply_preview(
            session,
            collection_id,
            title_id,
            ((video_ids[0], "target"),),
            (VariantGroupDraft("target", existing_group_id=group_a_id),),
        )
        session.commit()
        assert session.get(Video, video_ids[0]).video_variant_group_id == group_a_id

    with sessions() as session:
        _apply_preview(
            session,
            collection_id,
            title_id,
            ((video_ids[0], "target"),),
            (VariantGroupDraft("target", existing_group_id=group_b_id),),
        )
        session.commit()
        assert session.get(Video, video_ids[0]).video_variant_group_id == group_b_id

    with sessions() as session:
        _apply_preview(
            session,
            collection_id,
            title_id,
            ((video_ids[0], "null"),),
            (),
        )
        session.commit()
        assert session.get(Video, video_ids[0]).video_variant_group_id is None
        with pytest.raises(ValueError, match="nepatří"):
            preview_video_variant_assignments(
                session,
                collection_id,
                title_id,
                assignments=((video_ids[0], "foreign"),),
                drafts=(VariantGroupDraft("foreign", existing_group_id=foreign_id),),
            )


def test_bulk_clear_preview_cancel_multiple_and_empty_group_lifecycle(tmp_path):
    sessions, collection_id, title_id, _other_id, video_ids = _stored_graph(
        tmp_path,
        "bulk-clear.db",
        [("Show - 01.mkv", 1), ("Show - 02.mkv", 2)],
    )
    with sessions() as session:
        title = session.get(CatalogTitle, title_id)
        group = create_video_variant_group(title, manual_label="TV", release_source="tv")
        session.add(group)
        session.flush()
        group_id = group.id
        for video_id in video_ids:
            assign_video_variant_group(session.get(Video, video_id), group)
        session.commit()

    assignments = tuple((video_id, "null") for video_id in video_ids)
    with sessions() as session:
        preview = preview_video_variant_assignments(
            session,
            collection_id,
            title_id,
            assignments=assignments,
            drafts=(),
        )
        assert [row.current_label for row in preview.rows] == [
            "TV · source TV", "TV · source TV",
        ]
        assert [row.target_label for row in preview.rows] == ["neurčeno", "neurčeno"]
        assert len(session.get(VideoVariantGroup, group_id).videos) == 2
        session.rollback()

    with sessions() as session:
        assert {
            session.get(Video, video_id).video_variant_group_id for video_id in video_ids
        } == {group_id}
        preview = preview_video_variant_assignments(
            session,
            collection_id,
            title_id,
            assignments=assignments,
            drafts=(),
        )
        apply_video_variant_assignments(
            session,
            collection_id,
            title_id,
            assignments=assignments,
            drafts=(),
            expected_fingerprint=preview.fingerprint,
        )
        session.commit()

    with sessions() as session:
        assert all(
            session.get(Video, video_id).video_variant_group_id is None
            for video_id in video_ids
        )
        group = session.get(VideoVariantGroup, group_id)
        assert group is not None
        assert group.videos == []
        delete_empty_video_variant_group(
            session, collection_id, title_id, group_id
        )
        session.commit()
        assert session.get(VideoVariantGroup, group_id) is None


def test_clear_changes_only_variant_fk_and_preserves_manual_authorities(tmp_path):
    sessions, collection_id, title_id, _other_id, video_ids = _stored_graph(
        tmp_path,
        "clear-preservation.db",
        [("Show - 01 copy.mkv", 1), ("Show - 01.mkv", 1)],
    )
    with sessions() as session:
        title = session.get(CatalogTitle, title_id)
        title.hierarchy_manual_override = True
        title.season_number_manual = 1
        title.season_label_manual = "S1 manual"
        title.part_type_manual = "season"
        title.sort_order_manual = 7
        title.manual_display_title = "Manual Show"
        title.metadata_record = TitleMetadata(
            display_title="Metadata Show",
            metadata_provider="test",
            metadata_external_id="show-1",
        )
        group = create_video_variant_group(title, manual_label="TV")
        session.add(group)
        session.flush()
        selected = session.get(Video, video_ids[0])
        primary = session.get(Video, video_ids[1])
        assign_video_variant_group(selected, group)
        assign_video_variant_group(primary, group)
        selected.external_episode_number = 101
        selected.episode_number_manual_override = 1
        selected.duplicate_of = primary
        selected.duplicate_primary_missing = False
        selected.duplicate_status_manual = "suspected"
        selected.content_type_manual = "episode"
        selected.manual_hardsub_cs = True
        selected.czsk_availability_manual = "unavailable"
        selected.external_subtitles.append(ExternalSubtitle(
            relative_path="Anime/Show/Season 1/Show - 01 copy.cs.ass",
            codec="ass",
            language="cs",
            normalized_language="cs",
            manual_language="cs",
            match_method="manual",
        ))
        session.commit()

    def snapshots(session):
        video = session.get(Video, video_ids[0])
        title = session.get(CatalogTitle, title_id)
        subtitle = video.external_subtitles[0]
        return (
            (
                video.relative_path,
                video.root_folder,
                video.filename,
                video.size,
                video.mtime_ns,
                video.local_episode_number,
                video.season_episode_number,
                video.absolute_episode_number,
                video.external_episode_number,
                video.episode_number_source,
                video.episode_number_manual_override,
                video.duplicate_of_video_id,
                video.duplicate_primary_missing,
                video.duplicate_status_manual,
                video.content_type_manual,
                video.manual_hardsub_cs,
                video.czsk_availability_manual,
                video.catalog_title_id,
                video.catalog_collection_id,
            ),
            (
                title.hierarchy_manual_override,
                title.season_number_manual,
                title.season_label_manual,
                title.part_type_manual,
                title.sort_order_manual,
                title.manual_display_title,
                title.metadata_record.display_title,
                title.metadata_record.metadata_provider,
                title.metadata_record.metadata_external_id,
            ),
            (
                subtitle.id,
                subtitle.video_id,
                subtitle.relative_path,
                subtitle.codec,
                subtitle.language,
                subtitle.normalized_language,
                subtitle.manual_language,
                subtitle.match_method,
            ),
        )

    with sessions() as session:
        before = snapshots(session)
        preview = preview_video_variant_assignments(
            session,
            collection_id,
            title_id,
            assignments=((video_ids[0], "null"),),
            drafts=(),
        )
        apply_video_variant_assignments(
            session,
            collection_id,
            title_id,
            assignments=((video_ids[0], "null"),),
            drafts=(),
            expected_fingerprint=preview.fingerprint,
        )
        session.commit()

    with sessions() as session:
        assert session.get(Video, video_ids[0]).video_variant_group_id is None
        assert snapshots(session) == before


def test_distinct_same_known_null_and_clear_collision_semantics(tmp_path):
    sessions, collection_id, title_id, _other_id, video_ids = _stored_graph(
        tmp_path,
        "collisions.db",
        [("Show - 01 Ver.TV.mp4", 1), ("Show - 01.mp4", 1)],
    )
    with sessions() as session:
        distinct = _apply_preview(
            session,
            collection_id,
            title_id,
            ((video_ids[0], "tv"), (video_ids[1], "plain")),
            (
                VariantGroupDraft("tv", manual_label="TV", release_source="tv"),
                VariantGroupDraft("plain", manual_label="Other master"),
            ),
            workflow="collision",
            require_distinct=True,
        )
        assert (distinct.unresolved_collisions_before, distinct.unresolved_collisions_after) == (
            1, 0,
        )
        session.commit()
        title = session.get(CatalogTitle, title_id)
        assert unresolved_duplicate_groups(list(title.videos)) == ()
        summary = summarize_title_numbering(list(title.videos), title)
        assert summary.logical_episode_count == 1
        assert summary.confirmed_variant_instance_count == 2
        group_ids = [session.get(Video, video_id).video_variant_group_id for video_id in video_ids]

    with sessions() as session:
        clear_preview = _apply_preview(
            session,
            collection_id,
            title_id,
            ((video_ids[1], "null"),),
            (),
        )
        assert clear_preview.unresolved_collisions_after == 1
        session.commit()
        title = session.get(CatalogTitle, title_id)
        assert len(unresolved_duplicate_groups(list(title.videos))) == 1
        summary = summarize_title_numbering(list(title.videos), title)
        assert summary.logical_episode_count == 1
        assert summary.confirmed_variant_instance_count == 1
        assert [video.season_episode_number for video in title.videos] == [1, 1]
        assert len(session.get(VideoVariantGroup, group_ids[1]).videos) == 0

    with sessions() as session:
        restored = _apply_preview(
            session,
            collection_id,
            title_id,
            ((video_ids[1], "restored"),),
            (VariantGroupDraft("restored", existing_group_id=group_ids[1]),),
        )
        assert restored.unresolved_collisions_after == 0
        session.commit()
        title = session.get(CatalogTitle, title_id)
        assert unresolved_duplicate_groups(list(title.videos)) == ()
        assert summarize_title_numbering(
            list(title.videos), title
        ).confirmed_variant_instance_count == 2
        assert len(session.get(VideoVariantGroup, group_ids[1]).videos) == 1

    with sessions() as session:
        same = _apply_preview(
            session,
            collection_id,
            title_id,
            ((video_ids[1], "same"),),
            (VariantGroupDraft("same", existing_group_id=group_ids[0]),),
        )
        assert (same.unresolved_collisions_after, same.duplicate_collisions_after) == (1, 1)
        session.commit()


def test_confirmed_duplicate_cross_group_is_rejected_and_same_group_is_valid(tmp_path):
    sessions, collection_id, title_id, _other_id, video_ids = _stored_graph(
        tmp_path,
        "duplicate-safety.db",
        [("Show - 01 A.mkv", 1), ("Show - 01 B.mkv", 1)],
    )
    with sessions() as session:
        title = session.get(CatalogTitle, title_id)
        first, second = [session.get(Video, video_id) for video_id in video_ids]
        second.duplicate_of = first
        group_a = create_video_variant_group(title, manual_label="A")
        group_b = create_video_variant_group(title, manual_label="B")
        session.add_all([group_a, group_b])
        session.flush()
        group_a_id, group_b_id = group_a.id, group_b.id
        session.commit()

    with sessions() as session:
        with pytest.raises(ValueError, match="Potvrzená duplicita"):
            preview_video_variant_assignments(
                session,
                collection_id,
                title_id,
                assignments=((video_ids[0], "a"), (video_ids[1], "b")),
                drafts=(
                    VariantGroupDraft("a", existing_group_id=group_a_id),
                    VariantGroupDraft("b", existing_group_id=group_b_id),
                ),
            )
        session.rollback()
        preview = _apply_preview(
            session,
            collection_id,
            title_id,
            ((video_ids[0], "same"), (video_ids[1], "same")),
            (VariantGroupDraft("same", existing_group_id=group_a_id),),
        )
        assert preview.new_blockers == ()
        session.commit()
        assert {
            session.get(Video, video_id).video_variant_group_id for video_id in video_ids
        } == {group_a_id}

    with sessions() as session:
        first, second = [session.get(Video, video_id) for video_id in video_ids]
        with pytest.raises(ValueError, match=CONFIRMED_DUPLICATE_VARIANT_CONFLICT_MESSAGE):
            assign_video_variant_group(second, session.get(VideoVariantGroup, group_b_id))


def test_nande_style_lane_proposal_preview_reject_and_atomic_confirm(tmp_path):
    filenames = [
        item
        for episode in range(1, 13)
        for item in (
            (f"Show - {episode:02d} Ver.TV.mp4", episode),
            (f"Show - {episode:02d}.mp4", episode),
        )
    ]
    sessions, collection_id, title_id, _other_id, _video_ids = _stored_graph(
        tmp_path, "nande.db", filenames
    )
    with sessions() as session:
        title = session.get(CatalogTitle, title_id)
        proposal = repeated_variant_lane_proposal(title)
        assert proposal is not None and len(proposal.pairs) == 12
        assert proposal.hinted_suggestion == parser_variant_suggestion(
            next(video for video in title.videos if "Ver.TV" in video.filename)
        )
        assert proposal.hinted_suggestion.release_source == "tv"
        preview = preview_repeated_variant_lane(
            session,
            collection_id,
            title_id,
            hinted_draft=VariantGroupDraft(
                "hinted", manual_label="TV", release_source="tv"
            ),
            plain_draft=VariantGroupDraft("plain", manual_label="Other master"),
            expected_proposal_fingerprint=proposal.fingerprint,
        )
        assert (preview.unresolved_collisions_before, preview.unresolved_collisions_after) == (
            12, 0,
        )
        assert next(draft for draft in preview.drafts if draft.key == "plain").release_source is None
        assert next(draft for draft in preview.drafts if draft.key == "plain").content_variant is None
        assert session.scalar(select(func.count()).select_from(VideoVariantGroup)) == 0
        session.rollback()

    with sessions() as session:
        title = session.get(CatalogTitle, title_id)
        proposal = repeated_variant_lane_proposal(title)
        preview = preview_repeated_variant_lane(
            session,
            collection_id,
            title_id,
            hinted_draft=VariantGroupDraft(
                "hinted", manual_label="TV", release_source="tv"
            ),
            plain_draft=VariantGroupDraft("plain", manual_label="Other master"),
            expected_proposal_fingerprint=proposal.fingerprint,
        )
        apply_video_variant_assignments(
            session,
            collection_id,
            title_id,
            assignments=preview.assignments,
            drafts=preview.drafts,
            expected_fingerprint=preview.fingerprint,
            workflow="repeated_lane",
            require_distinct=True,
        )
        session.commit()
        title = session.get(CatalogTitle, title_id)
        assert len(title.video_variant_groups) == 2
        assert all(video.video_variant_group_id is not None for video in title.videos)
        assert unresolved_duplicate_groups(list(title.videos)) == ()


@pytest.mark.parametrize("malformation", ["third", "different_hint", "duplicate"])
def test_malformed_or_duplicate_lane_pattern_has_no_unsafe_proposal(tmp_path, malformation):
    filenames = [
        ("Show - 01 Ver.TV.mp4", 1),
        ("Show - 01.mp4", 1),
        ("Show - 02 Ver.TV.mp4", 2),
        ("Show - 02.mp4", 2),
    ]
    if malformation == "third":
        filenames.append(("Show Copy - 01.mp4", 1))
    if malformation == "different_hint":
        filenames[2] = ("Show - 02 (UC).mp4", 2)
    sessions, _collection_id, title_id, _other_id, video_ids = _stored_graph(
        tmp_path, f"malformed-{malformation}.db", filenames
    )
    with sessions() as session:
        if malformation == "duplicate":
            session.get(Video, video_ids[1]).duplicate_of = session.get(Video, video_ids[0])
            session.flush()
        assert repeated_variant_lane_proposal(session.get(CatalogTitle, title_id)) is None


def test_parser_hint_is_only_suggestion_and_uc_does_not_infer_uncensored(tmp_path):
    sessions, _collection_id, title_id, _other_id, _video_ids = _stored_graph(
        tmp_path,
        "suggestions.db",
        [("Show - 01 Ver.TV.mp4", 1), ("Show - 02 (UC).mp4", 2), ("Show - 03.mp4", 3)],
    )
    with sessions() as session:
        title = session.get(CatalogTitle, title_id)
        by_name = {video.filename: video for video in title.videos}
        tv = parser_variant_suggestion(by_name["Show - 01 Ver.TV.mp4"])
        uc = parser_variant_suggestion(by_name["Show - 02 (UC).mp4"])
        plain = parser_variant_suggestion(by_name["Show - 03.mp4"])
        assert (tv.manual_label, tv.release_source, tv.content_variant) == ("TV", "tv", None)
        assert (uc.hint, uc.content_variant) == ("UC", None)
        assert plain is None
        assert title.video_variant_groups == []
        assert all(video.video_variant_group_id is None for video in title.videos)


def test_manual_bulk_selection_supports_existing_and_new_group(tmp_path):
    sessions, collection_id, title_id, _other_id, video_ids = _stored_graph(
        tmp_path,
        "manual-bulk.db",
        [("Show - 01.mkv", 1), ("Show - 02.mkv", 2), ("Show - 03.mkv", 3)],
    )
    with sessions() as session:
        title = session.get(CatalogTitle, title_id)
        existing = create_video_variant_group(title, manual_label="TV")
        session.add(existing)
        session.flush()
        existing_id = existing.id
        session.commit()

    with sessions() as session:
        _apply_preview(
            session,
            collection_id,
            title_id,
            ((video_ids[0], "target"), (video_ids[1], "target")),
            (VariantGroupDraft("target", existing_group_id=existing_id),),
        )
        session.commit()
        assert {
            session.get(Video, video_id).video_variant_group_id
            for video_id in video_ids[:2]
        } == {existing_id}

    with sessions() as session:
        preview = _apply_preview(
            session,
            collection_id,
            title_id,
            ((video_ids[2], "new"),),
            (VariantGroupDraft("new", manual_label="WEB", release_source="web"),),
        )
        assert preview.groups_to_create == ("WEB",)
        session.commit()
        assert session.get(Video, video_ids[2]).video_variant_group.manual_label == "WEB"


def test_ab_preview_confirm_and_existing_group_reuse(tmp_path):
    sessions, collection_id, title_id, _other_id, video_ids = _stored_graph(
        tmp_path,
        "ab.db",
        [("Show - 01A.mkv", None), ("Show - 01B.mkv", None)],
    )
    with sessions() as session:
        title = session.get(CatalogTitle, title_id)
        proposal = structural_ab_pair_proposals(title)[0]
        assert (proposal.video_a_id, proposal.video_b_id) == tuple(video_ids)
        preview = preview_structural_ab_confirmation(
            session,
            collection_id,
            title_id,
            video_a_id=video_ids[0],
            video_b_id=video_ids[1],
            draft_a=VariantGroupDraft("a", manual_label="A"),
            draft_b=VariantGroupDraft("b", manual_label="B"),
            expected_proposal_fingerprint=proposal.fingerprint,
        )
        assert all(session.get(Video, video_id).season_episode_number is None for video_id in video_ids)
        assert session.scalar(select(func.count()).select_from(VideoVariantGroup)) == 0
        apply_structural_ab_confirmation(
            session,
            collection_id,
            title_id,
            video_a_id=video_ids[0],
            video_b_id=video_ids[1],
            draft_a=VariantGroupDraft("a", manual_label="A"),
            draft_b=VariantGroupDraft("b", manual_label="B"),
            expected_proposal_fingerprint=proposal.fingerprint,
            expected_assignment_fingerprint=preview.assignment_preview.fingerprint,
        )
        session.commit()
        videos = [session.get(Video, video_id) for video_id in video_ids]
        assert [video.episode_number_manual_override for video in videos] == [1, 1]
        assert [video.season_episode_number for video in videos] == [1, 1]
        assert len({video.video_variant_group_id for video in videos}) == 2
        assert unresolved_duplicate_groups(videos) == ()

    sessions2, collection2, title2, _other2, ids2 = _stored_graph(
        tmp_path,
        "ab-reuse.db",
        [("Show - 02A.mkv", None), ("Show - 02B.mkv", None)],
    )
    with sessions2() as session:
        title = session.get(CatalogTitle, title2)
        group_a = create_video_variant_group(title, manual_label="A")
        group_b = create_video_variant_group(title, manual_label="B")
        session.add_all([group_a, group_b])
        session.flush()
        proposal = structural_ab_pair_proposals(title)[0]
        preview = preview_structural_ab_confirmation(
            session,
            collection2,
            title2,
            video_a_id=ids2[0],
            video_b_id=ids2[1],
            draft_a=VariantGroupDraft("a", existing_group_id=group_a.id),
            draft_b=VariantGroupDraft("b", existing_group_id=group_b.id),
            expected_proposal_fingerprint=proposal.fingerprint,
        )
        apply_structural_ab_confirmation(
            session,
            collection2,
            title2,
            video_a_id=ids2[0],
            video_b_id=ids2[1],
            draft_a=VariantGroupDraft("a", existing_group_id=group_a.id),
            draft_b=VariantGroupDraft("b", existing_group_id=group_b.id),
            expected_proposal_fingerprint=proposal.fingerprint,
            expected_assignment_fingerprint=preview.assignment_preview.fingerprint,
        )
        session.commit()
        assert session.scalar(select(func.count()).select_from(VideoVariantGroup)) == 2


def test_ab_atomic_failure_rolls_back_numbering_and_groups(tmp_path, monkeypatch):
    sessions, collection_id, title_id, _other_id, video_ids = _stored_graph(
        tmp_path,
        "ab-rollback.db",
        [("Show - 01A.mkv", None), ("Show - 01B.mkv", None)],
    )
    with sessions() as session:
        title = session.get(CatalogTitle, title_id)
        proposal = structural_ab_pair_proposals(title)[0]
        preview = preview_structural_ab_confirmation(
            session,
            collection_id,
            title_id,
            video_a_id=video_ids[0],
            video_b_id=video_ids[1],
            draft_a=VariantGroupDraft("a", manual_label="A"),
            draft_b=VariantGroupDraft("b", manual_label="B"),
            expected_proposal_fingerprint=proposal.fingerprint,
        )
        monkeypatch.setattr(
            "app.hierarchy_evaluation.finalize_hierarchy_write",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("forced")),
        )
        with pytest.raises(RuntimeError, match="forced"):
            apply_structural_ab_confirmation(
                session,
                collection_id,
                title_id,
                video_a_id=video_ids[0],
                video_b_id=video_ids[1],
                draft_a=VariantGroupDraft("a", manual_label="A"),
                draft_b=VariantGroupDraft("b", manual_label="B"),
                expected_proposal_fingerprint=proposal.fingerprint,
                expected_assignment_fingerprint=preview.assignment_preview.fingerprint,
            )
        session.rollback()
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(VideoVariantGroup)) == 0
        for video_id in video_ids:
            video = session.get(Video, video_id)
            assert (
                video.episode_number_manual_override,
                video.season_episode_number,
                video.video_variant_group_id,
            ) == (None, None, None)


def test_assignment_clear_survives_startup_and_hierarchy_rebuild(tmp_path):
    sessions, collection_id, title_id, _other_id, video_ids = _stored_graph(
        tmp_path, "lifecycle.db", [("Show - 01.mkv", 1)]
    )
    engine = sessions.kw["bind"]
    with sessions() as session:
        title = session.get(CatalogTitle, title_id)
        group = create_video_variant_group(title, manual_label="TV")
        session.add(group)
        session.flush()
        assign_video_variant_group(session.get(Video, video_ids[0]), group)
        session.commit()
    with sessions() as session:
        _apply_preview(
            session,
            collection_id,
            title_id,
            ((video_ids[0], "null"),),
            (),
        )
        session.commit()

    migrate_schema(engine)
    migrate_schema(engine)
    with sessions() as session:
        assert session.get(Video, video_ids[0]).video_variant_group_id is None
        plan = build_hierarchy_rebuild_plan(session)
        apply_hierarchy_rebuild_plan(session, plan)
        session.commit()
    with sessions() as session:
        assert session.get(Video, video_ids[0]).video_variant_group_id is None


def test_hierarchy_review_renders_variant_management_collision_lane_and_ab(tmp_path):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'ui.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection = CatalogCollection(
            local_title="Show", normalized_local_title="show", relative_root_path="Anime/Show"
        )
        title = _title(collection)
        for episode in (1, 2):
            _video(collection, title, f"Show - {episode:02d} Ver.TV.mp4", episode)
            _video(collection, title, f"Show - {episode:02d}.mp4", episode)
        _video(collection, title, "Show - 03A.mkv", None)
        _video(collection, title, "Show - 03B.mkv", None)
        session.add(collection)
        session.commit()
        collection_id = collection.id

    endpoint = next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None) == "/hierarchy-review/{collection_id}"
    )
    rendered = endpoint(
        _get_request(web_app, f"/hierarchy-review/{collection_id}"), collection_id
    ).body.decode()
    assert "Video varianty" in rendered
    assert "Potvrdit E01 jako různé video varianty" in rendered
    assert "Nalezen kandidát na opakující se video varianty" in rendered
    assert "Lane B nemá bezpečný BD/UC/uncensored návrh" in rendered
    assert "Potvrdit A/B jako varianty stejné epizody" in rendered
    assert "parserový návrh" in rendered
    assert "Přiřadit, změnit nebo odebrat variantu u vybraných videí" in rendered
    assert '<option value="null">Neurčeno / odebrat z varianty</option>' in rendered
    assert "Zobrazit náhled změny varianty" in rendered
    assert "Video.id" not in rendered


def test_hierarchy_review_clear_target_previews_and_confirms_without_video_id_input(
    tmp_path,
):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'ui-clear.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection = CatalogCollection(
            local_title="Show", normalized_local_title="show",
            relative_root_path="Anime/Show",
        )
        title = _title(collection)
        tv_video = _video(collection, title, "Show - 06 Ver.TV.mp4", 6)
        plain_video = _video(collection, title, "Show - 06.mp4", 6)
        session.add(collection)
        session.flush()
        tv_group = create_video_variant_group(title, manual_label="TV", release_source="tv")
        bd_group = create_video_variant_group(title, manual_label="BD", release_source="bd")
        session.add_all([tv_group, bd_group])
        session.flush()
        assign_video_variant_group(tv_video, tv_group)
        assign_video_variant_group(plain_video, bd_group)
        session.commit()
        collection_id, title_id = collection.id, title.id
        plain_video_id, bd_group_id = plain_video.id, bd_group.id

    endpoints = {
        route.path: route.endpoint for route in web_app.routes
        if hasattr(route, "endpoint")
    }
    detail = endpoints["/hierarchy-review/{collection_id}"](
        _get_request(web_app, f"/hierarchy-review/{collection_id}"), collection_id
    ).body.decode()
    assert "Show - 06.mp4" in detail
    assert "Současná varianta: BD" in detail
    assert '<option value="null">Neurčeno / odebrat z varianty</option>' in detail
    assert "Video.id" not in detail

    preview_path = f"/hierarchy-review/{collection_id}/variants/assignment-preview"
    response = asyncio.run(endpoints[
        "/hierarchy-review/{collection_id}/variants/assignment-preview"
    ](_post_request(web_app, preview_path, [
        ("catalog_title_id", str(title_id)),
        ("workflow", "manual_bulk"),
        ("video_ids", str(plain_video_id)),
        ("group_choice_bulk", "null"),
    ]), collection_id))
    rendered = response.body.decode()
    assert response.status_code == 200
    assert "Náhled hromadné změny video varianty" in rendered
    assert "Show - 06.mp4" in rendered
    assert "BD · source BD" in rendered
    assert "neurčeno" in rendered
    assert 'name="confirm_variant_assignment" value="true" required' in rendered
    with web_app.state.sessions() as session:
        assert session.get(Video, plain_video_id).video_variant_group_id == bd_group_id
        assert len(session.get(VideoVariantGroup, bd_group_id).videos) == 1
        preview = preview_video_variant_assignments(
            session,
            collection_id,
            title_id,
            assignments=((plain_video_id, "null"),),
            drafts=(),
        )

    confirm_path = f"/hierarchy-review/{collection_id}/variants/assignment-confirm"
    response = asyncio.run(endpoints[
        "/hierarchy-review/{collection_id}/variants/assignment-confirm"
    ](_post_request(web_app, confirm_path, [
        ("catalog_title_id", str(title_id)),
        ("workflow", "manual_bulk"),
        ("expected_fingerprint", preview.fingerprint),
        ("variant_assignment", f"{plain_video_id}|null"),
        ("confirm_variant_assignment", "true"),
    ]), collection_id))
    assert response.status_code == 303
    with web_app.state.sessions() as session:
        assert session.get(Video, plain_video_id).video_variant_group_id is None
        group = session.get(VideoVariantGroup, bd_group_id)
        assert group is not None and group.videos == []

    response = asyncio.run(endpoints[
        "/hierarchy-review/{collection_id}/variants/assignment-preview"
    ](_post_request(web_app, preview_path, [
        ("catalog_title_id", str(title_id)),
        ("workflow", "manual_bulk"),
        ("group_choice_bulk", "null"),
    ]), collection_id))
    assert response.status_code == 400
    assert "Vyberte alespoň jedno video" in response.body.decode()
    assert not response.headers.get("content-type", "").startswith("application/json")


def test_invalid_variant_post_returns_readable_hierarchy_review_and_required_confirmation(tmp_path):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'ui-invalid.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection = CatalogCollection(
            local_title="Show", normalized_local_title="show", relative_root_path="Anime/Show"
        )
        title = _title(collection)
        video = _video(collection, title, "Show - 01.mkv", 1)
        session.add(collection)
        session.commit()
        collection_id, title_id, video_id = collection.id, title.id, video.id

    endpoints = {
        route.path: route.endpoint for route in web_app.routes
        if hasattr(route, "endpoint")
    }
    path = f"/hierarchy-review/{collection_id}/variants/assignment-preview"
    response = asyncio.run(endpoints[
        "/hierarchy-review/{collection_id}/variants/assignment-preview"
    ](_post_request(web_app, path, [
        ("catalog_title_id", str(title_id)),
        ("workflow", "manual_bulk"),
        ("video_ids", str(video_id)),
        ("group_choice_bulk", "new"),
        ("group_label_bulk", ""),
    ]), collection_id))
    assert response.status_code == 400
    assert "Označení video variant group nesmí být prázdné" in response.body.decode()
    assert not response.headers.get("content-type", "").startswith("application/json")
    with web_app.state.sessions() as session:
        assert session.scalar(select(func.count()).select_from(VideoVariantGroup)) == 0

    response = asyncio.run(endpoints[
        "/hierarchy-review/{collection_id}/variants/assignment-preview"
    ](_post_request(web_app, path, [
        ("catalog_title_id", str(title_id)),
        ("workflow", "manual_bulk"),
        ("video_ids", str(video_id)),
        ("group_choice_bulk", "new"),
        ("group_label_bulk", "TV"),
        ("group_source_bulk", "tv"),
    ]), collection_id))
    rendered = response.body.decode()
    assert response.status_code == 200
    assert "Náhled hromadné změny video varianty" in rendered
    assert 'name="confirm_variant_assignment" value="true" required' in rendered
    assert "Show - 01.mkv" in rendered
    with web_app.state.sessions() as session:
        assert session.scalar(select(func.count()).select_from(VideoVariantGroup)) == 0
        assert session.get(Video, video_id).video_variant_group_id is None

    confirm_path = f"/hierarchy-review/{collection_id}/variants/assignment-confirm"
    response = asyncio.run(endpoints[
        "/hierarchy-review/{collection_id}/variants/assignment-confirm"
    ](_post_request(web_app, confirm_path, [
        ("catalog_title_id", str(title_id)),
        ("workflow", "manual_bulk"),
    ]), collection_id))
    assert response.status_code == 400
    assert "explicitně potvrdit" in response.body.decode()
    with web_app.state.sessions() as session:
        assert session.get(Video, video_id).video_variant_group_id is None


def test_variant_mutation_routes_are_post_only():
    from app.main import app

    paths = {route.path: route.methods for route in app.routes if hasattr(route, "methods")}
    for suffix in (
        "groups",
        "assignment-preview",
        "assignment-confirm",
        "lane-preview",
        "lane-confirm",
        "ab-preview",
        "ab-confirm",
    ):
        assert paths[f"/hierarchy-review/{{collection_id}}/variants/{suffix}"] == {"POST"}
