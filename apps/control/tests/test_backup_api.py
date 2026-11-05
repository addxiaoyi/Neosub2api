"""备份与恢复端点验收：真实 sqlite、真实 backup_core，不 mock 任何备份函数。

跑在 conftest 起的独立控制面进程 + 临时 UNIFIED_DATA_DIR 上，
备份/恢复目标是测试自己的库，不碰任何预览实例。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from app import security
from app.db import Database
from app.routers import backup as backup_mod

JUNK_NAME = "unified-backup-19991231-235959.zip"
VIEWER_USER = "backup-viewer"
LATE_USER = "u-after-snapshot"


def _backups_dir(db_path: str) -> Path:
    return Path(db_path).parent / "backups"


def _audit_rows(db_path: str, action: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(
            conn.execute(
                "SELECT count(*) FROM audit_logs WHERE action=?", (action,)
            ).fetchone()[0]
        )
    finally:
        conn.close()


def _login(base_url: str, username: str, password: str) -> httpx.Client:
    client = httpx.Client(base_url=base_url, trust_env=False, timeout=10.0)
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, f"登录失败: {r.status_code} {r.text}"
    return client


def _viewer(base_url: str, admin: httpx.Client) -> httpx.Client:
    created = admin.post(
        "/api/users",
        json={"username": VIEWER_USER, "password": "viewer-pass", "role": "viewer"},
    )
    assert created.status_code in (200, 409), created.text
    if created.status_code == 409:
        admin.patch(f"/api/users/{VIEWER_USER}", json={"role": "viewer"})
    return _login(base_url, VIEWER_USER, "viewer-pass")


def _fresh(base_url: str) -> httpx.Client:
    return httpx.Client(base_url=base_url, trust_env=False, timeout=10.0)


def test_backup_creates_verifiable_archive(admin: httpx.Client, db_path: str) -> None:
    before = _audit_rows(db_path, "backup.create")
    r = admin.post("/api/system/backup")
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["ok"] is True
    assert body["name"].startswith("unified-backup-")
    assert body["size_bytes"] > 0
    assert body["duration_ms"] >= 0
    assert body["path"].endswith(body["name"])
    assert len(body["sha256"]) == 64

    archive = _backups_dir(db_path) / body["name"]
    assert archive.is_file(), f"备份文件不存在: {archive}"
    assert archive.stat().st_size == body["size_bytes"]

    sidecar = archive.with_name(archive.name + ".sha256")
    assert sidecar.is_file(), "缺少 sha256 侧车"
    actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert sidecar.read_text(encoding="utf-8").split()[0] == actual == body["sha256"]

    # 产物本身必须是能过 PRAGMA integrity_check 的真 sqlite
    with zipfile.ZipFile(archive) as zf:
        payload = zf.read("db/unified.db")
    probe = Path(tempfile.mkdtemp()) / "probe.db"
    probe.write_bytes(payload)
    conn = sqlite3.connect(probe)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT count(*) FROM users WHERE username='admin'").fetchone()[0] == 1
    finally:
        conn.close()
        probe.unlink(missing_ok=True)

    listing = admin.get("/api/system/backups")
    assert listing.status_code == 200, listing.text
    listed = listing.json()
    assert isinstance(listed["backups"], list)
    match = [item for item in listed["backups"] if item["name"] == body["name"]]
    assert match and match[0]["size_bytes"] == body["size_bytes"]
    assert match[0]["created_at"] > 0 and match[0]["has_checksum"] is True
    assert _audit_rows(db_path, "backup.create") >= before + 1


def test_backup_requires_admin(admin: httpx.Client, db_path: str, base_url: str) -> None:
    stranger = _fresh(base_url)  # conftest 的 anon 和 admin 是同一个 client（同一份 cookie）
    assert stranger.get("/api/system/backups").status_code == 401
    assert stranger.post("/api/system/backup").status_code == 401
    stranger.close()

    directory = _backups_dir(db_path)
    before = set(directory.glob("unified-backup-*.zip")) if directory.is_dir() else set()
    viewer = _viewer(base_url, admin)
    try:
        r_list = viewer.get("/api/system/backups")
        r_make = viewer.post("/api/system/backup")
        r_restore = viewer.post(
            "/api/system/restore", json={"name": JUNK_NAME, "confirm": True}
        )
        assert r_list.status_code == 403, r_list.text
        assert r_make.status_code == 403, r_make.text
        assert r_restore.status_code == 403, r_restore.text
        assert r_list.json()["error"]["type"] == "permission_denied"
    finally:
        viewer.close()
    after = set(directory.glob("unified-backup-*.zip")) if directory.is_dir() else set()
    assert after == before, "非 admin 触发了备份，产生了新文件"


def test_concurrent_backup_returns_conflict(admin: httpx.Client, db_path: str) -> None:
    directory = _backups_dir(db_path)
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / "backup.lock"
    lock.write_text(
        json.dumps({"op": "backup", "pid": 0, "started_at": int(time.time())}),
        encoding="utf-8",
    )
    try:
        r = admin.post("/api/system/backup")
        assert r.status_code == 409, f"锁被占用时应 409，实际 {r.status_code}: {r.text}"
        assert "进行中" in r.json()["detail"]
    finally:
        lock.unlink(missing_ok=True)

    # 锁释放后必须能正常出备份
    again = admin.post("/api/system/backup")
    assert again.status_code == 200, again.text

    # 真并发：同时打一串请求，只允许 200/409，且每个 200 的产物都可校验
    codes: list[int] = []
    lock_gate = threading.Barrier(4)

    def hit() -> None:
        try:
            lock_gate.wait(timeout=10)
            codes.append(admin.post("/api/system/backup").status_code)
        except Exception:  # noqa: BLE001 — 线程里的断言统一在主线程判断
            codes.append(-1)

    threads = [threading.Thread(target=hit) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(codes) == 4
    assert all(code in (200, 409) for code in codes), f"并发请求状态码异常: {codes}"
    assert 200 in codes, f"并发下没有一次成功备份: {codes}"

    # 并发不能写出坏文件：列表里每份归档的 sha256 都要和侧车对上
    listing = admin.get("/api/system/backups").json()["backups"]
    assert listing, "并发后列表为空"
    for item in listing:
        archive = directory / item["name"]
        sidecar = archive.with_name(archive.name + ".sha256")
        assert sidecar.is_file(), f"{item['name']} 缺侧车"
        assert hashlib.sha256(archive.read_bytes()).hexdigest() == sidecar.read_text().split()[0], (
            f"{item['name']} 归档损坏（并发写坏）"
        )


def test_restore_rejects_invalid_targets(admin: httpx.Client, db_path: str) -> None:
    directory = _backups_dir(db_path)
    directory.mkdir(parents=True, exist_ok=True)
    junk = directory / JUNK_NAME

    # 1) 没有 confirm 的裸请求
    r = admin.post("/api/system/restore", json={"name": "unified-backup-19991231-235959.zip"})
    assert r.status_code == 400, r.text
    assert "确认" in r.json()["detail"]

    # 2) 文件名不合法（含路径穿越）
    r = admin.post(
        "/api/system/restore",
        json={"name": "../../unified-backup-20990101-000000.zip", "confirm": True},
    )
    assert r.status_code == 400, r.text
    assert "不合法" in r.json()["detail"]

    # 3) 文件不存在
    r = admin.post(
        "/api/system/restore",
        json={"name": "unified-backup-20990101-000000.zip", "confirm": True},
    )
    assert r.status_code == 400, r.text
    assert "不存在" in r.json()["detail"]

    try:
        # 4) 连 zip 都不是
        junk.write_bytes(b"definitely not a zip archive")
        r = admin.post("/api/system/restore", json={"name": JUNK_NAME, "confirm": True})
        assert r.status_code == 400, r.text
        assert "已中止恢复" in r.json()["detail"]

        # 5) 是 zip，但没有 manifest（不可校验 → 拒绝）
        with zipfile.ZipFile(junk, "w") as zf:
            zf.writestr("db/unified.db", b"no manifest here")
        r = admin.post("/api/system/restore", json={"name": JUNK_NAME, "confirm": True})
        assert r.status_code == 400, r.text
        assert "manifest" in r.json()["detail"]

        # 6) manifest 齐全，但库本体不是合法 sqlite（integrity_check 拦下）
        real = admin.post("/api/system/backup").json()["name"]
        with zipfile.ZipFile(directory / real) as zf:
            manifest = json.loads(zf.read("manifest.json"))
            members = {
                info.filename: zf.read(info.filename)
                for info in zf.infolist()
                if info.filename not in ("db/unified.db", "manifest.json")
            }
        garbage = b"SQLite format 3 but truncated \x00" * 16
        manifest["database"]["sha256"] = hashlib.sha256(garbage).hexdigest()
        manifest["database"]["size_bytes"] = len(garbage)
        with zipfile.ZipFile(junk, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
            zf.writestr("db/unified.db", garbage)
            for name, payload in members.items():
                zf.writestr(name, payload)
        r = admin.post("/api/system/restore", json={"name": JUNK_NAME, "confirm": True})
        assert r.status_code == 400, r.text
        assert "已中止恢复" in r.json()["detail"]
    finally:
        junk.unlink(missing_ok=True)
        Path(f"{junk}.sha256").unlink(missing_ok=True)

    # 全部拒绝之后，当前库一个字节都没被改：admin 还在，能继续出备份
    still_ok = admin.get("/api/system/backups")
    assert still_ok.status_code == 200 and still_ok.json()["count"] >= 1


def test_restore_roundtrip_keeps_library_consistent(admin: httpx.Client, db_path: str) -> None:
    snapshot = admin.post("/api/system/backup")
    assert snapshot.status_code == 200, snapshot.text
    taken = snapshot.json()["name"]

    created = admin.post(
        "/api/users",
        json={"username": LATE_USER, "password": "late-pass-1", "role": "viewer"},
    )
    assert created.status_code == 200, created.text
    names = [u["username"] for u in admin.get("/api/users").json()]
    assert LATE_USER in names

    restores_before = _audit_rows(db_path, "backup.restore")
    r = admin.post("/api/system/restore", json={"name": taken, "confirm": True})
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["ok"] is True
    assert body["restored"] == taken
    assert body["selfcheck_ok"] is True
    assert body["duration_ms"] >= 0
    # 恢复前快照已生成，且真实存在于列表里（恢复错了能一键退回去）
    assert body["pre_restore_backup"].startswith("unified-backup-")
    assert body["pre_restore_backup"] != taken
    snapshot_file = _backups_dir(db_path) / body["pre_restore_backup"]
    assert snapshot_file.is_file() and snapshot_file.stat().st_size > 0
    listed = [i["name"] for i in admin.get("/api/system/backups").json()["backups"]]
    assert body["pre_restore_backup"] in listed

    # 服务不重启也能读到恢复后的库：A 快照之后建的用户必须消失
    after = admin.get("/api/users")
    assert after.status_code == 200, after.text
    assert LATE_USER not in [u["username"] for u in after.json()]
    assert "admin" in [u["username"] for u in after.json()]

    # 恢复动作写进了恢复后的审计日志
    assert _audit_rows(db_path, "backup.restore") == restores_before + 1

    # 恢复后的库仍是合法 sqlite，且和快照里的行数一致
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        rows_live = conn.execute("SELECT count(*) FROM users").fetchone()[0]
    finally:
        conn.close()
    with zipfile.ZipFile(_backups_dir(db_path) / taken) as zf:
        probe = Path(tempfile.mkdtemp()) / "snap.db"
        probe.write_bytes(zf.read("db/unified.db"))
    conn = sqlite3.connect(probe)
    try:
        rows_snap = conn.execute("SELECT count(*) FROM users").fetchone()[0]
    finally:
        conn.close()
        probe.unlink(missing_ok=True)
    assert rows_live == rows_snap, f"恢复后 users 行数 {rows_live} != 快照 {rows_snap}"


# ---------------------------------------------------------------- 失败与回滚（R-07）
# restore 已改成 SQLite 在线备份（src=归档, dst=活库），不再 os.replace 换文件。
# 下面三条覆盖新的失败面：应用前拦截（活库一个字节都不动）、应用中途失败后的
# 在线回滚、以及恢复后 journal_mode 仍然是 wal。


def _panel_app(db_path: str) -> FastAPI:
    """最小控制面：只挂备份路由，鉴权用 dependency_overrides 顶掉。

    失败分支要在同进程里 monkeypatch 备份函数，而 conftest 的控制面是子进程，
    跨进程打不了补丁，所以这里按 test_upstash_probe 的做法就地起一个 ASGI app。
    """
    db = Database(Path(db_path))
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO users(username, password_hash, role, created_at) VALUES(?,?,?,?)",
            ("admin", "not-a-real-hash", "admin", int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()

    panel = FastAPI()
    panel.state.db = db
    panel.state.settings = SimpleNamespace(db_path=db_path)
    panel.include_router(backup_mod.router)
    panel.dependency_overrides[security.admin_user] = lambda: {"username": "admin", "role": "admin"}
    return panel


async def _api(panel: FastAPI, method: str, path: str, payload: dict | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=panel)
    async with httpx.AsyncClient(transport=transport, base_url="http://control.test", trust_env=False) as client:
        return await client.request(method, path, json=payload)


def _files_state(db_path: str) -> dict[str, bytes | None]:
    """db / -wal / -shm 的存在性与字节；None = 不存在。"""
    state: dict[str, bytes | None] = {}
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{db_path}{suffix}")
        state[suffix or "db"] = path.read_bytes() if path.is_file() else None
    return state


def _archive_with_stale_row_counts(archive: Path, target: Path) -> None:
    """只改 manifest 的 tables 行数：成员 sha256 与 integrity 全对得上，唯独 selfcheck 行数会漂。"""
    with zipfile.ZipFile(archive) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        members = {
            info.filename: zf.read(info.filename)
            for info in zf.infolist()
            if info.filename != "manifest.json"
        }
    manifest["tables"]["users"] = int(manifest["tables"]["users"]) + 100
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        for name, blob in members.items():
            zf.writestr(name, blob)
    # 不落侧车：带侧车会先被归档 sha256 拦在 400，就到不了 selfcheck


async def test_restore_selfcheck_failure_leaves_live_db_untouched(tmp_path) -> None:
    db_path = str(tmp_path / "data" / "unified.db")
    panel = _panel_app(db_path)

    made = await _api(panel, "POST", "/api/system/backup")
    assert made.status_code == 200, made.text
    good = _backups_dir(db_path) / made.json()["name"]

    name = "unified-backup-20990101-000000.zip"
    tampered = _backups_dir(db_path) / name
    _archive_with_stale_row_counts(good, tampered)

    # 前提必须成立：归档四道校验全过，selfcheck 只因行数漂移而失败
    core = backup_mod.core_module()
    db_file, manifest, staging = core.load_archive(tampered)
    try:
        verdict = core.selfcheck(db_file, manifest)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    assert verdict["ok"] is False
    assert verdict["checks"]["row_counts"]["ok"] is False
    assert verdict["checks"]["required_tables"]["ok"] is True
    assert verdict["checks"]["integrity_check"] == "ok"

    before = _files_state(db_path)
    r = await _api(panel, "POST", "/api/system/restore", {"name": name, "confirm": True})
    assert r.status_code == 500, f"自检失败必须报错而不是 200：{r.status_code} {r.text}"
    detail = r.json()["detail"]
    assert "归档自检未通过" in detail, detail
    assert "row_counts" in detail, f"拒绝原因应点名失败项：{detail}"
    assert _files_state(db_path) == before, "应用前就拦下的恢复不该动活库（含 -wal/-shm）"

    # 拦下之后库仍可用，且没留下替换用的临时文件
    assert (await _api(panel, "GET", "/api/system/backups")).status_code == 200
    assert not list(Path(db_path).parent.glob(f".{Path(db_path).name}.restore-*.tmp"))


async def test_restore_apply_failure_rolls_back_from_snapshot(tmp_path, monkeypatch) -> None:
    db_path = str(tmp_path / "data" / "unified.db")
    panel = _panel_app(db_path)

    made = await _api(panel, "POST", "/api/system/backup")
    assert made.status_code == 200, made.text
    name = made.json()["name"]

    # 快照之后才有的用户：恢复失败必须回滚回来，否则就是「写了一半」
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO users(username, password_hash, role, created_at) VALUES(?,?,?,?)",
            (LATE_USER, "not-a-real-hash", "viewer", int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()

    real_backup_into = backup_mod._backup_into
    fired = {"count": 0}

    def fail_after_applied(source: Path, target: Path) -> None:
        # 第一次：真把归档写进活库，再报错（模拟切换窗口里被抢占）
        # 第二次：回滚必须走真的在线备份，否则回滚无法落地
        fired["count"] += 1
        if fired["count"] == 1:
            real_backup_into(source, target)
            raise sqlite3.OperationalError("模拟在线恢复中断：目标库持续被占用")
        return real_backup_into(source, target)

    monkeypatch.setattr(backup_mod, "_backup_into", fail_after_applied)

    r = await _api(panel, "POST", "/api/system/restore", {"name": name, "confirm": True})
    assert r.status_code == 500, f"恢复失败必须报错而不是 200：{r.status_code} {r.text}"
    assert "在线恢复失败，已回滚" in r.json()["detail"], r.text
    assert fired["count"] == 2, "回滚没有重新走在线备份（_rollback_db 没被执行）"

    conn = sqlite3.connect(db_path)
    try:
        users = [row[0] for row in conn.execute("SELECT username FROM users")]
        assert users.count(LATE_USER) == 1, f"回滚后应保留快照之后的用户：{users}"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


async def test_restore_with_open_handle_keeps_wal(tmp_path) -> None:
    """恢复时有别的连接开着也要成功（os.replace 在 Windows 上会 WinError 5），且 WAL 不能丢。"""
    db_path = str(tmp_path / "data" / "unified.db")
    panel = _panel_app(db_path)

    made = await _api(panel, "POST", "/api/system/backup")
    assert made.status_code == 200, made.text
    name = made.json()["name"]

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO users(username, password_hash, role, created_at) VALUES(?,?,?,?)",
            (LATE_USER, "not-a-real-hash", "viewer", int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()

    holder = sqlite3.connect(db_path)
    try:
        holder.execute("SELECT COUNT(*) FROM users").fetchone()
        r = await _api(panel, "POST", "/api/system/restore", {"name": name, "confirm": True})
    finally:
        holder.close()

    assert r.status_code == 200, f"有连接开着就不该失败：{r.status_code} {r.text}"
    assert r.json()["selfcheck_ok"] is True

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        users = [row[0] for row in conn.execute("SELECT username FROM users")]
        assert LATE_USER not in users, f"恢复没有生效，快照之后的用户还在：{users}"
    finally:
        conn.close()
