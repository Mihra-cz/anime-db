from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.metadata.providers.base import MetadataProviderError, ProviderTitleMetadata
from app.metadata.service import (
    MetadataConflictError, MetadataLockedError, confirm_anilist_candidate,
    refresh_title_metadata, sanitize_description, set_manual_display_title,
    unlink_title_metadata,
)
from app.models import CatalogTitle, ExternalTitleLink, TitleMetadata, Video


class Provider:
    def __init__(self, titles=None, error=None):
        self.titles = titles or {}
        self.error = error

    def fetch_title(self, external_id):
        if self.error:
            raise self.error
        return self.titles[str(external_id)]


def data(external_id="1", english="English", description="Safe", episodes=12):
    return ProviderTitleMetadata(
        provider="anilist", external_id=external_id, title_romaji="Romaji",
        title_english=english, title_native="Native", synonyms=["Alias"],
        release_year=2024, season="SPRING", format="TV", status="FINISHED",
        episode_count=episodes, episode_duration_minutes=24,
        description=description, genres=["Drama"], tags=["School"],
        country_of_origin="JP", is_adult=False,
        cover_image_url=f"https://img/{external_id}.jpg",
        site_url=f"https://anilist.co/anime/{external_id}",
    )


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value
    engine.dispose()


def add_title(session, name="Local", path="Anime/Local", *, hardsub=False):
    title = CatalogTitle(
        local_title=name, normalized_local_title=name.casefold(), relative_root_path=path,
    )
    session.add(title)
    session.flush()
    video = Video(
        relative_path=f"{path}/E01.mkv", root_folder="Anime", filename="E01.mkv",
        size=1, mtime_ns=1, catalog_title_id=title.id,
        manual_hardsub_cs=hardsub,
    )
    session.add(video)
    session.flush()
    return title, video


def test_confirm_creates_manual_primary_link_and_metadata(session):
    title, _ = add_title(session)
    local_before = title.local_title
    link = confirm_anilist_candidate(session, title, "1", Provider({"1": data()}))
    metadata = session.get(TitleMetadata, title.id)
    assert link.is_primary and link.is_manual and link.match_method == "manual_search"
    assert title.metadata_status == "linked_manual"
    assert title.preferred_metadata_provider == "anilist"
    assert metadata.display_title == "English"
    assert metadata.episode_duration_minutes == 24
    assert title.local_title == local_before


def test_reconfirm_is_idempotent_and_change_keeps_old_link(session):
    title, _ = add_title(session)
    provider = Provider({"1": data("1"), "2": data("2", "Second")})
    confirm_anilist_candidate(session, title, "1", provider)
    confirm_anilist_candidate(session, title, "1", provider)
    assert session.scalar(select(func.count()).select_from(ExternalTitleLink)) == 1
    confirm_anilist_candidate(session, title, "2", provider)
    links = session.scalars(select(ExternalTitleLink).order_by(ExternalTitleLink.external_id)).all()
    assert len(links) == 2
    assert links[0].is_primary is False
    assert links[1].is_primary is True
    assert session.get(TitleMetadata, title.id).display_title == "Second"


def test_same_primary_anilist_id_requires_explicit_confirmation(session):
    first, _ = add_title(session, "First", "Anime/First")
    second, _ = add_title(session, "Second", "Anime/Second")
    provider = Provider({"1": data()})
    confirm_anilist_candidate(session, first, "1", provider)
    with pytest.raises(MetadataConflictError):
        confirm_anilist_candidate(session, second, "1", provider)
    assert second.metadata_status == "unlinked"
    confirm_anilist_candidate(session, second, "1", provider, confirm_conflict=True)
    assert second.metadata_status == "linked_manual"


def test_fetch_failure_leaves_catalog_unchanged(session):
    title, _ = add_title(session)
    session.commit()
    title_id = title.id
    title = session.get(CatalogTitle, title_id)
    with pytest.raises(MetadataProviderError):
        confirm_anilist_candidate(
            session, title, "1", Provider(error=MetadataProviderError("offline"))
        )
    session.rollback()
    assert session.scalar(select(func.count()).select_from(ExternalTitleLink)) == 0
    assert session.get(TitleMetadata, title_id) is None
    assert session.get(CatalogTitle, title_id).metadata_status == "unlinked"


def test_manual_display_title_has_priority_and_survives_refresh(session):
    title, _ = add_title(session)
    set_manual_display_title(session, title, "  My title  ")
    provider = Provider({"1": data()})
    confirm_anilist_candidate(session, title, "1", provider)
    assert session.get(TitleMetadata, title.id).display_title == "My title"
    provider.titles["1"] = data(english="Changed")
    refresh_title_metadata(session, title, provider)
    assert title.manual_display_title == "My title"
    assert session.get(TitleMetadata, title.id).display_title == "My title"
    set_manual_display_title(session, title, "   ")
    assert title.manual_display_title is None
    assert session.get(TitleMetadata, title.id).display_title == "Changed"


def test_unlink_preserves_video_and_hardsub_and_link_history(session):
    title, video = add_title(session, hardsub=True)
    confirm_anilist_candidate(session, title, "1", Provider({"1": data()}))
    unlink_title_metadata(session, title)
    session.flush()
    assert session.get(TitleMetadata, title.id) is None
    assert title.metadata_status == "unlinked"
    assert title.preferred_external_id is None
    assert session.get(Video, video.id).manual_hardsub_cs is True
    assert session.scalar(select(ExternalTitleLink)).is_primary is False


def test_locked_metadata_rejects_refresh_and_requires_confirmed_change(session):
    title, _ = add_title(session)
    provider = Provider({"1": data("1"), "2": data("2")})
    confirm_anilist_candidate(session, title, "1", provider)
    title.metadata_locked = True
    with pytest.raises(MetadataLockedError):
        refresh_title_metadata(session, title, provider)
    with pytest.raises(MetadataLockedError):
        confirm_anilist_candidate(session, title, "2", provider)
    confirm_anilist_candidate(session, title, "2", provider, confirm_locked=True)
    title.metadata_locked = False
    refresh_title_metadata(session, title, provider)
    assert session.get(TitleMetadata, title.id).metadata_external_id == "2"


def test_refresh_is_idempotent(session):
    title, _ = add_title(session)
    provider = Provider({"1": data()})
    moment = datetime(2026, 8, 5, tzinfo=timezone.utc)
    confirm_anilist_candidate(session, title, "1", provider, now=moment)
    refresh_title_metadata(session, title, provider, now=moment)
    refresh_title_metadata(session, title, provider, now=moment)
    assert session.scalar(select(func.count()).select_from(TitleMetadata)) == 1
    assert session.scalar(select(func.count()).select_from(ExternalTitleLink)) == 1


def test_description_is_plain_text_and_scripts_are_removed():
    cleaned = sanitize_description("<p>Hello <b>world</b></p><script>alert(1)</script>")
    assert cleaned == "Hello world"
    assert "<" not in cleaned
