from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi import Request
from sqlalchemy import event

from app.catalog import build_catalog_results, video_matches_search
from app.catalog_video_presentation import build_catalog_title_video_presentation
from app.config import Settings
from app.database import Base
from app.main import create_app
from app.models import (
    AudioTrack,
    CatalogCollection,
    CatalogTitle,
    ExternalSubtitle,
    ExternalSubtitleCompatibility,
    Video,
    VideoVariantGroup,
)


def _collection(identifier=1, name="Show"):
    return CatalogCollection(
        id=identifier,
        local_title=name,
        normalized_local_title=name.casefold(),
        relative_root_path=f"Anime/{name}",
    )


def _title(collection, identifier=1, name="Season 1", path=None, part_type="season"):
    return CatalogTitle(
        id=identifier,
        collection=collection,
        local_title=name,
        normalized_local_title=name.casefold(),
        relative_root_path=path or f"{collection.relative_root_path}/{name}",
        part_type=part_type,
        season_number=1 if part_type == "season" else None,
        season_label="S1" if part_type == "season" else None,
    )


def _video(collection, title, identifier, filename, episode, *, file_type="episode"):
    return Video(
        id=identifier,
        relative_path=f"{title.relative_root_path}/{filename}",
        root_folder="Anime",
        filename=filename,
        size=identifier,
        mtime_ns=identifier,
        file_type=file_type,
        local_episode_number=episode,
        season_episode_number=episode,
        absolute_episode_number=episode,
        episode_number_source="filename",
        catalog_collection=collection,
        catalog_title=title,
    )


def _add_automatic_external(video, subtitle):
    video.external_subtitle_compatibilities.append(ExternalSubtitleCompatibility(
        external_subtitle=subtitle,
        status="automatic_match",
        match_method="filename",
    ))


def _group(title, identifier, label, source=None, content=None):
    return VideoVariantGroup(
        id=identifier,
        catalog_title=title,
        manual_label=label,
        release_source=source,
        content_variant=content,
    )


def _assign(video, group):
    video.video_variant_group = group
    video.video_variant_group_id = group.id


def _request(web_app, path, query=""):
    return Request({
        "type": "http",
        "app": web_app,
        "method": "GET",
        "path": path,
        "root_path": "",
        "scheme": "http",
        "query_string": query.encode(),
        "headers": [],
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
    })


def test_single_null_video_keeps_one_simple_logical_episode_row():
    collection = _collection()
    title = _title(collection)
    video = _video(collection, title, 1, "Show - 01.mkv", 1)

    presentation = build_catalog_title_video_presentation([video], title)

    assert len(presentation.logical_episodes) == 1
    assert presentation.logical_episodes[0].label == "E01"
    assert presentation.logical_episodes[0].show_variant_lanes is False
    assert len(presentation.display_rows) == 1
    assert presentation.display_rows[0].episode_heading is None
    assert presentation.display_rows[0].variant_heading is None


def test_distinct_known_null_and_same_group_variant_lanes():
    collection = _collection()
    title = _title(collection)
    tv = _group(title, 1, "TV", "tv", "censored")
    bd = _group(title, 2, "BD", "bd", "uncensored")
    first = _video(collection, title, 1, "Show - 01 Ver.TV.mkv", 1)
    second = _video(collection, title, 2, "Show - 01.mkv", 1)
    _assign(first, tv)
    _assign(second, bd)

    distinct = build_catalog_title_video_presentation([first, second], title)
    assert len(distinct.logical_episodes) == 1
    assert [lane.label for lane in distinct.logical_episodes[0].lanes] == [
        "TV · Censored", "BD · Uncensored",
    ]
    assert distinct.logical_episodes[0].unresolved_variant_ambiguity is False

    second.video_variant_group = None
    second.video_variant_group_id = None
    known_null = build_catalog_title_video_presentation([first, second], title)
    assert [lane.label for lane in known_null.logical_episodes[0].lanes] == [
        "TV · Censored", "Varianta neurčena",
    ]
    assert known_null.logical_episodes[0].unresolved_variant_ambiguity is True

    _assign(second, tv)
    same = build_catalog_title_video_presentation([first, second], title)
    assert len(same.logical_episodes[0].lanes) == 1
    assert same.logical_episodes[0].lanes[0].label == "TV · Censored"
    assert len(same.logical_episodes[0].lanes[0].physical_rows) == 2
    assert same.logical_episodes[0].lanes[0].unresolved_duplicate_candidate is True


def test_confirmed_duplicates_are_physical_children_not_lanes():
    collection = _collection()
    title = _title(collection)
    tv = _group(title, 1, "TV", "tv", "censored")
    bd = _group(title, 2, "BD", "bd", "uncensored")
    primary = _video(collection, title, 1, "Show - 01 TV.mkv", 1)
    copy = _video(collection, title, 2, "Show - 01 TV copy.mkv", 1)
    bd_video = _video(collection, title, 3, "Show - 01 BD.mkv", 1)
    _assign(primary, tv)
    _assign(copy, tv)
    _assign(bd_video, bd)
    copy.duplicate_of_video_id = primary.id

    presentation = build_catalog_title_video_presentation(
        [primary, copy, bd_video], title
    )
    episode = presentation.logical_episodes[0]

    assert len(episode.lanes) == 2
    tv_lane = next(lane for lane in episode.lanes if lane.group_id == tv.id)
    assert len(tv_lane.physical_rows) == 1
    assert tv_lane.physical_rows[0].video is primary
    assert tv_lane.physical_rows[0].duplicate_copies == (copy,)
    assert sum(len(lane.physical_rows) for lane in episode.lanes) == 2


def test_nande_counts_and_detail_shape_are_logical_and_variant_aware():
    collection = _collection(name="Nande")
    title = _title(collection, name="Nande Season")
    tv = _group(title, 1, "TV", "tv", "censored")
    bd = _group(title, 2, "BD", "bd", "uncensored")
    videos = []
    identifier = 1
    for episode in range(1, 14):
        bd_video = _video(
            collection, title, identifier, f"Nande - {episode:02d}.mp4", episode
        )
        identifier += 1
        _assign(bd_video, bd)
        videos.append(bd_video)
        if episode <= 12:
            tv_video = _video(
                collection, title, identifier,
                f"Nande - {episode:02d} Ver.TV.mp4", episode,
            )
            identifier += 1
            _assign(tv_video, tv)
            videos.append(tv_video)
    for number in range(1, 14):
        videos.append(_video(
            collection, title, identifier, f"Nande NCOP {number:02d}.mkv", None,
            file_type="ncop",
        ))
        identifier += 1

    results = build_catalog_results(videos, "all")
    group = results.groups[0]
    presentation = build_catalog_title_video_presentation(videos, title)

    assert (group.total, group.episodes, group.variants, group.bonus) == (38, 13, 25, 13)
    assert len(presentation.logical_episodes) == 13
    assert [len(episode.lanes) for episode in presentation.logical_episodes[:12]] == [2] * 12
    assert len(presentation.logical_episodes[12].lanes) == 1
    assert len(presentation.other_physical_rows) == 13


def test_collection_aggregation_keeps_same_episode_number_scoped_per_title():
    collection = _collection()
    first_title = _title(collection, 1, "Season 1")
    second_title = _title(collection, 2, "Season 2", "Anime/Show/Season 2")
    second_title.season_number = 2
    second_title.season_label = "S2"
    first = _video(collection, first_title, 1, "S1E01.mkv", 1)
    second = _video(collection, second_title, 2, "S2E01.mkv", 1)

    group = build_catalog_results([first, second], "all").groups[0]

    assert group.total == 2
    assert group.episodes == 2
    assert group.variants == 0


def test_normal_title_keeps_physical_and_logical_counts_without_fake_variants():
    collection = _collection()
    title = _title(collection)
    videos = [
        _video(collection, title, episode, f"Show - {episode:02d}.mkv", episode)
        for episode in range(1, 13)
    ]

    group = build_catalog_results(videos, "all").groups[0]

    assert (group.total, group.episodes, group.variants, group.bonus) == (12, 12, 0, 0)


def test_catalog_translation_counts_use_positive_variant_compatibility():
    collection = _collection()
    title = _title(collection)
    tv = _group(title, 1, "TV", "tv", "censored")
    bd = _group(title, 2, "BD", "bd", "uncensored")
    tv_video = _video(collection, title, 1, "Show - 01 TV.mkv", 1)
    bd_video = _video(collection, title, 2, "Show - 01 BD.mkv", 1)
    _assign(tv_video, tv)
    _assign(bd_video, bd)
    subtitle = ExternalSubtitle(
        relative_path="Anime/Show/Season 1/Show - 01.cs.ass",
        codec="ass",
        language="cs",
        normalized_language="cs",
    )
    _add_automatic_external(bd_video, subtitle)

    group = build_catalog_results([tv_video, bd_video], "all").groups[0]

    assert (group.cs, group.sk, group.missing) == (1, 0, 1)

    tv_video.external_subtitle_compatibilities.append(ExternalSubtitleCompatibility(
        external_subtitle=subtitle,
        status="confirmed_compatible",
        match_method="manual",
        verified_at=datetime.now(timezone.utc),
    ))
    shared = build_catalog_results([tv_video, bd_video], "all").groups[0]
    assert (shared.total, shared.cs, shared.sk, shared.missing) == (2, 2, 0, 0)


def test_search_matches_filename_canonical_episode_and_variant_label():
    collection = _collection()
    title = _title(collection)
    tv = _group(title, 1, "TV", "tv", "censored")
    video = _video(collection, title, 1, "Show - 01 Ver.TV.mkv", 1)
    _assign(video, tv)

    assert video_matches_search(video, "ver.tv")
    assert video_matches_search(video, "e01")
    assert video_matches_search(video, "tv")
    assert video_matches_search(video, "censored")


def test_supplementary_video_stays_ungrouped_but_shows_variant_label():
    collection = _collection()
    title = _title(collection, part_type="bonus")
    group = _group(title, 1, "TV", "tv", "censored")
    video = _video(
        collection, title, 1, "NCOP.mkv", None, file_type="ncop"
    )
    _assign(video, group)

    presentation = build_catalog_title_video_presentation([video], title)

    assert presentation.logical_episodes == ()
    assert len(presentation.other_physical_rows) == 1
    assert presentation.display_rows[0].compact_variant_label == "TV · Censored"


def test_homepage_and_title_detail_render_counts_lanes_forms_and_search(tmp_path):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'catalog-variant.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        collection = CatalogCollection(
            local_title="Nande", normalized_local_title="nande",
            relative_root_path="Anime/Nande",
        )
        title = CatalogTitle(
            collection=collection, local_title="Season 1",
            normalized_local_title="season 1",
            relative_root_path="Anime/Nande/Season 1", part_type="season",
            season_number=1, season_label="S1",
        )
        session.add(collection)
        session.flush()
        tv = VideoVariantGroup(
            catalog_title=title, manual_label="TV", release_source="tv",
            content_variant="censored",
        )
        bd = VideoVariantGroup(
            catalog_title=title, manual_label="BD", release_source="bd",
            content_variant="uncensored",
        )
        session.add_all([tv, bd])
        session.flush()
        identifier = 1
        for episode in range(1, 14):
            bd_video = _video(
                collection, title, identifier, f"Nande - {episode:02d}.mp4", episode
            )
            identifier += 1
            bd_video.id = None
            bd_video.video_variant_group = bd
            if episode == 1:
                bd_video.audio_tracks.append(AudioTrack(
                    stream_index=0, codec="aac", language="ja"
                ))
                _add_automatic_external(bd_video, ExternalSubtitle(
                    relative_path="Anime/Nande/Season 1/Nande - 01.cs.ass",
                    codec="ass", language="cs", normalized_language="cs",
                ))
            session.add(bd_video)
            if episode <= 12:
                tv_video = _video(
                    collection, title, identifier,
                    f"Nande - {episode:02d} Ver.TV.mp4", episode,
                )
                identifier += 1
                tv_video.id = None
                tv_video.video_variant_group = tv
                session.add(tv_video)
        for number in range(1, 14):
            bonus = _video(
                collection, title, identifier, f"NCOP {number:02d}.mkv", None,
                file_type="ncop",
            )
            identifier += 1
            bonus.id = None
            session.add(bonus)
        normal_collection = CatalogCollection(
            local_title="Normal", normalized_local_title="normal",
            relative_root_path="Anime/Normal",
        )
        normal_title = CatalogTitle(
            collection=normal_collection, local_title="Season 1",
            normalized_local_title="season 1",
            relative_root_path="Anime/Normal/Season 1", part_type="season",
            season_number=1, season_label="S1",
        )
        session.add(Video(
            relative_path="Anime/Normal/Season 1/01.mkv", root_folder="Anime",
            filename="01.mkv", size=1, mtime_ns=1, file_type="episode",
            local_episode_number=1, season_episode_number=1,
            absolute_episode_number=1, episode_number_source="filename",
            catalog_collection=normal_collection, catalog_title=normal_title,
        ))
        session.commit()
        title_id = title.id

    endpoints = {
        route.path: route.endpoint for route in web_app.routes
        if hasattr(route, "endpoint")
    }
    homepage = endpoints["/"](_request(web_app, "/")).body.decode()
    logical = homepage.split('class="panel logical-catalog"', 1)[1].split(
        'class="panel physical-folders"', 1
    )[0]
    assert '<td data-label="Videa">38</td>' in logical
    assert '<td data-label="Epizody">13</td>' in logical
    assert '<td data-label="Varianty">25</td>' in logical
    assert '<td data-label="Varianty">—</td>' in logical
    assert '<td data-label="Bonusy">13</td>' in logical

    detail = endpoints["/titles/{catalog_title_id}"](
        _request(web_app, f"/titles/{title_id}"), title_id,
    ).body.decode()
    assert detail.count('class="logical-episode-heading"') == 13
    assert detail.count('class="variant-lane-heading"') == 25
    assert "TV · Censored" in detail and "BD · Uncensored" in detail
    assert "Zdroj TV" in detail and "Zdroj BD" in detail
    assert 'action="/videos/' in detail
    assert '/hardsub"' in detail
    assert '/episode-number"' in detail
    assert '/media-part"' in detail
    assert '/audio-tracks/' in detail
    assert '/external-subtitles/' in detail
    assert "CZ (ASS)" in detail
    assert "JA · aac" in detail

    for query, expected in (
        ("BD", "Nande - 01.mp4"),
        ("E01", "Nande - 01 Ver.TV.mp4"),
        ("Ver.TV.mp4", "Nande - 01 Ver.TV.mp4"),
    ):
        encoded = urlencode({"q": query})
        searched = endpoints["/titles/{catalog_title_id}"](
            _request(web_app, f"/titles/{title_id}", encoded), title_id, q=query,
        ).body.decode()
        assert expected in searched


def test_title_detail_eager_loading_has_fixed_query_budget(tmp_path):
    web_app = create_app(Settings(
        anime_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'catalog-query-count.db'}",
        metadata_download_artwork=False,
        metadata_artwork_directory=tmp_path / "artwork",
    ))
    title_ids = []
    with web_app.state.sessions() as session:
        Base.metadata.create_all(session.get_bind())
        for collection_index, episode_count in ((1, 1), (2, 12)):
            collection = CatalogCollection(
                local_title=f"Show {collection_index}",
                normalized_local_title=f"show {collection_index}",
                relative_root_path=f"Anime/Show {collection_index}",
            )
            title = CatalogTitle(
                collection=collection, local_title="Season 1",
                normalized_local_title="season 1",
                relative_root_path=f"Anime/Show {collection_index}/Season 1",
                part_type="season", season_number=1, season_label="S1",
            )
            session.add(collection)
            session.flush()
            group = VideoVariantGroup(
                catalog_title=title, manual_label="TV", release_source="tv",
            )
            session.add(group)
            session.flush()
            for episode in range(1, episode_count + 1):
                video = Video(
                    relative_path=f"{title.relative_root_path}/{episode:02d}.mkv",
                    root_folder="Anime", filename=f"{episode:02d}.mkv",
                    size=1, mtime_ns=episode, file_type="episode",
                    local_episode_number=episode, season_episode_number=episode,
                    absolute_episode_number=episode, episode_number_source="filename",
                    catalog_collection=collection, catalog_title=title,
                    video_variant_group=group,
                )
                session.add(video)
            session.flush()
            title_ids.append(title.id)
        session.commit()

    endpoint = next(
        route.endpoint for route in web_app.routes
        if getattr(route, "path", None) == "/titles/{catalog_title_id}"
    )
    engine = web_app.state.sessions.kw["bind"]

    def query_count(title_id):
        statements = 0

        def count_statement(*_args):
            nonlocal statements
            statements += 1

        event.listen(engine, "before_cursor_execute", count_statement)
        try:
            endpoint(_request(web_app, f"/titles/{title_id}"), title_id)
        finally:
            event.remove(engine, "before_cursor_execute", count_statement)
        return statements

    assert query_count(title_ids[0]) == query_count(title_ids[1])
