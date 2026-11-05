"""契约回归：OpenAPI 能正常生成，网关与近期新增的管理端点没有从路由表里消失。

不依赖正在运行的控制面（/openapi.json 默认关闭），直接在测试进程里按临时数据目录
构造应用，避免写仓库里的 data/。
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest

CONTROL = Path(__file__).resolve().parents[1]
if str(CONTROL) not in sys.path:
    sys.path.insert(0, str(CONTROL))

GATEWAY_ROUTES = {
    "/v1/chat/completions": {"post"},
    "/v1/messages": {"post"},
    "/v1/responses": {"post"},
}

RECENT_ROUTES = {
    "/api/health": {"get"},
    "/api/health/ready": {"get"},
    "/api/metrics": {"get"},
    "/api/metrics/json": {"get"},
    "/api/accounts/capabilities": {"get"},
    "/api/settings/runtime-config": {"get", "post"},
    "/api/settings/runtime-config/export": {"get"},
    "/api/settings/runtime-config/import": {"post"},
    "/api/models": {"get"},
}


@pytest.fixture(scope="module")
def schema() -> dict:
    with tempfile.TemporaryDirectory(prefix="contract-openapi-") as tmp:
        overrides = {
            "UNIFIED_DATA_DIR": str(Path(tmp) / "data"),
            "UNIFIED_DB_PATH": str(Path(tmp) / "data" / "unified.db"),
            "UNIFIED_WEB_DIR": str(Path(tmp) / "no-web"),
            "UNIFIED_ADMIN_PASSWORD": "contract-test",
            "UNIFIED_ENABLE_DOCS": "0",
            "GROK_REGISTER_EXECUTOR_URL": "",
        }
        previous = {key: os.environ.get(key) for key in overrides}
        os.environ.update(overrides)
        try:
            module = sys.modules.get("app.main") or importlib.import_module("app.main")
            return module.app.openapi()
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def _declared_methods(schema: dict, path: str) -> set[str]:
    return set(schema.get("paths", {}).get(path, {}))


def test_openapi_schema_generates(schema: dict) -> None:
    assert schema.get("openapi"), "OpenAPI 版本字段缺失"
    assert schema.get("info", {}).get("title"), "OpenAPI info.title 缺失"
    assert schema.get("paths"), "OpenAPI paths 为空"


@pytest.mark.parametrize("path,methods", sorted(GATEWAY_ROUTES.items()))
def test_gateway_routes_present(schema: dict, path: str, methods: set[str]) -> None:
    assert path in schema["paths"], f"网关路由缺失：{path}"
    assert methods <= _declared_methods(schema, path), f"{path} 方法不完整：{_declared_methods(schema, path)}"


@pytest.mark.parametrize("path,methods", sorted(RECENT_ROUTES.items()))
def test_recent_admin_routes_present(schema: dict, path: str, methods: set[str]) -> None:
    assert path in schema["paths"], f"管理端点缺失：{path}"
    assert methods <= _declared_methods(schema, path), f"{path} 方法不完整：{_declared_methods(schema, path)}"
