#!/usr/bin/env python3
"""一键启动脚本的脚本级用例（N-01）。

覆盖四件事，全部用临时目录 + 动态端口，不碰仓库现有 `.run/`、`data/`，不需要 GUI
或管理员权限：

1. 前端产物缺失时退出码 2，并给出「前端产物不存在」；
2. 页面端口被占时自动顺延，实际端口写进 `.run/start.json`；
3. 正常启动后状态文件字段齐全（api/web 端口与 pid），`/healthz` 与首页可达；
4. 进程异常退出后再次 `-Stop` 会清掉状态文件。

平台分流：Windows 跑 `start.ps1`（powershell/pwsh），Linux/macOS 跑 `start.sh`。
两者测的是同一套退出码与状态文件契约。Windows 上 WSL 的 bash 看不到 Windows
进程（kill/PID 语义不通用），所以本文件在 Windows 只跑 PowerShell 分支。

start.sh 的 Linux 分支 2026-11-06 在 WSL2（Ubuntu 24.04，bash 5.2，Python 3.12，
Node 20）实跑过：全部用例通过；另补一条「PATH 里没有 node」的降级用例。
Windows 侧的 start.sh 不适用（Windows 版 Python 不认 POSIX 路径）。
注意：`scripts/start.sh` 必须是 LF 行尾，`core.autocrlf=true` 的检出会让 bash
在第 9 行 `set -o pipefail` 处报 `invalid option name`，WSL 直接跑 /mnt/d 下的
工作区文件时会踩到；Linux 侧按 LF 检出即可。

运行：python -m pytest scripts/tests/test_start_scripts.py -v
"""

from __future__ import annotations

import json
import locale
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
CONTROL = REPO / "apps" / "control"
IS_WIN = os.name == "nt"


# ── 环境与临时仓库根 ─────────────────────────────────────────────


def _ps_exe() -> str:
    for name in ("pwsh", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    pytest.skip("[env] 未找到 PowerShell，无法执行 start.ps1")


def _bash_exe() -> str:
    found = shutil.which("bash")
    if not found:
        pytest.skip("[env] 未找到 bash，无法执行 start.sh")
    return found


def _link_dir(src: Path, dst: Path) -> None:
    """临时根里指向真实 apps/control 的目录链接。

    Windows 用 junction（不要求管理员/开发者模式）；POSIX 用 symlink。
    这样脚本里的 $root 是临时目录，但控制面代码仍是工作区里那一份。
    """
    if IS_WIN:
        cmd = (
            f"New-Item -ItemType Junction -Path '{dst}' -Target '{src}' "
            "| Out-Null"
        )
        subprocess.run([_ps_exe(), "-NoProfile", "-Command", cmd], check=True, capture_output=True)
    else:
        os.symlink(src, dst, target_is_directory=True)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", locale.getpreferredencoding(False) or "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _http_status(url: str, timeout: float = 3.0) -> int | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return int(resp.status)
    except Exception:
        return None


def _wait_http(url: str, seconds: float = 10.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _http_status(url) == 200:
            return True
        time.sleep(0.25)
    return False


def _kill_tree(pid: int) -> None:
    if not pid:
        return
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=15)
        else:
            os.kill(pid, 9)
    except Exception:
        pass


@dataclass
class Run:
    """一次脚本调用的结果。"""

    code: int
    output: str
    state: dict | None
    repo: Path
    env: dict

    @property
    def state_file(self) -> Path:
        return self.repo / ".run" / "start.json"


class Harness:
    """在临时根里复制脚本、挂好控制面链接，并准备隔离的库/产物目录。"""

    def __init__(self, tmp_path: Path, *, with_web: bool) -> None:
        self.repo = tmp_path / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / "apps").mkdir(parents=True)
        script = "start.ps1" if IS_WIN else "start.sh"
        shutil.copy2(SCRIPTS / script, self.repo / "scripts" / script)
        shutil.copy2(SCRIPTS / "serve-web.mjs", self.repo / "scripts" / "serve-web.mjs")
        _link_dir(CONTROL, self.repo / "apps" / "control")

        self._known_pids: list[int] = []
        self.data = tmp_path / "data"
        self.data.mkdir()
        self.web = tmp_path / "web"
        if with_web:
            self.web.mkdir()
            (self.web / "index.html").write_text("<!doctype html><title>start test</title>ok", encoding="utf-8")

        self.env = os.environ.copy()
        # 外部环境里已有的 UNIFIED_* 会盖过测试期望，先清掉再用显式值
        for key in list(self.env):
            if key.startswith("UNIFIED_"):
                self.env.pop(key)
        self.env.update(
            {
                "UNIFIED_DATA_DIR": str(self.data),
                "UNIFIED_DB_PATH": str(self.data / "unified.db"),
                "UNIFIED_WEB_DIR": str(self.web),
                "PYTHON": sys.executable,
            }
        )

    def _command(self, args: list[str]) -> list[str]:
        if IS_WIN:
            script = self.repo / "scripts" / "start.ps1"
            return [_ps_exe(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *args]
        script = self.repo / "scripts" / "start.sh"
        return [_bash_exe(), str(script), *args]

    def run(self, *args: str, timeout: float = 150.0) -> Run:
        # 输出落文件而不是管道：脚本用 Start-Process 起的 uvicorn/node 会继承
        # PowerShell 的管道句柄，用 capture_output 时父进程退出了 pipe 仍不 EOF。
        log_dir = self.repo / ".run"
        log_dir.mkdir(parents=True, exist_ok=True)
        out_file = log_dir / "test-script.out.log"
        err_file = log_dir / "test-script.err.log"
        with out_file.open("wb") as fout, err_file.open("wb") as ferr:
            proc = subprocess.run(
                self._command(list(args)),
                cwd=str(self.repo),
                env=self.env,
                stdout=fout,
                stderr=ferr,
                timeout=timeout,
            )
        raw = out_file.read_bytes() + err_file.read_bytes()
        state = None
        state_file = self.repo / ".run" / "start.json"
        if state_file.is_file():
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                state = None
        return Run(proc.returncode, _decode(raw), state, self.repo, self.env)

    def cleanup(self) -> None:
        """停止并兜底清进程；状态文件不存在时按临时数据目录里的 pid 也无法找回，只能尽力。"""
        stop_args = "-Stop" if IS_WIN else "--stop"
        try:
            subprocess.run(
                self._command([stop_args]),
                cwd=str(self.repo),
                env=self.env,
                capture_output=True,
                timeout=60,
            )
        except Exception:
            pass
        for pid in self._known_pids:
            _kill_tree(pid)

    def track(self, *pids: int | None) -> None:
        for pid in pids:
            if isinstance(pid, int) and pid > 0:
                self._known_pids.append(pid)


def _start(h: Harness, *, web_port: int | None = None, api_port: int | None = None) -> Run:
    args: list[str] = []
    if web_port is not None:
        args += (["-WebPort", str(web_port)] if IS_WIN else ["--web-port", str(web_port)])
    if api_port is not None:
        args += (["-ApiPort", str(api_port)] if IS_WIN else ["--api-port", str(api_port)])
    return h.run(*args)


# ── 用例 ────────────────────────────────────────────────────────


def test_missing_web_build_exits_2(tmp_path: Path) -> None:
    """缺 apps/web/out/index.html：退出码 2，提示前端产物不存在，且不留状态文件。"""
    h = Harness(tmp_path, with_web=False)
    try:
        result = _start(h)
        assert result.code == 2, f"期望退出码 2，实际 {result.code}；输出：\n{result.output}"
        assert "前端产物不存在" in result.output
        assert not result.state_file.exists()
    finally:
        h.cleanup()


def test_busy_port_shifts_and_state_records_actual(tmp_path: Path) -> None:
    """页面端口被占：顺延到下一个可用端口，start.json 记的是实际端口。"""
    if not shutil.which("node"):
        pytest.skip("[env] 未检测到 node，脚本会跳过静态前端，无法验证页面端口顺延")

    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    occupied = int(holder.getsockname()[1])
    api = _free_port()
    if api == occupied:
        api = _free_port()
    h = Harness(tmp_path, with_web=True)
    try:
        result = _start(h, web_port=occupied, api_port=api)
        assert result.code == 0, f"期望启动成功，实际 {result.code}；输出：\n{result.output}"
        assert "被占用，改用" in result.output
        assert result.state and result.state["web_port"] > occupied, result.state
        assert _http_status(f"http://127.0.0.1:{result.state['web_port']}/") == 200
        h.track(result.state["api_pid"], result.state["web_pid"])
    finally:
        h.cleanup()
        holder.close()


def test_state_fields_complete_and_healthz_reachable(tmp_path: Path) -> None:
    """正常启动：状态文件字段齐全，/healthz 与首页都可达；-Stop 后状态文件被清掉。"""
    if not shutil.which("node"):
        pytest.skip("[env] 未检测到 node，状态文件里不会有 web_port/web_pid")

    h = Harness(tmp_path, with_web=True)
    web_port = _free_port()
    api_port = _free_port()
    if api_port == web_port:
        api_port = _free_port()
    try:
        result = _start(h, web_port=web_port, api_port=api_port)
        assert result.code == 0, f"期望启动成功，实际 {result.code}；输出：\n{result.output}"
        state = result.state or {}
        for key in ("started_at", "root", "api_port", "web_port", "api_pid", "web_pid"):
            assert key in state, f"状态文件缺字段 {key}：{state}"
        for key in ("api_port", "web_port", "api_pid", "web_pid"):
            assert isinstance(state[key], int) and state[key] > 0, f"{key} 不是有效 pid/端口：{state}"
        h.track(state["api_pid"], state["web_pid"])

        assert _wait_http(f"http://127.0.0.1:{state['api_port']}/healthz", 20)
        assert _http_status(f"http://127.0.0.1:{state['web_port']}/") == 200

        stop_args = "-Stop" if IS_WIN else "--stop"
        stopped = h.run(stop_args)
        assert stopped.code == 0, stopped.output
        assert not result.state_file.exists(), "停止后状态文件应被删除"
        assert _http_status(f"http://127.0.0.1:{state['api_port']}/healthz") is None
    finally:
        h.cleanup()


def test_stop_cleans_state_after_process_exit(tmp_path: Path) -> None:
    """进程先异常退出：状态文件仍在；再执行 -Stop 负责清理，退出码 0。"""
    if not shutil.which("node"):
        pytest.skip("[env] 未检测到 node，无法覆盖带前端进程的完整状态文件")

    h = Harness(tmp_path, with_web=True)
    web_port = _free_port()
    api_port = _free_port()
    if api_port == web_port:
        api_port = _free_port()
    try:
        result = _start(h, web_port=web_port, api_port=api_port)
        assert result.code == 0, result.output
        state = result.state or {}
        h.track(state.get("api_pid"), state.get("web_pid"))
        assert result.state_file.exists()

        _kill_tree(state["api_pid"])
        _kill_tree(state["web_pid"])
        deadline = time.time() + 15
        while time.time() < deadline and _http_status(f"http://127.0.0.1:{state['api_port']}/healthz") == 200:
            time.sleep(0.25)
        assert result.state_file.exists(), "进程退出不会自动删状态文件，要等下一次 Stop/Start"

        stop_args = "-Stop" if IS_WIN else "--stop"
        stopped = h.run(stop_args)
        assert stopped.code == 0, stopped.output
        if IS_WIN:
            assert "状态文件存在，但进程已经退出；已清理状态" in stopped.output
        assert not result.state_file.exists()
        assert _http_status(f"http://127.0.0.1:{state['web_port']}/") is None
    finally:
        h.cleanup()


def test_start_without_node_serves_page_from_control(tmp_path: Path) -> None:
    """POSIX：PATH 里没有 node 时降级为控制面同端口托管页面，状态文件 web_port 为空。"""
    if IS_WIN:
        pytest.skip("[env] Windows 走 start.ps1，node 缺失分支属 start.sh 的 POSIX 路径")

    h = Harness(tmp_path, with_web=True)
    # 只从 PATH 里摘掉 node，Python/curl 等其余命令保持可用
    h.env["PATH"] = os.pathsep.join(
        p for p in h.env.get("PATH", "").split(os.pathsep) if p and not (Path(p) / "node").exists()
    )
    api_port = _free_port()
    try:
        result = _start(h, api_port=api_port)
        assert result.code == 0, f"期望启动成功，实际 {result.code}；输出：\n{result.output}"
        assert "未检测到 Node.js" in result.output
        state = result.state or {}
        assert state.get("web_port") is None, f"没有 node 时 web_port 应为 null：{state}"
        h.track(state.get("api_pid"))
        assert _wait_http(f"http://127.0.0.1:{state['api_port']}/healthz", 20)
        assert _http_status(f"http://127.0.0.1:{api_port}/") == 200, "控制面应同端口托管页面"
    finally:
        h.cleanup()


def test_start_ignores_broken_http_proxy(tmp_path: Path) -> None:
    """POSIX：宿主/CI 设了不通的 HTTP(S)_PROXY 时，回环探针仍应判为健康。"""
    if IS_WIN:
        pytest.skip("[env] 该回归针对 start.sh 的 Python 探针，Windows 走 start.ps1")

    h = Harness(tmp_path, with_web=True)
    dead_proxy = f"http://127.0.0.1:{_free_port()}"
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        h.env[key] = dead_proxy
    for key in ("no_proxy", "NO_PROXY"):
        h.env.pop(key, None)
    api_port = _free_port()
    try:
        result = _start(h, api_port=api_port)
        assert result.code == 0, f"代理不该影响回环探针，实际退出码 {result.code}；输出：\n{result.output}"
        state = result.state or {}
        h.track(state.get("api_pid"), state.get("web_pid"))
        assert _wait_http(f"http://127.0.0.1:{state['api_port']}/healthz", 20)
    finally:
        h.cleanup()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
