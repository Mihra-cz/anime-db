from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.metadata.candidates import LOW_SCORE_THRESHOLD, score_candidate, set_candidate_rejected, store_candidates
from app.metadata.providers.base import ProviderTitleMetadata
from app.metadata.service import confirm_anilist_candidate
from app.models import CatalogTitle, ExternalTitleLink, MetadataCandidate, Video


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
