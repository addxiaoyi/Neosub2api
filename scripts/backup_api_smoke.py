#!/usr/bin/env python3
"""API 层备份恢复演练（CI 用）：起一个临时 control 实例，把四道关各打一遍。

覆盖 admin-only、缺 confirm、文件名穿越、归档校验（zip/manifest/成员 sha256/
integrity_check），以及正常 backup → restore 往返。全部走真实 HTTP + 真实 sqlite，
不依赖任何外部服务；端口随机挑，跑完销毁实例并确认端口没有残留监听。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
CONTROL = REPO / "apps" / "control"
ADMIN_PASS = "smoke-admin-pass"
JUNK = "unified-backup-19991231-235959.zip"
TRAVERSAL = "../../unified-backup-20990101-000000.zip"
LATE_USER = "u-after-smoke-snapshot"


def _fail(message: str) -> None:
    print(f"::error::备份恢复 API 演练失败：{message}")
    raise SystemExit(1)


def _pick_port() -> int:
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
    finally:
        probe.close()


def _port_free(port: int) -> bool:
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", port))
    except OSError:
        return False
    finally:
        probe.close()
    return True


def _wait_up(base: str, proc: subprocess.Popen, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(f"{base}/healthz", timeout=1.0, trust_env=False).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        if proc.poll() is not None:
            break
        time.sleep(0.2)
    _fail("控制面没能在 20s 内就绪")


def _login(base: str, username: str, password: str) -> httpx.Client:
    client = httpx.Client(base_url=base, trust_env=False, timeout=20.0)
    r = client.post("/api/login", json={"username": username, "password": password})
    if r.status_code != 200:
        _fail(f"登录 {username} 失败：{r.status_code} {r.text}")
    return client


def _files_digest(db: Path) -> dict[str, str | None]:
    """db / -wal / -shm 的 sha256；None = 文件不存在。"""
    state: dict[str, str | None] = {}
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{db}{suffix}")
        state[suffix or "db"] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    return state


def _write_junk_archive(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)


def _write_zip_without_manifest(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("db/unified.db", b"no manifest here")


def _write_zip_with_bad_member_sha(path: Path) -> None:
    """结构齐全、integrity_check 也写 ok，但成员 sha256 对不上：必须卡在成员校验。"""
    manifest = {
        "manifest_version": 1,
        "database": {
            "member": "db/unified.db",
            "size_bytes": 8,
            "sha256": "0" * 64,
            "integrity_check": "ok",
        },
        "files": [],
        "tables": {},
        "totals": {"tables": 0, "rows": 0},
        "demo": {"excluded": True},
    }
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("db/unified.db", b"not-a-db")


def _write_zip_with_bad_integrity(path: Path) -> None:
    """成员 sha256 全对得上，但解出来的库过不了 PRAGMA integrity_check。"""
    blob = b"SQLite format 3 but truncated \x00" * 16
    manifest = {
        "manifest_version": 1,
        "database": {
            "member": "db/unified.db",
            "size_bytes": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest(),
            "integrity_check": "ok",
        },
        "files": [],
        "tables": {},
        "totals": {"tables": 0, "rows": 0},
        "demo": {"excluded": True},
    }
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("db/unified.db", blob)


def _expect_400(client: httpx.Client, payload: dict, why: str) -> None:
    """只断言契约层的状态码：拒绝原因文案随实现走，不写成固定字符串。"""
    r = client.post("/api/system/restore", json=payload)
    if r.status_code != 400:
        _fail(f"{why}：应 400，实际 {r.status_code} {r.text}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="u2a-backup-smoke-"))
    data = tmp / "data"
    data.mkdir()
    db = data / "unified.db"
    backups = data / "backups"
    port = _pick_port()
    base = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    env.update(
        {
            "UNIFIED_DATA_DIR": str(data),
            "UNIFIED_DB_PATH": str(db),
            "UNIFIED_ADMIN_USER": "admin",
            "UNIFIED_ADMIN_PASSWORD": ADMIN_PASS,
            "UNIFIED_WEB_DIR": str(tmp / "no-web"),
            "UNIFIED_ALLOW_ANONYMOUS_GATEWAY": "false",
            "UNIFIED_ENABLE_DOCS": "0",
            "GROK_REGISTER_EXECUTOR_URL": "",
        }
    )
    log = (tmp / "control.log").open("wb")
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
        ],
        cwd=str(CONTROL),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=log,
    )
    admin: httpx.Client | None = None
    viewer: httpx.Client | None = None
    code = 0
    try:
        _wait_up(base, proc)
        admin = _login(base, "admin", ADMIN_PASS)

        # 第一关：admin-only
        made = admin.post(
            "/api/users",
            json={"username": "smoke-viewer", "password": "viewer-pass-1", "role": "viewer"},
        )
        if made.status_code not in (200, 409):
            _fail(f"创建 viewer 失败：{made.status_code} {made.text}")
        viewer = _login(base, "smoke-viewer", "viewer-pass-1")
        for method, path, payload in (
            ("GET", "/api/system/backups", None),
            ("POST", "/api/system/backup", None),
            ("POST", "/api/system/restore", {"name": JUNK, "confirm": True}),
        ):
            r = viewer.request(method, path, json=payload)
            if r.status_code != 403:
                _fail(f"非 admin 访问 {method} {path} 应 403，实际 {r.status_code} {r.text}")

        # 第二关 / 第三关：缺 confirm、文件名穿越
        _expect_400(admin, {"name": JUNK}, "缺 confirm")
        _expect_400(admin, {"name": TRAVERSAL, "confirm": True}, "路径穿越文件名")

        # 第四关：坏归档一律 400，且当前库一个字节都不动
        backups.mkdir(parents=True, exist_ok=True)
        junk = backups / JUNK
        before = _files_digest(db)
        try:
            _write_junk_archive(junk, b"definitely not a zip archive")
            _expect_400(admin, {"name": JUNK, "confirm": True}, "非 zip 归档")

            _write_zip_without_manifest(junk)
            _expect_400(admin, {"name": JUNK, "confirm": True}, "缺 manifest 的 zip")

            _write_zip_with_bad_member_sha(junk)
            _expect_400(admin, {"name": JUNK, "confirm": True}, "成员 sha256 不符")

            _write_zip_with_bad_integrity(junk)
            _expect_400(admin, {"name": JUNK, "confirm": True}, "库 integrity_check 不通过")
        finally:
            junk.unlink(missing_ok=True)
            Path(f"{junk}.sha256").unlink(missing_ok=True)
        if _files_digest(db) != before:
            _fail("坏归档被拒后当前库仍被改动（字节不一致）")

        # 正常往返：备份 → 制造差异 → 恢复 → 差异被覆盖
        created = admin.post("/api/system/backup")
        if created.status_code != 200:
            _fail(f"备份失败：{created.status_code} {created.text}")
        name = str(created.json()["name"])
        if not (backups / name).is_file():
            _fail(f"备份产物不存在：{name}")

        added = admin.post(
            "/api/users",
            json={"username": LATE_USER, "password": "late-pass-1", "role": "viewer"},
        )
        if added.status_code != 200:
            _fail(f"快照后建用户失败：{added.status_code} {added.text}")

        restored = admin.post("/api/system/restore", json={"name": name, "confirm": True})
        if restored.status_code != 200:
            _fail(f"正常恢复应 200，实际 {restored.status_code} {restored.text}")
        body = restored.json()
        if body.get("ok") is not True:
            _fail(f"恢复结果不自洽：{body}")
        names = [item["username"] for item in admin.get("/api/users").json()]
        if LATE_USER in names or "admin" not in names:
            _fail(f"恢复没有真正覆盖当前库，用户列表={names}")

        print(
            f"备份恢复 API 演练通过：admin-only / 缺 confirm / 路径穿越 / 坏归档 / 正常往返，"
            f"快照={name}，恢复前快照={body.get('pre_restore_backup')}"
        )
    except SystemExit as exc:  # _fail 的统一出口
        code = int(exc.code or 1)
    finally:
        for client in (viewer, admin):
            if client is not None:
                client.close()
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log.close()
        if not _port_free(port):
            print(f"::error::备份恢复 API 演练失败：端口 {port} 仍有残留监听")
            code = code or 1
        shutil.rmtree(tmp, ignore_errors=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
