"""Provider 凭据落库加密的统一入口。

密钥复用 UNIFIED_BACKUP_ENCRYPTION_KEY，格式沿用 backup_crypto 的 `u2a-enc:v1:`，
所以备份归档里的字段与库内字段是同一套密文，恢复时无需二次转换。

缺密钥时的边界：
- 读：明文直通（旧数据兼容）；遇到已封存的值返回空值 + 错误码，绝不把密文当密钥发往上游。
- 写：保持明文并打一次 ERROR，由 /api/security/secret-at-rest 暴露未加密计数。
- UNIFIED_SECRET_AT_REST_REQUIRED=1 时由 bootstrap() 直接拒绝启动。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from typing import Any

from ..security import SESSION_TOKEN_PREFIX
from .backup_crypto import (
    CryptoKeyInvalid,
    CryptoKeyMissing,
    CryptoVerifyError,
    is_sealed,
    load_key,
    seal_text,
    unseal_text,
)

REQUIRED_ENV = "UNIFIED_SECRET_AT_REST_REQUIRED"

log = logging.getLogger("unified.secret_at_rest")

_warned_missing = False


class SecretUnavailable(RuntimeError):
    """已封存但当前进程解不开；调用方必须按「无可用凭据」处理，不得回退到密文。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def required() -> bool:
    raw = os.environ.get(REQUIRED_ENV, "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def key_state() -> str:
    """ok / missing / invalid：invalid 与 missing 在写入侧一样按未配置处理，但日志措辞不同。"""
    try:
        load_key()
    except CryptoKeyMissing:
        return "missing"
    except CryptoKeyInvalid:
        return "invalid"
    return "ok"


def _key() -> bytes | None:
    try:
        return load_key()
    except (CryptoKeyMissing, CryptoKeyInvalid) as exc:
        global _warned_missing
        if not _warned_missing:
            _warned_missing = True
            log.error("凭据落库加密未启用：%s；新写入将保持明文", exc)
        return None


def reset_warnings() -> None:
    """只给测试用：清掉「缺密钥只报一次」的进程级状态。"""
    global _warned_missing
    _warned_missing = False


def seal(value: Any) -> str:
    """空值/已封存直通；缺密钥时保持明文（并告警），绝不产出半加密数据。"""
    text = "" if value is None else str(value)
    if not text or is_sealed(text):
        return text
    key = _key()
    if key is None:
        return text
    return seal_text(text, key)


def unseal(value: Any) -> str:
    """明文直通；密文缺密钥或校验失败抛 SecretUnavailable，由调用方决定降级方式。"""
    text = "" if value is None else str(value)
    if not text or not is_sealed(text):
        return text
    key = _key()
    if key is None:
        raise SecretUnavailable("key_missing", "已加密的凭据需要 UNIFIED_BACKUP_ENCRYPTION_KEY 才能读取")
    try:
        return unseal_text(text, key)
    except CryptoVerifyError as exc:
        raise SecretUnavailable("decrypt_failed", "凭据解密失败：密钥不匹配或密文被改动") from exc
    except ValueError as exc:
        raise SecretUnavailable("decode_failed", "凭据密文格式不可识别") from exc


def read_secret(value: Any) -> tuple[str, str]:
    """热路径友好的读：返回 (明文, 错误码)。错误码为空表示正常。"""
    try:
        return unseal(value), ""
    except SecretUnavailable as exc:
        return "", exc.code


def seal_provider_config(configs: Any) -> Any:
    """写 provider_config 整表前调用：只处理各家 api_key，幂等。"""
    if not isinstance(configs, dict):
        return configs
    for cfg in configs.values():
        if isinstance(cfg, dict) and cfg.get("api_key"):
            cfg["api_key"] = seal(cfg["api_key"])
    return configs


def unseal_provider_config(configs: Any) -> Any:
    """展示/导出/下发前调用；解不开的项置空，避免密文出现在响应里。"""
    if not isinstance(configs, dict):
        return configs
    for cfg in configs.values():
        if isinstance(cfg, dict) and cfg.get("api_key"):
            plain, _err = read_secret(cfg["api_key"])
            cfg["api_key"] = plain
    return configs


def migrate(db) -> dict[str, int]:
    """把已明文落库的 secret / api_key 就地封存。单事务、逐条回验、幂等。"""
    key = _key()
    counts = {"credentials": 0, "api_keys": 0, "undecryptable": 0}
    if key is None:
        return counts
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT id, secret FROM provider_credentials WHERE secret != ''"
        ).fetchall()
        for row in rows:
            raw = str(row["secret"])
            if is_sealed(raw):
                try:
                    unseal_text(raw, key)
                except (CryptoVerifyError, ValueError):
                    counts["undecryptable"] += 1
                continue
            sealed = seal_text(raw, key)
            # 写回前回验：密钥错、后端异常都不能把原值弄坏
            if unseal_text(sealed, key) != raw:
                raise SecretUnavailable("verify_failed", "凭据封装回验失败，已中止迁移")
            conn.execute("UPDATE provider_credentials SET secret = ? WHERE id = ?", (sealed, row["id"]))
            counts["credentials"] += 1

        raw_cfg = _json_setting(conn, "provider_config")
        if isinstance(raw_cfg, dict):
            changed = 0
            for cfg in raw_cfg.values():
                if not isinstance(cfg, dict):
                    continue
                value = str(cfg.get("api_key") or "")
                if not value or is_sealed(value):
                    continue
                sealed = seal_text(value, key)
                if unseal_text(sealed, key) != value:
                    raise SecretUnavailable("verify_failed", "api_key 封装回验失败，已中止迁移")
                cfg["api_key"] = sealed
                changed += 1
            if changed:
                _put_json_setting(conn, "provider_config", raw_cfg)
                counts["api_keys"] = changed
        conn.commit()
        return counts
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def status(db) -> dict[str, Any]:
    """只读汇总，供 /api/security/secret-at-rest；不返回任何明文或密文片段。"""
    state = key_state()
    counts = {
        "credentials_total": 0,
        "credentials_sealed": 0,
        "credentials_plaintext": 0,
        "api_keys_total": 0,
        "api_keys_sealed": 0,
        "api_keys_plaintext": 0,
        "undecryptable": 0,
        "sessions_plaintext": 0,
    }
    conn = db.connect()
    try:
        for row in conn.execute(
            "SELECT secret FROM provider_credentials WHERE secret != ''"
        ).fetchall():
            counts["credentials_total"] += 1
            raw = str(row["secret"])
            if not is_sealed(raw):
                counts["credentials_plaintext"] += 1
                continue
            counts["credentials_sealed"] += 1
            plain, err = read_secret(raw)
            if err:
                counts["undecryptable"] += 1

        raw_cfg = _json_setting(conn, "provider_config")
        if isinstance(raw_cfg, dict):
            for cfg in raw_cfg.values():
                if not isinstance(cfg, dict) or not cfg.get("api_key"):
                    continue
                counts["api_keys_total"] += 1
                if is_sealed(str(cfg["api_key"])):
                    counts["api_keys_sealed"] += 1
                    plain, err = read_secret(cfg["api_key"])
                    if err:
                        counts["undecryptable"] += 1
                else:
                    counts["api_keys_plaintext"] += 1

        row = conn.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE token NOT LIKE ?", (SESSION_TOKEN_PREFIX + "%",)
        ).fetchone()
        counts["sessions_plaintext"] = int(row["n"] or 0)
    finally:
        conn.close()
    counts["key_configured"] = state == "ok"
    counts["key_state"] = state
    counts["required"] = required()
    counts["mode"] = "encrypted" if state == "ok" else "plaintext"
    return counts


def bootstrap(db) -> dict[str, Any]:
    """启动时调用：缺密钥告警/严格模式拒绝启动，有密钥则一次性迁移。"""
    state = key_state()
    if state != "ok":
        message = (
            "UNIFIED_BACKUP_ENCRYPTION_KEY 缺失，上游凭据将以明文落库；"
            "已有密文凭据在本次运行内不可用"
            if state == "missing"
            else "UNIFIED_BACKUP_ENCRYPTION_KEY 不是合法的 32 字节 base64，凭据加密不可用"
        )
        log.error(message)
        if required():
            raise RuntimeError(message + "（UNIFIED_SECRET_AT_REST_REQUIRED=1）")
        return status(db)
    counts = migrate(db)
    if counts["undecryptable"]:
        log.error("有 %d 个凭据无法用当前密钥解开，相关 provider 将按无可用凭据处理", counts["undecryptable"])
    if counts["credentials"] or counts["api_keys"]:
        log.info(
            "凭据落库加密迁移完成：credentials=%d api_keys=%d",
            counts["credentials"],
            counts["api_keys"],
        )
    return status(db)


def _json_setting(conn: sqlite3.Connection, key: str) -> Any:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return {}
    try:
        return json.loads(row["value"])
    except (TypeError, json.JSONDecodeError):
        return {}


def _put_json_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    """不提交：迁移要整体成功或整体回滚。"""
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )
