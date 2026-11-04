#!/usr/bin/env python3
"""前后端契约检查：前端调用的路径/方法/query 参数/body 字段是否与后端一致。

后端：导入 `app.main:app`，直接读 `app.routes`（带 HTTP 方法）、每条路由的
`dependant.query_params` 与 `body_field`（Pydantic 模型字段）；handler 里对
`request.query_params` 的显式读取另做一次源码级提取，提取不出来的登记白名单。
前端：扫描 `apps/web` 的 app / lib / components / hooks 目录，识别
`fetch(...)`、`http.<verb>(...)`、以及本文件内定义的 `get/post/patch/put/del` 封装，
并静态解析调用点的 query 实参（URL 字面量 + axios params）与 body 实参
（对象字面量 / 同文件标识符 / type、interface 声明）。

结果分级：
  error（退出码 1）：幽灵端点、方法不匹配、前端参数/字段后端明确不存在。
  warn（不影响退出码）：无法静态判定（后端动态读取、白名单、前后端一侧解析不出）。
  提示（不影响退出码）：后端有路由但前端零调用。

用法：
    python scripts/contract_check.py
    python scripts/contract_check.py --json
    python scripts/contract_check.py --fail-on-ghost   # 兼容旗标，语义已并入 error 分级
"""

from __future__ import annotations

import argparse
import atexit
import inspect
import json
import os
import re
import shutil
import sys
import tempfile
import time
import types
import typing
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONTROL = REPO / "apps" / "control"
WEB = REPO / "apps" / "web"
SCAN_DIRS = ("app", "lib", "components", "hooks")
SOURCE_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".mjs"}

VERB_METHOD = {
    "get": "GET",
    "post": "POST",
    "put": "PUT",
    "patch": "PATCH",
    "delete": "DELETE",
    "del": "DELETE",
}

# 归一化后参数段统一成 {}：/api/keys/{key_id}、/api/keys/12、/api/keys/${id} 等价
_PARAM_SEGMENT = re.compile(r"^(?:\$\{[^{}]*\}|\{[^{}]*\}|\d+)$")
_CONST = re.compile(
    r"""^\s*(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*(?::\s*[^=\n]+)?=\s*(['"])(/api[^'"]*|/v1[^'"]*)\2""",
    re.M,
)
_WRAPPER_DEF = re.compile(r"^\s*(?:export\s+)?const\s+(get|post|put|patch|del)\b", re.M)
_FETCH_CALL = re.compile(r"\bfetch\s*\(")
_HTTP_CALL = re.compile(r"\bhttp\.(get|post|put|patch|delete)\b")
# 单独出现的路径字面量（如协议表里的 path: '/v1/responses'），没有方法信息
_LITERAL = re.compile(r"""(['"])(/api[^'"\n]*|/v1[^'"\n]*)\1""")

# 前端 type/interface 声明：body/query 实参是标识符时靠它还原字段名
_DECL_TYPE = re.compile(r"(?:export\s+)?type\s+([A-Za-z_$][\w$]*)\s*(?:<[^<>{}]*>)?\s*=\s*")
_DECL_INTERFACE = re.compile(
    r"(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)\s*(?:<[^<>{}]*>)?\s*(?:extends\s+([^{]+?))?\s*\{"
)
_OMIT = re.compile(r"Omit<\s*([A-Za-z_$][\w$]*)\s*,\s*(.+?)\s*>")
_IDENT = re.compile(r"[A-Za-z_$][\w$]*")

# 后端 handler 用 request.query_params 做了无法静态枚举的读取时（当前仓库 0 处），
# 按 (METHOD, 归一化路径) 显式登记该路由认可的 query 参数，避免动态路由被静默放过。
QUERY_PARAM_WHITELIST: dict[tuple[str, str], tuple[str, ...]] = {}

# handler 源码里显式读取的 query 参数：query_params['x'] / query_params.get('x')
_QUERY_EXPLICIT_READ = re.compile(r"""query_params\s*(?:\[\s*|\.get\(\s*)['"]([\w.-]+)['"]""")

WARN_REASON = {
    "whitelisted": "命中脚本白名单（后端动态读取 query）",
    "backend-dynamic": "后端 query 读取无法静态判定，且未列入白名单",
    "frontend-params-undetermined": "前端 params 键无法静态枚举",
    "frontend-body-undetermined": "前端 body 字段无法静态枚举",
}


@dataclass(frozen=True)
class Route:
    path: str
    methods: tuple[str, ...]


@dataclass(frozen=True)
class RouteSpec:
    """一条后端路由的字段级契约。"""

    accepted_query: frozenset[str]  # 静态可判定的合法 query 参数（签名 + 显式读取）
    query_dynamic: bool  # handler 对 query_params 的读取无法静态枚举
    body_fields: frozenset[str] | None  # None = 无 Pydantic 模型，跳过 body 对账
    body_model: str | None
    body_extra_allow: bool


@dataclass(frozen=True)
class AliasDecl:
    """前端 type/interface 声明：对象体键 + extends 链 + 其余 RHS 原文。"""

    body: frozenset[str] | None = None
    extends: tuple[str, ...] = ()
    rhs: str = ""


@dataclass(frozen=True)
class Call:
    method: str
    path: str
    source: str
    query: tuple[str, ...] = ()  # 能静态枚举出的 query 参数名
    query_known: bool = True  # False = 带了 params 但键枚举不出来
    body: tuple[str, ...] | None = None  # None = 发了 body 但字段枚举不出来；() = 没发 body


@dataclass(frozen=True)
class Reference:
    """无法确定方法的路径字面量，只用来给「后端零调用」提示降噪。"""

    path: str
    source: str


def _is_api_path(path: str) -> bool:
    if path == "/healthz":
        return True
    return any(path == prefix or path.startswith(prefix + "/") for prefix in ("/api", "/v1"))


def _prepare_import_env() -> None:
    """隔离启动副作用：提取路由不应碰仓库里的 data/ 目录，也不该挂载前端产物。"""
    tmp = Path(tempfile.mkdtemp(prefix="unified-contract-"))
    atexit.register(shutil.rmtree, tmp, ignore_errors=True)
    os.environ.update(
        {
            "UNIFIED_DATA_DIR": str(tmp / "data"),
            "UNIFIED_DB_PATH": str(tmp / "data" / "unified.db"),
            "UNIFIED_WEB_DIR": str(tmp / "no-web"),
            "UNIFIED_ADMIN_PASSWORD": "contract-check",
            "UNIFIED_ENABLE_DOCS": "0",
            "GROK_REGISTER_EXECUTOR_URL": "",
        }
    )


def _import_app():
    """延迟导入：导入即建临时库，必须在 env 就绪之后。"""
    _prepare_import_env()
    control = str(CONTROL)
    inserted = control not in sys.path
    if inserted:
        sys.path.insert(0, control)
    try:
        from app.main import app

        return app
    finally:
        if inserted:
            try:
                sys.path.remove(control)
            except ValueError:
                pass


def _api_routes(app) -> list:
    picked = []
    for route in app.routes:
        # FastAPI 0.162 起 include_router 包成 _IncludedRouter，不再摊平进 app.routes
        contexts = getattr(route, "effective_route_contexts", None)
        candidates = list(contexts()) if callable(contexts) else [route]
        for cand in candidates:
            path = getattr(cand, "path", "")
            methods = getattr(cand, "methods", None)
            if not path or not methods or not _is_api_path(path):
                continue
            picked.append(cand)
    return picked


def load_backend_routes() -> list[Route]:
    routes: list[Route] = []
    for route in _api_routes(_import_app()):
        verbs = tuple(sorted({str(m).upper() for m in route.methods} - {"HEAD", "OPTIONS"}))
        if verbs:
            routes.append(Route(path=route.path, methods=verbs))
    return routes


def _flatten_deps(dep):
    yield dep
    for sub in dep.dependencies:
        yield from _flatten_deps(sub)


def _query_contract(route) -> tuple[frozenset[str], bool]:
    """handler 接受的 query 参数（签名 + 源码显式读取）与是否存在动态读取。"""
    accepted: set[str] = set()
    dynamic = False
    callables = []
    seen: set[int] = set()
    endpoint = getattr(route, "endpoint", None)
    if endpoint is not None:
        seen.add(id(endpoint))
        callables.append(endpoint)
    for dep in _flatten_deps(route.dependant):
        accepted.update(field.name for field in dep.query_params)
        fn = dep.call
        if fn is not None and id(fn) not in seen:
            seen.add(id(fn))
            callables.append(fn)
    for fn in callables:
        try:
            source = _blank_comments(inspect.getsource(fn))
        except (TypeError, OSError, IndentationError):
            dynamic = True  # 源码取不到，保守视为无法静态判定
            continue
        accepted.update(_QUERY_EXPLICIT_READ.findall(source))
        if "query_params" in _QUERY_EXPLICIT_READ.sub("", source):
            dynamic = True
    return frozenset(accepted), dynamic


def _pydantic_models(typ) -> list:
    """展开 Optional/Union 后取其中的 Pydantic 模型类（dict/Request 等返回空）。"""
    import pydantic

    origin = typing.get_origin(typ)
    if origin is typing.Union or origin is types.UnionType:
        models = []
        for arg in typing.get_args(typ):
            if arg is not type(None):
                models.extend(_pydantic_models(arg))
        return models
    try:
        if isinstance(typ, type) and issubclass(typ, pydantic.BaseModel):
            return [typ]
    except TypeError:
        pass
    return []


def _body_contract(body_field) -> tuple[frozenset[str] | None, str | None, bool]:
    if body_field is None:
        return None, None, False
    models = _pydantic_models(getattr(body_field, "type_", None))
    if not models:
        return None, None, False
    fields: set[str] = set()
    for model in models:
        fields.update(getattr(model, "model_fields", {}))
    allow = any((getattr(m, "model_config", None) or {}).get("extra") == "allow" for m in models)
    return frozenset(fields), "|".join(m.__name__ for m in models), allow


def load_route_specs() -> dict[tuple[str, str], RouteSpec]:
    """(METHOD, 归一化路径) → 字段级契约。"""
    specs: dict[tuple[str, str], RouteSpec] = {}
    for route in _api_routes(_import_app()):
        if not hasattr(route, "dependant"):
            continue
        accepted, dynamic = _query_contract(route)
        fields, model, allow = _body_contract(getattr(route, "body_field", None))
        normalized = normalize_path(route.path)
        verbs = {str(m).upper() for m in route.methods} - {"HEAD", "OPTIONS"}
        for verb in verbs:
            key = (verb, normalized)
            prev = specs.get(key)
            if prev is None:
                specs[key] = RouteSpec(accepted, dynamic, fields, model, allow)
                continue
            # 同方法同路径重复注册：query 取并集；body 定义不一致时放弃对账
            same_body = prev.body_fields == fields
            specs[key] = RouteSpec(
                accepted_query=prev.accepted_query | accepted,
                query_dynamic=prev.query_dynamic or dynamic,
                body_fields=prev.body_fields if same_body else None,
                body_model=prev.body_model if same_body else None,
                body_extra_allow=(prev.body_extra_allow or allow) if same_body else False,
            )
    return specs


def normalize_path(path: str) -> str:
    path = path.split("?", 1)[0].split("#", 1)[0]
    if not path.startswith("/"):
        path = "/" + path
    parts = [p for p in path.split("/") if p]
    normalized = "/" + "/".join("{}" if _PARAM_SEGMENT.match(p) else p for p in parts)
    return normalized or "/"


def _char(text: str, index: int) -> str:
    return text[index] if 0 <= index < len(text) else ""


def _read_arg(text: str, start: int) -> tuple[str, int]:
    """读取函数调用的一个实参，返回 (原文, 结束位置)。结束位置指向逗号或右括号。"""
    index = start
    depth = 0
    quote = ""
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in "'\"`":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                return text[start:index], index
            depth -= 1
        elif char == "," and depth == 0:
            return text[start:index], index
        index += 1
    return text[start:], len(text)


def _skip_type_args(text: str, index: int) -> int:
    """跳过 TS 泛型实参（可嵌套），返回 `(` 所在位置；不是泛型就原样返回。"""
    if _char(text, index) != "<":
        return index
    depth = 0
    while index < len(text):
        if text[index] == "<":
            depth += 1
        elif text[index] == ">":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return index


def _find_quote_end(text: str, quote: str) -> int | None:
    index = 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == quote:
            return index
        index += 1
    return None


def _resolve_template(raw: str, consts: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        if re.fullmatch(r"[A-Za-z_$][\w$]*", name) and name in consts:
            return consts[name]
        return "{p}"

    return re.sub(r"\$\{([^{}]*)\}", replace, raw)


def _literal_path(expr: str, consts: dict[str, str]) -> str | None:
    expr = expr.strip()
    wrapped = re.fullmatch(r"withBasePath\s*\(\s*(.+?)\s*\)", expr, re.S)
    if wrapped:
        expr = wrapped.group(1).strip()
    if expr in consts:
        return consts[expr]

    quote = expr[:1]
    if quote not in ("'", '"', "`"):
        return None
    end = _find_quote_end(expr, quote)
    if end is None:
        return None
    raw = expr[1:end]
    if quote == "`":
        raw = _resolve_template(raw, consts)
    return raw if raw.startswith(("/api", "/v1")) else None


# ── payload 静态解析：query 参数名 / body 字段名 ────────────────────────────


def _span_end(text: str, start: int) -> int | None:
    """text[start] 必须是 `{`；返回配对 `}` 下标，未闭合返回 None。跳过字符串。"""
    if text[start : start + 1] != "{":
        return None
    depth = 0
    quote = ""
    index = start
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = ""
        elif char in "'\"`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _split_entries(body: str) -> list[str]:
    """按顶层 `,`/`;` 切条目；`<>` 泛型深度单独计数，防止 Record<A, B> 被切开。"""
    entries: list[str] = []
    buf: list[str] = []
    depth = 0
    generic = 0
    quote = ""
    index = 0
    while index < len(body):
        char = body[index]
        if quote:
            buf.append(char)
            if char == "\\" and index + 1 < len(body):
                buf.append(body[index + 1])
                index += 2
                continue
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in "'\"`":
            quote = char
            buf.append(char)
        elif char in "([{":
            depth += 1
            buf.append(char)
        elif char in ")]}":
            depth = max(depth - 1, 0)
            buf.append(char)
        elif body[index : index + 2] == "=>":
            buf.append("=>")
            index += 2
            continue
        elif char == "<":
            generic += 1
            buf.append(char)
        elif char == ">":
            if generic > 0:
                generic -= 1
            buf.append(char)
        elif char in ",;" and depth == 0 and generic == 0:
            entries.append("".join(buf))
            buf = []
        else:
            buf.append(char)
        index += 1
    entries.append("".join(buf))
    return entries


def _object_keys(body: str) -> frozenset[str] | None:
    """对象字面量/类型体的顶层键；出现展开符、计算键、索引签名等返回 None（枚举不出）。"""
    keys: set[str] = set()
    for entry in _split_entries(body):
        entry = entry.strip()
        if not entry:
            continue
        if entry.startswith("..."):
            return None
        ident = re.match(r"([A-Za-z_$][\w$]*)", entry)
        if ident:
            keys.add(ident.group(1))
            continue
        quoted = re.match(r"""['"]([^'"\\]+)['"]\s*:""", entry)
        if quoted:
            keys.add(quoted.group(1))
            continue
        return None
    return frozenset(keys)


def _outermost_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = ""
        elif char in "'\"`":
            quote = char
        elif char == "{":
            end = _span_end(text, index)
            if end is None:
                return spans
            spans.append((index, end))
            index = end
        index += 1
    return spans


def _string_contents(text: str) -> list[str]:
    """普通/模板字符串的内容（用于从 URL 表达式里抠出 `?k=v` 片段）。"""
    contents: list[str] = []
    quote = ""
    buf: list[str] = []
    for char in text:
        if quote:
            if char == "\\":
                continue
            if char == quote:
                contents.append("".join(buf))
                quote = ""
                buf = []
                continue
            buf.append(char)
        elif char in "'\"`":
            quote = char
    return contents


def _query_of_url(expr: str) -> tuple[tuple[str, ...], bool]:
    """URL 表达式里字符串片段携带的 query 参数名；动态键片段返回 known=False。"""
    names: set[str] = set()
    known = True
    for content in _string_contents(expr):
        qpos = content.find("?")
        if qpos < 0:
            continue
        fragment = content[qpos + 1 :]
        if "#" in fragment:
            fragment = fragment.split("#", 1)[0]
        for piece in fragment.split("&"):
            if not piece:
                continue
            found = re.match(r"([A-Za-z_$][\w$%.-]*)(?:=|$)", piece)
            if found:
                names.add(found.group(1))
            else:
                known = False  # 形如 ?${key}= 的动态参数名
    return tuple(sorted(names)), known


def _read_type_value(text: str, start: int) -> str:
    """读声明右侧的类型/表达式；顶层遇到 `,;=)` 或换行、`=>` 截断（`<>` 泛型深度内除外）。"""
    index = start
    generic = 0
    while index < len(text):
        if text[index : index + 2] == "=>":
            break
        char = text[index]
        if char == "\n" or char in ",;=)":
            if generic == 0:
                break
        elif char == "<":
            generic += 1
        elif char == ">":
            if generic > 0:
                generic -= 1
        elif char in "'\"":
            quote = char
            index += 1
            while index < len(text) and text[index] != quote:
                index += 2 if text[index] == "\\" else 1
            if index >= len(text):
                break
        index += 1
    return text[start:index].strip()


def _resolve_ident(name: str, text: str, pos: int, aliases: dict[str, AliasDecl],
                   depth: int = 0, seen: frozenset[str] = frozenset()) -> frozenset[str] | None:
    """就近解析标识符：取调用点之前最近的 `name: X` / `name = X` 声明并解析它的值；
    最近声明解析不出时直接判未知，不回退到更早的同名声明（防止跨作用域串键）。"""
    if depth > 5 or name in seen:
        return None
    declaration: re.Match[str] | None = None
    for found in re.finditer(rf"\b{re.escape(name)}\s*[:=]\s*", text[:pos]):
        # 跳过 ==/=== 这类比较运算（不是声明）
        if text[found.end() : found.end() + 1] == "=":
            continue
        declaration = found
    if declaration is None:
        return _alias_keys(name, aliases, depth, seen)
    value_start = declaration.end()
    while value_start < len(text) and text[value_start] in " \t":
        value_start += 1
    if text[value_start : value_start + 1] == "{":
        end = _span_end(text, value_start)
        if end is None:
            return None
        return _object_keys(text[value_start + 1 : end])
    value = _read_type_value(text, value_start)
    if not value:
        return None
    return _expr_keys(value, text, value_start, aliases, depth + 1)


def _alias_keys(name: str, aliases: dict[str, AliasDecl], depth: int = 0,
                seen: frozenset[str] = frozenset()) -> frozenset[str] | None:
    if depth > 5 or name in seen:
        return None
    decl = aliases.get(name)
    if decl is None:
        return None
    seen = seen | {name}
    if decl.body is not None:
        keys = set(decl.body)
        for parent in decl.extends:
            inherited = _alias_keys(parent, aliases, depth + 1, seen)
            if inherited is None:
                return None  # 继承链解析不全时整体判未知，避免误报
            keys |= inherited
        return frozenset(keys)
    rhs = decl.rhs.strip()
    if _IDENT.fullmatch(rhs):
        return _alias_keys(rhs, aliases, depth + 1, seen)
    omit = _OMIT.fullmatch(rhs)
    if omit:
        return _omit_keys(omit.group(1), omit.group(2), aliases, depth + 1, seen)
    return None


def _omit_keys(base_name: str, spec: str, aliases: dict[str, AliasDecl],
               depth: int = 0, seen: frozenset[str] = frozenset()) -> frozenset[str] | None:
    base = _alias_keys(base_name, aliases, depth, seen)
    removed = set(re.findall(r"""['"]([^'"\\]+)['"]""", spec))
    if base is None or not removed:
        return None
    return frozenset(base - removed)


def _expr_keys(expr: str, text: str, pos: int, aliases: dict[str, AliasDecl],
               depth: int = 0) -> frozenset[str] | None:
    """实参表达式能静态枚举出的顶层键；枚举不出来返回 None。pos 为 expr 在 text 中的起点。"""
    expr = expr.strip().rstrip(";").strip()
    if not expr or expr in ("undefined", "null"):
        return frozenset()
    stringify = re.match(r"JSON\.stringify\s*\(\s*", expr)
    if stringify:
        inner, _ = _read_arg(expr, stringify.end())
        return _expr_keys(inner, text, pos + stringify.end(), aliases, depth + 1)
    if expr.startswith("{"):
        end = _span_end(expr, 0)
        if end is None:
            return None
        return _object_keys(expr[1:end])
    if _IDENT.fullmatch(expr):
        return _resolve_ident(expr, text, pos, aliases, depth)
    omit = _OMIT.fullmatch(expr)
    if omit:
        return _omit_keys(omit.group(1), omit.group(2), aliases, depth)
    keys: set[str] = set()
    spans = _outermost_spans(expr)
    for start, end in spans:
        part = _object_keys(expr[start + 1 : end])
        if part is None:
            return None
        keys |= part
    return frozenset(keys) if spans else None


def _axios_params(config: str, text: str, pos: int, aliases: dict[str, AliasDecl]) -> frozenset[str] | None:
    """axios config 实参里 params 的键；config 整体解析不出时返回 None。"""
    config = config.strip()
    if not config or config in ("undefined", "null"):
        return frozenset()
    if config.startswith("{"):
        found = re.search(r"\bparams\s*:", config)
        if found:
            value_pos = pos + found.end()
            value, _ = _read_arg(text, value_pos)
            return _expr_keys(value, text, value_pos, aliases)
        if re.search(r"\bparams\s*[,}]", config):
            return _expr_keys("params", text, pos, aliases)
        return frozenset()
    return None


def _fetch_body(options: str, text: str, pos: int, aliases: dict[str, AliasDecl]) -> tuple[str, ...] | None:
    found = re.search(r"\bbody\s*:\s*", options)
    if not found:
        return ()
    value_pos = pos + found.end()
    value, _ = _read_arg(text, value_pos)
    keys = _expr_keys(value, text, value_pos, aliases)
    return None if keys is None else tuple(sorted(keys))


def _collect_aliases(files: list[Path]) -> dict[str, AliasDecl]:
    """全量收集前端 type/interface 声明，供 body/query 实参标识符解析。"""
    table: dict[str, AliasDecl] = {}
    for file in files:
        text = _blank_comments(file.read_text(encoding="utf-8", errors="replace"))
        for match in _DECL_INTERFACE.finditer(text):
            brace = match.end() - 1
            end = _span_end(text, brace)
            if end is None:
                continue
            body = _object_keys(text[brace + 1 : end])
            extends = tuple(x.strip() for x in (match.group(2) or "").split(",") if x.strip())
            table.setdefault(match.group(1), AliasDecl(body=body, extends=extends))
        for match in _DECL_TYPE.finditer(text):
            cursor = match.end()
            while cursor < len(text) and text[cursor] in " \t":
                cursor += 1
            name = match.group(1)
            if text[cursor : cursor + 1] == "{":
                end = _span_end(text, cursor)
                if end is None:
                    continue
                tail = re.split(r"[;\n]", text[end + 1 :], 1)[0].strip()
                if tail:
                    table.setdefault(name, AliasDecl(rhs="<complex>"))  # 对象体外还有联合等，判未知
                else:
                    table.setdefault(name, AliasDecl(body=_object_keys(text[cursor + 1 : end])))
            else:
                rhs = re.split(r"[;\n]", text[cursor:], 1)[0].strip()
                table.setdefault(name, AliasDecl(rhs=rhs))
    return table


def _blank_comments(text: str) -> str:
    """把注释替换成空格（保留换行），避免注释里的 `fetch('/api/...')` 被当成调用点。"""
    out = list(text)
    index = 0
    quote = ""
    block = False
    while index < len(text):
        char = text[index]
        nxt = text[index + 1] if index + 1 < len(text) else ""
        if block:
            if char == "*" and nxt == "/":
                out[index] = out[index + 1] = " "
                block = False
                index += 2
                continue
            if char != "\n":
                out[index] = " "
            index += 1
        elif quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = ""
            index += 1
        elif char == "/" and nxt == "/":
            while index < len(text) and text[index] != "\n":
                out[index] = " "
                index += 1
        elif char == "/" and nxt == "*":
            out[index] = out[index + 1] = " "
            block = True
            index += 2
        elif char in "'\"`":
            quote = char
            index += 1
        else:
            index += 1
    return "".join(out)


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _source_label(file: Path, line: int) -> str:
    try:
        rel = file.relative_to(REPO)
    except ValueError:
        rel = file
    return f"{str(rel).replace(os.sep, '/')}:{line}"


def _scan_file(file: Path, aliases: dict[str, AliasDecl]) -> tuple[list[Call], list[Reference]]:
    text = _blank_comments(file.read_text(encoding="utf-8", errors="replace"))
    consts = {name: value for name, _quote, value in _CONST.findall(text)}
    calls: list[Call] = []

    for match in _FETCH_CALL.finditer(text):
        arg, arg_end = _read_arg(text, match.end())
        options = ""
        options_pos = arg_end + 1
        if _char(text, arg_end) == ",":
            options, _ = _read_arg(text, options_pos)
        method_match = re.search(r"""method\s*:\s*['"]([A-Za-z]+)['"]""", options)
        method = method_match.group(1).upper() if method_match else "GET"
        path = _literal_path(arg, consts)
        if path:
            query, query_known = _query_of_url(arg)
            body = _fetch_body(options, text, options_pos, aliases)
            calls.append(
                Call(method, path, _source_label(file, _line_of(text, match.start())),
                     query, query_known, body)
            )

    for match in _HTTP_CALL.finditer(text):
        cursor = _skip_type_args(text, match.end())
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if _char(text, cursor) != "(":
            continue
        arg, arg_end = _read_arg(text, cursor + 1)
        path = _literal_path(arg, consts)
        if not path:
            continue
        verb = VERB_METHOD[match.group(1)]
        second = ""
        second_pos = arg_end + 1
        if _char(text, arg_end) == ",":
            second, _ = _read_arg(text, second_pos)
        query, query_known = _query_of_url(arg)
        body: tuple[str, ...] | None = ()
        if verb in ("POST", "PUT", "PATCH"):
            if second.strip():
                keys = _expr_keys(second, text, second_pos, aliases)
                body = None if keys is None else tuple(sorted(keys))
        elif second.strip():
            # GET/DELETE 的第二实参是 axios config，query 藏在它的 params 里
            params = _axios_params(second, text, second_pos, aliases)
            if params is None:
                query_known = False
            else:
                query = tuple(sorted(set(query) | params))
        calls.append(
            Call(verb, path, _source_label(file, _line_of(text, match.start())),
                 query, query_known, body)
        )

    # 只在该文件自己定义了封装函数时才把裸 get/post/... 当调用点（避免误抓 map.get 之类）
    wrappers = set(_WRAPPER_DEF.findall(text))
    for name in wrappers:
        pattern = re.compile(rf"(?<![.\w$]){name}\b")
        for match in pattern.finditer(text):
            cursor = _skip_type_args(text, match.end())
            while cursor < len(text) and text[cursor].isspace():
                cursor += 1
            if _char(text, cursor) != "(":
                continue
            arg, arg_end = _read_arg(text, cursor + 1)
            path = _literal_path(arg, consts)
            if not path:
                continue
            method = VERB_METHOD[name]
            query, query_known = _query_of_url(arg)
            body: tuple[str, ...] | None = ()
            if _char(text, arg_end) == ",":
                value_pos = arg_end + 1
                value, _ = _read_arg(text, value_pos)
                if value.strip():
                    if method in ("POST", "PUT", "PATCH"):
                        keys = _expr_keys(value, text, value_pos, aliases)
                        body = None if keys is None else tuple(sorted(keys))
                    elif method == "GET":
                        params = _expr_keys(value, text, value_pos, aliases)
                        if params is None:
                            query_known = False
                        else:
                            query = tuple(sorted(set(query) | params))
                    # DELETE 封装运行时丢弃第二实参，不参与对账
            calls.append(
                Call(method, path, _source_label(file, _line_of(text, match.start())),
                     query, query_known, body)
            )

    references = [
        Reference(path=match.group(2), source=_source_label(file, _line_of(text, match.start())))
        for match in _LITERAL.finditer(text)
    ]
    return calls, references


def scan_frontend(web: Path = WEB) -> tuple[list[Call], list[Reference]]:
    files: list[Path] = []
    for directory in SCAN_DIRS:
        root = web / directory
        if not root.is_dir():
            continue
        for file in sorted(root.rglob("*")):
            if file.is_file() and file.suffix in SOURCE_SUFFIXES:
                files.append(file)
    aliases = _collect_aliases(files)
    calls: list[Call] = []
    references: list[Reference] = []
    for file in files:
        file_calls, file_refs = _scan_file(file, aliases)
        calls.extend(file_calls)
        references.extend(file_refs)
    return calls, references


def _finalize(entries: dict[tuple, dict]) -> list[dict]:
    """把 call_sites 集合转成排序列表，按键稳定排序。"""
    final: list[dict] = []
    for key in sorted(entries, key=lambda item: tuple(str(part) for part in item)):
        entry = dict(entries[key])
        entry["call_sites"] = sorted(entry.pop("call_sites"))
        final.append(entry)
    return final


def build_report(routes: list[Route], calls: list[Call], references: list[Reference] = (),
                 specs: dict[tuple[str, str], RouteSpec] | None = None) -> dict:
    backend_methods: dict[str, set[str]] = defaultdict(set)
    backend_paths: dict[str, set[str]] = defaultdict(set)
    for route in routes:
        normalized = normalize_path(route.path)
        backend_methods[normalized].update(route.methods)
        backend_paths[normalized].add(route.path)

    grouped: dict[tuple[str, str], list[Call]] = defaultdict(list)
    for call in calls:
        grouped[(call.method, normalize_path(call.path))].append(call)

    ghosts: list[dict] = []
    mismatches: list[dict] = []
    used: set[str] = set()

    for (method, normalized), group in sorted(grouped.items()):
        unique_sites = sorted({call.source for call in group})
        if normalized not in backend_methods:
            ghosts.append({"method": method, "path": normalized, "call_sites": unique_sites})
            continue
        used.add(normalized)
        if method not in backend_methods[normalized]:
            mismatches.append(
                {
                    "method": method,
                    "path": normalized,
                    "backend_methods": sorted(backend_methods[normalized]),
                    "call_sites": unique_sites,
                }
            )

    # query/body 字段级对账：只对「路径+方法都命中」的调用做
    query_errors: dict[tuple, dict] = {}
    query_warnings: dict[tuple, dict] = {}
    body_errors: dict[tuple, dict] = {}
    body_warnings: dict[tuple, dict] = {}
    if specs:
        for (method, normalized), group in grouped.items():
            spec = specs.get((method, normalized))
            if spec is None:
                continue  # 幽灵端点或方法不匹配，上面已经报过
            whitelist = frozenset(QUERY_PARAM_WHITELIST.get((method, normalized), ()))
            for call in group:
                for param in call.query:
                    if spec.query_dynamic:
                        reason = "whitelisted" if param in whitelist else "backend-dynamic"
                        key = (method, normalized, reason, param)
                        entry = query_warnings.setdefault(
                            key, {"method": method, "path": normalized, "param": param,
                                  "reason": reason, "call_sites": set()})
                        entry["call_sites"].add(call.source)
                    elif param not in spec.accepted_query:
                        key = (method, normalized, param)
                        entry = query_errors.setdefault(
                            key, {"method": method, "path": normalized, "param": param,
                                  "accepted": sorted(spec.accepted_query), "call_sites": set()})
                        entry["call_sites"].add(call.source)
                if not call.query_known:
                    key = (method, normalized, "frontend-params-undetermined", "")
                    entry = query_warnings.setdefault(
                        key, {"method": method, "path": normalized, "param": None,
                              "reason": "frontend-params-undetermined", "call_sites": set()})
                    entry["call_sites"].add(call.source)
                if spec.body_fields is None or spec.body_extra_allow:
                    continue  # 无 Pydantic 模型 / extra=allow 均不参与 body 对账
                if call.body is None:
                    key = (method, normalized, "frontend-body-undetermined")
                    entry = body_warnings.setdefault(
                        key, {"method": method, "path": normalized,
                              "reason": "frontend-body-undetermined",
                              "model": spec.body_model, "call_sites": set()})
                    entry["call_sites"].add(call.source)
                    continue
                for field in call.body:
                    if field in spec.body_fields:
                        continue
                    key = (method, normalized, field)
                    entry = body_errors.setdefault(
                        key, {"method": method, "path": normalized, "field": field,
                              "model": spec.body_model, "call_sites": set()})
                    entry["call_sites"].add(call.source)

    query_param_mismatches = _finalize(query_errors)
    query_param_warnings = _finalize(query_warnings)
    body_field_mismatches = _finalize(body_errors)
    body_field_warnings = _finalize(body_warnings)

    call_norms = {normalize_path(call.path) for call in calls}
    supplementary = [
        ref for ref in references if normalize_path(ref.path) not in call_norms
    ]
    referenced = used | {normalize_path(ref.path) for ref in supplementary}

    unused = [
        {"path": normalized, "declared_paths": sorted(backend_paths[normalized]),
         "methods": sorted(backend_methods[normalized])}
        for normalized in sorted(backend_methods)
        if normalized not in referenced
    ]

    error_count = (len(ghosts) + len(mismatches)
                   + len(query_param_mismatches) + len(body_field_mismatches))
    warn_count = len(query_param_warnings) + len(body_field_warnings)

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "backend_routes": [
            {"path": route.path, "normalized": normalize_path(route.path), "methods": list(route.methods)}
            for route in sorted(routes, key=lambda r: (r.path, r.methods))
        ],
        "frontend_calls": [
            {"method": call.method, "path": call.path, "normalized": normalize_path(call.path),
             "source": call.source, "query": list(call.query), "query_known": call.query_known,
             "body": list(call.body) if call.body is not None else None}
            for call in sorted(calls, key=lambda c: (c.path, c.method, c.source))
        ],
        "ghost_endpoints": ghosts,
        "method_mismatches": mismatches,
        "query_param_mismatches": query_param_mismatches,
        "query_param_warnings": query_param_warnings,
        "body_field_mismatches": body_field_mismatches,
        "body_field_warnings": body_field_warnings,
        "frontend_literal_references": [
            {"path": ref.path, "normalized": normalize_path(ref.path), "source": ref.source}
            for ref in sorted(supplementary, key=lambda r: (r.path, r.source))
        ],
        "unused_backend_routes": unused,
        "summary": {
            "backend_paths": len(backend_methods),
            "backend_routes": len(routes),
            "frontend_calls": len(calls),
            "frontend_literal_references": len(supplementary),
            "ghost_endpoints": len(ghosts),
            "method_mismatches": len(mismatches),
            "query_param_mismatches": len(query_param_mismatches),
            "query_param_warnings": len(query_param_warnings),
            "body_field_mismatches": len(body_field_mismatches),
            "body_field_warnings": len(body_field_warnings),
            "errors": error_count,
            "warnings": warn_count,
            "unused_backend_routes": len(unused),
        },
    }


def _print_human(report: dict) -> None:
    summary = report["summary"]
    print(
        f"契约检查：后端 {summary['backend_paths']} 个路径 / {summary['backend_routes']} 条路由，"
        f"前端 {summary['frontend_calls']} 处调用 —— "
        f"error {summary['errors']} / warn {summary['warnings']}"
    )

    ghosts = report["ghost_endpoints"]
    print(f"\n[幽灵端点] {len(ghosts)}")
    for item in ghosts:
        print(f"  {item['method']} {item['path']}")
        for site in item["call_sites"]:
            print(f"    ← {site}")

    mismatches = report["method_mismatches"]
    print(f"\n[方法不匹配] {len(mismatches)}")
    for item in mismatches:
        allowed = "/".join(item["backend_methods"])
        print(f"  {item['method']} {item['path']}（后端仅有 {allowed}）")
        for site in item["call_sites"]:
            print(f"    ← {site}")

    query_errors = report["query_param_mismatches"]
    print(f"\n[query 参数不存在] {len(query_errors)}")
    for item in query_errors:
        accepted = "、".join(item["accepted"]) or "（后端无任何 query 参数）"
        print(f"  {item['method']} {item['path']} 参数 {item['param']}（后端接受：{accepted}）")
        for site in item["call_sites"]:
            print(f"    ← {site}")

    body_errors = report["body_field_mismatches"]
    print(f"\n[body 字段不存在] {len(body_errors)}")
    for item in body_errors:
        print(f"  {item['method']} {item['path']} 字段 {item['field']}（模型 {item['model']}）")
        for site in item["call_sites"]:
            print(f"    ← {site}")

    query_warns = report["query_param_warnings"]
    body_warns = report["body_field_warnings"]
    print(f"\n[存疑 warn，不影响退出码] {len(query_warns) + len(body_warns)}")
    for item in query_warns:
        param = item["param"] if item["param"] is not None else "params（整体）"
        print(f"  {item['method']} {item['path']} query {param}：{WARN_REASON[item['reason']]}")
        for site in item["call_sites"]:
            print(f"    ← {site}")
    for item in body_warns:
        print(f"  {item['method']} {item['path']} body（模型 {item['model']}）：{WARN_REASON[item['reason']]}")
        for site in item["call_sites"]:
            print(f"    ← {site}")

    unused = report["unused_backend_routes"]
    print(f"\n[后端有路由但前端零调用] {len(unused)}（仅提示）")
    for item in unused:
        declared = "、".join(item["declared_paths"])
        methods = "/".join(item["methods"])
        extra = f"（声明为 {declared}）" if declared != item["path"] else ""
        print(f"  {methods} {item['path']}{extra}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查前端调用点与后端路由是否一致")
    parser.add_argument("--json", action="store_true", help="输出 JSON，便于 CI 消费")
    parser.add_argument(
        "--fail-on-ghost",
        action="store_true",
        help="兼容旗标：幽灵端点必以退出码 1 结束（现并入 error 分级，非空 error 一律失败）",
    )
    args = parser.parse_args(argv)

    routes = load_backend_routes()
    specs = load_route_specs()
    calls, references = scan_frontend()
    report = build_report(routes, calls, references, specs)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(report)

    # 退出码只看 error；warn（无法静态判定/白名单）不致失败
    # --fail-on-ghost 的既有语义（幽灵端点→非 0）由 error 集天然覆盖
    if report["summary"]["errors"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
