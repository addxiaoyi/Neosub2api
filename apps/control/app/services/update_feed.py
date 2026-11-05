"""在线更新的发布源：拉 manifest + 签名、验签、按 TTL 缓存，并给出本机可执行能力边界。

默认 UNIFIED_UPDATE_ENABLED=false：不开开关就一个网络请求都不发，现有部署行为不变。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import httpx

from .. import __version__
from ..api_utils import exc_summary, mask_text, safe_url
from ..config import REPO_ROOT
from ..services import update_manifest as mf
from ..services.update_manifest import UpdateError
from ..services.update_signing import (
    SigningError,
    backend_info as signing_backend_info,
    decode_signature,
    load_public_key,
    verify,
)

REQUEST_TIMEOUT_SEC = 8.0
SIGNATURE_LIMIT_BYTES = 8 * 1024
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
APP_DIR = Path("apps") / "control" / "app"


class FeedError(UpdateError):
    """拉取/验签链条上的失败；reason 会被原样展示，因此消息必须可读、无密钥。"""


class MissingSignature(FeedError):
    """发布物没有 .sig：按策略拒绝，而不是「先装上再说」。"""


class SignatureInvalid(FeedError):
    """签名存在但验不过，包含被篡改的 manifest。"""


class FetchStatus(FeedError):
    def __init__(self, status: int, url: str):
        super().__init__(f"发布源返回 HTTP {status}：{safe_url(url)}")
        self.status = status


def detect_container() -> bool:
    """容器判定：/.dockerenv 之外再看 cgroup，兼容 containerd/k8s 运行时。"""
    if Path("/.dockerenv").exists():
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    marks = ("docker", "containerd", "kubepods", "lxc")
    return any(mark in cgroup for mark in marks)


def app_dir_mounted(repo_root: Path = REPO_ROOT) -> bool:
    """判断 app 目录是否落在某个挂载点之下。

    目录交换靠 rename；容器里如果 app/ 是镜像层而不是绑挂，换完一重建就回退，
    所以「能改文件」不等于「能更新」——必须确认挂载链存在。
    """
    target = (repo_root / APP_DIR).resolve()
    current = target
    while True:
        try:
            if os.path.ismount(current):
                return True
        except OSError:
            return False
        if current == repo_root.resolve() or current.parent == current:
            return False
        current = current.parent


def capability(settings: Any, repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    """返回 in_container / can_apply / updater_found / reason。"""
    in_container = detect_container()
    if not settings.update_enabled:
        return {
            "in_container": in_container,
            "updater_found": False,
            "can_apply": False,
            "reason": "在线更新未启用（UNIFIED_UPDATE_ENABLED=false）",
        }
    missing: list[str] = []
    if not (settings.update_manifest_url or "").strip():
        missing.append("UNIFIED_UPDATE_MANIFEST_URL")
    if not Path(settings.update_pubkey).is_file():
        missing.append(f"公钥文件 {settings.update_pubkey}")
    if missing:
        return {
            "in_container": in_container,
            "updater_found": False,
            "can_apply": False,
            "reason": "在线更新缺配置：" + "、".join(missing),
        }
    if in_container:
        if app_dir_mounted(repo_root):
            return {"in_container": True, "updater_found": True, "can_apply": True, "reason": ""}
        return {
            "in_container": True,
            "updater_found": True,
            "can_apply": False,
            "reason": (
                "容器内 apps/control/app 不是绑定挂载：替换文件后重建镜像即回退，"
                "请按 docs/DEPLOYMENT.md 把代码目录绑挂进容器并配 restart: unless-stopped"
            ),
        }
    if (settings.service_name or "").strip():
        return {"in_container": False, "updater_found": True, "can_apply": True, "reason": ""}
    return {
        "in_container": False,
        "updater_found": True,
        "can_apply": False,
        "reason": (
            "裸机部署未配置 UNIFIED_SERVICE_NAME（Windows 需能用 sc 管理、Linux 需能用 "
            "systemctl restart）：换完文件无法自动拉起服务，拒绝在线更新"
        ),
    }


class UpdateFeed:
    """进程内单例；缓存 TTL 用 UNIFIED_UPDATE_CHECK_TTL_SEC，force 时绕过。"""

    def __init__(self, settings: Any, *, timeout: float | None = None, repo_root: Path = REPO_ROOT):
        self.settings = settings
        self.timeout = float(timeout if timeout is not None else REQUEST_TIMEOUT_SEC)
        self.repo_root = repo_root
        self._cached: dict[str, Any] | None = None
        self._cached_at = 0.0
        self._manifest: dict[str, Any] | None = None
        self._signed_ok = False

    def summary(self, *, force: bool = False) -> dict[str, Any]:
        ttl = max(0, int(getattr(self.settings, "update_check_ttl_sec", 0) or 0))
        fresh = self._cached is not None and (time.time() - self._cached_at) < ttl
        cached = not (force or not fresh)
        if not cached:
            self._cached = self._check()
            self._cached_at = time.time()
        assert self._cached is not None
        payload = dict(self._cached)
        payload["cached"] = cached
        return payload

    def last_manifest(self) -> dict[str, Any] | None:
        """最近一次验签通过的 manifest；apply 只认这份，不重新相信前端传的东西。"""
        return self._manifest

    def _check(self) -> dict[str, Any]:
        # 先清掉上一轮的 manifest：检测失败时绝不能让旧包还能被 apply
        self._manifest = None
        self._signed_ok = False
        cap = capability(self.settings, self.repo_root)
        payload: dict[str, Any] = {
            "current_version": __version__,
            "latest_version": __version__,
            "available": False,
            "channel": mf.CHANNEL,
            "signature": "none",
            "updater_found": cap["updater_found"],
            "in_container": cap["in_container"],
            "can_apply": cap["can_apply"],
            "reason": cap["reason"],
            "checked_at": int(time.time()),
            "error": "",
            # 验签走 lib 还是纯标准库，必须能从状态里看出来，不允许静默切换
            "crypto_backend": signing_backend_info(),
        }
        if not cap["updater_found"]:
            return payload
        try:
            manifest = self._verified_manifest()
        except MissingSignature as exc:
            payload.update(signature="unsigned", reason=masked(exc))
            return payload
        except (SignatureInvalid, SigningError) as exc:
            payload.update(signature="invalid", reason=masked(exc))
            return payload
        except UpdateError as exc:
            # 签名过了但版本/平台/大小不合规：如实报 verified，别把「包合法但版本不对」说成没签名
            payload.update(signature="verified" if self._signed_ok else "none", reason=masked(exc))
            return payload
        payload.update(
            latest_version=str(manifest["version"]),
            available=True,
            signature="verified",
            reason="",
        )
        self._manifest = manifest
        return payload

    def _verified_manifest(self) -> dict[str, Any]:
        url = str(self.settings.update_manifest_url).strip()
        assert_transport(url)
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=True, trust_env=False) as client:
                manifest_bytes = self._get(client, url, mf.MANIFEST_LIMIT_BYTES)
                sig_bytes = self._get(client, url + mf.SIG_SUFFIX, SIGNATURE_LIMIT_BYTES)
        except MissingSignature:
            raise
        except httpx.TimeoutException as exc:
            raise FeedError(f"拉取发布源超时（{self.timeout:.0f}s）：{safe_url(url)}") from exc
        except httpx.HTTPError as exc:
            raise FeedError(f"拉取发布源失败：{exc_summary(exc)}") from exc

        public_key = load_public_key(Path(self.settings.update_pubkey).read_text(encoding="utf-8"))
        signature = decode_signature(sig_bytes)
        if not verify(public_key, manifest_bytes, signature):
            raise SignatureInvalid("manifest 签名校验失败：发布物被篡改或公钥不匹配")
        self._signed_ok = True
        manifest = mf.validate_manifest(manifest_bytes)
        mf.check_compatible(manifest, __version__)
        return manifest

    def _get(self, client: httpx.Client, url: str, limit: int) -> bytes:
        with client.stream("GET", url) as response:
            final = str(response.url)
            assert_transport(final)
            if response.status_code == 404 and url.endswith(mf.SIG_SUFFIX):
                raise MissingSignature(f"发布物没有签名文件：{safe_url(url)}")
            if response.status_code != 200:
                raise FetchStatus(response.status_code, url)
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > limit:
                    raise FeedError(f"{safe_url(url)} 超过 {limit} 字节上限")
                chunks.append(chunk)
            return b"".join(chunks)


def assert_transport(url: str) -> None:
    """只走 HTTPS；http 仅对本机回环放开（测试与内网自托管用），其余一律拒。"""
    parsed = httpx.URL(url)
    host = parsed.host or ""
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and host in LOOPBACK_HOSTS:
        return
    raise FeedError(f"发布源必须是 HTTPS（本机回环除外），实际 {parsed.scheme}://{host}")


def masked(exc: Exception) -> str:
    """reason 直接回前端，统一脱敏并截断，避免把 URL 里的凭据带出去。"""
    return mask_text(str(exc))[:300]
