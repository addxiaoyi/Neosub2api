"""远端备份：客户端加密后上传 S3 兼容存储，校验、轮转、状态落库。

原则（产品已定）：归档里有 provider_credentials.secret 的明文，所以任何情况下都
不允许把 zip 原样上传。密钥只从环境变量读；没有密钥就明确失败，不提供逃生开关。
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import io
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from ..db import get_json_setting, set_json_setting
from ..net_guard import UpstreamAddressError, guard_upstream_url
from . import backup_crypto
from .ops_lock import OpsBusy, busy_guard
from .s3_client import S3Client, S3Config, S3Error

logger = logging.getLogger("unified.backup.remote")

CONFIG_KEY = "backup_remote_config"
STATE_KEY = "backup_remote_state"
DEFAULT_PREFIX = "unified-2api-backups"
DEFAULT_REGION = "us-east-1"

# 凭据只从环境变量读（产品口径：与加密密钥同原则，不进库/导出/日志）。
# 库内只放非敏感连接参数；历史版本写入库里的 access_key / secret_key 会被忽略。
ACCESS_KEY_ENV = "UNIFIED_BACKUP_S3_ACCESS_KEY_ID"
SECRET_KEY_ENV = "UNIFIED_BACKUP_S3_SECRET_ACCESS_KEY"
SESSION_TOKEN_ENV = "UNIFIED_BACKUP_S3_SESSION_TOKEN"
CREDENTIAL_HINT = (
    f"凭据只从环境变量读取（{ACCESS_KEY_ENV} / {SECRET_KEY_ENV}，"
    f"临时凭据另加 {SESSION_TOKEN_ENV}），不接受写入数据库"
)
_NOT_CONFIGURED_MSG = (
    "远端备份未配置：endpoint / bucket 由 API 写入，"
    f"凭据必须放在环境变量 {ACCESS_KEY_ENV} / {SECRET_KEY_ENV}"
)
RETRY_DELAYS = (30, 120, 600)
# 首试 + 三次退避（30s/2m/10m），三次都失败才推到下一个周期
MAX_ATTEMPTS = len(RETRY_DELAYS) + 1
ARCHIVE_RE = re.compile(r"^unified-backup-\d{8}-\d{6}\.zip$")
_PREFIX_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
_REGION_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

STATE_DEFAULTS: dict[str, Any] = {
    "last_attempt_at": 0,
    "last_ok_at": 0,
    "last_uploaded": "",
    "last_error": "",
    "next_retry_at": 0,
    "auth_blocked": False,
    "last_error_code": "",
    # 列表端点每次探活都会改写这三项；与上传状态机分开，探活失败不阻塞定时上传
    "s3_reachable": None,
    "s3_checked_at": 0,
    "s3_last_error": "",
}


class ConfigError(ValueError):
    """连接参数不合法：路由层据此 400，把具体字段告诉调用方。"""


class KeyUnavailable(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class RemoteFetchError(RuntimeError):
    """远端读取失败：reason 由路由层映射成 HTTP 状态，message 已脱敏。"""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class DownloadedArchive:
    """已下载并验过的远端归档；prior 是本次覆盖掉的同名本地文件。"""

    name: str
    path: Path
    prior: Path | None
    object_key: str
    sha256: str
    size_bytes: int


def _clamp_int(raw: Any, default: int, low: int, high: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def clamp_interval(raw: Any) -> int:
    return _clamp_int(raw, 86400, 3600, 604800)


def clamp_keep(raw: Any) -> int:
    return _clamp_int(raw, 14, 1, 90)


def mask_access_key(value: str) -> str:
    text = value or ""
    if len(text) <= 8:
        return "****" if text else ""
    return f"{text[:4]}****{text[-4:]}"


def _env_credentials() -> dict[str, str]:
    return {
        "access_key": os.environ.get(ACCESS_KEY_ENV, "").strip(),
        "secret_key": os.environ.get(SECRET_KEY_ENV, "").strip(),
        "session_token": os.environ.get(SESSION_TOKEN_ENV, "").strip(),
    }


def _redact(text: str, config: dict[str, Any]) -> str:
    out = str(text or "")
    for secret in (
        config.get("secret_key"),
        config.get("access_key"),
        config.get("session_token"),
    ):
        if secret:
            out = out.replace(secret, "***")
    out = out.replace("\n", " ")
    return out[:300]


def _prefix_of(config: dict[str, Any]) -> str:
    return str(config.get("prefix") or DEFAULT_PREFIX)


def remote_key(config: dict[str, Any], archive_name: str) -> str:
    return f"{_prefix_of(config)}/{archive_name}.enc"


def remote_re(pattern: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(pattern)}/unified-backup-\d{{8}}-\d{{6}}\.zip\.enc$")


class BackupRemoteService:
    def __init__(self, settings: Any, db: Any) -> None:
        self._settings = settings
        self._db = db

    # ---- 配置 ----

    def config(self) -> dict[str, Any]:
        conn = self._db.connect()
        try:
            raw = get_json_setting(conn, CONFIG_KEY, {})
        finally:
            conn.close()
        cfg = raw if isinstance(raw, dict) else {}
        creds = _env_credentials()
        # 凭据一律以环境变量为准；即使库里有历史值也覆盖，避免出现「两套凭据」
        return {
            "endpoint": str(cfg.get("endpoint") or "").strip(),
            "region": str(cfg.get("region") or DEFAULT_REGION).strip(),
            "bucket": str(cfg.get("bucket") or "").strip(),
            "access_key": creds["access_key"],
            "secret_key": creds["secret_key"],
            "session_token": creds["session_token"],
            "path_style": bool(cfg.get("path_style", True)),
            "prefix": str(cfg.get("prefix") or DEFAULT_PREFIX).strip("/"),
        }

    def configured(self) -> bool:
        cfg = self.config()
        return bool(cfg["endpoint"] and cfg["bucket"] and cfg["access_key"] and cfg["secret_key"])

    def public_config(self) -> dict[str, Any]:
        cfg = self.config()
        return {
            "configured": self.configured(),
            "endpoint": cfg["endpoint"],
            "region": cfg["region"],
            "bucket": cfg["bucket"],
            "prefix": cfg["prefix"],
            "path_style": cfg["path_style"],
            "access_key_id_masked": mask_access_key(cfg["access_key"]),
            "credential_source": "env",
            "encryption_key_configured": backup_crypto.key_configured(),
            # 加密走 lib 还是纯标准库，必须能从状态里看出来，不允许静默切换
            "crypto_backend": backup_crypto.backend_info(),
            "state": self.state(),
        }

    def save_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise ConfigError("请求体必须是对象")
        if "access_key" in patch or "secret_key" in patch or "session_token" in patch:
            raise ConfigError(CREDENTIAL_HINT)
        current = self.config()
        # 只持久化非敏感连接参数；凭据那三个字段永远不落库
        updated = {
            "endpoint": current["endpoint"],
            "region": current["region"],
            "bucket": current["bucket"],
            "path_style": current["path_style"],
            "prefix": current["prefix"],
        }
        for field in ("endpoint", "region", "bucket", "prefix"):
            if field in patch:
                updated[field] = str(patch[field] or "").strip()
        if "path_style" in patch:
            if not isinstance(patch["path_style"], bool):
                raise ConfigError("path_style 必须是布尔值")
            updated["path_style"] = patch["path_style"]

        endpoint = updated["endpoint"].rstrip("/")
        if endpoint:
            parts = urlsplit(endpoint)
            if parts.scheme not in {"http", "https"} or not parts.netloc:
                raise ConfigError("endpoint 必须是 http(s)://host[:port] 形式")
            if parts.username or parts.password:
                raise ConfigError("endpoint 里不能带用户名/口令，凭据请改用环境变量")
            # 与 provider base_url 同一套守卫：默认拒绝环回/私网/链路本地，云元数据永远拒绝
            try:
                endpoint = guard_upstream_url(
                    endpoint,
                    allow_private=bool(getattr(self._settings, "allow_private_upstreams", False)),
                    field="远端备份 endpoint",
                )
            except UpstreamAddressError as exc:
                raise ConfigError(str(exc)) from exc
        updated["endpoint"] = endpoint
        if updated["region"] and not _REGION_RE.match(updated["region"]):
            raise ConfigError("region 只能是小写字母、数字和连字符")
        if updated["bucket"]:
            bucket = updated["bucket"]
            if len(bucket) > 128 or any(ch.isspace() or ord(ch) < 32 for ch in bucket):
                raise ConfigError("bucket 名称不合法：不能含空白或控制字符，且不超过 128 字符")
        if updated["prefix"]:
            if not _PREFIX_RE.match(updated["prefix"]):
                raise ConfigError("prefix 只能由字母、数字、点、下划线、连字符和 / 组成，且不能以 / 开头或结尾")

        conn = self._db.connect()
        try:
            set_json_setting(conn, CONFIG_KEY, updated)
            # 改完连接参数就解除上一次的认证阻塞，并让下一轮立即重试
            state = self._read_state(conn)
            state["auth_blocked"] = False
            state["next_retry_at"] = 0
            state["last_attempt_at"] = 0
            set_json_setting(conn, STATE_KEY, state)
            conn.commit()
        finally:
            conn.close()
        return self.public_config()

    # ---- 状态 ----

    def _read_state(self, conn) -> dict[str, Any]:
        raw = get_json_setting(conn, STATE_KEY, {})
        merged = dict(STATE_DEFAULTS)
        if isinstance(raw, dict):
            merged.update({key: raw[key] for key in STATE_DEFAULTS if key in raw})
        merged["auth_blocked"] = bool(merged["auth_blocked"])
        return merged

    def state(self) -> dict[str, Any]:
        conn = self._db.connect()
        try:
            return self._read_state(conn)
        finally:
            conn.close()

    def _write_state(self, **changes: Any) -> dict[str, Any]:
        conn = self._db.connect()
        try:
            state = self._read_state(conn)
            state.update(changes)
            set_json_setting(conn, STATE_KEY, state)
            conn.commit()
            return state
        finally:
            conn.close()

    def clear_block(self) -> None:
        self._write_state(auth_blocked=False, last_error="", last_error_code="", next_retry_at=0)

    def _mark_attempt(self, now: int) -> None:
        self._write_state(last_attempt_at=now)

    def _mark_ok(self, now: int, key: str) -> None:
        self._write_state(
            last_attempt_at=now,
            last_ok_at=now,
            last_uploaded=key,
            last_error="",
            last_error_code="",
            next_retry_at=0,
            auth_blocked=False,
        )

    def _mark_blocked(self, now: int, code: str, message: str) -> None:
        self._write_state(
            last_attempt_at=now,
            last_error=message,
            last_error_code=code,
            next_retry_at=0,
            auth_blocked=True,
        )

    def _mark_retry(self, now: int, code: str, message: str) -> None:
        self._write_state(
            last_attempt_at=now,
            last_error=message,
            last_error_code=code,
            next_retry_at=now + clamp_interval(getattr(self._settings, "backup_remote_interval_sec", 86400)),
            auth_blocked=False,
        )

    # ---- 密钥 ----

    @staticmethod
    def encryption_key() -> bytes:
        try:
            return backup_crypto.load_key()
        except backup_crypto.CryptoKeyMissing as exc:
            raise KeyUnavailable("key_missing", str(exc)) from exc
        except backup_crypto.CryptoKeyInvalid as exc:
            raise KeyUnavailable("key_invalid", str(exc)) from exc

    def note_key_problem(self, now: int) -> dict[str, Any] | None:
        """密钥不可用时写状态并返回结果；可用时返回 None，供调度与手动上传共用。"""
        try:
            self.encryption_key()
        except KeyUnavailable as exc:
            message = f"缺密钥：{exc.message}；定时上传已停用，且不会以明文上传"
            self._mark_blocked(now, exc.code, message)
            return {"ok": False, "skipped": False, "reason": exc.code, "message": message}
        return None

    # ---- 远端读取 ----

    def list_remote(self) -> dict[str, Any]:
        if not self.configured():
            return {
                "ok": True,
                "configured": False,
                "objects": [],
                "count": 0,
                "s3_reachable": None,
            }
        cfg = self.config()
        pattern = remote_re(_prefix_of(cfg))
        client = self._client(cfg)
        try:
            listed = client.list_objects_v2(prefix=_prefix_of(cfg) + "/")
        except (S3Error, OSError) as exc:
            message = _redact(getattr(exc, "message", str(exc)), cfg)
            # 探活失败只落状态，不写回上传状态机（auth_blocked / next_retry_at 都不动）
            self._write_state(s3_reachable=False, s3_checked_at=int(time.time()), s3_last_error=message)
            return {
                "ok": False,
                "configured": True,
                "objects": [],
                "count": 0,
                "s3_reachable": False,
                "error": {"code": getattr(exc, "code", "error"), "message": message},
            }
        finally:
            client.close()
        self._write_state(s3_reachable=True, s3_checked_at=int(time.time()), s3_last_error="")
        # 同一份列举结果里做集合比对，判断旁注是否存在；不为每个对象额外发 HEAD
        all_keys = {obj.key for obj in listed}
        objects = [obj for obj in listed if pattern.match(obj.key)]
        objects.sort(key=lambda obj: obj.key, reverse=True)
        return {
            "ok": True,
            "configured": True,
            "objects": [
                {
                    "key": obj.key,
                    "size_bytes": obj.size,
                    "last_modified": obj.last_modified,
                    "has_sidecar": f"{obj.key}.sha256" in all_keys,
                }
                for obj in objects
            ],
            "count": len(objects),
            "s3_reachable": True,
        }

    # ---- 远端恢复 / 删除 ----

    def download_archive(self, *, name: str, directory: Path) -> DownloadedArchive:
        """下载 `<name>.zip.enc`，先验旁注再解密，最后 os.replace 原子落名。

        锁由调用方持有；任何失败都不会留下 `.part` / `.dec.tmp`，也不会破坏同名
        本地归档（覆盖前先挪到 `.prior`，由调用方决定提交还是还原）。
        """
        cfg = self.config()
        if not self.configured():
            raise RemoteFetchError("not_configured", _NOT_CONFIGURED_MSG)
        try:
            key = self.encryption_key()
        except KeyUnavailable as exc:
            raise RemoteFetchError(exc.code, f"缺密钥：{exc.message}；拒绝尝试恢复远端备份") from exc

        object_key = remote_key(cfg, name)
        sidecar_key = object_key + ".sha256"
        part = directory / f".{name}.part"
        dec_tmp = directory / f".{name}.dec.tmp"
        final = directory / name
        prior = directory / f".{name}.prior"
        client = self._client(cfg)
        placed = False
        moved_prior = False
        try:
            try:
                payload = client.get_object(object_key)
            except S3Error as exc:
                raise self._fetch_failure(exc, cfg, object_key) from exc
            part.write_bytes(payload)

            try:
                sidecar = client.get_object(sidecar_key)
            except S3Error as exc:
                if exc.status == 404:
                    raise RemoteFetchError(
                        "sidecar_missing",
                        f"远端缺少旁注 {sidecar_key.rsplit('/', 1)[-1]}，拒绝恢复未经校验的归档",
                    ) from exc
                raise self._fetch_failure(exc, cfg, sidecar_key) from exc
            tokens = sidecar.split()
            expected = tokens[0].decode("ascii", "ignore").strip().lower() if tokens else ""
            digest = hashlib.sha256(payload).hexdigest()
            if not hmac.compare_digest(expected, digest):
                raise RemoteFetchError(
                    "checksum_mismatch",
                    f"旁注 sha256 与下载密文不一致（旁注 {expected[:12] or '(空)'}…，实算 {digest[:12]}…），已中止恢复",
                )

            try:
                plain = backup_crypto.decrypt_bytes(payload, key)
            except backup_crypto.CryptoVerifyError as exc:
                raise RemoteFetchError("decrypt_failed", f"解密校验失败：{exc}") from exc
            dec_tmp.write_bytes(plain)
            if final.exists():
                os.replace(final, prior)
                moved_prior = True
            os.replace(dec_tmp, final)
            placed = True
            return DownloadedArchive(
                name=name,
                path=final,
                prior=prior if moved_prior else None,
                object_key=object_key,
                sha256=digest,
                size_bytes=len(payload),
            )
        except OSError as exc:
            raise RemoteFetchError("file_error", f"写入下载文件失败：{exc}") from exc
        finally:
            client.close()
            with contextlib.suppress(OSError):
                part.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                dec_tmp.unlink(missing_ok=True)
            if not placed and moved_prior:
                with contextlib.suppress(OSError):
                    os.replace(prior, final)

    def commit_download(self, item: DownloadedArchive) -> None:
        """恢复成功后丢弃覆盖前的同名本地归档。"""
        if item.prior is not None:
            with contextlib.suppress(OSError):
                item.prior.unlink(missing_ok=True)

    def discard_download(self, item: DownloadedArchive) -> None:
        """恢复失败后删掉本次产物；若覆盖过同名文件，把它还原回去。"""
        with contextlib.suppress(OSError):
            if item.prior is not None and item.prior.exists():
                os.replace(item.prior, item.path)
            else:
                item.path.unlink(missing_ok=True)

    def delete_remote(self, name: str) -> dict[str, Any]:
        """删远端一份归档及其旁注；name 只接受本地归档白名单里的名字。"""
        cfg = self.config()
        if not self.configured():
            raise RemoteFetchError("not_configured", _NOT_CONFIGURED_MSG)
        object_key = remote_key(cfg, name)
        client = self._client(cfg)
        try:
            try:
                client.delete_object(object_key)
            except S3Error as exc:
                raise self._fetch_failure(exc, cfg, object_key) from exc
            sidecar_error = ""
            try:
                client.delete_object(object_key + ".sha256")
            except S3Error as exc:
                # 主对象已删；旁注删不掉只记一笔，不回滚也不谎报成功
                sidecar_error = _redact(exc.message, cfg)
        finally:
            client.close()
        return {"object": object_key, "sidecar_error": sidecar_error}

    def _fetch_failure(self, exc: S3Error, cfg: dict[str, Any], key: str) -> RemoteFetchError:
        """把 S3 错误分成致命/可重试两类，顺手写状态（403/签名错会阻塞后续上传）。"""
        now = int(time.time())
        message = _redact(exc.message, cfg)
        if exc.status == 404:
            return RemoteFetchError("object_missing", f"远端对象不存在：{key}（{message}）")
        if exc.fatal:
            self._mark_blocked(now, "auth_rejected", message)
            return RemoteFetchError(
                "auth_rejected", f"远端读取被拒绝（致命，不再重试）：{exc.code}: {message}"
            )
        self._write_state(last_error=message, last_error_code="retryable", auth_blocked=False)
        return RemoteFetchError("retryable", f"远端暂时不可用（可重试）：{exc.code}: {message}")

    # ---- 上传 ----

    def upload(
        self,
        *,
        name: str | None = None,
        create_before: bool = False,
        retry_delays: tuple[int, ...] = RETRY_DELAYS,
        now: int | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> dict[str, Any]:
        """加密并上传一个本地归档；锁抢不到时返回 skipped，由调用方决定 HTTP 语义。"""
        now = int(time.time()) if now is None else int(now)
        cfg = self.config()
        if not self.configured():
            message = _NOT_CONFIGURED_MSG
            self._write_state(
                last_attempt_at=now,
                last_error=message,
                last_error_code="not_configured",
                next_retry_at=now + clamp_interval(getattr(self._settings, "backup_remote_interval_sec", 86400)),
                auth_blocked=False,
            )
            return {"ok": False, "skipped": False, "reason": "not_configured", "message": message}

        problem = self.note_key_problem(now)
        if problem is not None:
            return problem
        key = self.encryption_key()

        directory = Path(self._settings.db_path).parent / "backups"
        try:
            with busy_guard(directory, "backup-remote"):
                return self._upload_locked(
                    cfg=cfg,
                    key=key,
                    directory=directory,
                    name=name,
                    create_before=create_before,
                    retry_delays=retry_delays,
                    now=now,
                    sleeper=sleeper,
                )
        except OpsBusy as exc:
            logger.info("远端备份跳过：%s", exc)
            return {"ok": False, "skipped": True, "reason": "busy", "message": str(exc)}

    def _upload_locked(
        self,
        *,
        cfg: dict[str, Any],
        key: bytes,
        directory: Path,
        name: str | None,
        create_before: bool,
        retry_delays: tuple[int, ...],
        now: int,
        sleeper: Callable[[float], None],
    ) -> dict[str, Any]:
        self._mark_attempt(now)
        if create_before:
            try:
                archive = self._create_archive()
            except Exception as exc:  # noqa: BLE001 - 本地备份失败是单独一类，不碰远端状态里的认证位
                message = f"本地备份失败，本轮不上传：{exc}"
                self._write_state(last_error=message, last_error_code="local_backup_failed", auth_blocked=False)
                return {"ok": False, "skipped": False, "reason": "local_backup_failed", "message": message}
        else:
            archive = self._pick_archive(directory, name)
            if archive is None:
                message = "没有可上传的本地备份归档"
                self._write_state(last_error=message, last_error_code="no_archive", auth_blocked=False)
                return {"ok": False, "skipped": False, "reason": "no_archive", "message": message}

        object_key = remote_key(cfg, archive.name)
        sidecar_key = object_key + ".sha256"
        client = self._client(cfg)
        encrypted_path: Path | None = None
        try:
            encrypted_path, digest, size = self._encrypt(archive, key, directory)
            metadata = {"sha256": digest, "size": str(size), "source": archive.name}
            payload = encrypted_path.read_bytes()
            sidecar = f"{digest}  {object_key}\n".encode()
            last_error = ""
            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    client.put_object(object_key, payload, metadata)
                    head = client.head_object(object_key)
                    if head.size != size or head.metadata.get("sha256") != digest:
                        raise S3Error(
                            409,
                            "ChecksumMismatch",
                            f"HEAD 校验失败：大小 {head.size}/{size}，sha256 {head.metadata.get('sha256', '')[:12]}…/{digest[:12]}…",
                            fatal=False,
                        )
                    client.put_object(sidecar_key, sidecar, {"sha256": digest})
                    self._mark_ok(now, object_key)
                    removed = self._rotate(client, cfg)
                    logger.info(
                        "远端备份完成：%s（%d 字节，第 %d 次尝试，轮转删除 %d 个旧对象）",
                        object_key,
                        size,
                        attempt,
                        removed,
                    )
                    return {
                        "ok": True,
                        "skipped": False,
                        "reason": "",
                        "object": object_key,
                        "sidecar": sidecar_key,
                        "size_bytes": size,
                        "sha256": digest,
                        "attempts": attempt,
                        "rotated": removed,
                    }
                except S3Error as exc:
                    message = _redact(exc.message, cfg)
                    if exc.fatal:
                        self._mark_blocked(now, "auth_rejected", message)
                        return {
                            "ok": False,
                            "skipped": False,
                            "reason": "auth_rejected",
                            "message": message,
                            "code": exc.code,
                        }
                    last_error = f"{exc.code}: {message}"
                except (OSError, ValueError) as exc:
                    last_error = f"{type(exc).__name__}: {_redact(str(exc), cfg)}"
                if attempt < MAX_ATTEMPTS:
                    delay = retry_delays[min(attempt - 1, len(retry_delays) - 1)] if retry_delays else 0
                    if delay:
                        sleeper(delay)
            message = f"上传连续 {MAX_ATTEMPTS} 次失败：{last_error}"
            self._mark_retry(now, "retryable", message)
            return {"ok": False, "skipped": False, "reason": "retryable", "message": message}
        finally:
            client.close()
            if encrypted_path is not None:
                with contextlib.suppress(OSError):
                    encrypted_path.unlink()

    def _client(self, cfg: dict[str, Any]) -> S3Client:
        return S3Client(
            S3Config(
                endpoint=cfg["endpoint"],
                bucket=cfg["bucket"],
                access_key=cfg["access_key"],
                secret_key=cfg["secret_key"],
                region=cfg["region"] or DEFAULT_REGION,
                path_style=bool(cfg["path_style"]),
                session_token=cfg.get("session_token", ""),
            )
        )

    def _encrypt(self, archive: Path, key: bytes, directory: Path) -> tuple[Path, str, int]:
        handle, raw_path = tempfile.mkstemp(dir=str(directory), prefix=".backup-remote-", suffix=".enc.tmp")
        os.close(handle)
        target = Path(raw_path)
        info = backup_crypto.encrypt_file(archive, target, key)
        return target, str(info["sha256"]), int(info["size_bytes"])

    def _pick_archive(self, directory: Path, name: str | None) -> Path | None:
        if name:
            if not ARCHIVE_RE.match(name):
                return None
            candidate = directory / name
            return candidate if candidate.is_file() else None
        if not directory.is_dir():
            return None
        candidates = [
            entry
            for entry in directory.iterdir()
            if entry.is_file() and ARCHIVE_RE.match(entry.name)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda entry: entry.name)

    def _create_archive(self) -> Path:
        # 复用本地备份的同一条链路（含 keep 轮转），避免这里再造一份归档格式
        from ..routers import backup as backup_router

        settings = self._settings
        core = backup_router.core_module()
        directory = backup_router._backup_dir(settings)  # noqa: SLF001 - 同一份目录口径
        with contextlib.redirect_stdout(io.StringIO()):
            archive = backup_router._make_archive(core, settings, directory)  # noqa: SLF001
        return archive

    def _rotate(self, client: S3Client, cfg: dict[str, Any]) -> int:
        keep = clamp_keep(getattr(self._settings, "backup_remote_keep", 14))
        prefix = _prefix_of(cfg) + "/"
        pattern = remote_re(_prefix_of(cfg))
        try:
            objects = client.list_objects_v2(prefix=prefix)
        except S3Error as exc:
            logger.warning("远端轮转跳过：列举失败 %s", exc)
            return 0
        keys = sorted({obj.key for obj in objects if pattern.match(obj.key)}, reverse=True)
        removed = 0
        for stale in keys[keep:]:
            try:
                client.delete_object(stale)
                with contextlib.suppress(S3Error):
                    client.delete_object(stale + ".sha256")
                removed += 1
            except S3Error as exc:
                logger.warning("远端轮转删除失败 %s：%s", stale, exc)
        return removed
