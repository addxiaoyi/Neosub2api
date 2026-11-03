#!/usr/bin/env python3
"""校验 config/model-aliases.yaml 的结构与内容。

检查项：别名唯一（含 YAML 重复键）、provider 存在于适配器注册表、
weight 在 0-1 且同别名合计为 1、fallback 不成环、capabilities 取值合法。
-Strict 时 upstream_model 为 unknown 也算错误。

退出码：0 通过；1 有校验错误；2 配置缺失 / YAML 语法错误 / 注册表读取失败。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

TRIGGERS = frozenset({"429", "5xx", "timeout", "capability_unsupported"})
CAP_KEYS = ("stream", "tools", "vision", "long_context", "reasoning")
ALIAS_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
SPEC_ID_RE = re.compile(r'^\s+id="([A-Za-z0-9_-]+)"', re.MULTILINE)
BUILDER_RE = re.compile(r"^\s+([A-Za-z_][A-Za-z0-9_]*).build,$", re.MULTILINE)
ROOT_KEYS = {"version", "aliases", "fallbacks", "capabilities", "notes"}
CANDIDATE_KEYS = {"provider", "upstream_model", "weight", "priority"}
FALLBACK_KEYS = {"provider", "upstream_model", "alias", "priority", "on"}


class UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader + 重复键检测；PyYAML 默认会静默让后者覆盖前者。"""


def _construct_mapping(loader: UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    seen: set[Any] = set()
    out: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            hash(key)
        except TypeError:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"键不可哈希：{key!r}",
                key_node.start_mark,
            )
        if key in seen:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"重复键 {key!r}",
                key_node.start_mark,
            )
        seen.add(key)
        out[key] = loader.construct_object(value_node, deep=deep)
    return out


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def load_provider_registry(repo_root: Path) -> dict[str, str]:
    """按 adapters/__init__.py 的 BUILDERS 求真实注册表，返回 {provider_id: 模块名}。"""
    adapters_dir = repo_root / "apps" / "control" / "app" / "adapters"
    init_file = adapters_dir / "__init__.py"
    if not init_file.is_file():
        raise RuntimeError(f"找不到适配器注册表：{init_file}")
    modules = BUILDER_RE.findall(init_file.read_text(encoding="utf-8"))
    if not modules:
        raise RuntimeError(f"{init_file} 里没有解析到 BUILDERS 条目")
    registry: dict[str, str] = {}
    for module in modules:
        source = adapters_dir / f"{module}.py"
        if not source.is_file():
            raise RuntimeError(f"BUILDERS 引用的模块不存在：{source}")
        match = SPEC_ID_RE.search(source.read_text(encoding="utf-8"))
        if not match:
            raise RuntimeError(f"{source} 里没有解析到 ProviderSpec.id")
        registry[match.group(1)] = module
    return registry


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_priority(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def find_cycle(graph: dict[str, set[str]]) -> list[str] | None:
    state: dict[str, int] = {}
    stack: list[str] = []

    def walk(node: str) -> list[str] | None:
        state[node] = 1
        stack.append(node)
        for nxt in graph.get(node, set()):
            if state.get(nxt, 0) == 0:
                found = walk(nxt)
                if found:
                    return found
            elif state.get(nxt) == 1:
                return stack[stack.index(nxt):] + [nxt]
        stack.pop()
        state[node] = 2
        return None

    for node in graph:
        if state.get(node, 0) == 0:
            found = walk(node)
            if found:
                return found
    return None


def validate(doc: Any, registry: dict[str, str], strict: bool) -> tuple[list[str], list[str], dict[str, int]]:
    errors: list[str] = []
    warnings: list[str] = []
    counts = {"aliases": 0, "candidates": 0, "fallbacks": 0, "capabilities": 0, "notes": 0}

    if not isinstance(doc, dict):
        return ["根节点必须是 mapping"], warnings, counts

    for key in doc:
        if key not in ROOT_KEYS:
            warnings.append(f"未知顶层键 {key!r}，当前校验器不识别")

    version = doc.get("version")
    if not is_priority(version) or version < 1:
        errors.append("version 必须是 >= 1 的整数")

    aliases = doc.get("aliases")
    if not isinstance(aliases, dict) or not aliases:
        errors.append("aliases 必须是非空 mapping")
        aliases = {}
    counts["aliases"] = len(aliases)

    # aliases: 别名 -> 候选列表
    for alias, candidates in aliases.items():
        path = f"aliases.{alias}"
        if not isinstance(alias, str):
            errors.append(f"{path}: 别名键必须是字符串")
            continue
        if not ALIAS_KEY_RE.match(alias):
            errors.append(f"{path}: 别名必须是小写且只含 a-z0-9._-（不含 /）")
        if not isinstance(candidates, list) or not candidates:
            errors.append(f"{path}: 候选列表不能为空")
            continue

        seen_pairs: set[tuple[str, str]] = set()
        weight_total = 0.0
        weights_ok = True

        for idx, item in enumerate(candidates):
            item_path = f"{path}[{idx}]"
            counts["candidates"] += 1
            if not isinstance(item, dict):
                errors.append(f"{item_path}: 候选必须是 mapping")
                weights_ok = False
                continue
            for key in item:
                if key not in CANDIDATE_KEYS:
                    warnings.append(f"{item_path}: 未知字段 {key!r}")

            provider = item.get("provider")
            if not isinstance(provider, str) or not provider:
                errors.append(f"{item_path}: provider 缺失或不是字符串")
            elif provider not in registry:
                errors.append(f"{item_path}: provider {provider!r} 不在注册表（可选：{', '.join(sorted(registry))}）")

            model = item.get("upstream_model")
            if not isinstance(model, str) or not model.strip():
                errors.append(f"{item_path}: upstream_model 缺失或为空")
            elif model.strip().lower() == "unknown":
                msg = f"{item_path}: upstream_model 为 unknown，缺少可验证的上游名"
                (errors if strict else warnings).append(msg)
            elif provider and isinstance(provider, str):
                pair = (provider, model)
                if pair in seen_pairs:
                    errors.append(f"{item_path}: 同一别名内重复候选 {provider}/{model}")
                seen_pairs.add(pair)

            weight = item.get("weight")
            if not is_number(weight) or not 0 <= weight <= 1:
                errors.append(f"{item_path}: weight 必须是 0-1 的数字")
                weights_ok = False
            else:
                weight_total += float(weight)

            priority = item.get("priority")
            if not is_priority(priority):
                errors.append(f"{item_path}: priority 必须是 >= 0 的整数")

        if weights_ok and abs(weight_total - 1.0) > 1e-6:
            errors.append(f"{path}: weight 合计为 {weight_total:.6f}，必须等于 1.0")

    # capabilities: 每个别名都必须有且仅有五个布尔位
    caps = doc.get("capabilities")
    if not isinstance(caps, dict):
        errors.append("capabilities 必须是 mapping")
        caps = {}
    counts["capabilities"] = len(caps)
    for alias in aliases:
        if alias not in caps:
            errors.append(f"capabilities.{alias}: 缺少能力矩阵")
    for alias, cap in caps.items():
        path = f"capabilities.{alias}"
        if alias not in aliases:
            errors.append(f"{path}: 别名不存在")
        if not isinstance(cap, dict):
            errors.append(f"{path}: 必须是 mapping")
            continue
        for key in CAP_KEYS:
            if key not in cap:
                errors.append(f"{path}: 缺少能力位 {key}")
            elif not isinstance(cap[key], bool):
                errors.append(f"{path}.{key}: 取值必须是 true/false")
        for key in cap:
            if key not in CAP_KEYS:
                errors.append(f"{path}: 未知能力位 {key!r}")

    # notes: 每个别名一行说明
    notes = doc.get("notes")
    if not isinstance(notes, dict):
        errors.append("notes 必须是 mapping")
        notes = {}
    counts["notes"] = len(notes)
    for alias in aliases:
        note = notes.get(alias)
        if not isinstance(note, str) or not note.strip():
            errors.append(f"notes.{alias}: 缺少非空说明")
        elif "\n" in note:
            errors.append(f"notes.{alias}: 必须是一行说明")
    for alias in notes:
        if alias not in aliases:
            errors.append(f"notes.{alias}: 别名不存在")

    # fallbacks: 具体 provider/model 或别名链，触发条件必须来自白名单
    fallbacks = doc.get("fallbacks")
    if not isinstance(fallbacks, dict):
        errors.append("fallbacks 必须是 mapping")
        fallbacks = {}
    graph: dict[str, set[str]] = {}
    for alias, entries in fallbacks.items():
        path = f"fallbacks.{alias}"
        if alias not in aliases:
            errors.append(f"{path}: 别名不存在")
        if not isinstance(entries, list) or not entries:
            errors.append(f"{path}: 兜底列表不能为空")
            continue
        target_seen: set[tuple[str, str]] = set()
        for idx, item in enumerate(entries):
            item_path = f"{path}[{idx}]"
            counts["fallbacks"] += 1
            if not isinstance(item, dict):
                errors.append(f"{item_path}: 兜底项必须是 mapping")
                continue
            for key in item:
                if key is True:
                    warnings.append(
                        f'{item_path}: on 未加引号，被 YAML 解析为布尔键；建议写成 "on"'
                    )
                elif key not in FALLBACK_KEYS:
                    warnings.append(f"{item_path}: 未知字段 {key!r}")

            has_provider = "provider" in item
            has_alias = "alias" in item
            if has_provider == has_alias:
                errors.append(f"{item_path}: provider 与 alias 必须二选一")
            if has_alias:
                target = item.get("alias")
                if not isinstance(target, str) or target not in aliases:
                    errors.append(f"{item_path}: alias {target!r} 不存在")
                elif target == alias:
                    errors.append(f"{item_path}: 兜底不能指向自身")
                else:
                    graph.setdefault(alias, set()).add(target)
                if "upstream_model" in item:
                    warnings.append(f"{item_path}: alias 链里的 upstream_model 会被忽略")
            if has_provider:
                provider = item.get("provider")
                if not isinstance(provider, str) or provider not in registry:
                    errors.append(f"{item_path}: provider {provider!r} 不在注册表")
                model = item.get("upstream_model")
                if not isinstance(model, str) or not model.strip():
                    errors.append(f"{item_path}: upstream_model 缺失或为空")
                elif model.strip().lower() == "unknown":
                    msg = f"{item_path}: upstream_model 为 unknown，缺少可验证的上游名"
                    (errors if strict else warnings).append(msg)
                elif isinstance(provider, str):
                    pair = (provider, model)
                    if pair in target_seen:
                        warnings.append(f"{item_path}: 同一别名内重复兜底 {provider}/{model}")
                    target_seen.add(pair)
                if "alias" in item:
                    errors.append(f"{item_path}: 具体 provider 与 alias 链不能同时出现")

            priority = item.get("priority")
            if not is_priority(priority):
                errors.append(f"{item_path}: priority 必须是 >= 0 的整数")

            on = item.get("on")
            if on is None and True in item:
                on = item[True]
            if not isinstance(on, list) or not on:
                errors.append(f"{item_path}: on 必须是非空列表")
            else:
                if len(on) != len(set(map(str, on))):
                    warnings.append(f"{item_path}: on 有重复触发条件")
                for trigger in on:
                    if trigger not in TRIGGERS:
                        errors.append(f"{item_path}: 未知触发条件 {trigger!r}（可选：{', '.join(sorted(TRIGGERS))}）")

    cycle = find_cycle(graph)
    if cycle:
        errors.append("fallback 成环：" + " -> ".join(cycle))

    return errors, warnings, counts


def main() -> int:
    parser = argparse.ArgumentParser(description="校验模型别名矩阵")
    parser.add_argument("--config", help="YAML 路径，默认 <repo>/config/model-aliases.yaml")
    parser.add_argument("--repo-root", help="仓库根目录，默认脚本的上一级")
    parser.add_argument("--strict", action="store_true", help="upstream_model 为 unknown 时报错")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve() if args.repo_root else Path(__file__).resolve().parents[1]
    config_path = Path(args.config).resolve() if args.config else repo_root / "config" / "model-aliases.yaml"

    print(f"配置：{config_path}")
    try:
        registry = load_provider_registry(repo_root)
    except RuntimeError as exc:
        print(f"FAIL 注册表读取失败：{exc}")
        return 2
    print(f"provider 注册表：{len(registry)} 个（{', '.join(sorted(registry))}）")

    if not config_path.is_file():
        print(f"FAIL 配置不存在：{config_path}")
        return 2
    try:
        doc = yaml.load(config_path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    except yaml.YAMLError as exc:
        print(f"FAIL YAML 解析失败：{exc}")
        return 2

    errors, warnings, counts = validate(doc, registry, args.strict)
    print(
        "条目统计：别名 {aliases}，候选 {candidates}，兜底 {fallbacks}，能力矩阵 {capabilities}，说明 {notes}".format(
            **counts
        )
    )
    for msg in warnings:
        print(f"WARN {msg}")
    for msg in errors:
        print(f"ERR  {msg}")

    if errors:
        print(f"结果：FAIL（{len(errors)} 个错误，{len(warnings)} 个警告）")
        return 1
    print(f"结果：PASS（0 个错误，{len(warnings)} 个警告）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
