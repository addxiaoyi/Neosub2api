"""发布 manifest 与发布 zip 的校验口径。

顺序是硬的（与 docs/DEPLOYMENT.md 一致）：验签 → 版本/通道/平台判定 → 包内路径白名单
→ 逐文件 sha256。任何一步失败都在解压前中止，绝不把半验过的内容放进运行目录。
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import shutil
import stat
import sys
import zipfile
from pathlib import Path
from typing import Any

MANIFEST_NAME = "release-manifest.json"
SIG_SUFFIX = ".sig"
ARCHIVE_NAME = "release.zip"
# 只有这个取值的 manifest 结构被认；将来改结构必须提升版本号并同步这里
MANIFEST_VERSION = 1
CHANNEL = "stable"

# 可替换的文件白名单：用户资产（config/、data/）永远不在其中
FILE_WHITELIST = ("scripts/backup_core.py", "CHANGELOG.md")
DIR_WHITELIST = ("apps/control/app/", "apps/web/out/")
FORBIDDEN_PREFIXES = ("config/", "data/")

MANIFEST_LIMIT_BYTES = 4 * 1024 * 1024
SHA256_HEX_LEN = 64


class UpdateError(ValueError):
    """发布物不可用：消息直接进 reason/error，必须可读且不含任何密钥。"""


def archive_url(manifest_url: str) -> str:
    """发布物地址约定：与 manifest 同目录，文件名换成 release.zip。

    GitHub Release 的 `.../releases/latest/download/release-manifest.json` 按此规则
    得到 `.../releases/latest/download/release.zip`，不需要在 manifest 里再带一条 URL
    （manifest 由发布方签发，多一个可指向任意地址的字段等于给了重定向后门）。
    """
    base, _, tail = manifest_url.rpartition("/")
    if tail == MANIFEST_NAME:
        return f"{base}/{ARCHIVE_NAME}"
    return f"{manifest_url}.zip"


def parse_version(text: str) -> tuple[int, ...]:
    """只认数字点分版本（1 / 1.2 / 1.2.3）；预发布后缀会让比较失去意义，直接拒。"""
    raw = (text or "").strip()
    if not raw:
        raise UpdateError("版本号为空")
    parts = raw.split(".")
    if len(parts) > 4 or any(not part.isdigit() for part in parts):
        raise UpdateError(f"版本号不合法：{raw!r}（只接受数字点分，如 0.2.0）")
    return tuple(int(part) for part in parts)


def _pad(value: tuple[int, ...], size: int) -> tuple[int, ...]:
    return value + (0,) * (size - len(value))


def compare_versions(left: str, right: str) -> int:
    """返回 -1/0/1；调用方拿它做降级与 min_current_version 判定。"""
    a, b = _pad(parse_version(left), 4), _pad(parse_version(right), 4)
    return (a > b) - (a < b)


def _require(mapping: dict[str, Any], key: str, types: tuple[type, ...]) -> Any:
    if key not in mapping:
        raise UpdateError(f"manifest 缺字段：{key}")
    value = mapping[key]
    if not isinstance(value, types) or isinstance(value, bool) and bool not in types:
        raise UpdateError(f"manifest 字段类型不对：{key}")
    return value


def validate_manifest(raw: bytes | str) -> dict[str, Any]:
    """解析并校验 manifest 结构；未知 manifest_version 一律拒。"""
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="strict")
    else:
        text = raw
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise UpdateError(f"manifest 不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise UpdateError("manifest 顶层必须是对象")
    version_field = data.get("manifest_version")
    if not isinstance(version_field, int) or isinstance(version_field, bool):
        raise UpdateError("manifest 缺 manifest_version 或类型不对")
    if version_field != MANIFEST_VERSION:
        raise UpdateError(
            f"未知的 manifest_version={version_field}，当前程序只认 {MANIFEST_VERSION}"
        )
    parse_version(_require(data, "version", (str,)))
    parse_version(_require(data, "min_current_version", (str,)))
    _require(data, "channel", (str,))
    _require(data, "platform", (str,))
    _require(data, "issued_at", (int,))
    _require(data, "commit", (str,))
    files = _require(data, "files", (list,))
    if not files:
        raise UpdateError("manifest 的 files 为空")
    seen: set[str] = set()
    total = 0
    normalized: list[dict[str, Any]] = []
    for item in files:
        if not isinstance(item, dict):
            raise UpdateError("manifest.files 的每一项必须是对象")
        path = _safe_path(_require(item, "path", (str,)))
        digest = _require(item, "sha256", (str,))
        size = _require(item, "size", (int,))
        if len(digest) != SHA256_HEX_LEN or any(c not in "0123456789abcdef" for c in digest):
            raise UpdateError(f"sha256 不是 64 位小写十六进制：{path}")
        if size < 0:
            raise UpdateError(f"文件大小不能为负：{path}")
        if path.lower() in seen:
            raise UpdateError(f"manifest 里有重复路径（大小写不敏感）：{path}")
        seen.add(path.lower())
        total += size
        normalized.append({"path": path, "sha256": digest, "size": size})
    data["files"] = normalized
    data["_total_size"] = total
    return data


def _safe_path(raw: str) -> str:
    """白名单 + 反穿越：绝对路径、`..`、反斜杠、非白名单目录一律拒。"""
    if not raw or raw != raw.strip():
        raise UpdateError("包内路径为空或首尾有空白")
    if "\\" in raw:
        raise UpdateError(f"包内路径不允许反斜杠：{raw}")
    if raw.startswith("/") or raw.startswith("~") or (len(raw) > 1 and raw[1] == ":"):
        raise UpdateError(f"包内路径不能是绝对路径：{raw}")
    parts = raw.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise UpdateError(f"包内路径含空段/`.`/`..`：{raw}")
    if posixpath.normpath(raw) != raw:
        raise UpdateError(f"包内路径不规整：{raw}")
    if raw.endswith("/"):
        raise UpdateError(f"文件路径不能以 / 结尾：{raw}")
    if raw in FILE_WHITELIST:
        return raw
    if any(raw.startswith(prefix) and len(raw) > len(prefix) for prefix in DIR_WHITELIST):
        return raw
    for prefix in FORBIDDEN_PREFIXES:
        if raw.startswith(prefix):
            raise UpdateError(f"{prefix} 是用户资产，发布包不得包含：{raw}")
    raise UpdateError(f"包内路径不在白名单：{raw}")


def check_compatible(manifest: dict[str, Any], current_version: str) -> None:
    """版本比较与通道/平台判定；降级重放、跨通道、跨平台都在这里拒。"""
    manifest_version = str(manifest.get("version") or "")
    if compare_versions(manifest_version, current_version) <= 0:
        raise UpdateError(
            f"发布物版本 {manifest_version} 不大于当前版本 {current_version}，拒绝降级或重放"
        )
    floor = str(manifest.get("min_current_version") or "0")
    if compare_versions(current_version, floor) < 0:
        raise UpdateError(
            f"当前版本 {current_version} 低于该发布物的最低要求 {floor}，请先升级到中间版本"
        )
    channel = str(manifest.get("channel") or "")
    if channel != CHANNEL:
        raise UpdateError(f"发布物通道 {channel} 与当前通道 {CHANNEL} 不符")
    platform = str(manifest.get("platform") or "")
    if platform not in ("any", sys.platform):
        raise UpdateError(
            f"发布物平台 {platform} 与当前平台 {sys.platform} 不符"
        )


def verify_archive(archive: Path, manifest: dict[str, Any], max_bytes: int) -> None:
    """只看不写：结构/路径/大小/sha256 全过才返回。

    读两次包体是故意的：第一遍全量校验，第二遍才落盘，避免「边解边发现不符」
    留下半套文件。包体在 data/updates 下，不在运行目录。
    """
    declared = {item["path"]: item for item in manifest["files"]}
    total_declared = int(manifest.get("_total_size") or 0)
    if total_declared > max_bytes:
        raise UpdateError(f"发布包声明总大小 {total_declared} 超过上限 {max_bytes}")
    try:
        with zipfile.ZipFile(archive) as bundle:
            infos = bundle.infolist()
            total = 0
            seen: set[str] = set()
            seen_lower: set[str] = set()
            for info in infos:
                name = info.filename
                if info.is_dir():
                    continue
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise UpdateError(f"发布包不允许符号链接：{name}")
                if info.flag_bits & 0x1:
                    raise UpdateError(f"发布包不允许加密成员：{name}")
                if "\\" in name:
                    raise UpdateError(f"发布包内路径不允许反斜杠：{name}")
                _safe_path(name)
                if name.lower() in seen_lower:
                    raise UpdateError(f"发布包内有大小写冲突的重复路径：{name}")
                seen.add(name)
                seen_lower.add(name.lower())
                if name not in declared:
                    raise UpdateError(f"发布包内有 manifest 未声明的文件：{name}")
                total += info.file_size
                if total > max_bytes:
                    raise UpdateError(f"发布包解压后总大小超过上限 {max_bytes} 字节")
            missing = sorted(set(declared) - seen)
            if missing:
                raise UpdateError(f"发布包缺少 manifest 声明的文件：{', '.join(missing[:5])}")
            for path, item in declared.items():
                info = bundle.getinfo(path)
                if info.file_size != item["size"]:
                    raise UpdateError(
                        f"文件大小与 manifest 不符：{path}（包内 {info.file_size}，声明 {item['size']}）"
                    )
                digest = hashlib.sha256(bundle.read(path)).hexdigest()
                if digest != item["sha256"]:
                    raise UpdateError(f"文件 sha256 与 manifest 不符：{path}")
    except zipfile.BadZipFile as exc:
        raise UpdateError(f"发布包不是合法 zip：{exc}") from exc
    except OSError as exc:
        raise UpdateError(f"读取发布包失败：{exc}") from exc


def extract_archive(archive: Path, manifest: dict[str, Any], dest: Path) -> None:
    """在 verify_archive 之后调用；仍逐文件再算一次 sha256 才落盘。"""
    declared = {item["path"]: item for item in manifest["files"]}
    # 清掉同版本的旧 stage：上一轮残留的未声明文件不能被换进运行目录
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive) as bundle:
            for path, item in declared.items():
                _safe_path(path)
                data = bundle.read(path)
                if hashlib.sha256(data).hexdigest() != item["sha256"]:
                    raise UpdateError(f"解压时文件内容变化，已中止：{path}")
                target = dest / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
    except zipfile.BadZipFile as exc:
        raise UpdateError(f"发布包不是合法 zip：{exc}") from exc
    except OSError as exc:
        raise UpdateError(f"解压发布包失败：{exc}") from exc


def load_manifest(path: Path) -> str:
    """读 manifest 原文（验签必须对原始字节做，不能先 json 再序列化回去）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UpdateError(f"读取 manifest 失败：{exc}") from exc
    if len(text.encode("utf-8")) > MANIFEST_LIMIT_BYTES:
        raise UpdateError(f"manifest 超过 {MANIFEST_LIMIT_BYTES} 字节上限")
    return text
