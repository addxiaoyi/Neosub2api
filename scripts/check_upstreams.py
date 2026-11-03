#!/usr/bin/env python3
"""巡检 upstreams/upstreams.yaml 里登记的上游仓库。

只读 GitHub REST API：取默认分支最新 commit 的 sha 与提交时间，和清单里的
analyzed_commit / checked_at 比对。不克隆仓库、不推进 analyzed_commit。

直接运行
    python scripts/check_upstreams.py

CI 消费
    python scripts/check_upstreams.py --json

回写巡检日期
    python scripts/check_upstreams.py --update

退出码：0 全部 unchanged；1 有 updated；2 有 error（可能同时有 updated）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

API_ROOT = "https://api.github.com"
TIMEOUT_SECONDS = 20
USER_AGENT = "unified-2api-upstream-sync"

UNCHANGED = "unchanged"
UPDATED = "updated"
ERROR = "error"

EXIT_OK = 0
EXIT_UPDATED = 1
EXIT_ERROR = 2

REPO_LINE = re.compile(r"^\s*-\s+repo:\s*([^\s#]+)\s*$")
FIELD_LINE = re.compile(r"^(\s+)(analyzed_commit|analyzed_at|checked_at):\s*([^\s#]+)\s*$")
CHECKED_AT_LINE = re.compile(r"^(\s*checked_at:\s*)([^\s#]+)(.*)$")

DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / "upstreams" / "upstreams.yaml"


@dataclass
class Record:
    """清单里的一条仓库记录，只保留巡检用得到的字段和行号。"""

    repo: str
    analyzed_commit: str | None = None
    checked_at: str | None = None
    checked_at_line: int = -1
    anchor_line: int = -1  # 缺 checked_at 时插在它后面


@dataclass
class Manifest:
    path: Path
    lines: list[str]
    records: list[Record]
    newline: str
    bom: bool


def load_manifest(path: Path) -> Manifest:
    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"

    lines = text.splitlines()
    records: list[Record] = []

    for index, line in enumerate(lines):
        repo_match = REPO_LINE.match(line)
        if repo_match:
            records.append(Record(repo=repo_match.group(1), anchor_line=index))
            continue
        if not records:
            continue

        field_match = FIELD_LINE.match(line)
        if not field_match:
            continue
        name, value = field_match.group(2), field_match.group(3)
        record = records[-1]
        record.anchor_line = index
        if name == "analyzed_commit":
            record.analyzed_commit = value
        elif name == "checked_at":
            record.checked_at = value
            record.checked_at_line = index

    return Manifest(path=path, lines=lines, records=records, newline=newline, bom=bom)


def github_token() -> str | None:
    # 只从环境变量取，不回显、不落盘。
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or None


def describe_http_error(err: urllib.error.HTTPError) -> dict:
    headers = err.headers or {}
    remaining = headers.get("X-RateLimit-Remaining")

    if err.code == 404:
        return {
            "reason": "not_found",
            "message": "404：仓库已删除、已改名或已私有化，需人工确认（脚本不会改清单地址）",
        }
    if err.code == 429 or (err.code == 403 and remaining == "0"):
        reset = headers.get("X-RateLimit-Reset")
        hint = ""
        if reset and reset.isdigit():
            reset_at = datetime.fromtimestamp(int(reset), tz=timezone.utc)
            hint = f"，限额重置于 {reset_at:%Y-%m-%d %H:%M}Z"
        return {
            "reason": "rate_limited",
            "message": f"GitHub API 限流（匿名 60 次/小时）{hint}",
        }
    if err.code == 403:
        return {"reason": "forbidden", "message": "403：无权访问（可能是私有仓库或令牌权限不足）"}
    return {"reason": f"http_{err.code}", "message": f"HTTP {err.code}：{err.reason}"}


def api_get(path: str, token: str | None, retries: int = 2) -> tuple[dict | None, dict | None]:
    """GET 一个 GitHub API 路径，返回 (payload, error)。网络抖动与 5xx 重试两次。"""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = API_ROOT + path
    last_error: dict | None = None

    for attempt in range(retries + 1):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode("utf-8")), None
        except urllib.error.HTTPError as err:
            last_error = describe_http_error(err)
            if err.code >= 500 and attempt < retries:
                time.sleep(1)
                continue
            return None, last_error
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            last_error = {"reason": "network", "message": f"网络错误：{err}"}
            if attempt < retries:
                time.sleep(1)
                continue
            return None, last_error

    return None, last_error


def parse_commit_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def checked_at_start(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.combine(date.fromisoformat(raw), datetime.min.time(), tzinfo=timezone.utc)
    except ValueError:
        return None


def classify(record: Record, head_sha: str, commit_at: datetime | None) -> tuple[str, str, str]:
    """返回 (status, reason, message)。"""
    baseline = (record.analyzed_commit or "").lower()

    if baseline and baseline == head_sha:
        return UNCHANGED, "same_commit", "与基线 commit 一致"

    # 基线曾刻意不推进（例如只动了 stars 统计），上次巡检已经见过这个 HEAD：
    # 用严格小于判断，同一日内的新提交仍会报一次，避免漏报。
    seen_from = checked_at_start(record.checked_at)
    if seen_from and commit_at and commit_at < seen_from:
        return (
            UNCHANGED,
            "observed_not_advanced",
            f"HEAD 已偏移但早于 {record.checked_at} 的巡检，基线未推进",
        )

    return UPDATED, "new_commit", "出现新 commit"


def inspect(record: Record, token: str | None) -> dict:
    repo = record.repo
    result = {
        "repo": repo,
        "status": ERROR,
        "reason": "",
        "message": "",
        "analyzed_commit": record.analyzed_commit,
        "base_short": short(record.analyzed_commit),
        "latest_commit": None,
        "latest_short": None,
        "commit_time": None,
        "checked_at": record.checked_at,
        "default_branch": None,
        "archived": False,
        "disabled": False,
    }

    meta, err = api_get(f"/repos/{repo}", token)
    if err:
        result["reason"] = err["reason"]
        result["message"] = err["message"]
        return result

    result["default_branch"] = meta.get("default_branch")
    result["archived"] = bool(meta.get("archived"))
    result["disabled"] = bool(meta.get("disabled"))

    full_name = meta.get("full_name") or repo
    if full_name.lower() != repo.lower():
        result["reason"] = "renamed"
        result["message"] = f"仓库已改名/重定向到 {full_name}，清单地址需人工确认"
        return result

    if result["archived"] or result["disabled"]:
        result["reason"] = "archived"
        result["message"] = "仓库已归档或停用（archived/disabled），后续不会再有新提交"
        return result

    branch = result["default_branch"] or "HEAD"
    payload, err = api_get(f"/repos/{repo}/commits/{branch}", token)
    if err:
        result["reason"] = err["reason"]
        result["message"] = err["message"]
        return result

    head_sha = (payload.get("sha") or "").lower()
    if not head_sha:
        result["reason"] = "bad_response"
        result["message"] = "commit 接口返回内容缺少 sha"
        return result

    commit = payload.get("commit") or {}
    commit_time = (commit.get("committer") or {}).get("date") or (commit.get("author") or {}).get("date")

    result["latest_commit"] = head_sha
    result["latest_short"] = short(head_sha)
    result["commit_time"] = commit_time

    status, reason, message = classify(record, head_sha, parse_commit_time(commit_time))
    result["status"] = status
    result["reason"] = reason
    result["message"] = message
    return result


def short(sha: str | None) -> str | None:
    if not sha:
        return None
    if sha == "unknown":
        return sha
    return sha[:7]


def rewrite_checked_at(manifest: Manifest, today: str) -> int:
    """只改 checked_at 值，保留其余行、行序、缩进与换行符。"""
    lines = list(manifest.lines)
    inserts: list[tuple[int, str]] = []
    touched = 0

    for record in manifest.records:
        if record.checked_at_line >= 0:
            line = lines[record.checked_at_line]
            match = CHECKED_AT_LINE.match(line)
            if not match:
                continue
            lines[record.checked_at_line] = f"{match.group(1)}{today}{match.group(3)}"
            touched += 1
        elif record.anchor_line >= 0:
            indent = "    "
            anchor = lines[record.anchor_line]
            leading = len(anchor) - len(anchor.lstrip(" "))
            inserts.append((record.anchor_line, " " * leading + f"checked_at: {today}"))
            touched += 1

    # 从后往前插，前面的行号才不会失效。
    for anchor_index, line in sorted(inserts, reverse=True):
        lines.insert(anchor_index + 1, line)

    if touched:
        text = manifest.newline.join(lines)
        raw = ("\ufeff" if manifest.bom else "") + text + manifest.newline
        manifest.path.write_bytes(raw.encode("utf-8"))

    return touched


def render_text(results: list[dict]) -> str:
    out = []
    for item in results:
        base = item["base_short"] or "-"
        latest = item["latest_short"] or "-"
        stamp = item["commit_time"] or "-"
        out.append(
            f"[{item['status']:<9}] {item['repo']:<45} {base} -> {latest}  {stamp}  {item['message']}"
        )

    counts = tally(results)
    out.append("")
    out.append(
        f"共 {counts['total']} 个仓库：unchanged {counts['unchanged']}，"
        f"updated {counts['updated']}，error {counts['error']}"
    )

    abnormal = [r for r in results if r["status"] == ERROR]
    if abnormal:
        out.append("异常仓库（清单地址未改动，需人工确认）：")
        for item in abnormal:
            out.append(f"  - {item['repo']}：{item['message']}")
    return "\n".join(out)


def tally(results: list[dict]) -> dict:
    counts = {"total": len(results), UNCHANGED: 0, UPDATED: 0, ERROR: 0}
    for item in results:
        counts[item["status"]] += 1
    return counts


def exit_code_for(counts: dict) -> int:
    if counts[ERROR]:
        return EXIT_ERROR
    if counts[UPDATED]:
        return EXIT_UPDATED
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="巡检上游仓库 HEAD 与本仓清单基线是否偏移")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST, help="清单路径")
    parser.add_argument("--json", action="store_true", help="输出单行 JSON（供 CI 消费）")
    parser.add_argument("--update", action="store_true", help="把可访问仓库的 checked_at 回写为今天")
    args = parser.parse_args(argv)

    manifest_path = args.manifest.resolve()
    if not manifest_path.is_file():
        print(f"找不到清单文件：{manifest_path}", file=sys.stderr)
        return EXIT_ERROR

    manifest = load_manifest(manifest_path)
    if not manifest.records:
        print(f"清单里没有解析到 repo 记录：{manifest_path}", file=sys.stderr)
        return EXIT_ERROR

    token = github_token()
    results = [inspect(record, token) for record in manifest.records]
    counts = tally(results)
    code = exit_code_for(counts)
    today = date.today().isoformat()

    if args.update:
        touched = rewrite_checked_at(manifest, today)

    if args.json:
        payload = {
            "generated_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "manifest": str(manifest_path),
            "authenticated": bool(token),
            "exit_code": code,
            "totals": counts,
            "results": results,
        }
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    else:
        print(render_text(results))
        if args.update:
            print(f"已回写 checked_at：{touched} 条（{today}），analyzed_commit 未改动")

    return code


if __name__ == "__main__":
    raise SystemExit(main())
