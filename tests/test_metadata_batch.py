from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.metadata.candidates import batch_search_candidates
from app.metadata.providers.base import ProviderTitleMetadata
from app.models import CatalogCollection, CatalogTitle, ExternalTitleLink, MetadataCandidate


class Provider:
    def __init__(self):
        self.queries = []

    def search_titles(self, query):
        self.queries.append(query)
        return [ProviderTitleMetadata(provider="anilist", external_id=str(len(self.queries)), title_romaji=query)]


def setup(tmp_path, specifications):
    engine = create_engine(f"sqlite:///{tmp_path / 'batch.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions() as session:
        for index, (locked, hierarchy) in enumerate(specifications, 1):
            collection = CatalogCollection(local_title=f"Collection {index}", normalized_local_title=f"collection {index}",
                                           relative_root_path=f"Anime/Collection {index}", hierarchy_status=hierarchy)
            session.add(collection); session.flush()
            session.add(CatalogTitle(local_title=f"Show {index}", normalized_local_title=f"show {index}",
                                     relative_root_path=f"Anime/Collection {index}/Show", collection=collection,
                                     metadata_locked=locked, metadata_status="unlinked"))
        session.commit()
    return engine, sessions


def test_batch_search_respects_limit_and_never_confirms(tmp_path):
    engine, sessions = setup(tmp_path, [(False, "automatic")] * 3)
    provider = Provider()
    result = batch_search_candidates(sessions, provider, limit=2)
    assert result.processed == 2
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(MetadataCandidate)) == 2
        assert session.scalar(select(func.count()).select_from(ExternalTitleLink)) == 0
        assert session.scalar(select(func.count()).select_from(CatalogTitle).where(CatalogTitle.metadata_status == "linked_manual")) == 0
    engine.dispose()


def test_batch_skips_locked_and_conflicts_but_warns_for_review(tmp_path):
    engine, sessions = setup(tmp_path, [(True, "automatic"), (False, "conflict"), (False, "review_required")])
    provider = Provider()
    result = batch_search_candidates(sessions, provider, limit=10)
    assert result.skipped == 2
    assert result.processed == 1
    assert result.warnings == 1
    assert len(provider.queries) == 1
    engine.dispose()
