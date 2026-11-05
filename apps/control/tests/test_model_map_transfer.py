"""模型映射批量导入/导出：结构校验、逐条结果、合并/覆盖差异、权限与回滚。"""

from __future__ import annotations

import uuid

import httpx
import pytest

pytestmark = pytest.mark.security

EXPORT_KIND = "unified-2api-model-map"
BASELINE = {"keep-a": "gptgrok/gpt-5", "old-b": "gptgrok/gpt-5-mini"}


@pytest.fixture()
def restore_map(admin: httpx.Client):
    original = admin.get("/api/settings/model-map").json()
    yield original
    assert admin.post("/api/settings/model-map", json=original).status_code == 200


def _seed(admin: httpx.Client, mapping: dict[str, str]) -> None:
    saved = admin.post("/api/settings/model-map", json=mapping)
    assert saved.status_code == 200, saved.text


def _viewer(admin: httpx.Client) -> httpx.Client:
    name = f"mm-viewer-{uuid.uuid4().hex[:8]}"
    created = admin.post(
        "/api/users", json={"username": name, "password": "viewer-pass", "role": "viewer"}
    )
    assert created.status_code == 200, created.text
    client = httpx.Client(base_url=str(admin.base_url), trust_env=False, timeout=10.0)
    login = client.post("/api/login", json={"username": name, "password": "viewer-pass"})
    assert login.status_code == 200, login.text
    return client


def test_export_downloads_current_mapping(admin: httpx.Client, restore_map) -> None:
    _seed(admin, {"alias-one": "gptgrok/gpt-5"})
    resp = admin.get("/api/settings/model-map/export")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == EXPORT_KIND
    assert body["version"] == 1
    assert body["mappings"] == {"alias-one": "gptgrok/gpt-5"}
    assert "attachment" in resp.headers.get("content-disposition", "")


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"mode": "merge", "mappings": []},
        {"mode": "replace", "mappings": {"a": "gptgrok/gpt-5"}},
    ],
)
def test_invalid_structure_is_400(admin: httpx.Client, restore_map, payload) -> None:
    resp = admin.post("/api/settings/model-map/import", json=payload)
    assert resp.status_code == 400, resp.text
    error = resp.json()["error"]
    assert error["type"] == "invalid_model_map"
    assert error["errors"]


def test_unknown_provider_is_400_and_writes_nothing(admin: httpx.Client, restore_map) -> None:
    _seed(admin, BASELINE)
    payload = {"mode": "merge", "mappings": {"bad-alias": "nope-provider/gpt-5"}}
    resp = admin.post("/api/settings/model-map/import", json=payload)
    assert resp.status_code == 400, resp.text
    error = resp.json()["error"]
    assert error["errors"][0]["reason"] == "unknown_provider"
    assert admin.get("/api/settings/model-map").json() == BASELINE


def test_unknown_declared_model_is_400(admin: httpx.Client, restore_map) -> None:
    _seed(admin, BASELINE)
    payload = {"mode": "merge", "mappings": {"bad-model": "gptgrok/definitely-not-declared"}}
    resp = admin.post("/api/settings/model-map/import", json=payload)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["errors"][0]["reason"] == "unknown_model"


def test_merge_keeps_existing_and_reports_each_change(admin: httpx.Client, restore_map) -> None:
    _seed(admin, BASELINE)
    payload = {
        "mode": "merge",
        "mappings": {
            "old-b": "gptgrok/gpt-5-mini",  # 未变，应跳过
            "new-c": "gptgrok/gpt-5-1",  # 新增
            "keep-a": "gptgrok/gpt-5-3",  # 改值
        },
    }
    resp = admin.post("/api/settings/model-map/import", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True and body["mode"] == "merge" and body["changed"] is True
    assert body["applied"] == 2 and body["skipped"] == 1 and body["failed"] == 0
    mapping = admin.get("/api/settings/model-map").json()
    assert mapping == {**BASELINE, "new-c": "gptgrok/gpt-5-1", "keep-a": "gptgrok/gpt-5-3"}

    by_alias = {item["alias"]: item for item in body["results"]}
    assert by_alias["old-b"]["status"] == "skipped"
    assert by_alias["old-b"]["reason"] == "unchanged"
    assert by_alias["new-c"]["status"] == "applied" and by_alias["new-c"]["action"] == "added"
    assert by_alias["keep-a"]["status"] == "applied" and by_alias["keep-a"]["action"] == "updated"
    assert by_alias["keep-a"]["previous"] == "gptgrok/gpt-5"


def test_overwrite_drops_absent_aliases_and_reports_removals(admin: httpx.Client, restore_map) -> None:
    _seed(admin, BASELINE)
    payload = {"mode": "overwrite", "mappings": {"only-c": "gptgrok/gpt-5-2"}}
    resp = admin.post("/api/settings/model-map/import", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "overwrite"
    assert body["removed"] == 2
    assert admin.get("/api/settings/model-map").json() == {"only-c": "gptgrok/gpt-5-2"}
    removed = [item for item in body["results"] if item["action"] == "removed"]
    assert {item["alias"] for item in removed} == {"keep-a", "old-b"}


def test_rollback_restores_previous_mapping(admin: httpx.Client, restore_map) -> None:
    _seed(admin, BASELINE)
    imported = admin.post(
        "/api/settings/model-map/import",
        json={"mode": "overwrite", "mappings": {"only-c": "gptgrok/gpt-5-2"}},
    )
    assert imported.status_code == 200, imported.text
    restored = admin.post("/api/settings/model-map/rollback")
    assert restored.status_code == 200, restored.text
    assert admin.get("/api/settings/model-map").json() == BASELINE


def test_transfer_is_admin_only(admin: httpx.Client, restore_map) -> None:
    # conftest 的 anon 与 admin 共用同一个 client（admin fixture 直接在 anon 上登录），
    # 所以匿名断言必须单独开一个不带 cookie 的 client
    anonymous = httpx.Client(base_url=str(admin.base_url), trust_env=False, timeout=10.0)
    try:
        assert anonymous.get("/api/settings/model-map/export").status_code == 401
        assert anonymous.post(
            "/api/settings/model-map/import", json={"mode": "merge", "mappings": {"a": "b"}}
        ).status_code == 401
        assert anonymous.post("/api/settings/model-map/rollback").status_code == 401
    finally:
        anonymous.close()

    viewer = _viewer(admin)
    try:
        assert viewer.get("/api/settings/model-map/export").status_code == 403
        assert viewer.post(
            "/api/settings/model-map/import", json={"mode": "merge", "mappings": {"a": "b"}}
        ).status_code == 403
        assert viewer.post("/api/settings/model-map/rollback").status_code == 403
    finally:
        viewer.close()
