"""系统信息面板：更新日志（仓库根 CHANGELOG.md）与更新状态。

本仓没有在线更新能力：update-status / check-update 都如实报告没有新版本，
check-update 复用 update-status 的同一份摘要（available/current_version/reason），
不另造一套「有新版本可用」的语义。
"""

import re
import sqlite3
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Request

from .. import __version__
from ..security import current_user

router = APIRouter(prefix="/api/system", tags=["system"])

CHANGELOG_FILE = "CHANGELOG.md"
# 本文件在 apps/control/app/routers/ 下，仓库根是上溯四级
CHANGELOG_PATH = Path(__file__).resolve().parents[4] / CHANGELOG_FILE
MAX_VERSIONS = 50

_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_UNRELEASED = {"未发布", "unreleased", "Unreleased"}


def parse_changelog(markdown: str) -> list[dict]:
    """`## 版本 [ - 日期]` + `### 分类` + `- 条目`；二级缩进记为 level 1。"""
    versions: list[dict] = []
    version: dict | None = None
    section: dict | None = None
    for raw in markdown.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            head = line[3:].strip()
            date = _DATE.search(head)
            name = _DATE.sub("", head).strip(" -—") or "未发布"
            version = {
                "version": name,
                "date": date.group(1) if date else "",
                "unreleased": name in _UNRELEASED,
                "sections": [],
            }
            versions.append(version)
            section = None
        elif line.startswith("### ") and version is not None:
            section = {"title": line[4:].strip(), "items": []}
            version["sections"].append(section)
        elif section is not None and line.lstrip().startswith(("- ", "* ")):
            text = line.lstrip()[2:].strip()
            if text:
                section["items"].append({"level": 1 if len(line) - len(line.lstrip()) >= 2 else 0, "text": text})
    return versions


def read_changelog(path: Path = CHANGELOG_PATH) -> dict:
    """文件缺失/不可读时返回 available=false + 空列表，绝不 500。"""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"available": False, "error": f"{CHANGELOG_FILE} 不存在", "path": CHANGELOG_FILE,
                "versions": [], "current": __version__}
    except OSError as exc:
        return {"available": False, "error": f"{CHANGELOG_FILE} 读取失败：{exc.strerror or exc}", "path": CHANGELOG_FILE,
                "versions": [], "current": __version__}
    versions = parse_changelog(text)
    return {
        "available": True,
        "path": CHANGELOG_FILE,
        "total": len(versions),
        "versions": versions[:MAX_VERSIONS],
        "truncated": len(versions) > MAX_VERSIONS,
        "current": __version__,
    }


@router.get("/changelog")
def get_changelog(user: dict = Depends(current_user)):
    return read_changelog()


NO_ONLINE_UPDATE = "本部署不提供在线更新"


def update_summary() -> dict:
    """update-status 与 check-update 共用，避免两套「是否有新版本」的语义。"""
    return {"available": False, "current_version": __version__, "reason": NO_ONLINE_UPDATE}


def count_upstream_accounts(request: Request) -> int | None:
    """已启用的上游账号数；读不到库时返回 None，不编造 0。"""
    try:
        conn = request.app.state.db.connect()
        try:
            row = conn.execute("SELECT COUNT(*) AS n FROM accounts WHERE disabled = 0").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return int(row["n"])


@router.get("/versions")
def get_versions(request: Request, user: dict = Depends(current_user)):
    accounts = count_upstream_accounts(request)
    return {
        "manager": __version__,
        "upstream_connected": bool(accounts),  # 有已启用的上游账号 = 已接入
        "upstream_accounts": accounts,
        "upstream_dir": "",  # 本部署没有可更新的上游代码目录
    }


@router.get("/check-update")
def check_update(force: bool = False, user: dict = Depends(current_user)):
    """没有远端可查，force 也改变不了结论；返回「无更新」而不是假的新版本号。"""
    summary = update_summary()
    reason = summary["reason"]
    return {
        **summary,
        "checked_at": int(time.time()),
        "cached": False,
        "manager": {
            "current": __version__,
            "latest": __version__,  # 没有远端可比，当前版本就是最新已知版本
            "has_update": False,
            "error": reason,
            "url": "",
            "repo": "",
        },
        "upstream": {
            "current": "",  # 没有本地上游代码版本可上报
            "latest": "",
            "has_update": False,
            "error": reason,
            "date": "",
            "subject": "",
            "repo": "",
        },
        "has_any": False,
    }


@router.get("/update-status")
def get_update_status(user: dict = Depends(current_user)):
    return {
        **update_summary(),
        "version": __version__,  # 前端 UpdatePanel 读的是 version
        "running": False,
        "ok": None,
        "step": "",
        "logs": [],
        "updater_found": False,
        "upstream_dir": "",
        "can_update_upstream": False,
        "in_container": Path("/.dockerenv").exists(),
        "upstream_ref": "",
    }
