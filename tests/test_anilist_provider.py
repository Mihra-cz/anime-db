import httpx
import pytest

from app.metadata.providers.anilist import AniListProvider, SEARCH_QUERY
from app.metadata.providers.base import MetadataProviderError, MetadataRateLimitError


def _response(payload, status=200, headers=None):
    return httpx.Response(
        status,
        json=payload,
        headers=headers,
        request=httpx.Request("POST", "https://graphql.anilist.co"),
    )


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


def test_known_temporary_disabled_403_has_human_outage_message():
    response = _response({
        "errors": [{
            "message": (
                "The AniList API has been temporarily disabled due to severe "
                "stability issues. Please check the official AniList Discord."
            ),
            "status": 403,
        }],
        "data": None,
    }, 403)

    with pytest.raises(MetadataProviderError) as raised:
        AniListProvider(client=StubClient(response)).search_titles("Show")

    assert str(raised.value) == (
        "AniList API je momentálně dočasně nedostupné kvůli problémům na straně "
        "AniListu. Zkuste akci později."
    )


def test_unrelated_403_does_not_claim_global_anilist_outage():
    response = _response({
        "errors": [{"message": "Access from this address is forbidden.", "status": 403}]
    }, 403)

    with pytest.raises(MetadataProviderError) as raised:
        AniListProvider(client=StubClient(response)).search_titles("Show")

    assert str(raised.value) == (
        "Nepodařilo se komunikovat s AniList API. HTTP stav 403."
    )
    assert "dočasně nedostupné" not in str(raised.value)


def test_rate_limit_without_retry_after_has_dedicated_message():
    with pytest.raises(MetadataRateLimitError) as raised:
        AniListProvider(client=StubClient(_response({}, 429))).search_titles("Show")

    assert str(raised.value) == (
        "Byl dosažen limit požadavků AniList API. Zkuste akci znovu později."
    )


def test_rate_limit_with_retry_after_reports_server_wait_seconds():
    response = _response({}, 429, headers={"Retry-After": "30"})

    with pytest.raises(MetadataRateLimitError) as raised:
        AniListProvider(client=StubClient(response)).search_titles("Show")

    assert str(raised.value) == (
        "Byl dosažen limit požadavků AniList API. Zkuste akci znovu přibližně "
        "za 30 sekund."
    )


@pytest.mark.parametrize("status", [500, 502, 503])
def test_server_http_errors_use_generic_communication_message(status):
    with pytest.raises(MetadataProviderError) as raised:
        AniListProvider(client=StubClient(_response({}, status))).search_titles("Show")

    assert str(raised.value) == (
        f"Nepodařilo se komunikovat s AniList API. HTTP stav {status}."
    )


def test_graphql_error_at_http_200_is_not_successful_data():
    response = _response({"data": {"Page": {"media": []}}, "errors": [{"message": "bad"}]})

    with pytest.raises(MetadataProviderError) as raised:
        AniListProvider(client=StubClient(response)).search_titles("Show")

    assert str(raised.value) == (
        "Nepodařilo se komunikovat s AniList API. GraphQL odpověď obsahovala chybu."
    )


@pytest.mark.parametrize(
    "error_type", [httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError]
)
def test_provider_catches_timeout_and_connection_error(error_type):
    request = httpx.Request("POST", "https://graphql.anilist.co")
    with pytest.raises(MetadataProviderError) as raised:
        AniListProvider(client=StubClient(error=error_type("timeout", request=request))).search_titles("Show")

    assert str(raised.value) == "Nepodařilo se komunikovat s AniList API."


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
