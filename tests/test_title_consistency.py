from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.catalog import catalog_title_display_title, classify_video
from app.database import Base
from app.hierarchy_review import (
    confirm_effective_collection_hierarchy,
    create_title_from_videos,
    move_videos_to_title,
    parse_simple_definitions,
)
from app.migrations import migrate_schema
from app.models import CatalogCollection, CatalogTitle, TitleMetadata, Video, utc_now
from app.title_order import catalog_title_sort_key


def _video(identifier: int, path: str, title: CatalogTitle) -> Video:
    return Video(
        id=identifier,
        relative_path=path,
        root_folder="Anime",
        filename=path.rsplit("/", 1)[-1],
        size=1,
        mtime_ns=identifier,
        file_type=classify_video(path),
        catalog_title=title,
        catalog_collection=title.collection,
    )


def _seed_creation(paths: list[str]):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        collection = CatalogCollection(
            local_title="High School DxD",
            normalized_local_title="high school dxd",
            relative_root_path="High School DxD",
        )
        source = CatalogTitle(
            collection=collection,
            local_title="High School DxD Born",
            normalized_local_title="high school dxd born",
            relative_root_path="High School DxD/Season 3",
            part_type="season",
            season_number=3,
            season_label="S3",
        )
        videos = [
            _video(index, path, source) for index, path in enumerate(paths, 1)
        ]
        session.add(collection)
        session.commit()
        return engine, collection.id, source.id, [video.id for video in videos]


def test_explicit_local_title_is_exact_authority_across_create_move_and_metadata():
    engine, collection_id, source_id, video_ids = _seed_creation([
        "High School DxD/NC/High School DxD Born/ED.mkv",
        "High School DxD/NC/High School DxD Born/OP 01.mkv",
        "High School DxD/NC/High School DxD Born/OP 02.mkv",
        "High School DxD/Season 3/Episode 01.mkv",
    ])
    with Session(engine) as session:
        created = create_title_from_videos(
            session,
            collection_id,
            video_ids[:2],
            local_title="NC – High School DxD Born",
            part_type="bonus",
            season_number=3,
            season_label="S3",
        )
        created_id = created.id
        assert created.local_title == "NC – High School DxD Born"
        assert created.sort_order_manual is None

        move_videos_to_title(session, collection_id, [video_ids[2]], created_id)
        created.metadata_record = TitleMetadata(
            display_title="External canonical title",
            title_romaji="External canonical title",
            metadata_provider="anilist",
            metadata_external_id="42",
        )
        session.commit()

    migrate_schema(engine)
    with Session(engine) as session:
        created = session.get(CatalogTitle, created_id)
        assert created.local_title == "NC – High School DxD Born"
        assert catalog_title_display_title(created) == "External canonical title"
        assert [video.file_type for video in created.videos] == ["ed", "op", "op"]
        assert session.get(CatalogTitle, source_id) is not None


def test_blank_local_title_uses_shared_nc_context_not_first_video():
    engine, collection_id, _, video_ids = _seed_creation([
        "High School DxD/NC/High School DxD Born/OP 01.mkv",
        "High School DxD/NC/High School DxD Born/ED.mkv",
        "High School DxD/NC/High School DxD Born/OP 02.mkv",
    ])
    with Session(engine) as session:
        created = create_title_from_videos(
            session,
            collection_id,
            video_ids,
            local_title="",
            part_type="bonus",
            season_number=3,
            season_label="S3",
        )

        assert created.local_title == "NC – High School DxD Born"
        assert created.local_title != "OP"
        assert created.sort_order_manual is None


def test_blank_local_title_without_safe_context_uses_generic_type_and_season():
    engine, collection_id, _, video_ids = _seed_creation([
        "High School DxD/OP 01.mkv",
        "High School DxD/ED.mkv",
    ])
    with Session(engine) as session:
        for video_id in video_ids:
            session.get(Video, video_id).catalog_title = None
        session.flush()
        created = create_title_from_videos(
            session,
            collection_id,
            video_ids,
            local_title="",
            part_type="ova",
            season_number=3,
        )

        assert created.local_title == "OVA – S3"
        assert created.local_title not in {"OP", "ED"}


def _ordered_labels(titles: list[CatalogTitle]) -> list[str]:
    return [title.local_title for title in sorted(titles, key=catalog_title_sort_key)]


def _structural_titles(id_order: list[int]) -> list[CatalogTitle]:
    values = [
        ("Bonus S1", "bonus", 1, None),
        ("Season 2", "season", 2, None),
        ("Special S1", "special", 1, None),
        ("Season 1", "season", 1, None),
        ("OVA S1", "ova", 1, None),
        ("Anime Bonus", "bonus", None, None),
    ]
    return [
        CatalogTitle(
            id=identifier,
            local_title=name,
            normalized_local_title=name.casefold(),
            relative_root_path=f"Anime/{identifier}",
            part_type=part_type,
            season_number=season,
            part_number=part,
        )
        for identifier, (name, part_type, season, part) in zip(id_order, values)
    ]


def test_structural_display_order_is_operation_independent_and_anime_level_last():
    first = _structural_titles([1, 2, 3, 4, 5, 6])
    second = _structural_titles([60, 50, 40, 30, 20, 10])
    expected = [
        "Season 1", "OVA S1", "Special S1", "Bonus S1",
        "Season 2", "Anime Bonus",
    ]

    assert _ordered_labels(first) == expected
    assert _ordered_labels(second) == expected
    assert _ordered_labels(first) == _ordered_labels(second)


def test_explicit_manual_order_precedes_structural_default_but_confirmation_does_not_create_it():
    titles = _structural_titles([1, 2, 3, 4, 5, 6])
    automatic = titles[3]
    collection = CatalogCollection(
        local_title="Show", normalized_local_title="show", relative_root_path="Show",
    )
    for title in titles:
        title.collection = collection

    confirm_effective_collection_hierarchy(collection)
    assert all(title.sort_order_manual is None for title in titles)

    override = titles[-1]
    override.hierarchy_manual_override = True
    override.part_type_manual = "bonus"
    override.season_number_manual = None
    override.part_number_manual = None
    override.sort_order_manual = 0
    override.hierarchy_verified_at = utc_now()

    assert _ordered_labels(titles)[0] == "Anime Bonus"
    assert automatic.sort_order_manual is None


def test_new_manual_split_row_uses_generic_name_and_keeps_manual_order_null():
    definition = parse_simple_definitions([{
        "title_id": "",
        "local_title": "",
        "part_type_manual": "ova",
        "season_number_manual": "3",
        "season_label_manual": "S3",
        "numbering_mode": "unknown",
        "video_ids": "1, 2",
        "sort_order": "",
    }])[0]

    assert definition.local_title == "OVA – S3"
    assert definition.sort_order is None
