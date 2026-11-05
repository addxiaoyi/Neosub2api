"""执行器与看门狗：假仓库树 + 注入的 restart/health，真跑换目录、回滚、重试与脱敏。"""

from __future__ import annotations

import hashlib
import io
import json
import os
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.services import update_boot, update_executor as ex
from app.services import update_manifest as mf

STAMP = "20261105-120000"


@dataclass
class Cfg:
    data_dir: Path
    db_path: Path
    port: int = 18999
    update_enabled: bool = True
    update_manifest_url: str = "https://example.com/release-manifest.json"
    update_pubkey: Path = Path("deploy/release-pubkey.pem")
    update_check_ttl_sec: int = 21600
    update_max_bytes: int = 268435456
    service_name: str = ""


def make_tree(root: Path, marker: str) -> None:
    files = {
        "apps/control/app/main.py": f"# {marker}\n",
        "apps/control/app/routers/branch.py": f"# {marker}\n",
        "apps/web/out/index.html": f"<html>{marker}</html>",
        "scripts/backup_core.py": f"# {marker}\n",
        "CHANGELOG.md": f"# changelog {marker}\n",
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def release_zip(version: str = "0.2.0", marker: str = "new") -> tuple[bytes, dict]:
    files = {
        "apps/control/app/main.py": f"# {marker}\n".encode(),
        "apps/control/app/routers/branch.py": f"# {marker}\n".encode(),
        "apps/web/out/index.html": f"<html>{marker}</html>".encode(),
        "scripts/backup_core.py": f"# {marker}\n".encode(),
        "CHANGELOG.md": f"# changelog {marker}\n".encode(),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path, data in files.items():
            bundle.writestr(path, data)
    manifest = {
        "manifest_version": 1,
        "version": version,
        "channel": "stable",
        "platform": "any",
        "issued_at": 1793877187,
        "commit": "deadbeef",
        "min_current_version": "0.0.0",
        "files": [
            {"path": path, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
            for path, data in sorted(files.items())
        ],
    }
    return buffer.getvalue(), mf.validate_manifest(json.dumps(manifest))


def build_executor(tmp_root: Path, cfg: Cfg, **kwargs) -> tuple[ex.UpdateExecutor, dict]:
    payload, manifest = release_zip()

    def fake_download(url: str, dest: Path, limit: int) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)

    kwargs.setdefault("sleep", lambda _seconds: None)
    health_timeout = kwargs.pop("health_timeout", 1.0)
    executor = ex.UpdateExecutor(
        cfg,
        repo_root=tmp_root,
        download=fake_download,
        health_timeout=health_timeout,
        **kwargs,
    )
    return executor, manifest


def wait_done(executor: ex.UpdateExecutor, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = executor.status()
        if status["state"] != "running":
            return status
        time.sleep(0.02)
    raise AssertionError(f"更新线程没有结束：{executor.status()}")


@pytest.fixture()
def repo(tmp_path: Path) -> tuple[Path, Cfg]:
    root = tmp_path / "repo"
    root.mkdir()
    make_tree(root, "old")
    data = tmp_path / "data"
    data.mkdir()
    cfg = Cfg(data_dir=data, db_path=data / "unified.db")
    return root, cfg


def test_successful_swap_replaces_code(repo):
    root, cfg = repo
    restarts = []
    seen = {}

    def fake_restart(_settings):
        restarts.append(1)
        # 重启那一刻的 pending 必须还是「未尝试启动」，由新进程自己置 true
        seen["pending"] = ex.read_json(cfg.data_dir / "updates" / "pending.json")

    executor, manifest = build_executor(root, cfg, restart=fake_restart, health=lambda _p: True)
    executor.apply(manifest, cfg.update_manifest_url)
    status = wait_done(executor)

    assert status["state"] == "done"
    assert status["progress"] == 100
    assert (root / "apps/control/app/main.py").read_text(encoding="utf-8") == "# new\n"
    assert (root / "apps/web/out/index.html").read_text(encoding="utf-8") == "<html>new</html>"
    assert (root / "scripts/backup_core.py").read_text(encoding="utf-8") == "# new\n"
    assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == "# changelog new\n"
    olds = list((root / "apps/control").glob("app.old-*"))
    assert len(olds) == 1
    assert (olds[0] / "main.py").read_text(encoding="utf-8") == "# old\n"
    assert not (cfg.data_dir / "updates" / "pending.json").exists()
    assert restarts == [1]
    assert seen["pending"]["boot_attempted"] is False
    assert seen["pending"]["moves"], "看门狗要靠 moves 整组回滚"
    assert executor.last_swap_file.is_file()
    # 逐步状态都落过盘，前端刷新后仍能看到日志尾部
    assert status["log_tail"]
    assert all(len(line) <= 500 for line in status["log_tail"])


def test_health_failure_rolls_back(repo):
    root, cfg = repo
    restarts = []
    executor, manifest = build_executor(
        root,
        cfg,
        restart=lambda _s: restarts.append(1),
        health=lambda _p: False,
        health_timeout=0.05,
    )
    executor.apply(manifest, cfg.update_manifest_url)
    status = wait_done(executor)

    assert status["state"] == "failed"
    assert (root / "apps/control/app/main.py").read_text(encoding="utf-8") == "# old\n"
    assert (root / "apps/web/out/index.html").read_text(encoding="utf-8") == "<html>old</html>"
    assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == "# changelog old\n"
    assert not list((root / "apps/control").glob("app.old-*"))
    assert not (cfg.data_dir / "updates" / "pending.json").exists()
    assert restarts == [1, 1]
    assert "回滚" in status["error"]


def test_permission_error_retries_three_times(repo, monkeypatch):
    root, cfg = repo
    real_rename = os.rename
    calls = {"attempts": 0, "delays": []}

    def flaky_rename(src, dst):
        # 只对「app 换出到 app.old-*」这一步失败，其他 rename 正常
        if "app.old-" in str(dst):
            calls["attempts"] += 1
            if calls["attempts"] <= 2:
                raise PermissionError(13, "文件被占用", str(dst))
        return real_rename(src, dst)

    def record_sleep(seconds):
        calls["delays"].append(seconds)

    monkeypatch.setattr(ex.os, "rename", flaky_rename)
    executor, manifest = build_executor(
        root, cfg, restart=lambda _s: None, health=lambda _p: True, sleep=record_sleep
    )
    executor.apply(manifest, cfg.update_manifest_url)
    status = wait_done(executor)

    assert status["state"] == "done"
    assert calls["attempts"] == 3
    assert calls["delays"] == [ex.RENAME_DELAY_SEC, ex.RENAME_DELAY_SEC]
    assert (root / "apps/control/app/main.py").read_text(encoding="utf-8") == "# new\n"


def test_watchdog_marks_first_boot_attempt(repo):
    """第一次拉起新进程：只把 boot_attempted 置 true，绝不能立刻回滚自己。"""
    root, cfg = repo
    work = cfg.data_dir / "updates"
    work.mkdir(parents=True, exist_ok=True)
    old = root / "apps/control" / f"app.old-{STAMP}"
    os.rename(root / "apps/control/app", old)
    (root / "apps/control/app").mkdir()
    (root / "apps/control/app/main.py").write_text("# new\n", encoding="utf-8")
    pending = {
        "from_dir": str(root / "apps/control/app"),
        "rollback_dir": str(old),
        "target_version": "0.2.0",
        "boot_attempted": False,
        "moves": [{"mode": "rename", "target": str(root / "apps/control/app"), "old": str(old)}],
    }
    ex.write_json(work / "pending.json", pending)
    exits: list[int] = []

    result = update_boot.reconcile(
        cfg, repo_root=root, exit_fn=lambda _s: exits.append(1), sleep=lambda _s: None
    )

    assert result == {"pending": True, "rolled_back": False}
    assert exits == []
    assert ex.read_json(work / "pending.json")["boot_attempted"] is True
    assert (root / "apps/control/app/main.py").read_text(encoding="utf-8") == "# new\n"


def test_watchdog_rolls_back_on_second_boot(repo):
    root, cfg = repo
    work = cfg.data_dir / "updates"
    work.mkdir(parents=True, exist_ok=True)
    old = root / "apps/control" / f"app.old-{STAMP}"
    os.rename(root / "apps/control/app", old)
    (root / "apps/control/app").mkdir()
    (root / "apps/control/app/main.py").write_text("# broken\n", encoding="utf-8")
    ex.write_json(
        work / "pending.json",
        {
            "from_dir": str(root / "apps/control/app"),
            "rollback_dir": str(old),
            "target_version": "0.2.0",
            "boot_attempted": True,
            "moves": [{"mode": "rename", "target": str(root / "apps/control/app"), "old": str(old)}],
        },
    )
    ex.write_json(work / "job.json", {"state": "running", "step": "start", "progress": 80})
    exits: list[int] = []

    result = update_boot.reconcile(
        cfg, repo_root=root, exit_fn=lambda _s: exits.append(1), sleep=lambda _s: None
    )

    assert result == {"pending": False, "rolled_back": True}
    assert exits == [1]
    assert (root / "apps/control/app/main.py").read_text(encoding="utf-8") == "# old\n"
    assert not (work / "pending.json").exists()
    job = ex.read_json(work / "job.json")
    assert job["state"] == "failed"
    assert "回滚" in job["error"]


def test_log_tail_is_masked(repo):
    root, cfg = repo

    def leaky_download(url: str, dest: Path, limit: int) -> None:
        raise RuntimeError(
            "403 Authorization: Bearer sk-live-9f8e7d6c5b4a and AKIAIOSFODNN7EXAMPLE"
        )

    executor = ex.UpdateExecutor(
        cfg,
        repo_root=root,
        download=leaky_download,
        restart=lambda _s: None,
        health=lambda _p: True,
        sleep=lambda _s: None,
    )
    _, manifest = release_zip()
    executor.apply(manifest, cfg.update_manifest_url)
    status = wait_done(executor)

    tail = "\n".join(status["log_tail"])
    assert status["state"] == "failed"
    assert "sk-live-9f8e7d6c5b4a" not in tail
    assert "AKIAIOSFODNN7EXAMPLE" not in tail
    assert "***" in tail
    # 失败发生在换目录之前，旧代码必须原样还在
    assert (root / "apps/control/app/main.py").read_text(encoding="utf-8") == "# old\n"
    assert not list((root / "apps/control").glob("app.old-*"))


def test_rollback_requires_previous_swap(repo):
    root, cfg = repo
    executor = ex.UpdateExecutor(cfg, repo_root=root, restart=lambda _s: None, sleep=lambda _s: None)
    with pytest.raises(mf.UpdateError) as caught:
        executor.rollback()
    assert "没有可回滚" in str(caught.value)
