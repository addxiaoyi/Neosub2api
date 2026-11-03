#!/usr/bin/env python3
"""校验 config/prices.yaml 的结构与精度。

检查项：provider+model+token_class 组合唯一（含生效时间）、token_class 取值、
价格为正数、美元价能无损换算成整数 micro-USD、source 非空；并与
config/model-aliases.yaml 对照，确认当前接入的模型都已被定价或显式列为 unpriced。
-Strict 时 unknown 与 unpriced 条目都算错误。

退出码：0 通过；1 有校验错误；2 配置缺失 / YAML 语法错误 / 别名表读取失败。
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

TOKEN_CLASSES = frozenset({"input", "output", "cache_read", "cache_write", "reasoning"})
ROOT_KEYS = {"version", "currency", "unit", "captured_at", "default_effective_from", "prices", "unpriced"}
PRICE_KEYS = {
    "provider",
    "model",
    "token_class",
    "price_per_mtok",
    "price_micro_per_1m",
    "effective_from",
    "source",
    "note",
}
UNPRICED_KEYS = {"provider", "model", "reason", "source", "checked_at"}
MICRO = Decimal(1_000_000)
MAX_DECIMALS = 6


class UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader + 重复键检测；PyYAML 默认会静默让后一个键覆盖前一个。"""


def _construct_mapping(loader: UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    seen: set[Any] = set()
    out: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
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


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def is_unknown(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() == "unknown"


def parse_price(value: Any) -> tuple[Decimal | None, str]:
    """把价格解析成 Decimal，返回 (值, 错误说明)。"""
    if is_unknown(value):
        return None, "unknown"
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None, "不是字符串或数字"
    try:
        return Decimal(str(value).strip()), ""
    except InvalidOperation:
        return None, "无法解析为十进制数"


def load_alias_models(path: Path) -> set[str]:
    """从别名表收集真实上游模型名（别名链不产生模型名）。"""
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise RuntimeError("根节点不是 mapping")
    models: set[str] = set()
    for groups in (doc.get("aliases"), doc.get("fallbacks")):
        if not isinstance(groups, dict):
            continue
        for entries in groups.values():
            if not isinstance(entries, list):
                continue
            for item in entries:
                if isinstance(item, dict):
                    model = item.get("upstream_model")
                    if isinstance(model, str) and model.strip():
                        models.add(model)
    return models


def validate(doc: Any, alias_models: set[str], strict: bool) -> tuple[list[str], list[str], dict[str, int]]:
    errors: list[str] = []
    warnings: list[str] = []
    counts = {"prices": 0, "unpriced": 0, "models": 0, "token_classes": 0}

    if not isinstance(doc, dict):
        return ["根节点必须是 mapping"], warnings, counts

    for key in doc:
        if key not in ROOT_KEYS:
            warnings.append(f"未知顶层键 {key!r}")

    version = doc.get("version")
    if not is_int(version) or version < 1:
        errors.append("version 必须是 >= 1 的整数")

    if doc.get("currency") != "USD":
        errors.append("currency 必须是 USD（M3 只落美元价）")
    if doc.get("unit") != "per_1m_tokens":
        errors.append("unit 必须是 per_1m_tokens")
    if not is_text(doc.get("captured_at")):
        errors.append("captured_at 缺失")

    prices = doc.get("prices")
    if not isinstance(prices, list) or not prices:
        errors.append("prices 必须是非空列表")
        prices = []
    counts["prices"] = len(prices)

    seen_keys: set[tuple[Any, Any, Any, Any]] = set()
    seen_pairs: dict[tuple[str, str, str], Decimal | None] = {}
    priced_models: set[str] = set()

    for idx, item in enumerate(prices):
        path = f"prices[{idx}]"
        if not isinstance(item, dict):
            errors.append(f"{path}: 条目必须是 mapping")
            continue
        for key in item:
            if key not in PRICE_KEYS:
                warnings.append(f"{path}: 未知字段 {key!r}")

        provider = item.get("provider")
        model = item.get("model")
        token_class = item.get("token_class")

        if not is_text(provider):
            errors.append(f"{path}: provider 缺失或为空")
        if not is_text(model):
            errors.append(f"{path}: model 缺失或为空")
        else:
            priced_models.add(model)
        if token_class not in TOKEN_CLASSES:
            errors.append(f"{path}: token_class {token_class!r} 不合法（可选：{', '.join(sorted(TOKEN_CLASSES))}）")

        effective_from = item.get("effective_from")
        if not is_int(effective_from) or effective_from < 0:
            errors.append(f"{path}: effective_from 必须是非负整数（epoch 秒）")

        source = item.get("source")
        if not is_text(source):
            errors.append(f"{path}: source 缺失或为空")
        elif is_unknown(source):
            msg = f"{path}: source 为 unknown"
            (errors if strict else warnings).append(msg)

        note = item.get("note")
        if not is_text(note):
            errors.append(f"{path}: note 缺失或为空")

        price, why = parse_price(item.get("price_per_mtok"))
        micro = item.get("price_micro_per_1m")

        if price is None:
            msg = f"{path}: price_per_mtok 为 {why}"
            (errors if strict else warnings).append(msg)
        else:
            if price <= 0:
                errors.append(f"{path}: price_per_mtok 必须为正数，当前 {price}")
            decimals = -price.as_tuple().exponent
            if decimals > MAX_DECIMALS:
                errors.append(
                    f"{path}: price_per_mtok 小数位 {decimals} 超过 {MAX_DECIMALS}，micro-USD 会丢精度"
                )
            expected = price * MICRO
            if expected != expected.to_integral_value():
                errors.append(f"{path}: {price} × 1,000,000 不是整数，无法无损换算 micro-USD")
            elif is_int(micro) and Decimal(micro) != expected:
                errors.append(
                    f"{path}: price_micro_per_1m={micro} 与 {price} × 1,000,000={expected:.0f} 不一致"
                )
            elif not is_int(micro):
                errors.append(f"{path}: price_micro_per_1m 必须是整数")

        if is_int(micro) and micro <= 0:
            errors.append(f"{path}: price_micro_per_1m 必须为正整数")

        key = (provider, model, token_class, effective_from)
        if key in seen_keys:
            errors.append(f"{path}: provider/model/token_class/effective_from 组合重复 {key}")
        seen_keys.add(key)

        triple = (str(provider), str(model), str(token_class))
        if triple in seen_pairs:
            prev = seen_pairs[triple]
            if prev is not None and price is not None and prev == price:
                warnings.append(f"{path}: 同一组合的多个生效版本价格相同，确认是否为历史版本")
        else:
            seen_pairs[triple] = price

    unpriced = doc.get("unpriced", [])
    if not isinstance(unpriced, list):
        errors.append("unpriced 必须是列表")
        unpriced = []
    counts["unpriced"] = len(unpriced)

    unpriced_models: set[str] = set()
    seen_unpriced: set[tuple[str, str]] = set()
    for idx, item in enumerate(unpriced):
        path = f"unpriced[{idx}]"
        if not isinstance(item, dict):
            errors.append(f"{path}: 条目必须是 mapping")
            continue
        for key in item:
            if key not in UNPRICED_KEYS:
                warnings.append(f"{path}: 未知字段 {key!r}")

        provider = item.get("provider")
        model = item.get("model")
        if not is_text(provider):
            errors.append(f"{path}: provider 缺失或为空")
        if not is_text(model):
            errors.append(f"{path}: model 缺失或为空")
        else:
            unpriced_models.add(model)
        if not is_text(item.get("reason")):
            errors.append(f"{path}: reason 缺失或为空")
        if not is_text(item.get("source")):
            errors.append(f"{path}: source 缺失或为空")

        pair = (str(provider), str(model))
        if pair in seen_unpriced:
            errors.append(f"{path}: provider/model 在 unpriced 中重复 {pair}")
        seen_unpriced.add(pair)

        msg = f"{path}: {provider}/{model} 没有公开定价（unknown）"
        (errors if strict else warnings).append(msg)

    counts["models"] = len(priced_models | unpriced_models)
    counts["token_classes"] = len({item.get("token_class") for item in prices if isinstance(item, dict)})

    missing = sorted(alias_models - priced_models - unpriced_models)
    for model in missing:
        msg = f"覆盖缺口：别名表里的 {model} 既没有价格行也没有列入 unpriced"
        (errors if strict else warnings).append(msg)

    extra = sorted(priced_models - alias_models)
    for model in extra:
        warnings.append(f"{model} 不在模型别名表中，确认是否为可路由模型")

    return errors, warnings, counts


def main() -> int:
    parser = argparse.ArgumentParser(description="校验价格表种子数据")
    parser.add_argument("--config", help="YAML 路径，默认 <repo>/config/prices.yaml")
    parser.add_argument("--aliases", help="别名表路径，默认 <repo>/config/model-aliases.yaml")
    parser.add_argument("--repo-root", help="仓库根目录，默认脚本的上一级")
    parser.add_argument("--strict", action="store_true", help="unknown / unpriced / 覆盖缺口都算错误")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve() if args.repo_root else Path(__file__).resolve().parents[1]
    config_path = Path(args.config).resolve() if args.config else repo_root / "config" / "prices.yaml"
    alias_path = Path(args.aliases).resolve() if args.aliases else repo_root / "config" / "model-aliases.yaml"

    print(f"配置：{config_path}")
    if not config_path.is_file():
        print(f"FAIL 配置不存在：{config_path}")
        return 2

    try:
        doc = yaml.load(config_path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    except yaml.YAMLError as exc:
        print(f"FAIL YAML 解析失败：{exc}")
        return 2

    try:
        alias_models = load_alias_models(alias_path)
    except (OSError, yaml.YAMLError, RuntimeError) as exc:
        print(f"FAIL 别名表读取失败：{exc}")
        return 2
    print(f"别名表模型：{len(alias_models)} 个（{', '.join(sorted(alias_models))}）")

    errors, warnings, counts = validate(doc, alias_models, args.strict)
    print(
        "条目统计：价格 {prices} 条，未定价 {unpriced} 条，模型 {models} 个，token_class {token_classes} 种".format(
            **counts
        )
    )
    for msg in warnings:
        print(f"WARN {msg}")
    for msg in errors:
        print(f"ERR  {msg}")

    mode = "strict" if args.strict else "normal"
    if errors:
        print(f"结果：FAIL（{mode}，{len(errors)} 个错误，{len(warnings)} 个警告）")
        return 1
    print(f"结果：PASS（{mode}，0 个错误，{len(warnings)} 个警告）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
