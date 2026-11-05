"""安全复测（2026-11-03）：权限矩阵、密钥泄露面、SSRF 边界。

本文件只做独立复测，不改源码、不改既有测试：
- 权限矩阵：未登录 / viewer / admin 三种身份逐条打真实 HTTP，校验预期与实际；
- 归属：api_keys.owner_user_id + /api/quota 的显式越权 403；
- 审计：actor 自限权、登录失败降级为聚合；
- 密钥泄露面：响应体 / request_logs / /api/logs / 访问日志脱敏 / X-Request-Id；
- SSRF：地址守卫函数级矩阵、真实保存接口、DNS 重绑定窗口。
"""

from __future__ import annotations

import ipaddress
import json
import logging
import sqlite3
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.api_utils import mask_access_args, mask_text
from app.net_guard import UpstreamAddressError, guard_upstream_url, is_loopback_url
from app.observability import JsonFormatter

pytestmark = pytest.mark.security

CONTROL_DIR = Path(__file__).resolve().parents[1]
APP_DIR = CONTROL_DIR / "app"
VIEWER_PASSWORD = "sec-viewer-pass"


def _fresh_client(base_url: str) -> httpx.Client:
    return httpx.Client(base_url=base_url, trust_env=False, timeout=20.0)


def _login(base_url: str, username: str, password: str) -> httpx.Client:
    client = _fresh_client(base_url)
    resp = client.post("/api/login", json={"username": username, "password": password})
    assert resp.status_code == 200, f"登录 {username} 失败: {resp.status_code} {resp.text}"
    return client


def _denied(resp: httpx.Response, detail: str | None = None) -> dict:
    assert resp.status_code == 403, f"{resp.request.method} {resp.request.url}: {resp.status_code} {resp.text}"
    body = resp.json()
    error = body["error"]
    assert error["type"] == "permission_denied"
    assert error["code"] == "insufficient_role"
    assert error["required_role"] == "admin"
    assert error["role"] == "viewer"
    if detail is not None:
        assert body["detail"] == detail
    return body


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row["name"] for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]


def _db_has_text(db_path: str, needle: str) -> tuple[bool, str]:
    """全库扫描：任一表任一文本列出现 needle 即返回 (True, 位置)。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        tables = [
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if not str(row[0]).startswith("sqlite_")
        ]
        for table in tables:
            for col in _table_columns(conn, table):
                try:
                    rows = conn.execute(f'SELECT "{col}" AS v FROM "{table}"').fetchall()
                except sqlite3.Error:
                    continue
                for row in rows:
                    value = row["v"]
                    if isinstance(value, str) and needle in value:
                        return True, f"{table}.{col}"
    finally:
        conn.close()
    return False, ""


def _seed_audit(db_path: str, actor: str, count: int, tag: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        for index in range(count):
            conn.execute(
                "INSERT INTO audit_logs(ts, actor, action, target, detail, ip) VALUES(?,?,?,?,?,?)",
                (1_700_000_000 + index, actor, f"seed.{tag}", f"sec-target-{tag}", "seeded", "203.0.113.9"),
            )
        conn.commit()
    finally:
        conn.close()


def _seed_login_failed(db_path: str, actor: str, count: int, *, ip: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        for index in range(count):
            conn.execute(
                "INSERT INTO audit_logs(ts, actor, action, target, detail, ip) VALUES(?,?,?,?,?,?)",
                (1_700_000_100 + index, actor, "login_failed", "login", "invalid password", ip),
            )
        conn.commit()
    finally:
        conn.close()


def _audit_count(db_path: str, actor: str, action: str | None = None) -> int:
    conn = sqlite3.connect(db_path)
    try:
        if action is None:
            row = conn.execute("SELECT COUNT(*) FROM audit_logs WHERE actor = ?", (actor,)).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE actor = ? AND action = ?", (actor, action)
            ).fetchone()
        return int(row[0])
    finally:
        conn.close()


@pytest.fixture
def stranger(base_url: str):
    client = _fresh_client(base_url)
    yield client
    client.close()


@pytest.fixture(scope="session")
def viewer(admin: httpx.Client, base_url: str):
    tag = uuid.uuid4().hex[:6]
    name = f"secview-{tag}"
    created = admin.post(
        "/api/users",
        json={"username": name, "password": VIEWER_PASSWORD, "role": "viewer"},
    )
    assert created.status_code == 200, created.text
    client = _login(base_url, name, VIEWER_PASSWORD)
    yield SimpleNamespace(client=client, name=name)
    client.close()


@pytest.fixture(scope="session")
def owned_keys(admin: httpx.Client, viewer: SimpleNamespace):
    tag = uuid.uuid4().hex[:6]
    viewer_key = admin.post(
        "/api/keys",
        json={"name": f"sec-viewer-key-{tag}", "owner_user_id": viewer.name},
    )
    assert viewer_key.status_code == 200, viewer_key.text
    admin_key = admin.post("/api/keys", json={"name": f"sec-admin-key-{tag}"})
    assert admin_key.status_code == 200, admin_key.text
    viewer_body = viewer_key.json()
    admin_body = admin_key.json()
    return {
        "viewer_id": viewer_body["id"],
        "viewer_name": viewer_body["name"],
        "viewer_token": viewer_body["key"],
        "admin_id": admin_body["id"],
        "admin_name": admin_body["name"],
        "admin_token": admin_body["key"],
    }


@pytest.fixture(scope="session")
def viewer_policy(admin: httpx.Client, owned_keys: dict):
    viewer_created = admin.post(
        "/api/quota/policies",
        json={
            "key_id": owned_keys["viewer_id"],
            "window": "day",
            "limit_requests": 50,
            "thresholds": [50],
        },
    )
    assert viewer_created.status_code == 200, viewer_created.text
    admin_created = admin.post(
        "/api/quota/policies",
        json={
            "key_id": owned_keys["admin_id"],
            "window": "day",
            "limit_requests": 50,
            "thresholds": [50],
        },
    )
    assert admin_created.status_code == 200, admin_created.text
    return {"viewer": viewer_created.json(), "admin": admin_created.json()}


# --------------------------------------------------------------------------- #
# 1. 权限矩阵
# --------------------------------------------------------------------------- #

PUBLIC_PATHS = ("/healthz", "/api/health")

AUTH_READ_PATHS = (
    "/api/me",
    "/api/keys",
    "/api/tokens",
    "/api/logs",
    "/api/quota/policies",
    "/api/quota/status",
    "/api/quota/alerts",
    "/api/audit-logs",
    "/api/security/audit",
    "/api/security/guard",
    "/api/security/login-failures",
    "/api/accounts",
    "/api/accounts/capabilities",
    "/api/providers",
    "/api/provider-credentials",
    "/api/models",
    "/api/status",
    "/api/stats/summary",
    "/api/stats/daily",
    "/api/system/changelog",
    "/api/system/versions",
)

ADMIN_READ_PATHS = (
    "/api/grok/register/tasks",
    "/api/grok/register/worker",
    "/api/users",
    "/api/metrics",
    "/api/sub2api/groups",
    "/api/sub2api/accounts",
    "/api/sub2api/ledger",
    "/api/settings/upstream",
    "/api/settings/gateway",
    "/api/settings/model-map",
    "/api/settings/runtime-config",
    "/api/settings/runtime-config/export",
)


def test_permission_matrix_reads(stranger, viewer, admin):
    for path in PUBLIC_PATHS:
        resp = stranger.get(path)
        assert resp.status_code == 200, f"{path}: {resp.status_code} {resp.text}"

    ready = stranger.get("/api/health/ready")
    # readiness 是业务语义：依赖健康 200、不健康 503，两者都不是鉴权问题
    assert ready.status_code in (200, 503), ready.text

    for path in AUTH_READ_PATHS:
        anonymous = stranger.get(path)
        assert anonymous.status_code == 401, f"{path} 未登录: {anonymous.status_code} {anonymous.text}"
        as_viewer = viewer.client.get(path)
        assert as_viewer.status_code == 200, f"{path} viewer: {as_viewer.status_code} {as_viewer.text}"
        as_admin = admin.get(path)
        assert as_admin.status_code == 200, f"{path} admin: {as_admin.status_code} {as_admin.text}"

    for path in ADMIN_READ_PATHS:
        anonymous = stranger.get(path)
        assert anonymous.status_code == 401, f"{path} 未登录: {anonymous.status_code}"
        as_viewer = viewer.client.get(path)
        _denied(as_viewer)
        as_admin = admin.get(path)
        assert as_admin.status_code == 200, f"{path} admin: {as_admin.status_code} {as_admin.text}"


def test_grok_worker_matches_admin_only_consumer(viewer, admin):
    """worker 只被 admin-only 的注册任务组件消费，鉴权须与消费方同口径。"""
    denied = _denied(viewer.client.get("/api/grok/register/worker"))
    assert denied["error"]["required_role"] == "admin"
    assert denied["error"]["role"] == "viewer"

    ok = admin.get("/api/grok/register/worker")
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert set(body) == {
        "queued", "running", "succeeded", "failed", "cancelled",
        "max_concurrency", "max_workers", "executor",
    }
    for key in ("queued", "running", "succeeded", "failed", "cancelled", "max_concurrency", "max_workers"):
        assert isinstance(body[key], int)
    assert body["executor"] in {"http", "unconfigured"}


def test_permission_matrix_writes(stranger, viewer, admin):
    tag = uuid.uuid4().hex[:8]
    group_id = f"sec-grp-{tag}"
    cases = [
        ("POST", "/api/keys", {"json": {"name": f"matrix-key-{tag}"}}, 200),
        ("PATCH", "/api/keys/999999", {"json": {"enabled": False}}, 404),
        ("DELETE", "/api/keys/999999", {}, 404),
        ("POST", "/api/tokens", {"json": {"name": f"matrix-token-{tag}"}}, 200),
        ("PATCH", "/api/tokens/999999", {"json": {"enabled": False}}, 404),
        ("DELETE", "/api/tokens/999999", {}, 404),
        ("POST", "/api/quota/policies", {"json": {"key_id": 99_999_999, "window": "day", "limit_requests": 1}}, 404),
        ("DELETE", "/api/quota/policies/999999", {}, 404),
        ("POST", "/api/sub2api/groups", {"json": {"id": group_id}}, 200),
        ("POST", "/api/sub2api/accounts", {"json": {"group_id": "no-such-group"}}, 404),
        ("DELETE", "/api/sub2api/accounts/999999", {}, 404),
        ("DELETE", f"/api/sub2api/groups/{group_id}", {}, 200),
        ("POST", "/api/accounts/sync", {}, 200),
        ("PUT", "/api/accounts/no-such-file/note", {"json": {"note": "x"}}, 404),
        ("POST", "/api/accounts/no-such-file/checkin", {}, 404),
        ("DELETE", "/api/accounts/no-such-file", {}, 404),
        ("PATCH", "/api/providers/codebuddy", {"json": {"label": "CodeBuddy"}}, 200),
        ("POST", "/api/provider-credentials", {"json": {"provider": "no-such-provider", "label": "x"}}, 404),
        ("PATCH", "/api/provider-credentials/999999", {"json": {"note": "x"}}, 404),
        ("DELETE", "/api/provider-credentials/999999", {}, 404),
        ("POST", "/api/settings/runtime-config", {"json": {}}, 400),
        ("POST", "/api/settings/runtime-config/import", {"json": {"kind": "nope"}}, 400),
        ("POST", "/api/settings/upstream", {"json": {}}, 200),
        ("POST", "/api/settings/secret/reveal", {"json": {"path": "provider_config.opencode.no_such_field"}}, 404),
        ("POST", "/api/settings/gateway", {"json": {"allow_anonymous": False}}, 200),
        ("POST", "/api/settings/model-map", {"json": {}}, 200),
        ("POST", "/api/settings/upstash/test", {}, 400),
        ("POST", "/api/users", {"json": {"username": "ab", "password": "123456"}}, 400),
        ("PATCH", "/api/users/no-such-user", {"json": {"role": "viewer"}}, 404),
        ("DELETE", "/api/users/no-such-user", {}, 404),
        ("POST", "/api/pricing/books", {"json": {}}, 422),
        ("POST", "/api/grok/register/tasks", {"json": {"count": 0}}, 422),
        ("POST", "/api/grok/register/tasks/999999/cancel", {}, 404),
    ]

    for method, path, kwargs, admin_expected in cases:
        anonymous = stranger.request(method, path, **kwargs)
        assert anonymous.status_code == 401, f"{method} {path} 未登录: {anonymous.status_code} {anonymous.text}"
        as_viewer = viewer.client.request(method, path, **kwargs)
        _denied(as_viewer)
        as_admin = admin.request(method, path, **kwargs)
        assert as_admin.status_code == admin_expected, (
            f"{method} {path} admin: {as_admin.status_code} != {admin_expected} {as_admin.text[:200]}"
        )


# --------------------------------------------------------------------------- #
# 2. 归属：viewer 只能看自己的密钥 / 配额；显式查他人 403
# --------------------------------------------------------------------------- #


def test_key_and_quota_ownership(viewer, stranger, admin, owned_keys, viewer_policy):
    viewer_name = viewer.name

    keys_body = viewer.client.get("/api/keys").json()
    assert all(row["owner_user_id"] == viewer_name for row in keys_body)
    assert owned_keys["admin_id"] not in {row["id"] for row in keys_body}

    tokens_body = viewer.client.get("/api/tokens").json()
    assert owned_keys["admin_id"] not in {row["id"] for row in tokens_body}
    assert all(row["id"] in {item["id"] for item in keys_body} for row in tokens_body)

    for path in ("/api/quota/policies", "/api/quota/status", "/api/quota/alerts"):
        own = viewer.client.get(path, params={"key_id": owned_keys["viewer_id"]})
        assert own.status_code == 200, f"{path} 自有 key: {own.status_code} {own.text}"
        denied = viewer.client.get(path, params={"key_id": owned_keys["admin_id"]})
        _denied(denied, "无权查看该密钥的配额数据")

    header_own = viewer.client.get("/api/quota/policies", headers={"X-Api-Key": owned_keys["viewer_token"]})
    assert header_own.status_code == 200, header_own.text
    header_other = viewer.client.get("/api/quota/policies", headers={"X-Api-Key": owned_keys["admin_token"]})
    _denied(header_other, "无权查看该密钥的配额数据")

    body_other = viewer.client.post(
        "/api/quota/policies",
        json={"key_id": owned_keys["admin_id"], "window": "day", "limit_requests": 1},
    )
    _denied(body_other)

    own_policies = viewer.client.get("/api/quota/policies").json()
    assert {row["key_id"] for row in own_policies} == {owned_keys["viewer_id"]}
    assert {row["key_name"] for row in own_policies} == {owned_keys["viewer_name"]}
    assert owned_keys["admin_name"] not in json.dumps(own_policies, ensure_ascii=False)

    admin_policies = admin.get("/api/quota/policies").json()
    assert {row["key_id"] for row in admin_policies} == {owned_keys["viewer_id"], owned_keys["admin_id"]}
    assert all(row["key_id"] != owned_keys["admin_id"] for row in own_policies)

    status_body = viewer.client.get("/api/quota/status").json()
    serialized = json.dumps(status_body, ensure_ascii=False)
    assert owned_keys["admin_name"] not in serialized
    assert status_body["items"], "viewer 自己的策略应出现在 status 里"
    for item in status_body["items"]:
        assert item["policy"]["key_id"] == owned_keys["viewer_id"]

    # 归属用户不存在时 admin 的建 key 请求被拒，不落一条无主记录
    ghost = admin.post("/api/keys", json={"name": "ghost-owner-key", "owner_user_id": "no-such-user"})
    assert ghost.status_code == 400, ghost.text


# --------------------------------------------------------------------------- #
# 2b. 读端点收紧：grok 任务行 / 用户枚举 → admin-only；账号列表按归属收窄
# --------------------------------------------------------------------------- #


def test_grok_task_read_is_admin_only(admin, viewer, db_path):
    """任务行整行回显 proxy / payload_json / result_json，读侧与写侧同为 admin-only。"""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO grok_tasks(status, count, workers, proxy, payload_json, result_json, created_at) "
            "VALUES('failed', 1, 1, ?, ?, ?, ?)",
            (
                "http://proxy-user:proxy-pass@203.0.113.7:8080",
                json.dumps({"mail": "ghost@example.com"}),
                json.dumps({"ok": False}),
                1_700_000_500,
            ),
        )
        task_id = int(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()

    try:
        listed = admin.get("/api/grok/register/tasks")
        assert listed.status_code == 200, listed.text
        assert any(row["id"] == task_id for row in listed.json())

        detail = admin.get(f"/api/grok/register/tasks/{task_id}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["id"] == task_id

        _denied(viewer.client.get("/api/grok/register/tasks"))
        _denied(viewer.client.get(f"/api/grok/register/tasks/{task_id}"))
    finally:
        conn = sqlite3.connect(db_path)
        conn.execute("DELETE FROM grok_tasks WHERE id = ?", (task_id,))
        conn.commit()
        conn.close()


def test_user_enumeration_is_admin_only(admin, viewer, stranger):
    listing = admin.get("/api/users")
    assert listing.status_code == 200, listing.text
    names = {row["username"] for row in listing.json()}
    assert {"admin", viewer.name} <= names

    denied = viewer.client.get("/api/users")
    _denied(denied, "需要管理员权限")
    # 403 壳里只能有权限原因，不能夹带任何一条用户记录
    assert '"username"' not in denied.text

    assert stranger.get("/api/users").status_code == 401


def test_accounts_list_scoped_by_owner(admin, viewer, db_path, base_url):
    """admin 拿全量；非 admin 只拿 owner_user_id = 自己的行（无主行不可见）。"""
    tag = uuid.uuid4().hex[:8]
    second_name = f"secview2-{tag}"
    created = admin.post(
        "/api/users",
        json={"username": second_name, "password": VIEWER_PASSWORD, "role": "viewer"},
    )
    assert created.status_code == 200, created.text
    second = _login(base_url, second_name, VIEWER_PASSWORD)

    seeded = {
        f"sec-own-{tag}-first.json": viewer.name,
        f"sec-own-{tag}-second.json": second_name,
        f"sec-own-{tag}-shared.json": None,
    }
    conn = sqlite3.connect(db_path)
    try:
        for file_name, owner in seeded.items():
            conn.execute(
                "INSERT INTO accounts(file, provider, uid, nickname, credits, created_at, owner_user_id) "
                "VALUES(?,?,?,?,?,?,?)",
                (file_name, "codebuddy", f"uid-{tag}", "sec", 10.0, 1_700_000_600, owner),
            )
        conn.commit()
    finally:
        conn.close()

    try:
        admin_files = {row["file"] for row in admin.get("/api/accounts").json()["accounts"]}
        assert set(seeded) <= admin_files, "admin 应看到全量（含无主行）"

        first_view = viewer.client.get("/api/accounts")
        assert first_view.status_code == 200, first_view.text
        first_files = {row["file"] for row in first_view.json()["accounts"]}
        assert f"sec-own-{tag}-first.json" in first_files
        assert f"sec-own-{tag}-second.json" not in first_files, "看到了另一个 viewer 的账号"
        assert f"sec-own-{tag}-shared.json" not in first_files, "无主行对非 admin 可见"

        second_view = second.get("/api/accounts")
        assert second_view.status_code == 200, second_view.text
        second_files = {row["file"] for row in second_view.json()["accounts"]}
        assert f"sec-own-{tag}-second.json" in second_files
        assert f"sec-own-{tag}-first.json" not in second_files, "看到了另一个 viewer 的账号"
        assert f"sec-own-{tag}-shared.json" not in second_files

        body = first_view.json()
        assert body["total"] == len(body["accounts"]), "total 应与收窄后的行数一致"
    finally:
        second.close()
        conn = sqlite3.connect(db_path)
        conn.execute("DELETE FROM accounts WHERE file LIKE ?", (f"sec-own-{tag}-%",))
        conn.commit()
        conn.close()


def test_full_key_in_url_is_rejected_without_echo(admin, owned_keys):
    token = owned_keys["admin_token"]
    for path in ("/api/quota/policies", "/api/quota/status", "/api/quota/alerts"):
        resp = admin.get(path, params={"key_id": token})
        assert resp.status_code == 400, f"{path}: {resp.status_code} {resp.text}"
        assert token not in resp.text

    both = admin.get(
        "/api/quota/policies",
        params={"key_id": owned_keys["admin_id"]},
        headers={"X-Api-Key": owned_keys["admin_token"]},
    )
    assert both.status_code == 400, both.text


# --------------------------------------------------------------------------- #
# 3. 审计：actor 自限权 + 登录失败降级为聚合
# --------------------------------------------------------------------------- #


def test_audit_actor_scope_and_login_failure_degrade(admin, viewer, db_path, base_url):
    tag = uuid.uuid4().hex[:8]
    other = f"sec-audit-other-{tag}"
    _seed_audit(db_path, viewer.name, 3, tag)
    _seed_audit(db_path, other, 5, tag)

    own_total = _audit_count(db_path, viewer.name)
    assert own_total >= 3

    first = viewer.client.get("/api/audit-logs", params={"limit": 500})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["total"] == own_total, f"viewer total 应只含自己 {own_total}，实际 {body['total']}"
    assert {row["actor"] for row in body["items"]} == {viewer.name}
    assert other not in first.text

    for path in ("/api/audit-logs", "/api/security/audit"):
        denied = viewer.client.get(path, params={"actor": other})
        _denied(denied, "无权查看他人审计日志")

    admin_rows = admin.get("/api/audit-logs", params={"action": f"seed.{tag}", "limit": 500}).json()
    assert admin_rows["total"] == 8
    assert {row["actor"] for row in admin_rows["items"]} == {viewer.name, other}

    # 登录失败：admin 全站明细，非 admin 只剩自己的聚合数字，且不含任何 IP / 他人用户名
    _seed_login_failed(db_path, viewer.name, 2, ip="203.0.113.61")
    _seed_login_failed(db_path, other, 4, ip="203.0.113.62")
    _seed_login_failed(db_path, "anonymous", 1, ip="203.0.113.63")

    self_view = viewer.client.get("/api/security/login-failures")
    assert self_view.status_code == 200, self_view.text
    payload = self_view.json()
    assert payload["scope"] == "self"
    assert payload["active"] == [] and payload["recent"] == []
    assert payload["stats"]["total"] == 2
    raw = self_view.text
    for leak in (other, "203.0.113.61", "203.0.113.62", "203.0.113.63", "anonymous"):
        assert leak not in raw, f"非 admin 登录失败视图泄露 {leak}"

    # 真实失败一次：只多一条窗口计数，不会把 viewer 锁死
    bad = _fresh_client(base_url)
    try:
        failed = bad.post("/api/login", json={"username": viewer.name, "password": "wrong-password"})
        assert failed.status_code == 401, failed.text
    finally:
        bad.close()

    after = viewer.client.get("/api/security/login-failures").json()
    assert after["window_failures"] >= 1

    site = admin.get("/api/security/login-failures", params={"limit": 200}).json()
    assert site["scope"] == "all"
    assert {row["actor"] for row in site["recent"]} >= {other, viewer.name}
    assert any(item["username"] == viewer.name and item["ip"] == "127.0.0.1" for item in site["active"])


def test_security_guard_exposes_only_static_policy(viewer, admin):
    as_viewer = viewer.client.get("/api/security/guard")
    assert as_viewer.status_code == 200, as_viewer.text
    body = as_viewer.json()
    assert set(body) == {
        "allow_private_upstreams",
        "blocked_hostnames",
        "blocked_addresses",
        "blocked_categories",
        "allowed_schemes",
    }
    assert set(body["blocked_hostnames"]) == {
        "localhost",
        "localhost.localdomain",
        "metadata.google.internal",
    }
    assert body["blocked_addresses"] == ["169.254.169.254", "fd00:ec2::254"]
    # 测试环境为假上游显式放开了私网，这里如实反映；不是泄露的真实封禁名单
    assert body["allow_private_upstreams"] is True

    identical = admin.get("/api/security/guard").json()
    assert identical == body, "guard 是静态策略，admin 与 viewer 不应有差异"

    serialized = json.dumps(body, ensure_ascii=False)
    assert viewer.name not in serialized
    assert "203.0.113" not in serialized


# --------------------------------------------------------------------------- #
# 4. 密钥泄露面
# --------------------------------------------------------------------------- #


def test_gateway_x_api_key_not_persisted_or_echoed(admin, stranger, viewer, db_path, owned_keys):
    token = owned_keys["admin_token"]
    request_id = f"sec-req-{uuid.uuid4().hex[:8]}"
    payload = {"model": "fake-chat-1", "messages": [{"role": "user", "content": "ping"}]}

    resp = admin.post(
        "/v1/chat/completions",
        headers={"x-api-key": token, "x-provider": "codebuddy", "x-request-id": request_id},
        json=payload,
    )
    assert resp.status_code == 200, resp.text
    assert token not in resp.text, "网关响应体不应回显完整密钥"
    assert all(token not in value for value in resp.headers.values()), "响应头不应带完整密钥"
    assert resp.headers["x-request-id"] == request_id, "X-Request-Id 只透传请求 id，与密钥无关"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM request_logs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None, "网关请求没有写 request_logs"
        assert row["key_id"] == owned_keys["admin_id"]
        assert row["key_name"] == owned_keys["admin_name"]
        for key in row.keys():
            value = row[key]
            assert token not in str(value), f"request_logs.{key} 出现完整密钥"
    finally:
        conn.close()

    logs = admin.get("/api/logs", params={"limit": 50})
    assert logs.status_code == 200, logs.text
    assert token not in logs.text

    # viewer 不带 key_id 时按 owner 自动限定范围，200 且不含密钥明文
    viewer_logs = viewer.client.get("/api/logs", params={"limit": 50})
    assert viewer_logs.status_code == 200, viewer_logs.text
    assert token not in viewer_logs.text
    # 非 admin 指定他人 key_id 一律 403，且响应不得带他人密钥明文/用户名
    cross_key_logs = viewer.client.get("/api/logs", params={"key_id": owned_keys["admin_id"]})
    _denied(cross_key_logs)
    assert owned_keys["admin_name"] not in cross_key_logs.text
    assert owned_keys["admin_token"] not in cross_key_logs.text
    # admin 指定任意 key_id 可读
    admin_cross = admin.get("/api/logs", params={"key_id": owned_keys["admin_id"]})
    assert admin_cross.status_code == 200, admin_cross.text
    assert owned_keys["admin_name"] in admin_cross.text
    assert token not in admin_cross.text

    leaked, where = _db_has_text(db_path, token)
    assert not leaked, f"完整密钥出现在 {where}"

    key_list = admin.get("/api/keys")
    assert token not in key_list.text


def test_invalid_and_conflicting_gateway_keys_not_echoed(stranger, owned_keys):
    bogus = "u2a_" + uuid.uuid4().hex
    payload = {"model": "fake-chat-1", "messages": [{"role": "user", "content": "ping"}]}

    invalid = stranger.post("/v1/chat/completions", headers={"x-api-key": bogus}, json=payload)
    assert invalid.status_code == 401, invalid.text
    assert bogus not in invalid.text

    conflict = stranger.post(
        "/v1/chat/completions",
        headers={
            "authorization": f"Bearer {owned_keys['admin_token']}",
            "x-api-key": owned_keys["viewer_token"],
        },
        json=payload,
    )
    assert conflict.status_code == 401, conflict.text
    assert owned_keys["admin_token"] not in conflict.text
    assert owned_keys["viewer_token"] not in conflict.text


def test_access_log_masks_u2a_in_query(owned_keys):
    token = owned_keys["admin_token"]
    raw_args = (
        "127.0.0.1:12345",
        "GET",
        f"/api/quota/policies?key_id={token}",
        "HTTP/1.1",
        400,
    )
    masked = mask_access_args(raw_args)
    assert token not in str(masked)
    assert "u2a_" in str(masked)  # 保留了可辨识前缀，但主体已掩码

    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d', raw_args, None,
    )
    formatter = JsonFormatter()
    line = formatter.format(record)
    assert token not in line
    assert json.loads(line)["path"] == "/api/quota/policies"

    assert token not in mask_text(f"upstream failed for key {token}")

    main_source = (APP_DIR / "main.py").read_text(encoding="utf-8")
    assert "mask_access_args" in main_source, "uvicorn.access 过滤器必须继续复用同一脱敏函数"


# --------------------------------------------------------------------------- #
# 5. SSRF 边界
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("url", "allow_loopback", "allow_private", "ok"),
    [
        ("http://127.0.0.1:7864", True, False, True),
        ("http://127.1.2.3:7864", True, False, True),
        ("http://127.1.2.3:7864", False, False, False),
        ("http://[::ffff:127.0.0.1]:7864", True, False, True),
        ("http://[::ffff:127.0.0.1]:7864", False, False, False),
        ("http://10.0.0.5", True, False, False),
        ("http://172.16.0.9", True, False, False),
        ("http://192.168.1.10", True, False, False),
        ("http://169.254.1.1", True, False, False),
        ("http://[::ffff:10.0.0.5]", True, False, False),
        ("http://8.8.8.8", False, False, True),
        ("ftp://example.com", False, False, False),
        ("http://169.254.169.254", True, True, False),
        ("http://[::ffff:169.254.169.254]", True, True, False),
        ("http://metadata.google.internal", True, True, False),
    ],
)
def test_ssrf_guard_function_matrix(url, allow_loopback, allow_private, ok):
    if ok:
        assert guard_upstream_url(url, allow_loopback=allow_loopback, allow_private=allow_private) == url
    else:
        with pytest.raises(UpstreamAddressError):
            guard_upstream_url(url, allow_loopback=allow_loopback, allow_private=allow_private)


def test_loopback_detection_covers_entire_127_slash_8():
    for url in ("http://127.0.0.1:7864", "http://127.1.2.3:7864", "http://localhost:7864"):
        assert is_loopback_url(url) is True
    assert is_loopback_url("http://8.8.8.8") is False


def test_ssrf_live_metadata_rejected_on_every_entry(admin, fake_base):
    for url in (
        "http://169.254.169.254/latest/meta-data",
        "http://[::ffff:169.254.169.254]/",
        "http://metadata.google.internal/",
    ):
        resp = admin.post(
            "/api/provider-credentials",
            json={"provider": "opencode", "label": "ssrf", "base_url": url, "secret": "x", "enabled": False},
        )
        assert resp.status_code == 400, f"{url}: {resp.status_code} {resp.text}"
        assert "元数据" in resp.json()["detail"]

    before = {row["id"]: row for row in admin.get("/api/providers").json()}
    patched = admin.patch("/api/providers/codebuddy", json={"base_url": "http://169.254.169.254"})
    assert patched.status_code == 400, patched.text
    after = {row["id"]: row for row in admin.get("/api/providers").json()}
    assert after["codebuddy"]["base_url"] == before["codebuddy"]["base_url"]

    created = admin.post(
        "/api/provider-credentials",
        json={"provider": "codebuddy", "label": "ssrf-loopback", "base_url": fake_base, "enabled": False},
    )
    assert created.status_code == 200, created.text
    credential_id = created.json()["id"]
    try:
        assert is_loopback_url(created.json()["base_url"]) is True
    finally:
        admin.delete(f"/api/provider-credentials/{credential_id}")

    group_id = f"sec-ssrf-{uuid.uuid4().hex[:8]}"
    group = admin.post("/api/sub2api/groups", json={"id": group_id})
    assert group.status_code == 200, group.text
    try:
        account = admin.post(
            "/api/sub2api/accounts",
            json={"group_id": group_id, "name": "ssrf", "upstream_base": "http://169.254.169.254"},
        )
        assert account.status_code == 400, account.text
        assert "元数据" in account.json()["detail"]
    finally:
        admin.delete(f"/api/sub2api/groups/{group_id}")


def test_private_ranges_live_bypass_is_harness_only(admin):
    guard = admin.get("/api/security/guard").json()
    # conftest 为了让假上游跑起来设置了 UNIFIED_ALLOW_PRIVATE_UPSTREAMS=1，
    # 因此「私网被拒」只能在函数级验证；这里如实记录环境差异，不伪造结论。
    assert guard["allow_private_upstreams"] is True


def test_dns_rebinding_window_not_revalidated(monkeypatch):
    import app.net_guard as net_guard

    calls: list[str] = []
    public_ip = ipaddress.ip_address("93.184.216.34")

    def resolve_public(host: str, port: int):
        calls.append(host)
        return [public_ip]

    monkeypatch.setattr(net_guard, "_resolve", resolve_public)
    cleaned = guard_upstream_url("http://rebind.example.test:8080", allow_private=False)
    assert cleaned == "http://rebind.example.test:8080"
    assert calls == ["rebind.example.test"], "保存时应做一次 DNS 解析"

    # 第二次解析若指向元数据，守卫本身会拒；但请求链路没有第二次调用
    monkeypatch.setattr(
        net_guard, "_resolve",
        lambda host, port: [ipaddress.ip_address("169.254.169.254")],
    )
    with pytest.raises(UpstreamAddressError):
        guard_upstream_url("http://rebind.example.test:8080", allow_private=False)

    request_path_sources = {
        name: (APP_DIR / "adapters" / name).read_text(encoding="utf-8")
        for name in ("base.py", "client.py", "codebuddy.py", "opencode.py")
    }
    for name, source in request_path_sources.items():
        assert "guard_upstream_url" not in source, f"{name} 请求路径不应再次调用守卫"
    assert "guard_upstream_url" in (APP_DIR / "adapters" / "sub2api_store.py").read_text(encoding="utf-8")
    assert "follow_redirects" not in request_path_sources["client.py"]


# --------------------------------------------------------------------------- #
# 收尾：清空 request_logs（放在最后，避免影响上面的证据采集）
# --------------------------------------------------------------------------- #


def test_logs_clear_admin_ok_at_end(admin):
    cleared = admin.post("/api/logs/clear")
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["ok"] is True
