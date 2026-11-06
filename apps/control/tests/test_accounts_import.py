"""账号页「导入账号」端点：校验、脱敏、归属、探测与删除回收。

HTTP 走 conftest 的真实控制面（不 mock 路由）；「响应不出现明文凭证」是硬断言。
权限口径跟既有 accounts 写端点一致（全部 admin_user），故非管理员导入一律 403。

隔离：控制面与 SQLite 是会话级共享的，本文件的导入用例会往 opencode 下写行
（连带镜像凭据）；不清理会把 test_credentials 的 credential_count 断言污染成 3
（单跑绿、全量红）。所以用 _purge_own_imports 让用例自造自收。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import httpx
import pytest

SECRET = "sk-live-IMPORT-99"
JSON_BAD = '{"token": 1,}'  # 尾逗号：解析失败且能给出行列


@pytest.fixture(scope="module", autouse=True)
def _enable_opencode(admin: httpx.Client) -> Iterator[None]:
    """导入用例需要一个已启用的 provider：种子只启用 codebuddy。

    收尾还原，避免污染依赖 opencode「停用」态的矩阵用例（test_model_resolution_matrix）。
    """
    was = next(p for p in admin.get("/api/providers").json() if p["id"] == "opencode").get("enabled")
    if not was:
        patched = admin.patch("/api/providers/opencode", json={"enabled": True})
        assert patched.status_code == 200, patched.text
    yield
    if not was:
        admin.patch("/api/providers/opencode", json={"enabled": False})


@pytest.fixture(autouse=True)
def _purge_own_imports(admin: httpx.Client) -> Iterator[None]:
    """用例后回收本用例新建的 import-* 行（删除会连带回收镜像凭据）。

    前/后快照差集，只删本次造的行，前缀撞车也伤不到别人的数据。
    """
    before = {a["file"] for a in admin.get("/api/accounts").json()["accounts"]}
    yield
    for row in admin.get("/api/accounts").json()["accounts"]:
        if row["file"].startswith("import-") and row["file"] not in before:
            admin.delete(f"/api/accounts/{row['file']}")


def _login_viewer(admin: httpx.Client) -> httpx.Client:
    name = f"imp-viewer-{uuid.uuid4().hex[:8]}"
    created = admin.post("/api/users", json={"username": name, "password": "viewer-pass", "role": "viewer"})
    assert created.status_code == 200, created.text
    client = httpx.Client(base_url=str(admin.base_url), trust_env=False, timeout=10.0)
    login = client.post("/api/login", json={"username": name, "password": "viewer-pass"})
    assert login.status_code == 200, login.text
    return client


def _import(admin: httpx.Client, **overrides) -> httpx.Response:
    payload = {
        "provider": "opencode",
        "label": f"导入号-{uuid.uuid4().hex[:6]}",
        "credential_type": "api_key",
        "credential": SECRET,
        "note": "smoke",
    }
    payload.update(overrides)
    return admin.post("/api/accounts/import", json=payload)


def test_import_creates_row_and_masks_credential(admin: httpx.Client) -> None:
    resp = _import(admin)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provider"] == "opencode"
    assert body["local_only"] is True
    assert body["nickname"].startswith("导入号-")
    assert SECRET not in resp.text  # 导入响应回明文=硬失败

    # GET 单条与导入响应同构（同一批键），且列表同样不回显明文
    listing = admin.get("/api/accounts", params={"provider": "opencode"})
    assert listing.status_code == 200
    assert SECRET not in listing.text
    row = next((a for a in listing.json()["accounts"] if a["file"] == body["file"]), None)
    assert row is not None
    assert row["local_only"] is True
    assert set(row.keys()) == set(body.keys())

    # 凭据同时镜像进统一凭据库（key 链的事实来源），那边也只回掩码
    creds = admin.get("/api/provider-credentials", params={"provider": "opencode"})
    assert creds.status_code == 200
    assert SECRET not in creds.text
    mirror = [c for c in creds.json() if c.get("label") == body["nickname"]]
    assert mirror, "导入的凭据没有进统一凭据库"
    assert mirror[0]["has_secret"] is True


def test_import_json_accepts_object_and_masks(admin: httpx.Client) -> None:
    resp = _import(admin, credential_type="json", credential={"token": SECRET})
    assert resp.status_code == 200, resp.text
    assert SECRET not in resp.text


def test_import_unknown_provider_is_400(admin: httpx.Client) -> None:
    resp = _import(admin, provider="no-such-provider")
    assert resp.status_code == 400
    assert "未知的服务商" in resp.json()["detail"]


def test_import_empty_credential_is_400(admin: httpx.Client) -> None:
    resp = _import(admin, credential="   ")
    assert resp.status_code == 400
    assert "凭证不能为空" in resp.json()["detail"]


def test_import_bad_json_points_at_position(admin: httpx.Client) -> None:
    resp = _import(admin, credential_type="json", credential=JSON_BAD)
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "JSON 解析失败" in detail
    assert "行" in detail and "列" in detail


def test_import_bad_credential_type_is_400(admin: httpx.Client) -> None:
    resp = _import(admin, credential_type="magic")
    assert resp.status_code == 400
    assert "credential_type" in resp.json()["detail"]


def test_import_sidecar_provider_rejected(admin: httpx.Client) -> None:
    """sidecar 家的账号不归本仓管：导入行永远不会被运行时消费，直接 400。"""
    resp = _import(admin, provider="trae_sidecar")
    assert resp.status_code == 400
    assert "sidecar" in resp.json()["detail"]


def test_import_disabled_provider_is_400(admin: httpx.Client) -> None:
    """未启用的服务商即便类型可导入也拒绝：写进库也不会被运行时选中（僵尸账号）。"""
    label = f"导入号-{uuid.uuid4().hex[:6]}"
    resp = _import(admin, provider="muse", label=label)
    assert resp.status_code == 400
    assert "未启用，请先在设置中启用" in resp.json()["detail"]
    # 拒绝不能留下半条数据：没有账号行，也没有镜像凭据
    rows = admin.get("/api/accounts", params={"provider": "muse"}).json()["accounts"]
    assert all(row["nickname"] != label for row in rows)
    creds = admin.get("/api/provider-credentials", params={"provider": "muse"}).json()
    assert all(c.get("label") != label for c in creds)


def test_probe_disabled_provider_is_400(admin: httpx.Client) -> None:
    """探测与保存同口径：未启用的服务商在探测阶段就 400，不做无意义的连通测试。"""
    resp = admin.post(
        "/api/accounts/import/probe",
        json={"provider": "muse", "credential_type": "api_key", "credential": SECRET},
    )
    assert resp.status_code == 400
    assert "未启用，请先在设置中启用" in resp.json()["detail"]


def test_non_admin_import_rejected(admin: httpx.Client) -> None:
    """写权限与既有 accounts 写端点同口径（admin_user）：非管理员一律 403。

    入参没有定向他人字段，owner_user_id 恒为当前用户，所以「给自己导」也无从越权。
    """
    viewer = _login_viewer(admin)
    resp = viewer.post(
        "/api/accounts/import",
        json={"provider": "opencode", "credential_type": "api_key", "credential": SECRET},
    )
    assert resp.status_code == 403


def test_probe_failure_is_reported_not_leaked(admin: httpx.Client) -> None:
    """死端口探测：连不上也要 200 + 结构化失败，且不回显凭证明文。"""
    resp = admin.post(
        "/api/accounts/import/probe",
        json={
            "provider": "codebuddy",
            "credential_type": "api_key",
            "credential": f"wbt_{SECRET}",
            "base_url": "http://127.0.0.1:9",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] in {"ok", "fail"}
    assert body["message"]
    assert SECRET not in resp.text


def test_probe_reuses_import_validation(admin: httpx.Client) -> None:
    """探测与保存同一套校验：坏 JSON 在探测阶段就 400。"""
    resp = admin.post(
        "/api/accounts/import/probe",
        json={"provider": "opencode", "credential_type": "json", "credential": JSON_BAD},
    )
    assert resp.status_code == 400
    assert "JSON 解析失败" in resp.json()["detail"]


def test_delete_imported_account_recycles_credential(admin: httpx.Client) -> None:
    resp = _import(admin)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    creds = admin.get("/api/provider-credentials", params={"provider": "opencode"}).json()
    mirror = next(c for c in creds if c.get("label") == body["nickname"])
    assert mirror["has_secret"] is True

    # 本端登记行上游没有这份账号：删除必须跳过上游、连带回收镜像凭据
    dele = admin.delete(f"/api/accounts/{body['file']}")
    assert dele.status_code == 200, dele.text
    assert dele.json()["success"] is True

    listing = admin.get("/api/accounts", params={"provider": "opencode"})
    assert not any(a["file"] == body["file"] for a in listing.json()["accounts"])
    after = admin.get("/api/provider-credentials", params={"provider": "opencode"}).json()
    assert all(c["id"] != mirror["id"] for c in after), "镜像凭据没有被回收"


def test_sync_returns_per_provider_shape(admin: httpx.Client) -> None:
    """手动同步响应按 provider 分组，每家都是 {synced, pool_available, note}。

    前端「同步账号」按钮的成功/失败提示全靠这个形状分流，键变了提示就静默失效。
    """
    resp = admin.post("/api/accounts/sync")
    assert resp.status_code == 200, resp.text
    results = resp.json()
    assert results, "同步未返回任何 provider"
    for item in results.values():
        assert set(item) == {"synced", "pool_available", "note"}
        assert isinstance(item["synced"], int)
        assert isinstance(item["note"], str)


def test_sync_noop_for_provider_without_list_capability(admin: httpx.Client) -> None:
    """没有 list 能力的服务商是全零 no-op：前端据此隐藏按钮，不该打上游。"""
    resp = admin.post("/api/accounts/sync", params={"provider": "opencode"})
    assert resp.status_code == 200, resp.text
    item = resp.json()["opencode"]
    assert item["synced"] == 0
    assert item["note"] == ""


def test_non_admin_sync_rejected(admin: httpx.Client) -> None:
    """同步端点 admin_user：与前端 canSync 的 isAdmin gating 同口径。"""
    viewer = _login_viewer(admin)
    assert viewer.post("/api/accounts/sync").status_code == 403
