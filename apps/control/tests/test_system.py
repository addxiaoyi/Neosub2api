"""系统信息面板（更新日志 / 更新状态）与访问令牌薄封装的回归。

权限口径：GET 允许 viewer（与 /api/keys 现状一致），写操作一律 admin。
"""

from __future__ import annotations

import re
import subprocess
import uuid
from pathlib import Path

import httpx
import pytest

from app import __version__
from app.routers import system

pytestmark = pytest.mark.system

# 日期与版本头的既有约定：`## 版本 [ - YYYY-MM-DD]`，未发布段允许写日期
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_VERSION_HEAD = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
# 断言基线锚定首个已发布版本，不锚定它排第几（顶部允许有未发布段）
_BASE_VERSION = "0.1.0"
_BASE_DATE = "2026-11-02"


def _assert_changelog_invariants(body: dict) -> dict:
    """仓库根 CHANGELOG 的结构+语义不变量，真实文件与 HEAD 快照两态共用。"""
    assert body["available"] is True
    assert body["current"] == __version__
    versions = body["versions"]
    assert versions, "仓库根 CHANGELOG.md 应该有内容"
    assert body["total"] >= len(versions)

    first = versions[0]
    assert first["unreleased"] or _VERSION_HEAD.match(first["version"]), (
        f"首段必须是未发布头或版本头，实得 {first['version']!r}"
    )

    for entry in versions:
        name, date = entry["version"], entry["date"]
        assert name, "段头不得解析出空版本名"
        # 语义与格式互为兜底：长得像版本号就必须是已发布态，否则段头格式已坏
        assert entry["unreleased"] == (not _VERSION_HEAD.match(name)), (
            f"段头 {name!r} 既不合规版本号也不是未发布标记"
        )
        assert _ISO_DATE.match(date), f"{name} 的日期 {date!r} 不是 YYYY-MM-DD"
        if not entry["unreleased"]:
            assert date, f"已发布版本 {name} 必须带日期"
        for section in entry["sections"]:
            assert section["title"].strip(), f"{name} 存在空分类标题"

    released = [e for e in versions if not e["unreleased"]]
    names = [e["version"] for e in released]
    assert len(names) == len(set(names)), f"已发布版本号重复：{names}"
    nums = [tuple(int(p) for p in n.split("-")[0].split("+")[0].split(".")) for n in names]
    assert all(len(n) == 3 for n in nums), f"版本号不是 X.Y.Z：{names}"
    assert nums == sorted(nums, reverse=True), f"新版本应排在前面，实得 {names}"

    base = next((e for e in released if e["version"] == _BASE_VERSION), None)
    assert base is not None, f"缺少 {_BASE_VERSION} 段"
    assert base["date"] == _BASE_DATE, f"{_BASE_VERSION} 日期应为 {_BASE_DATE}"
    titles = [s["title"] for s in base["sections"]]
    assert "安全" in titles and "新增" in titles, f"{_BASE_VERSION} 段缺分类：{titles}"
    assert all(item["level"] == 0 for s in base["sections"] for item in s["items"]), (
        f"{_BASE_VERSION} 段条目应是顶层缩进"
    )
    return body


def _login_viewer(admin: httpx.Client) -> httpx.Client:
    name = f"sys-viewer-{uuid.uuid4().hex[:8]}"
    created = admin.post(
        "/api/users", json={"username": name, "password": "viewer-pass", "role": "viewer"}
    )
    assert created.status_code == 200, created.text
    client = httpx.Client(base_url=str(admin.base_url), trust_env=False, timeout=10.0)
    assert client.post("/api/login", json={"username": name, "password": "viewer-pass"}).status_code == 200
    return client


def test_viewer_reads_tokens_but_cannot_write(admin: httpx.Client) -> None:
    created = admin.post("/api/tokens", json={"name": "viewer-probe"})
    assert created.status_code == 200, created.text
    token_id = created.json()["id"]
    viewer = _login_viewer(admin)
    try:
        assert viewer.get("/api/tokens").status_code == 200, "viewer 可读列表"
        assert viewer.post("/api/tokens", json={"name": "nope"}).status_code == 403
        assert viewer.patch(f"/api/tokens/{token_id}", json={"enabled": False}).status_code == 403
        assert viewer.delete(f"/api/tokens/{token_id}").status_code == 403
    finally:
        viewer.close()
        admin.delete(f"/api/tokens/{token_id}")


def test_admin_token_lifecycle_maps_into_keys(admin: httpx.Client, anon: httpx.Client) -> None:
    """创建返回明文、列表不含明文、删除后同一把 u2a_ 在 /v1 立即 401。"""
    r = admin.post("/api/tokens", json={"name": "ci-token", "expires_at": None})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token"].startswith("u2a_")
    assert body["scope"] == "readonly"
    assert body["expires_at"] is None
    assert body["created_by"] == ""
    token_id, plaintext = body["id"], body["token"]

    listed = next(it for it in admin.get("/api/tokens").json() if it["id"] == token_id)
    assert "token" not in listed, "列表不得回传明文"
    assert listed["prefix"] == plaintext[:10]

    patch = admin.patch(f"/api/tokens/{token_id}", json={"name": "ci-renamed", "enabled": False})
    assert patch.status_code == 200
    assert patch.json()["name"] == "ci-renamed" and patch.json()["enabled"] is False

    assert admin.delete(f"/api/tokens/{token_id}").status_code == 200
    assert all(it["id"] != token_id for it in admin.get("/api/tokens").json())

    hit = anon.post(
        "/v1/chat/completions",
        headers={"authorization": f"Bearer {plaintext}"},
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert hit.status_code == 401, f"删除令牌应等同吊销密钥：{hit.status_code}"


def test_token_rejects_admin_scope_and_expiry(admin: httpx.Client) -> None:
    """不支持的能力直接 400，不静默降级成假承诺。"""
    assert admin.post("/api/tokens", json={"name": "x", "scope": "admin"}).status_code == 400
    assert admin.post("/api/tokens", json={"name": "x", "expires_at": 1700000000}).status_code == 400

    created = admin.post("/api/tokens", json={"name": "scope-probe"}).json()
    try:
        assert admin.patch(f"/api/tokens/{created['id']}", json={"scope": "admin"}).status_code == 400
    finally:
        admin.delete(f"/api/tokens/{created['id']}")


def test_changelog_parses_repo_file(admin: httpx.Client) -> None:
    """真实仓库文件走 HTTP 面，校验不变量而非固定首条。"""
    r = admin.get("/api/system/changelog")
    assert r.status_code == 200, r.text
    _assert_changelog_invariants(r.json())


def test_changelog_head_snapshot_still_parses(tmp_path: Path) -> None:
    """HEAD 版（顶部没有未发布段）也过同一套不变量，防止断言写死成当前状态。"""
    repo_root = system.CHANGELOG_PATH.parent
    head = subprocess.run(
        ["git", "show", "HEAD:CHANGELOG.md"],
        cwd=str(repo_root),
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    snapshot = tmp_path / "CHANGELOG.md"
    snapshot.write_text(head, encoding="utf-8", newline="")
    _assert_changelog_invariants(system.read_changelog(snapshot))


def test_changelog_missing_file_is_not_500(tmp_path: Path) -> None:
    """解析层直接给一个不存在的路径：返回空列表 + available=false。"""
    missing = tmp_path / "CHANGELOG.md"
    body = system.read_changelog(missing)
    assert body["available"] is False
    assert body["versions"] == []
    assert body["current"] == __version__
    assert "不存在" in body["error"]


def test_update_status_is_honest(admin: httpx.Client) -> None:
    """默认配置下在线更新是关的：状态必须如实说明原因，不许假装能更新。"""
    r = admin.get("/api/system/update-status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["current_version"] == __version__
    assert body["latest_version"] == __version__
    assert body["available"] is False
    assert body["signature"] == "none"
    assert body["reason"]
    assert body["updater_found"] is False
    assert body["can_apply"] is False
    job = body["job"]
    assert job["state"] == "idle"
    assert job["progress"] == 0
    assert job["log_tail"] == []


def test_versions_shape_and_viewer_can_read(admin: httpx.Client) -> None:
    """单版本部署：manager 是真实版本，不编造多版本列表。"""
    r = admin.get("/api/system/versions")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["manager"] == __version__
    assert isinstance(body["upstream_connected"], bool)
    assert body["upstream_accounts"] is None or body["upstream_accounts"] >= 0
    assert body["upstream_dir"] == ""
    assert body["upstream_connected"] == bool(body["upstream_accounts"])

    viewer = _login_viewer(admin)
    try:
        assert viewer.get("/api/system/versions").status_code == 200
    finally:
        viewer.close()


