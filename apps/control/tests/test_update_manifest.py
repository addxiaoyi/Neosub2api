"""发布物校验口径：验签、manifest 结构、路径白名单、逐文件 sha256、大小上限。

这些用例都跑在临时目录、不连网络，两个平台同一套断言（因此不带 [env] 跳过）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from app.services import update_manifest as mf
from app.services import update_signing
from app.services.update_signing import load_public_key, public_pem, sign, verify

SEED = bytes(range(32))
PUBLIC = load_public_key(public_pem(SEED))
HAS_LIB = update_signing.lib_available()

# RFC 8032 §7.1 第一组测试向量：空消息，用来同时验证两条实现与历史签名格式
RFC_SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
RFC_PUB = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
RFC_SIG = bytes.fromhex(
    "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
    "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
)


def payload(files: dict[str, bytes]) -> list[dict]:
    return [
        {"path": path, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        for path, data in sorted(files.items())
    ]


def make_zip(tmp_path: Path, files: dict[str, bytes], *, extra: bool = False) -> Path:
    archive = tmp_path / "release.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for path, data in files.items():
            bundle.writestr(path, data)
        if extra:
            bundle.writestr("apps/control/app/extra.py", "# 未声明\n".encode())
    return archive


def make_manifest(
    files: dict[str, bytes],
    *,
    version: str = "0.2.0",
    manifest_version: int = 1,
    channel: str = "stable",
    platform: str = "any",
    min_current: str = "0.0.0",
) -> tuple[dict, bytes, bytes]:
    manifest = {
        "manifest_version": manifest_version,
        "version": version,
        "channel": channel,
        "platform": platform,
        "issued_at": 1793877187,
        "commit": "deadbeef",
        "min_current_version": min_current,
        "files": payload(files),
    }
    raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode()
    return manifest, raw, sign(SEED, raw)


def signed_manifest(files: dict[str, bytes]):
    manifest, raw, signature = make_manifest(files)
    return mf.validate_manifest(raw), signature


def test_valid_manifest_and_archive_pass(tmp_path: Path):
    files = {
        "apps/control/app/main.py": b"print('v2')\n",
        "apps/web/out/index.html": b"<html>v2</html>",
        "scripts/backup_core.py": b"# backup v2\n",
        "CHANGELOG.md": b"## 0.2.0\n",
    }
    manifest, raw, signature = make_manifest(files)
    assert verify(PUBLIC, raw, signature) is True
    archive = make_zip(tmp_path, files)
    mf.verify_archive(archive, mf.validate_manifest(raw), 268435456)

    dest = tmp_path / "stage"
    mf.extract_archive(archive, mf.validate_manifest(raw), dest)
    assert (dest / "apps/control/app/main.py").read_bytes() == b"print('v2')\n"
    assert sorted(p.name for p in dest.iterdir()) == ["CHANGELOG.md", "apps", "scripts"]


def test_tampered_manifest_fails_signature():
    files = {"apps/control/app/main.py": b"x"}
    manifest, raw, signature = make_manifest(files)
    tampered = raw.replace(b'"version": "0.2.0"', b'"version": "9.9.9"')
    assert tampered != raw
    assert verify(PUBLIC, tampered, signature) is False
    assert verify(PUBLIC, raw, signature) is True


def test_tampered_file_is_rejected(tmp_path: Path):
    files = {"apps/control/app/main.py": b"AAAA"}
    manifest, raw, _ = make_manifest(files)
    archive = make_zip(tmp_path, {"apps/control/app/main.py": b"BBBB"})
    with pytest.raises(mf.UpdateError) as caught:
        mf.verify_archive(archive, mf.validate_manifest(raw), 268435456)
    assert "sha256" in str(caught.value)


def test_downgrade_and_same_version_rejected():
    files = {"apps/control/app/main.py": b"x"}
    _, raw, _ = make_manifest(files, version="0.2.0")
    manifest = mf.validate_manifest(raw)
    with pytest.raises(mf.UpdateError) as equal:
        mf.check_compatible(manifest, "0.2.0")
    assert "拒绝降级" in str(equal.value)

    _, older, _ = make_manifest(files, version="0.1.0")
    with pytest.raises(mf.UpdateError) as downgrade:
        mf.check_compatible(mf.validate_manifest(older), "0.3.0")
    assert "拒绝降级" in str(downgrade.value)

    _, blocked, _ = make_manifest(files, version="0.4.0", min_current="0.3.0")
    with pytest.raises(mf.UpdateError) as floor:
        mf.check_compatible(mf.validate_manifest(blocked), "0.2.0")
    assert "最低要求" in str(floor.value)


def test_unknown_manifest_version_rejected():
    _, raw, _ = make_manifest({"apps/control/app/main.py": b"x"}, manifest_version=2)
    with pytest.raises(mf.UpdateError) as caught:
        mf.validate_manifest(raw)
    assert "未知的 manifest_version" in str(caught.value)


def test_channel_and_platform_mismatch_rejected():
    _, raw, _ = make_manifest({"apps/control/app/main.py": b"x"}, channel="beta")
    with pytest.raises(mf.UpdateError) as channel:
        mf.check_compatible(mf.validate_manifest(raw), "0.1.0")
    assert "通道" in str(channel.value)

    _, raw, _ = make_manifest({"apps/control/app/main.py": b"x"}, platform="plan9")
    with pytest.raises(mf.UpdateError) as platform:
        mf.check_compatible(mf.validate_manifest(raw), "0.1.0")
    assert "平台" in str(platform.value)


@pytest.mark.parametrize(
    "bad_member",
    [
        "apps/control/app/../../../evil.py",
        "/etc/cron.d/evil",
        "apps/control/app/../config/prod.json",
        "data/unified.db",
        "config/pricing.json",
    ],
)
def test_zip_slip_and_out_of_whitelist_rejected(tmp_path: Path, bad_member: str):
    files = {"apps/control/app/main.py": b"x"}
    manifest, raw, _ = make_manifest(files)
    archive = make_zip(tmp_path, files)
    with zipfile.ZipFile(archive, "a") as bundle:
        bundle.writestr(bad_member, b"boom")
    with pytest.raises(mf.UpdateError):
        mf.verify_archive(archive, mf.validate_manifest(raw), 268435456)


def test_undeclared_member_rejected(tmp_path: Path):
    files = {"apps/control/app/main.py": b"x"}
    _, raw, _ = make_manifest(files)
    archive = make_zip(tmp_path, files, extra=True)
    with pytest.raises(mf.UpdateError) as caught:
        mf.verify_archive(archive, mf.validate_manifest(raw), 268435456)
    assert "未声明" in str(caught.value)


def test_size_limit_rejected(tmp_path: Path):
    files = {"apps/control/app/main.py": b"x" * 4096}
    _, raw, _ = make_manifest(files)
    archive = make_zip(tmp_path, files)
    with pytest.raises(mf.UpdateError) as caught:
        mf.verify_archive(archive, mf.validate_manifest(raw), 1024)
    assert "上限" in str(caught.value)


def test_manifest_declared_size_limit_rejected():
    files = {"apps/control/app/main.py": b"x" * 4096}
    _, raw, _ = make_manifest(files)
    manifest = mf.validate_manifest(raw)
    with pytest.raises(mf.UpdateError) as caught:
        mf.verify_archive(Path("missing.zip"), manifest, 1024)
    assert "上限" in str(caught.value)


def test_signature_base64_must_be_64_bytes():
    _, raw, signature = make_manifest({"apps/control/app/main.py": b"x"})
    assert len(base64.b64decode(base64.b64encode(signature))) == 64


# ---- 双实现互通 / 签名格式不变 ----


def test_rfc8032_vector_matches_stdlib_and_lib():
    assert update_signing.sign_with(RFC_SEED, b"", "stdlib") == RFC_SIG
    assert update_signing.verify_with(RFC_PUB, b"", RFC_SIG, "stdlib") is True
    if HAS_LIB:
        assert update_signing.sign_with(RFC_SEED, b"", "lib") == RFC_SIG
        assert update_signing.verify_with(RFC_PUB, b"", RFC_SIG, "lib") is True


def test_signature_format_is_unchanged_across_backends():
    _, raw, _ = make_manifest({"apps/control/app/main.py": b"x"})
    stdlib_sig = update_signing.sign_with(SEED, raw, "stdlib")
    assert len(stdlib_sig) == 64
    # 可塑性防线仍在：S 必须落在 [0, L)，否则任何实现都该判失败
    assert int.from_bytes(stdlib_sig[32:], "little") < update_signing.L
    assert update_signing.verify_with(PUBLIC, raw, stdlib_sig, "stdlib") is True
    if HAS_LIB:
        assert update_signing.sign_with(SEED, raw, "lib") == stdlib_sig


@pytest.mark.skipif(not HAS_LIB, reason="未安装 cryptography，无法验证双实现互通")
def test_stdlib_signature_verifies_in_lib():
    _, raw, _ = make_manifest({"apps/control/app/main.py": b"x"})
    signature = update_signing.sign_with(SEED, raw, "stdlib")
    assert update_signing.verify_with(PUBLIC, raw, signature, "lib") is True
    assert update_signing.verify_with(PUBLIC, raw + b" ", signature, "lib") is False


@pytest.mark.skipif(not HAS_LIB, reason="未安装 cryptography，无法验证双实现互通")
def test_lib_signature_verifies_in_stdlib():
    _, raw, _ = make_manifest({"apps/control/app/main.py": b"x"})
    signature = update_signing.sign_with(SEED, raw, "lib")
    assert signature == update_signing.sign_with(SEED, raw, "stdlib")
    assert update_signing.verify_with(PUBLIC, raw, signature, "stdlib") is True
    tampered = bytearray(signature)
    tampered[0] ^= 0x01
    assert update_signing.verify_with(PUBLIC, raw, bytes(tampered), "stdlib") is False


def test_backend_is_labelled_and_fallback_has_reason():
    info = update_signing.backend_info()
    assert info["implementation"] in {"lib", "stdlib"}
    if info["implementation"] == "stdlib":
        assert info["reason"]  # 回落必须有原因，不能静默
    assert update_signing.backend() == info["implementation"]
