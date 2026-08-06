import httpx
import pytest

from app.metadata.providers.anilist import AniListProvider, SEARCH_QUERY
from app.metadata.providers.base import MetadataProviderError


def _response(payload, status=200):
    return httpx.Response(status, json=payload, request=httpx.Request("POST", "https://graphql.anilist.co"))


class StubClient:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


def test_search_is_static_parameterized_and_normalizes_response():
    client = StubClient(_response({"data": {"Page": {"media": [{
        "id": 21, "title": {"romaji": "One Piece", "english": None, "native": "ワンピース"},
        "synonyms": ["OP"], "seasonYear": 1999, "season": "FALL", "format": "TV",
        "status": "RELEASING", "episodes": None, "description": "Pirates",
        "coverImage": {"medium": "https://img/21.jpg"}, "siteUrl": "https://anilist.co/anime/21",
    }]}}}))
    result = AniListProvider(client=client).search_titles("One Piece")
    sent = client.calls[0][1]["json"]
    assert sent["query"] == SEARCH_QUERY
    assert "One Piece" not in sent["query"]
    assert sent["variables"] == {"search": "One Piece", "perPage": 10}
    assert result[0].external_id == "21"
    assert result[0].title_native == "ワンピース"
    assert result[0].cover_image_url == "https://img/21.jpg"


@pytest.mark.parametrize("query", ["", "   ", "x" * 201])
def test_search_rejects_invalid_query(query):
    with pytest.raises(ValueError):
        AniListProvider(client=StubClient()).search_titles(query)


@pytest.mark.parametrize("response", [
    _response({"message": "bad"}, 500),
    _response({"errors": [{"message": "bad"}]}),
])
def test_provider_catches_http_and_graphql_errors(response):
    with pytest.raises(MetadataProviderError):
        AniListProvider(client=StubClient(response)).search_titles("Show")


@pytest.mark.parametrize("error_type", [httpx.ConnectTimeout, httpx.ReadTimeout])
def test_provider_catches_timeout(error_type):
    request = httpx.Request("POST", "https://graphql.anilist.co")
    with pytest.raises(MetadataProviderError):
        AniListProvider(client=StubClient(error=error_type("timeout", request=request))).search_titles("Show")


def test_anilist_uses_explicit_split_timeout():
    client = StubClient(_response({"data": {"Page": {"media": []}}}))
    AniListProvider(timeout_seconds=15, client=client).search_titles("Show")
    timeout = client.calls[0][1]["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == 5
    assert timeout.read == 15
    assert timeout.write == 10
    assert timeout.pool == 5


@pytest.mark.parametrize("external_id", ["", "abc", "0", "-1", "1.5"])
def test_fetch_rejects_invalid_external_id_without_request(external_id):
    client = StubClient()
    with pytest.raises(ValueError):
        AniListProvider(client=client).fetch_title(external_id)
    assert client.calls == []
