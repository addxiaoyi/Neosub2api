"""提示词库路由回归：鉴权、分页、筛选、空态、越界 id 与删除幂等。

上游 gptGrok2api 只有只读 /api/prompts；本仓补的是管理员维护的本地模板库，
所以测试口径按「登录用户可读、管理员可写」来定。
"""

from __future__ import annotations

import uuid

import httpx


def _viewer(admin: httpx.Client) -> httpx.Client:
    name = f"prompt-viewer-{uuid.uuid4().hex[:8]}"
    created = admin.post(
        "/api/users", json={"username": name, "password": "viewer-pass", "role": "viewer"}
    )
    assert created.status_code == 200, created.text
    client = httpx.Client(base_url=str(admin.base_url), trust_env=False, timeout=10.0)
    login = client.post("/api/login", json={"username": name, "password": "viewer-pass"})
    assert login.status_code == 200, login.text
    return client


def _seed(admin: httpx.Client, count: int = 4) -> tuple[str, list[int]]:
    tag = uuid.uuid4().hex[:8]
    ids = []
    for index in range(count):
        created = admin.post(
            "/api/prompts",
            json={
                "title": f"seed-{tag}-{index}",
                "description": f"desc {index}",
                "prompt": f"full prompt body {index} " * 20,
                "category": "甲组" if index < 2 else "乙组",
                "sub_category": f"子-{index}",
                "mode": "edit" if index % 2 else "generate",
                "image_size": "4:3",
            },
        )
        assert created.status_code == 200, created.text
        ids.append(created.json()["id"])
    return tag, ids


def _cleanup(admin: httpx.Client, ids: list[int]) -> None:
    for prompt_id in ids:
        admin.delete(f"/api/prompts/{prompt_id}")


def test_admin_creates_and_lists_with_meta(admin: httpx.Client) -> None:
    tag, ids = _seed(admin, 2)
    try:
        listed = admin.get("/api/prompts", params={"q": tag})
        assert listed.status_code == 200
        body = listed.json()
        assert body["total"] == 2
        assert body["page"] == 1
        first = body["items"][0]
        assert first["title"].startswith(f"seed-{tag}")
        assert first["excerpt"]
        assert "prompt" not in first

        detail = admin.get(f"/api/prompts/{ids[0]}")
        assert detail.status_code == 200
        assert detail.json()["prompt"].startswith("full prompt body")
    finally:
        _cleanup(admin, ids)


def test_pagination_and_filters(admin: httpx.Client) -> None:
    tag, ids = _seed(admin, 4)
    try:
        page1 = admin.get("/api/prompts", params={"q": tag, "size": 2, "page": 1}).json()
        page2 = admin.get("/api/prompts", params={"q": tag, "size": 2, "page": 2}).json()
        assert page1["total"] == 4 and page2["total"] == 4
        assert len(page1["items"]) == 2 and len(page2["items"]) == 2
        assert {item["id"] for item in page1["items"]}.isdisjoint(
            {item["id"] for item in page2["items"]}
        )

        by_category = admin.get("/api/prompts", params={"q": tag, "category": "甲组"}).json()
        assert by_category["total"] == 2
        assert all(item["category"] == "甲组" for item in by_category["items"])

        categories = admin.get("/api/prompts/categories").json()["items"]
        assert "甲组" in categories and "乙组" in categories

        beyond = admin.get("/api/prompts", params={"q": tag, "page": 9, "size": 20}).json()
        assert beyond["items"] == []
    finally:
        _cleanup(admin, ids)


def test_empty_search_is_not_an_error(admin: httpx.Client) -> None:
    missing = admin.get("/api/prompts", params={"q": f"never-{uuid.uuid4().hex}"})
    assert missing.status_code == 200
    assert missing.json()["items"] == []


def test_viewer_reads_but_cannot_write(admin: httpx.Client) -> None:
    tag, ids = _seed(admin, 1)
    client = _viewer(admin)
    try:
        listed = client.get("/api/prompts", params={"q": tag})
        assert listed.status_code == 200
        assert listed.json()["total"] == 1
        assert client.get(f"/api/prompts/{ids[0]}").status_code == 200

        assert client.post(
            "/api/prompts", json={"title": "blocked", "prompt": "blocked"}
        ).status_code == 403
        assert client.patch(f"/api/prompts/{ids[0]}", json={"title": "blocked"}).status_code == 403
        assert client.delete(f"/api/prompts/{ids[0]}").status_code == 403

        anon = httpx.Client(base_url=str(admin.base_url), trust_env=False, timeout=10.0)
        assert anon.get("/api/prompts").status_code == 401
        anon.close()
    finally:
        client.close()
        _cleanup(admin, ids)


def test_unknown_ids_and_delete_idempotency(admin: httpx.Client) -> None:
    missing_id = 99_999_999
    assert admin.get(f"/api/prompts/{missing_id}").status_code == 404
    assert admin.patch(f"/api/prompts/{missing_id}", json={"title": "x"}).status_code == 404
    assert admin.delete(f"/api/prompts/{missing_id}").status_code == 404

    tag, ids = _seed(admin, 1)
    prompt_id = ids[0]
    first = admin.delete(f"/api/prompts/{prompt_id}")
    assert first.status_code == 200 and first.json() == {"ok": True}
    # 第二次删除结果状态一致（数据已不存在），返回 404 便于调用方区分
    assert admin.delete(f"/api/prompts/{prompt_id}").status_code == 404
    assert admin.get("/api/prompts", params={"q": tag}).json()["total"] == 0


def test_invalid_payloads(admin: httpx.Client) -> None:
    empty = admin.post("/api/prompts", json={"title": "  ", "prompt": "  "})
    assert empty.status_code == 400

    bad_mode = admin.post(
        "/api/prompts", json={"title": "mode", "prompt": "body", "mode": "video"}
    )
    assert bad_mode.status_code == 400

    too_long = admin.post("/api/prompts", json={"title": "x" * 121, "prompt": "body"})
    assert too_long.status_code == 422
