#!/usr/bin/env python3
"""生成新 provider 的最小骨架：适配器、测试、i18n 占位与 BUILDERS 接线说明。

用法：
  python scripts/new_provider.py --name demo-echo --capabilities chat
  python scripts/new_provider.py --name demo-echo --base-url https://api.demo.example \
      --capabilities chat,messages --dry-run

默认写到仓库内（--out 指仓库根）：
  apps/control/app/adapters/<module>.py
  apps/control/tests/test_<module>.py
  i18n/<module>.json            # 待人工合并进 apps/web/lib/i18n/locales/*.json

命令只生成文件，不修改 `__init__.py`、`config.py` 或任何前端代码；接线提示打印在 stdout。

退出码：0 成功；2 参数非法、id 已被占用、目标文件已存在。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CAPABILITY_SOURCE = REPO / "apps" / "control" / "app" / "adapters" / "base.py"
ID_IN_USE_RE = re.compile(r'id="([A-Za-z0-9._-]+)"')
CAPS_RE = re.compile(r"CAPABILITY_KEYS\s*=\s*\(([^)]+)\)", re.S)
QUOTED_RE = re.compile(r'"([^"]+)"')
ID_SHAPE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

# 只有这三个能力位受「声明为真就必须有真实覆写」的守卫约束
# （apps/control/tests/test_adapters_real.py::test_declared_capability_is_not_silently_stubbed）
GUARDED = {
    "chat": ("chat_completion", "/v1/chat/completions", "openai.chat.completions"),
    "messages": ("messages", "/v1/messages", "anthropic.messages"),
    "responses": ("responses", "/v1/responses", "openai.responses"),
}

# 声明后基类仍是 501、但不在守卫覆盖范围的能力位：生成时提醒，避免留下「假能力」
UNGUARDED = {"oauth", "checkin", "credits", "register", "sidecar"}

TODO_BODY = {
    "chat": (
        "        # TODO(协议)：确认上游真的是 OpenAI /v1/chat/completions 同形端点再删掉本段。\n"
        "        # 不同形要在这里改：路径、模型名映射、messages 结构、必填字段与鉴权头。\n"
        "        # 流式若返回非 `data:` 帧（逐行 JSON、嵌套信封、私有事件名），\n"
        "        # 必须在这里做增量翻译，不要整包拼接后再伪造 stream。\n"
    ),
    "messages": (
        "        # TODO(协议)：Anthropic 端点常见差异——鉴权头（x-api-key + anthropic-version）、\n"
        "        # 非标准路径（如 /anthropic/v1/messages）、只支持非流式的上游。\n"
        "        # 上游只给非流式时，stream=True 必须显式 501，参照 deepseek_sidecar。\n"
    ),
    "responses": (
        "        # TODO(协议)：/v1/responses 的入参不是 chat 的子集（input / instructions 等），\n"
        "        # 先确认上游同形再转发；流式事件名是 response.output_text.delta 一类，别按 chat 解析。\n"
    ),
}

I18N_LOCALES = ("zh-CN", "zh-TW", "en", "ja", "ko")


class ScaffoldError(Exception):
    """参数或落盘前置条件不满足。"""


def load_capability_keys() -> list[str]:
    """从 base.py 读 CAPABILITY_KEYS，避免脚本里再存一份能力位清单。"""
    try:
        text = CAPABILITY_SOURCE.read_text(encoding="utf-8")
    except OSError as exc:
        raise ScaffoldError(f"读不到 {CAPABILITY_SOURCE}：{exc}") from exc
    match = CAPS_RE.search(text)
    if match is None:
        raise ScaffoldError(f"{CAPABILITY_SOURCE} 里没有 CAPABILITY_KEYS 定义，脚本已与源码脱节")
    return QUOTED_RE.findall(match.group(1))


def class_name(module: str) -> str:
    return "".join(part.capitalize() for part in module.split("_")) + "Adapter"


def module_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def parse_capabilities(raw: str, known: list[str]) -> list[str]:
    wanted: list[str] = []
    for item in (part.strip() for part in raw.split(",")):
        if not item:
            continue
        if item not in known:
            raise ScaffoldError(f"未知能力位「{item}」，可用：{', '.join(known)}")
        if item not in wanted:
            wanted.append(item)
    if not wanted:
        raise ScaffoldError("至少要声明一个能力位（--capabilities chat 之类）")
    return wanted


def assert_id_free(provider_id: str, adapters_dir: Path) -> None:
    for path in sorted(adapters_dir.glob("*.py")):
        if provider_id in ID_IN_USE_RE.findall(path.read_text(encoding="utf-8")):
            raise ScaffoldError(f"id「{provider_id}」已被 {path.name} 占用；provider id 上线后不可改名")


def render_adapter(provider_id: str, module: str, caps: list[str], base_url: str) -> str:
    forwards = [cap for cap in ("chat", "messages", "responses") if cap in caps]
    protocols = [GUARDED[cap][2] for cap in forwards]
    base_cls = "OpenAICompatAdapter" if forwards else "ProviderAdapter"
    imports = ["from ..config import Settings"]
    if forwards:
        imports.insert(0, "from typing import Any")
        imports.insert(1, "")
        imports.append("from .base import ProviderError, ProviderSpec, stub_capabilities")
        imports.append("from .openai_compat import OpenAICompatAdapter")
    else:
        imports.append("from .base import ProviderAdapter, ProviderSpec, stub_capabilities")
    imports_text = "\n".join(imports) + "\n\n\n"
    caps_arg = ", ".join(f"{cap}=True" for cap in caps)
    protocols_arg = ", ".join(f'"{value}"' for value in protocols)
    # 单元素必须带尾逗号，否则 ("a") 是字符串不是 tuple
    protocols_expr = f"({protocols_arg},)" if len(protocols) == 1 else f"({protocols_arg})"
    cls = class_name(module)
    display = " ".join(part.capitalize() for part in re.split(r"[-_.]", provider_id) if part)
    env_prefix = module.upper()
    if forwards and not base_url:
        readiness = 'readiness="needs_config",\n'
        requires = '        requires=("上游地址由凭据/页面下发，default_base_url 为空",),\n'
    else:
        readiness = 'readiness="ready",\n'
        requires = ""

    head = f'''"""`{provider_id}` provider 适配器（脚手架生成，TODO 补齐协议细节）。

上游：TODO：owner/repo 与一句话定位。
许可证：TODO：核实上游 LICENSE 原文；核实前 risk_level 保持 warn，不要直接标 safe。

三条硬规则（新增 provider 时最容易踩的坑）：
1. 密钥放凭据库：api_key 只经 configure() 注入，不写进 ProviderSpec、不写进代码、不写进 env。
2. 地址用凭据覆盖：base_url 由 providers 表或凭据下发，default_base_url 只是兜底值。
3. 不要写进 env：不新增 {env_prefix}_BASE / {env_prefix}_API_KEY；只有 sidecar 必须固定本机
   地址时，才按 docs/ADDING-A-PROVIDER.md §2 同步 config.py 的 ENV_BASE_VARS / Settings /
   ENV_BASE_FIELDS 三处。
"""

{imports_text}class {cls}({base_cls}):
    """TODO：写清上游协议与边界——哪些能力真支持，哪些留给 sidecar 或后续补。"""

    spec = ProviderSpec(
        id="{provider_id}",
        name="{display}",
        vendor="TODO：上游 owner/repo",
        license="TODO：核实上游 LICENSE",
        runtime="主仓内置（直连）",
        status="implemented",
        description="TODO：一句话说清它提供什么、边界在哪。",
        default_base_url="{base_url}",
        capabilities=stub_capabilities({caps_arg}),
        protocols={protocols_expr},
        risk_level="warn",
        risk_note="脚手架默认 warn：核实 LICENSE 为 MIT/Apache/BSD 后改 safe 并清空本字段。",
        {readiness}{requires}    )
'''

    if not forwards:
        head += (
            "\n    def __init__(self, settings: Settings, base_url: str = \"\") -> None:\n"
            "        self._settings = settings\n"
            "        super().__init__(base_url)\n"
            "\n    # TODO：按 grok_register 的形态实现任务队列/状态机；health() 要如实反映外部执行器是否配置。\n"
        )
        return (
            head
            + f'\n\ndef build(settings: Settings, base_url: str = "") -> {cls}:\n'
            + f"    return {cls}(settings, base_url)\n"
        )

    head += (
        "\n    def _client(self, timeout: float | None = None):\n"
        "        # 没凭证先在本地失败，别让「忘记填 key」伪装成上游 401\n"
        f'        if not self.api_key:\n            raise ProviderError(503, "{provider_id} 未配置 api_key（服务商页凭据）")\n'
        "        return super()._client(timeout)\n"
    )

    for cap in forwards:
        method, path, _ = GUARDED[cap]
        head += (
            f"\n    async def {method}(self, payload: dict[str, Any], *, stream: bool):\n"
            f"{TODO_BODY[cap]}"
            f'        return await self._client().forward("{path}", payload, stream=stream)\n'
        )

    head += f'\n\ndef build(settings: Settings, base_url: str = "") -> {cls}:\n    return {cls}(settings, base_url)\n'
    return head


def render_test(provider_id: str, module: str, caps: list[str]) -> str:
    cls = class_name(module)
    forwards = [cap for cap in ("chat", "messages", "responses") if cap in caps]
    declared = ", ".join(f'"{cap}"' for cap in caps)
    base_import = "ProviderAdapter, ProviderError" if "chat" in caps else "ProviderAdapter"
    json_import = "import json\n\n" if {"messages", "responses"} & set(caps) else ""

    parts = [
        f'''"""`{provider_id}` 脚手架测试：声明位与真实覆写一致，转发链路打到假上游。

复制进 apps/control/tests 后可直接 pytest；假上游由 conftest 启动（默认 8061）。
"""

from __future__ import annotations

{json_import}from pathlib import Path
from typing import Any

import pytest

from app.adapters.base import {base_import}
from app.adapters.{module} import {cls}, build

pytestmark = pytest.mark.providers


def _settings(tmp: Path) -> Any:
    """最小 Settings：适配器只读 data_dir 与地址字段；新增配置项时在这里同步。"""
    from app.config import Settings

    return Settings(
        data_dir=tmp,
        db_path=tmp / "unified.db",
        web_dir=tmp / "web",
        admin_username="admin",
        admin_password="test",
        session_ttl_sec=3600,
        cookie_secure=False,
        codebuddy_base="http://127.0.0.1:7899",
        codebuddy_api_key="",
        opencode_upstream_base="http://127.0.0.1:7899",
        grok_register_executor_url="",
        grok_register_max_concurrency=1,
        grok_register_max_workers=2,
        allow_anonymous_gateway=False,
        cors_origins=(),
        allow_private_upstreams=False,
    )


def test_spec_matches_declaration() -> None:
    assert {cls}.spec.id == "{provider_id}"
    assert [cap for cap, on in {cls}.spec.capabilities.items() if on] == [{declared}]


def test_declared_capability_has_real_override(tmp_path: Path) -> None:
    """与 test_adapters_real 的能力守卫同规则：声明为真就不能落回基类 501。"""
    adapter = build(_settings(tmp_path))
    for cap, attr in (("chat", "chat_completion"), ("messages", "messages"), ("responses", "responses")):
        if not adapter.spec.capabilities.get(cap):
            continue
        assert getattr(type(adapter), attr) is not getattr(ProviderAdapter, attr), (
            f"{{cap}} 声明为真但仍是基类桩，网关会 501"
        )
'''
    ]

    if "chat" in caps:
        parts.append(
            f'''


async def test_chat_forwards_to_upstream(fake_base: str, tmp_path: Path) -> None:
    adapter = build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="scaffold-key")
    payload = {{"model": "fake-chat-1", "messages": [{{"role": "user", "content": "hi"}}]}}

    plain = await adapter.chat_completion(payload, stream=False)
    assert plain.status_code == 200
    assert b"model=fake-chat-1" in (plain.body or b"")

    streamed = await adapter.chat_completion({{**payload, "stream": True}}, stream=True)
    raw = b""
    async for chunk in streamed.iter_bytes():
        raw += chunk
    assert raw.endswith(b"data: [DONE]\\n\\n")


async def test_missing_credentials_is_explicit_503(tmp_path: Path) -> None:
    adapter = build(_settings(tmp_path), "http://127.0.0.1:19999")
    with pytest.raises(ProviderError) as caught:
        await adapter.chat_completion({{"model": "m", "messages": []}}, stream=False)
    assert caught.value.status_code == 503
'''
        )

    if "messages" in caps:
        parts.append(
            '''


async def test_messages_forwards_to_upstream(fake_base: str, tmp_path: Path) -> None:
    adapter = build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="scaffold-key")
    reply = await adapter.messages(
        {"model": "fake-chat-1", "max_tokens": 32, "messages": [{"role": "user", "content": "hi"}]},
        stream=False,
    )
    assert reply.status_code == 200
    assert json.loads(reply.body or b"{}")["type"] == "message"
'''
        )

    if "responses" in caps:
        parts.append(
            '''


async def test_responses_forwards_to_upstream(fake_base: str, tmp_path: Path) -> None:
    adapter = build(_settings(tmp_path), fake_base)
    adapter.configure(api_key="scaffold-key")
    reply = await adapter.responses({"model": "fake-chat-1", "input": "hi"}, stream=False)
    assert reply.status_code == 200
    assert json.loads(reply.body or b"{}")["object"] == "response"
'''
        )

    if not forwards:
        parts.append(
            '''
def test_non_inference_provider_builds(tmp_path: Path) -> None:
    adapter = build(_settings(tmp_path))
    assert adapter.spec.id == "%s"
    assert not adapter.spec.protocols, "非推理 provider 不应声明协议"
'''
            % provider_id
        )

    return "\n\n\n".join(part.strip("\n") for part in parts) + "\n"


def render_i18n(provider_id: str, module: str) -> str:
    key = module.split("_")[0] + "".join(part.capitalize() for part in module.split("_")[1:]) + "Note"
    payload = {
        "_usage": (
            "provider 的 name / description 直接来自 ProviderSpec，不进 i18n；"
            "只有需要给该服务商加界面文案时才合并下面的键。"
            "必须同时加进五个 locale（zh-CN / zh-TW / en / ja / ko），缺一视为未完成。"
        ),
        "locales": {
            locale: {"providers": {key: "TODO：一句话界面文案"}} for locale in I18N_LOCALES
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def render_wiring(module: str) -> str:
    return (
        "接线（两处，漏一处 provider 就不会出现在 /api/providers）：\n"
        "  apps/control/app/adapters/__init__.py\n"
        f"    from . import (..., {module})\n"
        f"    BUILDERS = (..., {module}.build)\n"
    )


def build_plan(args: argparse.Namespace) -> list[tuple[Path, str]]:
    module = module_name(args.name)
    known = load_capability_keys()
    caps = parse_capabilities(args.capabilities, known)

    warn = sorted(set(caps) & UNGUARDED)
    if warn:
        print(
            f"[warn] {', '.join(warn)} 不在一致性守卫覆盖范围：声明后基类仍是 501，记得实现对应方法",
            file=sys.stderr,
        )

    out = Path(args.out).resolve()
    adapters_dir = out / "apps" / "control" / "app" / "adapters"
    if adapters_dir.is_dir():
        assert_id_free(args.name, adapters_dir)

    return [
        (adapters_dir / f"{module}.py", render_adapter(args.name, module, caps, args.base_url)),
        (out / "apps" / "control" / "tests" / f"test_{module}.py", render_test(args.name, module, caps)),
        (out / "i18n" / f"{module}.json", render_i18n(args.name, module)),
    ]


def write_plan(plan: list[tuple[Path, str]]) -> None:
    clashes = [path for path, _ in plan if path.exists()]
    if clashes:
        raise ScaffoldError("目标已存在，拒绝覆盖：" + "、".join(str(path) for path in clashes))
    for path, content in plan:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        print(f"[ok] {path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成新 provider 的最小骨架")
    parser.add_argument("--name", required=True, help="provider id，例 demo-echo（小写，可带 - _ .）")
    parser.add_argument("--base-url", default="", help="default_base_url 兜底值；留空表示由凭据下发")
    parser.add_argument("--capabilities", default="chat", help="逗号分隔，例 chat,messages")
    parser.add_argument("--out", default=str(REPO), help="输出根目录，默认仓库根")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要写入的内容，不落盘")
    args = parser.parse_args(argv)

    try:
        if not ID_SHAPE_RE.match(args.name):
            raise ScaffoldError(f"provider id 非法：{args.name}（只允许小写字母、数字与 . _ -）")
        plan = build_plan(args)
    except ScaffoldError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2

    module = module_name(args.name)
    if args.dry_run:
        for path, content in plan:
            print(f"\n--- dry-run: {path} ({len(content.splitlines())} 行) ---")
            print(content, end="" if content.endswith("\n") else "\n")
        print(f"\n{render_wiring(module)}", end="")
        print("[dry-run] 未写入任何文件")
        return 0

    try:
        write_plan(plan)
    except ScaffoldError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2

    print(f"\n{render_wiring(module)}")
    print(
        "后续（见 docs/ADDING-A-PROVIDER.md）：\n"
        f"  1. python -m pytest apps/control/tests/test_{module}.py -q\n"
        f"  2. 补 docs/providers/{args.name}.md，并同步 docs/PROVIDERS.md 与 docs/PROTOCOL-MATRIX.md\n"
        "  3. 在 upstreams/upstreams.yaml 追加一条（license 与 commit 必须有证据）\n"
        f"  4. 合并 i18n/{module}.json 到五个 locale（没有界面文案就删掉本文件）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
