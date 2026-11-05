"""发布包与 manifest 的构建/校验工具（发布机构用，运行期不用它）。

build：  python scripts/release_manifest.py build --version 0.2.0 --private-key key.pem \
             --commit "$GITHUB_SHA" --out dist
verify： python scripts/release_manifest.py verify --zip dist/release.zip \
             --manifest dist/release-manifest.json --sig dist/release-manifest.json.sig \
             --pubkey deploy/release-pubkey.pem --current 0.1.0

签名只覆盖 manifest 的原始字节；发布包里的每个文件再由 manifest 的 sha256 约束。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control"))

from app.services import update_manifest as mf  # noqa: E402
from app.services.update_signing import (  # noqa: E402
    SigningError,
    load_public_key,
    load_seed,
    public_pem,
    sign,
    verify,
)

PAYLOAD_DIRS = ("apps/control/app", "apps/web/out")
PAYLOAD_FILES = ("scripts/backup_core.py", "CHANGELOG.md")
SKIP_DIRS = {"__pycache__", ".mypy_cache", ".pytest_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".DS_Store"}


def collect(root: Path) -> list[Path]:
    """按白名单收集文件；任何不在白名单里的东西都不进包，config/ 与 data/ 永远排除。"""
    picked: list[Path] = []
    for relative in PAYLOAD_DIRS:
        base = root / relative
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_dir():
                continue
            parts = set(path.relative_to(root).parts)
            if parts & SKIP_DIRS or path.suffix in SKIP_SUFFIXES:
                continue
            picked.append(path)
    for relative in PAYLOAD_FILES:
        path = root / relative
        if path.is_file():
            picked.append(path)
    return picked


def write_zip(root: Path, files: list[Path], dest: Path) -> list[dict]:
    """确定性打包：名字排序、固定时间戳与权限，便于复核。"""
    entries: list[dict] = []
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for path in sorted(files, key=lambda p: p.relative_to(root).as_posix()):
            name = path.relative_to(root).as_posix()
            mf._safe_path(name)  # 白名单自检，构建侧和运行侧用同一套口径
            data = path.read_bytes()
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(info, data)
            entries.append({"path": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
    return entries


def cmd_build(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    out = Path(args.out).resolve()
    files = collect(root)
    if not files:
        print("没有可打包的文件，检查 --root 与白名单目录", file=sys.stderr)
        return 1
    archive = out / mf.ARCHIVE_NAME
    entries = write_zip(root, files, archive)
    manifest = {
        "manifest_version": mf.MANIFEST_VERSION,
        "version": args.version,
        "channel": args.channel,
        "platform": args.platform,
        "issued_at": args.issued_at or int(time.time()),
        "commit": args.commit,
        "min_current_version": args.min_current,
        "files": entries,
    }
    mf.validate_manifest(json.dumps(manifest))  # 构建侧自检，防止发出运行侧不认的包
    raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n"
    manifest_path = out / mf.MANIFEST_NAME
    manifest_path.write_bytes(raw)
    seed = load_seed(Path(args.private_key).read_text(encoding="utf-8"))
    signature = base64.b64encode(sign(seed, raw)).decode() + "\n"
    (out / (mf.MANIFEST_NAME + mf.SIG_SUFFIX)).write_text(signature, encoding="utf-8")
    print(f"build ok version={args.version} files={len(entries)} manifest_bytes={len(raw)} zip={archive}")
    print(f"pubkey={public_pem(seed).splitlines()[1]}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    manifest_raw = Path(args.manifest).read_text(encoding="utf-8")
    public_key = load_public_key(Path(args.pubkey).read_text(encoding="utf-8"))
    signature = base64.b64decode("".join(Path(args.sig).read_text(encoding="utf-8").split()), validate=True)
    if not verify(public_key, manifest_raw.encode(), signature):
        print("verify failed: 签名与 manifest 不匹配", file=sys.stderr)
        return 1
    manifest = mf.validate_manifest(manifest_raw)
    if args.current:
        mf.check_compatible(manifest, args.current)
    mf.verify_archive(Path(args.zip), manifest, args.max_bytes)
    print(
        f"verify ok version={manifest['version']} files={len(manifest['files'])} "
        f"platform={manifest['platform']}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="发布包构建与校验")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="构建 release.zip + manifest + 签名")
    build.add_argument("--root", default=str(ROOT))
    build.add_argument("--version", required=True)
    build.add_argument("--channel", default=mf.CHANNEL)
    build.add_argument("--platform", default="any")
    build.add_argument("--commit", default=os.getenv("GITHUB_SHA", "unknown"))
    build.add_argument("--min-current", default="0.0.0")
    build.add_argument("--issued-at", type=int, default=0)
    build.add_argument("--private-key", required=True)
    build.add_argument("--out", default="dist")
    build.set_defaults(func=cmd_build)

    check = sub.add_parser("verify", help="校验发布包与签名")
    check.add_argument("--zip", required=True)
    check.add_argument("--manifest", required=True)
    check.add_argument("--sig", required=True)
    check.add_argument("--pubkey", default=str(ROOT / "deploy" / "release-pubkey.pem"))
    check.add_argument("--current", default="")
    check.add_argument("--max-bytes", type=int, default=268435456)
    check.set_defaults(func=cmd_verify)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (mf.UpdateError, SigningError, OSError, ValueError) as exc:
        print(f"{args.command} failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
