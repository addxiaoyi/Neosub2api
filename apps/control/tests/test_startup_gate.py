"""启动失败分流回归：CI 下进程起不来必须 fail（防假绿），本地仅环境性失败才 skip。

决策逻辑直接单测 _startup_down；端到端用嵌套 pytest 真实复现"起不来"：
控制面注入坏的 UNIFIED_SESSION_TTL_SEC，import 时 int() 必炸（实测 traceback 落 stderr）；
假上游用 PYTHONPATH 假 uvicorn 模块，让启动命令必然报 import 错。
嵌套进程显式设 CI，不依赖外层 shell 自带的 CI 值。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import CONTROL, CTRL_PORT, FAKE_PORT, STARTUP_TIMEOUT, _port_free, _startup_down

TEST_FILE = Path(__file__).resolve()

# 子会话端口必须独立于被测套件：套件用 UNIFIED_TEST_CTRL_PORT / UNIFIED_TEST_FAST_PORT
# （conftest 默认 8060/8061，运行期间被外层 fixture 整场占着），子会话默认每次动态挑选，
# 天然排除套件正占的那对端口。要固定端口用 UNIFIED_GATE_CTRL_PORT / UNIFIED_GATE_FAKE_PORT，
# 必须成对设置、不得与套件端口相同——原缺陷就是子会话与套件共用一对端口。
GATE_CTRL_RAW = os.getenv("UNIFIED_GATE_CTRL_PORT", "").strip()
GATE_FAKE_RAW = os.getenv("UNIFIED_GATE_FAKE_PORT", "").strip()

# 环境性失败的样例：端口/配置类错误，stderr 里没有代码级特征
ENV_TAIL = (
    "Traceback (most recent call last):\n"
    '  File "app\\config.py", line 116, in load_settings\n'
    "    session_ttl_sec=int(os.getenv(\"UNIFIED_SESSION_TTL_SEC\", \"86400\")),\n"
    "ValueError: invalid literal for int() with base 10: 'garbage'\n"
)
# 代码级失败的样例：依赖没装/模块名写错
IMPORT_TAIL = (
    "Traceback (most recent call last):\n"
    '  File "<frozen runpy>", line 198, in _run_module_as_main\n'
    "ModuleNotFoundError: No module named 'uvicorn'\n"
)


class _OutcomeRecorded(Exception):
    """模拟 pytest.fail/skip 的抛出行为：不抛的话函数会继续走完所有分支。"""


class _OutcomeLog:
    """替身 pytest.fail/skip：只记录动作和消息，避免断言依赖异常私有类。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def fail(self, msg: str) -> None:
        self.calls.append(("fail", msg))
        raise _OutcomeRecorded(msg)

    def skip(self, msg: str) -> None:
        self.calls.append(("skip", msg))
        raise _OutcomeRecorded(msg)


def _decide(monkeypatch, tmp_path, tail: str, ci: str | None) -> list[tuple[str, str]]:
    """跑一次真实进程退出后的 _startup_down，返回它选了哪个动作。"""
    log = _OutcomeLog()
    monkeypatch.setattr(pytest, "fail", log.fail)
    monkeypatch.setattr(pytest, "skip", log.skip)
    if ci is None:
        monkeypatch.delenv("CI", raising=False)
    else:
        monkeypatch.setenv("CI", ci)

    log_path = tmp_path / "control.stderr.log"
    log_path.write_text(tail, encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(7)"])
    proc.wait(timeout=10)

    try:
        _startup_down("控制面", proc, CTRL_PORT, STARTUP_TIMEOUT, log_path)
    except _OutcomeRecorded:
        pass
    return log.calls


def _actions(calls: list[tuple[str, str]]) -> list[str]:
    return [kind for kind, _ in calls]


def test_startup_down_ci_always_fails(monkeypatch, tmp_path):
    """CI 下启动失败必须 fail 且诊断齐全：进程名、端口、超时、退出码、stderr 尾部。"""
    calls = _decide(monkeypatch, tmp_path, ENV_TAIL, ci="true")
    assert _actions(calls) == ["fail"], calls
    msg = calls[0][1]
    assert "控制面" in msg
    assert str(CTRL_PORT) in msg
    assert f"{STARTUP_TIMEOUT:.0f}s" in msg
    assert "exit code 7" in msg
    assert "stderr 尾部" in msg and "ValueError" in msg


def test_startup_down_local_keeps_skip(monkeypatch, tmp_path):
    """本地 + 环境性错误（stderr 无代码级特征）→ 保留 skip，理由写明该环境不具备条件。"""
    calls = _decide(monkeypatch, tmp_path, ENV_TAIL, ci=None)
    assert _actions(calls) == ["skip"], calls
    assert "该环境不具备条件" in calls[0][1]


def test_startup_down_local_import_error_still_fails(monkeypatch, tmp_path):
    """本地也不能一概 skip：stderr 是 import 错就 fail。"""
    calls = _decide(monkeypatch, tmp_path, IMPORT_TAIL, ci=None)
    assert _actions(calls) == ["fail"], calls
    assert "代码级错误" in calls[0][1]


def test_startup_down_blank_stderr_fails(monkeypatch, tmp_path):
    """stderr 全空时无法区分环境性/代码性 → 统一 fail，宁可本地误报不放过 CI 假绿。"""
    calls = _decide(monkeypatch, tmp_path, "", ci=None)
    assert _actions(calls) == ["fail"], calls
    assert "无法区分" in calls[0][1]


def _nested_env(ctrl_port: int, fake_port: int, ci: str | None, **extra: str) -> dict:
    env = os.environ.copy()
    # 外层 shell 自带 CI=true 会污染嵌套分支，一律以参数为准
    env.pop("CI", None)
    env.update(
        {
            "PYTHONUTF8": "1",
            "UNIFIED_TEST_CTRL_PORT": str(ctrl_port),
            "UNIFIED_TEST_FAST_PORT": str(fake_port),
        }
    )
    if ci is not None:
        env["CI"] = ci
    for key, value in extra.items():
        if key == "PYTHONPATH" and env.get("PYTHONPATH"):
            value = value + os.pathsep + env["PYTHONPATH"]
        env[key] = value
    return env


def _run_nested(env: dict) -> tuple[int, str]:
    """只跑无副作用的靶子用例，让被测会话 fixture 完整走一遍启动/失败路径。"""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            f"{TEST_FILE}::test_nested_smoke",
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
        ],
        cwd=str(CONTROL),
        env=env,
        capture_output=True,
        timeout=120,
    )
    out = proc.stdout.decode("utf-8", errors="replace")
    out += proc.stderr.decode("utf-8", errors="replace")
    return proc.returncode, out


def _gate_busy_hint(port: int) -> str:
    """固定 gate 端口被第三方占用时的单点诊断：说清谁占的、怎么换。"""
    probe = (
        "Get-NetTCPConnection -LocalPort {p} -State Listen"
        if os.name == "nt"
        else "ss -ltnp 'sport = :{p}'"
    ).format(p=port)
    return (
        f"子会话端口 {port} 已被占用，嵌套测试起不来。\n"
        f"  查看占用者：{probe}\n"
        "  换端口：设 UNIFIED_GATE_CTRL_PORT / UNIFIED_GATE_FAKE_PORT 后重跑"
    )


def _gate_ports() -> tuple[int, int]:
    """子会话端口：未设 UNIFIED_GATE_* 时动态挑；显式固定时先校验不复用套件端口、未被占。"""
    if not GATE_CTRL_RAW and not GATE_FAKE_RAW:
        return _pick_ports()
    if not (GATE_CTRL_RAW and GATE_FAKE_RAW):
        pytest.fail(
            "UNIFIED_GATE_CTRL_PORT / UNIFIED_GATE_FAKE_PORT 必须成对设置"
            f"（当前 ctrl={GATE_CTRL_RAW or '未设'} fake={GATE_FAKE_RAW or '未设'}）"
        )
    try:
        ctrl_port, fake_port = int(GATE_CTRL_RAW), int(GATE_FAKE_RAW)
    except ValueError:
        pytest.fail(
            f"UNIFIED_GATE_* 必须是整数端口：ctrl={GATE_CTRL_RAW!r} fake={GATE_FAKE_RAW!r}"
        )
    for port in (ctrl_port, fake_port):
        if port in (CTRL_PORT, FAKE_PORT):
            pytest.fail(
                f"子会话端口 {port} 与被测套件端口复用（套件 UNIFIED_TEST_CTRL_PORT="
                f"{CTRL_PORT} / UNIFIED_TEST_FAST_PORT={FAKE_PORT} 运行期间被外层占着），"
                "必然撞车；改用其他端口，或不设 UNIFIED_GATE_* 走默认动态挑选"
            )
        if not _port_free(port):
            pytest.fail(_gate_busy_hint(port))
    return ctrl_port, fake_port


def _pick_ports() -> tuple[int, int]:
    """动态挑端口：并行会话跑的是同一份 tests/，固定嵌套端口会互抢（实测撞过一次）。"""
    picked: list[int] = []
    while len(picked) < 2:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
        s.close()
        if port not in picked and _port_free(port):
            picked.append(port)
    return picked[0], picked[1]


def _run_nested_with_free_ports(ci: str | None, **extra: str) -> tuple[int, str, int]:
    """用 gate 端口跑嵌套用例；输出出现"已被占用"说明挑完又被别的会话抢走，换端口重试一次。"""
    ctrl_port = fake_port = 0
    for _ in range(2):
        ctrl_port, fake_port = _gate_ports()
        code, out = _run_nested(_nested_env(ctrl_port, fake_port, ci, **extra))
        if "已被占用" not in out:
            break
    return code, out, ctrl_port


@pytest.mark.parametrize("ci, expect_fail", [("true", True), ("", False)], ids=["ci", "local"])
def test_control_plane_down_nested(ci, expect_fail):
    """控制面 import 即崩：CI 下整会话必须 fail，本地环境性失败才允许 skip。"""
    code, out, ctrl_port = _run_nested_with_free_ports(ci, UNIFIED_SESSION_TTL_SEC="garbage")
    if expect_fail:
        assert code != 0, out
        assert "控制面" in out and "启动失败" in out
        assert "CI 下不 skip" in out
        assert "ValueError" in out and "stderr 尾部" in out
        assert str(ctrl_port) in out
        assert "skipped" not in out
    else:
        assert code == 0, out
        assert "该环境不具备条件" in out
        assert "1 skipped" in out


@pytest.mark.parametrize("ci", ["true", ""], ids=["ci", "local_import_error"])
def test_fake_upstream_import_error_never_skips(tmp_path, ci):
    """假上游启动命令必然报 import 错：无论 CI 还是本地都 fail，不允许 skip 假绿。"""
    stub = tmp_path / "py-stub"
    stub.mkdir()
    (stub / "uvicorn.py").write_text(
        "import sys\n"
        "sys.stderr.write('sabotage: fake uvicorn refuses to start\\n')\n"
        "raise ImportError('sabotage uvicorn is not importable')\n",
        encoding="utf-8",
    )
    code, out, _ctrl_port = _run_nested_with_free_ports(ci, PYTHONPATH=str(stub))
    assert code != 0, out
    assert "假上游" in out and "启动失败" in out
    assert "ImportError" in out and "stderr 尾部" in out
    assert "代码级错误" in out
    assert "skipped" not in out


def test_gate_ports_isolated_from_suite_config():
    """回归：子会话端口与被测套件（外层正占着的 UNIFIED_TEST_*）必须是两对，不复用不撞车。

    原缺陷是子会话端口与套件共用一对（外层配 8092/8093 时 4 个嵌套用例全报端口被占），
    这里锁死"子会话端口 ≠ 套件端口"不变量，并真实跑一次嵌套会话验证端到端不冲突。
    """
    ctrl_port, fake_port = _gate_ports()
    assert (ctrl_port, fake_port) != (CTRL_PORT, FAKE_PORT), (
        f"子会话端口复用了套件端口（ctrl={ctrl_port} fake={fake_port}），外层占着必然撞车"
    )
    env = _nested_env(ctrl_port, fake_port, ci="")
    assert env["UNIFIED_TEST_CTRL_PORT"] != str(CTRL_PORT)
    assert env["UNIFIED_TEST_FAST_PORT"] != str(FAKE_PORT)
    code, out = _run_nested(env)
    assert code == 0, out


def test_nested_smoke():
    """嵌套 pytest 的落点用例：只用于拉起会话 fixture，本身不做业务断言。"""
    assert True
