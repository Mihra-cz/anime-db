from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
import pytest

from app.database import Base
from app.metadata.candidates import (
    LOW_SCORE_THRESHOLD,
    compare_episode_count,
    local_episode_count_evidence,
    score_candidate,
    score_candidate_breakdown,
    set_candidate_rejected,
    store_candidates,
)
from app.metadata.providers.base import ProviderTitleMetadata
from app.metadata.service import confirm_anilist_candidate
from app.models import (
    CatalogCollection,
    CatalogTitle,
    ExternalTitleLink,
    MetadataCandidate,
    TitleMetadata,
    Video,
    VideoVariantGroup,
)


class Provider:
    def __init__(self, item):
        self.item = item

    def fetch_title(self, external_id):
        return self.item


def item(external_id="1", romaji="Local Show", english=None, aliases=None, episodes=1):
    return ProviderTitleMetadata(
        provider="anilist", external_id=external_id, title_romaji=romaji,
        title_english=english, synonyms=aliases or [], release_year=2024,
        format="TV", episode_count=episodes, cover_image_url=f"https://img/{external_id}.jpg",
        site_url=f"https://anilist.co/anime/{external_id}",
    )


def setup():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    title = CatalogTitle(local_title="Local Show", normalized_local_title="local show", relative_root_path="Anime/Local Show")
    session.add(title)
    session.flush()
    session.add(Video(relative_path="Anime/Local Show/E01.mkv", root_folder="Anime", filename="E01.mkv", size=1, mtime_ns=1, file_type="episode", catalog_title_id=title.id))
    session.flush()
    return engine, session, title


def scoring_title(
    episode_count=1,
    *,
    local_title="Local Show",
    part_type="season",
    season_number=1,
):
    title = CatalogTitle(
        id=1,
        local_title=local_title,
        normalized_local_title="local show",
        relative_root_path=f"Anime/{local_title}",
        part_type=part_type,
        season_number=season_number,
    )
    for number in range(1, episode_count + 1):
        Video(
            id=number,
            relative_path=f"{title.relative_root_path}/E{number:02}.mkv",
            root_folder="Anime",
            filename=f"E{number:02}.mkv",
            size=number,
            mtime_ns=1,
            file_type="episode",
            season_episode_number=number,
            catalog_title=title,
        )
    return title


def scoring_item(
    *,
    title="Local Show",
    english=None,
    release_year=None,
    season=None,
    format=None,
    episodes=None,
    aliases=None,
):
    return ProviderTitleMetadata(
        provider="anilist",
        external_id="1",
        title_romaji=title,
        title_english=english,
        release_year=release_year,
        season=season,
        format=format,
        episode_count=episodes,
        synonyms=aliases or [],
    )


def test_candidate_is_saved_updated_without_duplicates():
    engine, session, title = setup()
    stored = store_candidates(session, title, [item()], query="Local Show")
    assert stored[0].candidate_title == "Local Show"
    assert title.metadata_status == "candidates_available"
    updated = item(romaji="Changed", english="Local Show")
    store_candidates(session, title, [updated], query="Local Show")
    assert session.scalar(select(func.count()).select_from(MetadataCandidate)) == 1
    candidate = session.scalar(select(MetadataCandidate))
    assert candidate.candidate_title == "Changed"
    assert candidate.title_english == "Local Show"
    assert "score_breakdown" in candidate.match_reasons_json
    session.close()
    engine.dispose()


def test_rejection_survives_search_and_can_be_undone():
    engine, session, title = setup()
    candidate = store_candidates(session, title, [item()])[0]
    set_candidate_rejected(session, title.id, candidate.id, True)
    rejected_at = candidate.rejected_at
    store_candidates(session, title, [item(romaji="Updated")])
    assert candidate.rejected_at == rejected_at
    set_candidate_rejected(session, title.id, candidate.id, False)
    assert candidate.rejected_at is None
    session.close()
    engine.dispose()


def test_confirmation_marks_persistent_candidate():
    engine, session, title = setup()
    data = item()
    candidate = store_candidates(session, title, [data])[0]
    confirm_anilist_candidate(session, title, "1", Provider(data), candidate_id=candidate.id)
    assert candidate.confirmed_at is not None
    assert session.scalar(select(ExternalTitleLink)).is_manual is True
    session.close()
    engine.dispose()


def test_exact_title_scores_higher_than_alias_only_and_low_score_is_marked():
    engine, session, title = setup()
    exact, _ = score_candidate(title, item(romaji="Local Show"))
    alias, _ = score_candidate(title, item(romaji="Different", aliases=["Local Show"]))
    low, _ = score_candidate(title, item(romaji="Unrelated"))
    assert exact > alias > low
    assert low < LOW_SCORE_THRESHOLD
    session.close()
    engine.dispose()


def test_exact_title_format_and_logical_episode_evidence_add_declared_points():
    title = scoring_title()

    exact = score_candidate(title, scoring_item())[0]
    with_format = score_candidate(title, scoring_item(format="TV"))[0]
    with_count = score_candidate(
        title, scoring_item(format="TV", episodes=1),
    )[0]

    assert exact == 0.66
    assert with_format == 0.70
    assert with_count == 0.76


@pytest.mark.parametrize(
    ("data", "expected_score"),
    (
        (scoring_item(title="Local Show"), 0.66),
        (scoring_item(title="Different", english="Local Show"), 0.68),
        (
            ProviderTitleMetadata(
                provider="anilist", external_id="1", title_native="Local Show",
            ),
            0.58,
        ),
        (scoring_item(title="Different", aliases=["Local Show"]), 0.38),
    ),
)
def test_all_provider_title_variants_have_the_existing_declared_semantics(
    data, expected_score,
):
    assert score_candidate(scoring_title(), data)[0] == expected_score


def test_exact_candidate_breakdown_explains_fixed_denominator_percentage():
    title = scoring_title()

    breakdown = score_candidate_breakdown(
        title,
        scoring_item(english="Local Show", format="TV", episodes=1),
    )
    points = {component.key: component.points for component in breakdown.components}

    assert points == {
        "title_identity": 0.58,
        "romaji_exact_bonus": 0.08,
        "english_exact_bonus": 0.10,
        "season_name_evidence": 0.0,
        "year_evidence": 0.0,
        "episode_count_evidence": 0.06,
        "format_evidence": 0.04,
    }
    assert breakdown.raw_score == breakdown.normalized_score == 0.86
    assert breakdown.maximum == 1.0
    assert breakdown.reasons["local_episode_count"] == 1
    assert breakdown.reasons["score_breakdown"]["normalized_score"] == 0.86


def test_synthetic_perfect_evidence_candidate_reaches_fixed_maximum():
    title = scoring_title(local_title="Local Show Season 1 2024")
    data = scoring_item(
        title="Local Show Season 1 2024",
        english="Local Show Season 1 2024",
        release_year=2024,
        season="WINTER",
        format="TV",
        episodes=1,
    )

    breakdown = score_candidate_breakdown(title, data)

    assert breakdown.raw_score == 1.0
    assert breakdown.maximum == breakdown.applicable_maximum == 1.0
    assert breakdown.normalized_score == 1.0


@pytest.mark.parametrize(
    ("part_type", "file_type", "provider_format"),
    (
        ("film", "film", "MOVIE"),
        ("ova", "ova", "OVA"),
        ("special", "special", "SPECIAL"),
    ),
)
def test_typical_film_ova_and_special_maximum_without_season_context_is_092(
    part_type, file_type, provider_format,
):
    title = scoring_title(
        0,
        local_title="Local Show 2024",
        part_type=part_type,
        season_number=None,
    )
    Video(
        id=1,
        relative_path=f"{title.relative_root_path}/Item.mkv",
        root_folder="Anime",
        filename="Item.mkv",
        size=1,
        mtime_ns=1,
        file_type=file_type,
        catalog_title=title,
    )
    breakdown = score_candidate_breakdown(
        title,
        scoring_item(
            title="Local Show 2024",
            english="Local Show 2024",
            release_year=2024,
            format=provider_format,
            episodes=1,
        ),
    )

    assert breakdown.raw_score == breakdown.normalized_score == 0.92
    assert breakdown.maximum == 1.0


def test_unavailable_evidence_is_explicit_and_does_not_become_a_match():
    title = scoring_title()

    breakdown = score_candidate_breakdown(title, scoring_item())
    components = {component.key: component for component in breakdown.components}

    assert breakdown.normalized_score == 0.66
    assert components["english_exact_bonus"].status == "unavailable"
    assert components["year_evidence"].status == "unavailable"
    assert components["episode_count_evidence"].status == "unavailable"
    assert components["format_evidence"].status == "unavailable"
    assert breakdown.applicable_maximum == 0.66


def test_confirmed_duplicate_secondary_does_not_create_false_count_mismatch():
    title = scoring_title(12)
    primary = title.videos[0]
    Video(
        id=13,
        relative_path=f"{title.relative_root_path}/E01-copy.mkv",
        root_folder="Anime",
        filename="E01-copy.mkv",
        size=13,
        mtime_ns=1,
        file_type="episode",
        season_episode_number=1,
        duplicate_of=primary,
        catalog_title=title,
    )

    comparison = compare_episode_count(title, 12)
    breakdown = score_candidate_breakdown(
        title, scoring_item(format="TV", episodes=12),
    )

    assert comparison.local.physical_video_count == 13
    assert comparison.local.count == 12
    assert comparison.delta == 0
    assert breakdown.reasons["episode_count_delta"] == 0
    assert breakdown.reasons["local_episode_count"] == 12


def test_video_variants_of_one_logical_episode_do_not_increase_count():
    title = scoring_title()
    first = title.videos[0]
    first.video_variant_group = VideoVariantGroup(
        id=1, catalog_title=title, manual_label="TV",
    )
    Video(
        id=2,
        relative_path=f"{title.relative_root_path}/E01-BD.mkv",
        root_folder="Anime",
        filename="E01-BD.mkv",
        size=2,
        mtime_ns=1,
        file_type="episode",
        season_episode_number=1,
        video_variant_group=VideoVariantGroup(
            id=2, catalog_title=title, manual_label="BD",
        ),
        catalog_title=title,
    )

    evidence = local_episode_count_evidence(title)

    assert evidence.physical_video_count == 2
    assert evidence.count == evidence.standard_logical_episode_count == 1


def test_two_media_parts_are_one_supplementary_logical_item():
    title = scoring_title(0, part_type="ova", season_number=2)
    for identifier, media_part in ((1, 1), (2, 2)):
        Video(
            id=identifier,
            relative_path=f"{title.relative_root_path}/OVA-MP{media_part}.mkv",
            root_folder="Anime",
            filename=f"OVA-MP{media_part}.mkv",
            size=identifier,
            mtime_ns=1,
            file_type="ova",
            season_episode_number=1,
            media_part_number=media_part,
            catalog_title=title,
        )

    evidence = local_episode_count_evidence(title)

    assert evidence.physical_video_count == 2
    assert evidence.count == 1
    assert evidence.semantics == "single_logical_item_from_media_parts"
    assert compare_episode_count(title, 1).matches is True


def test_numbered_supplementary_videos_use_unique_logical_ordinals():
    title = scoring_title(0, part_type="special", season_number=None)
    for identifier in (1, 2):
        Video(
            id=identifier,
            relative_path=f"{title.relative_root_path}/Special-{identifier:02}.mkv",
            root_folder="Anime",
            filename=f"Special-{identifier:02}.mkv",
            size=identifier,
            mtime_ns=1,
            file_type="special",
            season_episode_number=identifier,
            catalog_title=title,
        )

    evidence = local_episode_count_evidence(title)

    assert evidence.count == 2
    assert evidence.semantics == "supplementary_logical_items"


def test_fractional_recap_does_not_increase_standard_count():
    title = scoring_title(12)
    Video(
        id=13,
        relative_path=f"{title.relative_root_path}/Recap-6.5.mkv",
        root_folder="Anime",
        filename="Recap-6.5.mkv",
        size=13,
        mtime_ns=1,
        file_type="other",
        content_type_manual="recap",
        recap_episode_number_manual_tenths=65,
        catalog_title=title,
    )

    comparison = compare_episode_count(title, 12)

    assert comparison.local.physical_video_count == 13
    assert comparison.local.count == 12
    assert comparison.delta == 0


def test_supplementary_item_inside_season_does_not_increase_standard_count():
    title = scoring_title(12)
    Video(
        id=13,
        relative_path=f"{title.relative_root_path}/Special-00.mkv",
        root_folder="Anime",
        filename="Special-00.mkv",
        size=13,
        mtime_ns=1,
        file_type="special",
        content_type_manual="special",
        catalog_title=title,
    )

    comparison = compare_episode_count(title, 12)

    assert comparison.local.count == 12
    assert comparison.delta == 0


def test_candidate_and_confirmed_metadata_use_same_count_comparison():
    title = scoring_title(12)
    title.metadata_record = TitleMetadata(
        display_title="Local Show", episode_count=12,
    )
    candidate = score_candidate_breakdown(
        title, scoring_item(format="TV", episodes=12),
    )
    confirmed = compare_episode_count(title, title.metadata_record.episode_count)

    assert candidate.reasons["local_episode_count"] == confirmed.local.count == 12
    assert candidate.reasons["episode_count_delta"] == confirmed.delta == 0


def test_provider_episode_zero_semantics_stays_advisory_not_false_match():
    title = scoring_title(12)

    comparison = compare_episode_count(title, 13)

    assert comparison.local.count == 12
    assert comparison.delta == 1
    assert comparison.matches is False


def test_legacy_period_hint_and_provider_season_are_not_scoring_authority():
    collection = CatalogCollection(
        id=1,
        local_title="Local Show (J20-L20)",
        normalized_local_title="local show j20 l20",
        relative_root_path="Anime/Local Show (J20-L20)",
        local_period_hint="J20-L20",
    )
    title = scoring_title(local_title="Local Show (J20)")
    title.collection = collection
    spring = scoring_item(title="Local Show", release_year=2020, season="SPRING")
    summer = scoring_item(title="Local Show", release_year=2021, season="SUMMER")

    spring_score, spring_reasons = score_candidate(title, spring)
    summer_score, summer_reasons = score_candidate(title, summer)

    assert spring_score == summer_score
    assert spring_reasons["year_match"] is None
    assert summer_reasons["year_match"] is None
    assert spring_reasons["season_match"] is None
    assert summer_reasons["season_match"] is None


def test_explicit_season_mismatch_is_explained_but_never_a_hard_reject():
    title = scoring_title()

    breakdown = score_candidate_breakdown(
        title,
        scoring_item(
            title="Local Show Season 2",
            aliases=["Local Show"],
            format="TV",
            episodes=1,
        ),
    )
    season = next(
        component for component in breakdown.components
        if component.key == "season_name_evidence"
    )

    assert breakdown.reasons["season_match"] is False
    assert season.status == "conflict"
    assert season.points == 0
    assert breakdown.normalized_score == 0.48


def test_scoring_is_read_only_and_creates_no_candidate_automatically():
    engine, session, title = setup()
    session.commit()
    before = session.scalar(select(func.count()).select_from(MetadataCandidate))

    score_candidate_breakdown(title, item())

    assert session.scalar(select(func.count()).select_from(MetadataCandidate)) == before == 0
    assert not session.new
    assert not session.dirty
    session.close()
    engine.dispose()
