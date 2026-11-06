"""导入服务商配置与账号清单。

三档语义：
- merge：同名保留库里的记录，只补库里没有的
- overwrite：同键以文件为准覆盖，但不删除库里文件没有的记录
- dry-run：只出报告，绝不写库
预检有任一 fail 项时整批拒绝，退出码 2。写入在单事务内，异常回滚。

用法：python scripts/config_import.py --in config.json --mode merge|overwrite|dry-run [--db PATH]
退出码：0 成功（dry-run 无 fail），1 运行错误，2 预检失败整批拒绝。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "control"))

from app.config_bundle import (  # noqa: E402
    BundleRejected,
    BundleWriteFailed,
    apply_import,
    plan_import,
)

import backup_core as core  # noqa: E402


def default_db() -> Path:
    env = os.getenv("UNIFIED_DB_PATH", "").strip()
    if env:
        return Path(env).resolve()
    data = os.getenv("UNIFIED_DATA_DIR", "").strip()
    if data:
        return (Path(data) / "unified.db").resolve()
    return core.repo_root() / "data" / "unified.db"


def known_providers(conn) -> set[str]:
    return {str(row["id"]) for row in conn.execute("SELECT id FROM providers").fetchall()}


def print_report(report: dict) -> None:
    totals = report["totals"]
    label = report["mode"]
    if report.get("semantics") and report["mode"] == "dry-run":
        label = f"dry-run(as={report['semantics']})"
    print(f"mode={label} 新增={totals['create']} 覆盖={totals['overwrite']} "
          f"跳过={totals['skip']} 失败={totals['fail']}")
    for item in report["create"]:
        print(f"  [新增] {item['path']} {item['key']} — {item['reason']}")
    for item in report["overwrite"]:
        diff = ",".join(item["diff"]) or "-"
        print(f"  [覆盖] {item['path']} {item['key']} — 差异字段：{diff}")
    for item in report["skip"]:
        diff = ",".join(item["diff"]) or "-"
        print(f"  [跳过] {item['path']} {item['key']} — {item['reason']}（差异：{diff}）")
    for issue in report["issues"]:
        if issue["level"] == "fail":
            print(f"  [失败] {issue['path']} 第{issue['index']}条 列={issue['column']} — {issue['reason']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入 unified-2api 配置与账号")
    parser.add_argument("--in", dest="source", required=True, help="输入 JSON 路径")
    parser.add_argument("--mode", required=True, choices=("merge", "overwrite", "dry-run"))
    parser.add_argument(
        "--as", dest="dry_run_as", default="merge", choices=("merge", "overwrite"),
        help="dry-run 时按哪种语义分类（默认 merge）",
    )
    parser.add_argument("--db", default="", help="SQLite 库路径（默认按环境变量推导）")
    args = parser.parse_args(argv)

    db_path = Path(args.db).resolve() if args.db else default_db()
    src = Path(args.source).resolve()
    if not src.is_file():
        print(f"导入失败：文件不存在 {src}", file=sys.stderr)
        return core.EXIT_FAIL
    if not db_path.is_file():
        print(f"导入失败：库不存在 {db_path}", file=sys.stderr)
        return core.EXIT_FAIL

    try:
        doc = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"导入失败：文件不是合法 JSON — {exc}", file=sys.stderr)
        return core.EXIT_VERIFY

    conn = core.open_db(db_path)
    try:
        known = known_providers(conn)
        if args.mode == "dry-run":
            report = plan_import(
                doc, conn=conn, mode="dry-run", known_ids=known,
                dry_run_as=args.dry_run_as,
            )
            print_report(report)
            bad = any(issue["level"] == "fail" for issue in report["issues"])
            print("dry-run：未写入任何行")
            return core.EXIT_VERIFY if bad else 0
        report = apply_import(doc, conn=conn, mode=args.mode, known_ids=known)
    except BundleRejected as exc:
        print_report(exc.report)
        print("整批拒绝：预检未通过，库未改动", file=sys.stderr)
        return core.EXIT_VERIFY
    except BundleWriteFailed as exc:
        print(f"写入失败，事务已回滚：{exc.path} — {exc.error}", file=sys.stderr)
        return core.EXIT_FAIL
    except Exception as exc:
        print(f"导入失败：{exc}", file=sys.stderr)
        return core.EXIT_FAIL
    finally:
        conn.close()

    print_report(report)
    print(f"已写入 {report['written']} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
