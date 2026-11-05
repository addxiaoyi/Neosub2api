"""pytest 基建：起临时控制面（默认 8060）+ 假上游（默认 8061），fixture 化 httpx client。

端口可用 UNIFIED_TEST_CTRL_PORT / UNIFIED_TEST_FAST_PORT（兼容旧的 ...FAKE_PORT）覆盖，
避开开发实例（8010/8011）。
所有业务代码引用通过 importlib 动态 reload，防止上一个测试残留状态污染。
"""

from __future__ import annotations

import locale
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
# 起子进程就用当前解释器，不绑定 Windows 内嵌运行时的目录布局
PY = sys.executable
# FAST_PORT 是审计后约定的端口名；FAKE_PORT 为历史写法，保留兼容
FAKE_PORT = int(os.getenv("UNIFIED_TEST_FAST_PORT", os.getenv("UNIFIED_TEST_FAKE_PORT", "8061")))
CTRL_PORT = int(os.getenv("UNIFIED_TEST_CTRL_PORT", "8060"))
# 就绪等待上限（秒）；启动失败的诊断消息里报同一个数
STARTUP_TIMEOUT = 15.0
# 启动失败时贴多少行 stderr 尾部
STDERR_TAIL_LINES = 20
# 代码级失败判据：stderr 见到这些就是仓库/依赖坏了，任何环境都 fail
CODE_ERROR_MARKS = (
    "ImportError",
    "ModuleNotFoundError",
    "SyntaxError",
    "FileNotFoundError",
    "No module named",
)


def _port_free(port: int) -> bool:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
    except OSError:
        return False
    finally:
        s.close()
    return True


def _free_port() -> int:
    """绑定 0 端口，由内核分配一个此刻没被占用的端口（固定端口被占不该让用例静默消失）。"""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def _wait_up(url: str, timeout: float = 15.0, proc: subprocess.Popen | None = None) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(url, timeout=1.0, trust_env=False)
            if r.status_code < 500:
                return True
        except Exception:
            pass
        # 进程已经退出就别等满超时：失败要带着诊断尽快抛出去
        if proc is not None and proc.poll() is not None:
            return False
        time.sleep(0.2)
    return False


def _port_busy_hint(port: int) -> str:
    """只给识别命令，不解析 PID（避免引入 psutil 这类平台相关依赖）。"""
    probe = (
        "Get-NetTCPConnection -LocalPort {p} -State Listen"
        if os.name == "nt"
        else "ss -ltnp 'sport = :{p}'"
    ).format(p=port)
    return (
        f"测试端口 {port} 已被占用，无法起假上游/控制面。\n"
        f"  查看占用者：{probe}\n"
        "  换端口自救：设 UNIFIED_TEST_CTRL_PORT / UNIFIED_TEST_FAST_PORT 后重跑"
    )


def _startup_down(
    name: str, proc: subprocess.Popen, port: int, timeout: float, log_path: Path
) -> None:
    """启动失败的统一出口。

    分流口径与上面 _port_busy_hint 那段一致：同一个 CI 环境变量、同一对 skip/fail 动作，
    但方向相反（那段 CI skip/本地 fail，这里 CI 必 fail）——进程起不来大概率是导入错误、
    依赖缺失这类代码问题，CI 下整会话 skip 等于把"代码崩了"伪装成"这环境跑不了"的假绿。
    取舍：stderr 为空或读不到时无法区分环境性/代码性失败，统一按 fail 处理，
    宁可本地误报也不放过 CI 假绿；本地只有 stderr 明确不含代码级错误才允许 skip。
    """
    readable = True
    try:
        raw = log_path.read_bytes()
    except OSError as exc:
        raw, readable = f"(读取 stderr 日志失败: {exc})".encode("utf-8"), False

    # 子进程 stderr 落文件按系统 locale 编码（Windows 常见 cp936），先试 UTF-8 再回退
    tail = ""
    for enc in ("utf-8", locale.getpreferredencoding(False) or "utf-8"):
        try:
            tail = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        tail = raw.decode("utf-8", errors="replace")
    tail = tail.strip()
    tail = "\n".join(tail.splitlines()[-STDERR_TAIL_LINES:]) if tail else "(stderr 为空)"

    state = (
        f"进程已退出，exit code {proc.returncode}"
        if proc.poll() is not None
        else "进程仍在运行"
    )
    detail = (
        f"{name} 未能在 {timeout:.0f}s 内就绪（端口 {port}，{state}）\n"
        f"  stderr 尾部（{log_path.name}）：\n{tail}"
    )
    if any(mark in tail for mark in CODE_ERROR_MARKS):
        pytest.fail(
            f"{name} 启动失败：stderr 是代码级错误（import/语法/缺文件），本地也不 skip\n{detail}"
        )
    if os.environ.get("CI"):
        pytest.fail(f"{name} 启动失败：CI 下不 skip，避免假绿\n{detail}")
    if not readable or tail == "(stderr 为空)":
        pytest.fail(
            f"{name} 启动失败：stderr 无有效信息，无法区分环境性/代码性，按 fail 处理"
            f"（宁可本地误报也不放过 CI 假绿）\n{detail}"
        )
    pytest.skip(
        f"{name} 启动失败：该环境不具备条件（stderr 未见代码级错误），本地跳过\n{detail}"
    )


@pytest.fixture(scope="session", autouse=True)
def _bring_up_services(tmp_path_factory) -> Iterator[dict]:
    """会话级：起假上游 + 控制面；退出时强杀。"""
    # 端口被占时本地直接失败：整会话 skip 会伪装成全绿
    for port in (FAKE_PORT, CTRL_PORT):
        if not _port_free(port):
            hint = _port_busy_hint(port)
            if os.environ.get("CI"):
                pytest.skip(hint)
            pytest.fail(hint)

    tmp = tmp_path_factory.mktemp("unified-test")
    data_dir = tmp / "data"
    data_dir.mkdir()

    # stderr 落文件而不是 DEVNULL：启动失败时要靠尾部判定是代码错还是环境错
    fake_log = tmp / "fake_upstream.stderr.log"
    fake_err = fake_log.open("wb")
    fake = subprocess.Popen(
        [str(PY), str(CONTROL / "scripts" / "fake_upstream.py"), str(FAKE_PORT)],
        stdout=subprocess.DEVNULL,
        stderr=fake_err,
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

    ctrl_log = tmp / "control_plane.stderr.log"
    ctrl_err = ctrl_log.open("wb")
    ctrl = subprocess.Popen(
        [str(PY), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(CTRL_PORT), "--log-level", "warning"],
        cwd=str(CONTROL),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=ctrl_err,
    )

    try:
        if not _wait_up(f"http://127.0.0.1:{FAKE_PORT}/healthz", STARTUP_TIMEOUT, fake):
            _startup_down("假上游", fake, FAKE_PORT, STARTUP_TIMEOUT, fake_log)
        if not _wait_up(f"http://127.0.0.1:{CTRL_PORT}/healthz", STARTUP_TIMEOUT, ctrl):
            _startup_down("控制面", ctrl, CTRL_PORT, STARTUP_TIMEOUT, ctrl_log)
        # /healthz 不再回传库路径，测试直接拿临时 data_dir（S-09）
        yield {"data_dir": data_dir}
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
        # 进程都退了再关句柄，子进程退出前可能还在往里写
        fake_err.close()
        ctrl_err.close()


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


@pytest.fixture(scope="session")
def db_path(_bring_up_services) -> str:
    """临时 SQLite 路径（由启动 fixture 提供，不再从 /healthz 取）。"""
    return str(Path(_bring_up_services["data_dir"]) / "unified.db")
