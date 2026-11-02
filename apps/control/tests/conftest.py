"""pytest 基建：起临时控制面（端口 8020）+ 假上游（端口 8021），fixture 化 httpx client。

端口 8020 避开开发实例（8010），假上游走 8021 避免冲突。
所有业务代码引用通过 importlib 动态 reload，防止上一个测试残留状态污染。
"""

from __future__ import annotations

import multiprocessing
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

import httpx
import pytest

REPO = Path(__file__).resolve().parents[3]
CONTROL = REPO / "apps" / "control"
PY = REPO / ".runtime" / "python" / "python.exe"
FAKE_PORT = 8021
CTRL_PORT = 8020


def _port_free(port: int) -> bool:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
    except OSError:
        return False
    finally:
        s.close()
    return True


def _wait_up(url: str, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(url, timeout=1.0)
            if r.status_code < 500:
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


@pytest.fixture(scope="session", autouse=True)
def _bring_up_services(tmp_path_factory) -> Iterator[None]:
    """会话级：起假上游 + 控制面；退出时强杀。"""
    if not _port_free(FAKE_PORT):
        pytest.skip(f"端口 {FAKE_PORT} 已被占用")
    if not _port_free(CTRL_PORT):
        pytest.skip(f"端口 {CTRL_PORT} 已被占用")

    tmp = tmp_path_factory.mktemp("unified-test")
    data_dir = tmp / "data"
    data_dir.mkdir()

    # 起假上游
    fake = subprocess.Popen(
        [str(PY), str(CONTROL / "scripts" / "fake_upstream.py"), str(FAKE_PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    env = os.environ.copy()
    env.update(
        {
            "UNIFIED_DATA_DIR": str(data_dir),
            "UNIFIED_DB_PATH": str(data_dir / "unified.db"),
            "UNIFIED_ADMIN_USER": "admin",
            "UNIFIED_ADMIN_PASSWORD": "test",
            # 所有默认 provider 指向假上游
            "CODEBUDDY_BASE": f"http://127.0.0.1:{FAKE_PORT}",
            "OPENCODE_UPSTREAM_BASE": f"http://127.0.0.1:{FAKE_PORT}",
            # 禁用前端静态目录（避免静态文件 mount）
            "UNIFIED_WEB_DIR": str(tmp / "no-web"),
            # 匿名网关默认关（测试里按需开）
            "UNIFIED_ALLOW_ANONYMOUS_GATEWAY": "false",
            # 测试把上游指向本机假服务，必须显式放开地址守卫
            "UNIFIED_ALLOW_PRIVATE_UPSTREAMS": "1",
            # 禁用 grok 注册器（会真跑浏览器）
            "GROK_REGISTER_EXECUTOR_URL": "",
        }
    )

    ctrl = subprocess.Popen(
        [str(PY), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(CTRL_PORT), "--log-level", "warning"],
        cwd=str(CONTROL),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        if not _wait_up(f"http://127.0.0.1:{FAKE_PORT}/healthz"):
            pytest.skip("假上游启动失败")
        if not _wait_up(f"http://127.0.0.1:{CTRL_PORT}/healthz"):
            pytest.skip("控制面启动失败")
        yield
    finally:
        for p in (ctrl, fake):
            try:
                p.terminate()
            except Exception:
                pass
        for p in (ctrl, fake):
            try:
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
                try:
                    p.wait(timeout=2)
                except Exception:
                    pass


@pytest.fixture(scope="session")
def base_url() -> str:
    return f"http://127.0.0.1:{CTRL_PORT}"


@pytest.fixture(scope="session")
def anon(base_url) -> httpx.Client:
    """无登录的 httpx client（trust_env=False 防止本地代理劫持 localhost）。"""
    return httpx.Client(base_url=base_url, trust_env=False, timeout=10.0)


@pytest.fixture(scope="session")
def admin(anon) -> httpx.Client:
    """管理员登录后的 httpx client（带 session cookie）。"""
    r = anon.post("/api/login", json={"username": "admin", "password": "test"})
    assert r.status_code == 200, f"登录失败: {r.status_code} {r.text}"
    return anon


@pytest.fixture(scope="session")
def fake_base() -> str:
    return f"http://127.0.0.1:{FAKE_PORT}"


@pytest.fixture(scope="function")
def db_path(base_url, admin) -> str:
    """临时 SQLite 路径（从 /healthz 取）。"""
    r = admin.get("/healthz")
    assert r.status_code == 200
    return r.json()["db"]
