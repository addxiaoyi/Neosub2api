"""导出服务商配置与账号清单（单个 JSON）。

默认不含任何明文凭据；--include-secrets 才带 secret 与 provider_config.api_key，
并在文件头写警告。文件权限尽量收紧到 0600；Windows 无 POSIX 权限位时如实标注。

用法：python scripts/config_export.py --out config.json [--include-secrets] [--db PATH]
退出码：0 成功，1 运行错误。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "control"))

from app.config_bundle import build_bundle  # noqa: E402

import backup_core as core  # noqa: E402


def default_db() -> Path:
    env = os.getenv("UNIFIED_DB_PATH", "").strip()
    if env:
        return Path(env).resolve()
    data = os.getenv("UNIFIED_DATA_DIR", "").strip()
    if data:
        return (Path(data) / "unified.db").resolve()
    return core.repo_root() / "data" / "unified.db"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出 unified-2api 配置与账号")
    parser.add_argument("--out", required=True, help="输出 JSON 路径")
    parser.add_argument("--include-secrets", action="store_true", help="包含明文凭据（危险）")
    parser.add_argument("--db", default="", help="SQLite 库路径（默认按环境变量推导）")
    args = parser.parse_args(argv)

    db_path = Path(args.db).resolve() if args.db else default_db()
    if not db_path.is_file():
        print(f"导出失败：库不存在 {db_path}", file=sys.stderr)
        return core.EXIT_FAIL

    out = Path(args.out).resolve()
    conn = core.open_db(db_path, readonly=True)
    try:
        bundle = build_bundle(conn, None, include_secrets=args.include_secrets)
    except Exception as exc:  # 读库失败：给一句结论，不打堆栈
        print(f"导出失败：{exc}", file=sys.stderr)
        return core.EXIT_FAIL
    finally:
        conn.close()

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    core._chmod600(out)
    posix = not sys.platform.startswith("win")

    print(f"out={out}")
    print(f"contains_secrets={bundle['contains_secrets']}")
    print(f"providers={len(bundle['providers'])} accounts={len(bundle['accounts'])} "
          f"credentials={len(bundle['credentials'])}")
    print(f"posix_mode_applied={posix}")
    if not posix:
        print("提示：当前是 Windows，NTFS 无 POSIX 0600，只继承目录 ACL；含密文件请放受控目录")
    if bundle["contains_secrets"]:
        print("警告：文件含明文密钥，勿提交、勿外传")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
