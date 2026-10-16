"""HTTP error boundaries expose validation messages and roll back technical failures."""
import asyncio
from contextlib import contextmanager

import httpx
import pytest
from sqlalchemy import event

from app.database import Base
from test_naming_review_web import post, unit, web


INTERNAL_DETAIL = "/srv/private/anime.db: SELECT secret FROM credentials; token=internal-test"
STALE_MESSAGE = "Kontext pojmenování se změnil. Načti aktuální kandidáty a potvrď volbu znovu."


def _post(app, path, data, accept="text/html"):
    async def request():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(path, data=data, headers={"accept": accept})

    return asyncio.run(request())


def _snapshot(app):
    with app.state.sessions() as session:
        return {
            table.name: tuple(tuple(row) for row in session.execute(
                table.select().order_by(*table.primary_key.columns)
            ))
            for table in Base.metadata.sorted_tables
        }


@contextmanager
def _failing_commit(app, error_type):
    def fail_after_flush(session):
        session.flush()
        raise error_type(INTERNAL_DETAIL)

    event.listen(app.state.sessions, "before_commit", fail_after_flush)
    try:
        yield
    finally:
        event.remove(app.state.sessions, "before_commit", fail_after_flush)


@pytest.mark.parametrize("accept", ["application/json", "text/html"])
@pytest.mark.parametrize("action,key", [("lane-confirm", "hinted"), ("ab-confirm", "a")])
def test_variant_missing_draft_does_not_expose_internal_key(web, accept, action, key, caplog):
    app, _, (collection_id, title_id) = web
    before = _snapshot(app)
    response = _post(app, f"/hierarchy-review/{collection_id}/variants/{action}", {
        "catalog_title_id": str(title_id),
        "confirm_variant_assignment": "true",
        "confirm_ab_variant": "true",
        "video_a_id": "1", "video_b_id": "2",
    }, accept)

    assert response.status_code == 400
    assert f"'{key}'" not in response.text
    assert f"&#39;{key}&#39;" not in response.text
    if accept == "application/json":
        assert response.json()["error"] != f"'{key}'"
    assert _snapshot(app) == before
    assert f"KeyError: '{key}'" in caplog.text


@pytest.mark.parametrize("accept", ["application/json", "text/html"])
@pytest.mark.parametrize("error_type", [ValueError, TypeError])
def test_variant_commit_failure_hides_technical_detail_and_rolls_back(web, accept, error_type, caplog):
    app, _, (collection_id, title_id) = web
    before = _snapshot(app)
    with _failing_commit(app, error_type):
        response = _post(app, f"/hierarchy-review/{collection_id}/variants/groups", {
            "catalog_title_id": str(title_id), "operation": "create", "manual_label": "TV",
        }, accept)

    assert response.status_code == 400
    assert INTERNAL_DETAIL not in response.text
    assert "/srv/private" not in response.text
    if accept == "application/json":
        assert response.json()["error"]
    assert _snapshot(app) == before
    assert INTERNAL_DETAIL in caplog.text


@pytest.mark.parametrize("action", ["save", "reset", "reconfirm"])
def test_naming_commit_failure_hides_technical_detail_and_rolls_back(web, action, caplog):
    app, client, (_, title_id) = web
    assert post(app, client, "title", title_id, "romaji").status_code == 303
    before = _snapshot(app)
    with _failing_commit(app, ValueError):
        response = _post(app, f"/naming-review/title/{title_id}/{action}", {
            "fingerprint": unit(app, "title", title_id).fingerprint,
            "candidate_key": "custom", "custom_text": "Replacement name",
        })

    assert response.status_code == 400
    assert INTERNAL_DETAIL not in response.text
    assert "/srv/private" not in response.text
    assert _snapshot(app) == before
    assert INTERNAL_DETAIL in caplog.text


@pytest.mark.parametrize("workflow", ["naming", "variant"])
def test_uncaught_technical_failure_keeps_generic_500_and_rolls_back(web, workflow):
    app, _, (collection_id, title_id) = web
    before = _snapshot(app)
    if workflow == "naming":
        path = f"/naming-review/title/{title_id}/save"
        data = {"fingerprint": unit(app, "title", title_id).fingerprint,
                "candidate_key": "custom", "custom_text": "Replacement name"}
    else:
        path = f"/hierarchy-review/{collection_id}/variants/groups"
        data = {"catalog_title_id": str(title_id), "operation": "create", "manual_label": "TV"}
    with _failing_commit(app, RuntimeError):
        response = _post(app, path, data)

    assert response.status_code == 500
    assert INTERNAL_DETAIL not in response.text
    assert _snapshot(app) == before


@pytest.mark.parametrize("fingerprint", [None, "old", "あ", INTERNAL_DETAIL])
def test_naming_stale_preview_only_returns_fixed_validation_message(web, fingerprint):
    app, _, (_, title_id) = web
    before = _snapshot(app)
    data = {"custom_text": "Safe preview"}
    if fingerprint is not None:
        data["fingerprint"] = fingerprint
    response = _post(app, f"/naming-review/title/{title_id}/preview", data, "application/json")

    assert response.status_code == 409
    assert response.json() == {"detail": STALE_MESSAGE}
    assert _snapshot(app) == before


@pytest.mark.parametrize("accept", ["application/json", "text/html"])
def test_variant_safe_validation_message_remains_useful(web, accept):
    app, _, (collection_id, title_id) = web
    before = _snapshot(app)
    response = _post(app, f"/hierarchy-review/{collection_id}/variants/groups", {
        "catalog_title_id": str(title_id), "operation": "create", "manual_label": "",
    }, accept)

    assert response.status_code == 400
    assert "Označení video variant group nesmí být prázdné." in response.text
    assert _snapshot(app) == before


def test_naming_safe_validation_message_remains_useful(web):
    app, _, (_, title_id) = web
    before = _snapshot(app)
    response = _post(app, f"/naming-review/title/{title_id}/save", {
        "fingerprint": unit(app, "title", title_id).fingerprint,
        "candidate_key": "custom", "custom_text": "a\tb",
    })

    assert response.status_code == 400
    assert "Název musí obsahovat 1–500 Unicode znaků bez řídicích znaků a zalomení řádků." in response.text
    assert _snapshot(app) == before
