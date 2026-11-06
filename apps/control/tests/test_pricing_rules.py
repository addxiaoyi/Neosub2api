"""2-D1：price_rules（按次 / 按秒 / 长上下文倍率）。

规则与 token 价正交：token 价仍只认 price_books，规则单独一张表；
金额一次性并入同一条 billing_ledger，快照里保留命中的规则。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.billing import (
    import_price_rules,
    resolve_price_rules,
    settle,
)
from app.db import Database

REPO = Path(__file__).resolve().parents[3]
PRICES_YAML = REPO / "config" / "prices.yaml"


def _conn(tmp_path):
    return Database(Path(tmp_path) / "unified.db").connect()


def _rule(conn, **over):
    row = {
        "provider": "*",
        "model": "*",
        "kind": "per_call",
        "unit_micro": 0,
        "threshold": 0,
        "multiplier_milli": 1000,
        "effective_from": 0,
        "note": "test",
    } | over
    conn.execute(
        "INSERT INTO price_rules(provider, model, kind, unit_micro, threshold, multiplier_milli, "
        "effective_from, created_by, note) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            row["provider"],
            row["model"],
            row["kind"],
            row["unit_micro"],
            row["threshold"],
            row["multiplier_milli"],
            row["effective_from"],
            "test",
            row["note"],
        ),
    )
    return row


def _bill(conn, **over):
    row = {
        "ts": 1000,
        "key_id": None,
        "key_name": "k-test",
        "provider": "openai",
        "model": "gpt-image-1",
        "status": 200,
        "usage": None,
    } | over
    return settle(conn, **row)


def test_price_rules_table_is_created(tmp_path):
    conn = _conn(tmp_path)
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(price_rules)")}
    assert {
        "provider",
        "model",
        "kind",
        "unit_micro",
        "threshold",
        "multiplier_milli",
        "effective_from",
    } <= cols


def test_resolve_prefers_exact_provider_and_model(tmp_path):
    conn = _conn(tmp_path)
    _rule(conn, provider="*", model="*", unit_micro=100, effective_from=0)
    _rule(conn, provider="sub2api", model="*", unit_micro=70, effective_from=0)
    _rule(conn, provider="sub2api", model="gpt-4o", unit_micro=50, effective_from=0)
    rules = resolve_price_rules(conn, provider="sub2api", model="gpt-4o", at=1000)
    assert rules["per_call"]["unit_micro"] == 50
    other = resolve_price_rules(conn, provider="zcode", model="gpt-4o", at=1000)
    assert other["per_call"]["unit_micro"] == 100, "没命中精确行时回落到通配行"


def test_resolve_respects_effective_from(tmp_path):
    conn = _conn(tmp_path)
    _rule(conn, provider="*", model="*", unit_micro=100, effective_from=0)
    _rule(conn, provider="*", model="*", unit_micro=200, effective_from=5000)
    assert resolve_price_rules(conn, provider="x", model="y", at=1000)["per_call"]["unit_micro"] == 100
    assert resolve_price_rules(conn, provider="x", model="y", at=6000)["per_call"]["unit_micro"] == 200


def test_per_call_charges_image_even_without_usage(tmp_path):
    conn = _conn(tmp_path)
    _rule(conn, model="gpt-image-1", unit_micro=42000)
    result = _bill(conn)
    assert result["written"] is True
    assert result["billed_micro"] == 42000, "按次规则要给出金额，而不是 0"
    assert result["note"] == "per_call", "有金额的请求不该再标 usage_missing"
    assert result["estimated"] == 0
    snapshot = conn.execute(
        "SELECT lines_json FROM price_book_snapshots WHERE id = ?", (result["snapshot_id"],)
    ).fetchone()
    payload = json.loads(snapshot["lines_json"])
    assert payload["rules"]["per_call"]["unit_micro"] == 42000


def test_per_call_not_charged_when_rule_does_not_match(tmp_path):
    conn = _conn(tmp_path)
    _rule(conn, model="gpt-image-1", unit_micro=42000)
    result = _bill(conn, model="gpt-5")
    assert result["billed_micro"] == 0
    assert result["note"] == "usage_missing"


def test_per_second_requires_seconds(tmp_path):
    conn = _conn(tmp_path)
    _rule(conn, model="sora-2", kind="per_second", unit_micro=60000)
    assert _bill(conn, model="sora-2")["billed_micro"] == 0, "没有时长就不猜"
    billed = _bill(conn, model="sora-2", seconds=8)
    assert billed["billed_micro"] == 480000
    assert "per_second:8s" in billed["note"]


def _token_pricing(model: str = "long-m", micro: int = 10000000) -> dict:
    return {
        "at": 0,
        "lines": {
            "input": {
                "price_id": 1,
                "provider": "*",
                "model": model,
                "price_micro_per_1m": micro,
            }
        },
    }


def test_long_context_multiplies_token_cost(tmp_path):
    conn = _conn(tmp_path)
    usage = {"prompt_tokens": 250000, "completion_tokens": 0}
    base = _bill(conn, model="long-m", usage=usage, pricing=_token_pricing())
    assert base["billed_micro"] == 2500000, "250k 输入 × 10 美元/百万"

    _rule(conn, model="long-m", kind="long_context", threshold=200000, multiplier_milli=2000)
    doubled = _bill(conn, model="long-m", usage=usage, pricing=_token_pricing())
    assert doubled["billed_micro"] == 5000000
    assert "long_context:2000" in doubled["note"]


def test_long_context_ignores_short_prompt(tmp_path):
    conn = _conn(tmp_path)
    _rule(conn, model="long-m", kind="long_context", threshold=200000, multiplier_milli=2000)
    short = _bill(conn, model="long-m", usage={"prompt_tokens": 100000}, pricing=_token_pricing())
    assert short["billed_micro"] == 1000000
    assert "long_context" not in short["note"]


def test_import_rules_is_idempotent_and_dry_run(tmp_path):
    conn = _conn(tmp_path)
    first = import_price_rules(conn, PRICES_YAML, dry_run=True)
    assert first["inserted"] >= 1 and first["dry_run"] is True
    assert conn.execute("SELECT COUNT(*) AS n FROM price_rules").fetchone()["n"] == 0

    written = import_price_rules(conn, PRICES_YAML)
    assert written["inserted"] >= 1
    second = import_price_rules(conn, PRICES_YAML)
    assert second["inserted"] == 0 and second["skipped"] == written["total"]
    assert len(second["pending"]) == 2, "图片/视频单价仍在 rules_pending，不允许编价入库"
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM price_rules WHERE kind = 'long_context'"
    ).fetchone()["n"] == 1


def test_old_yaml_without_rules_section_is_fine(tmp_path):
    conn = _conn(tmp_path)
    old = tmp_path / "old.yaml"
    old.write_text(
        "version: 1\nprices:\n  - provider: '*'\n    model: m\n    token_class: input\n"
        "    price_micro_per_1m: 1\n",
        encoding="utf-8",
    )
    result = import_price_rules(conn, old)
    assert result["inserted"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM price_rules").fetchone()["n"] == 0


# --------------------------- 接口：只追加 ---------------------------


def test_api_rules_append_only(admin):
    body = {
        "kind": "per_call",
        "provider": "*",
        "model": "api-rule-model",
        "unit_micro": 1234,
        "effective_from": 1000000000,
    }
    created = admin.post("/api/pricing/rules", json=body)
    assert created.status_code == 200, created.text
    assert created.json()["unit_micro"] == 1234

    duplicate = admin.post("/api/pricing/rules", json=body)
    assert duplicate.status_code == 409, "同版本重复导入要拒绝，改价用新 effective_from"

    listed = admin.get("/api/pricing/rules", params={"model": "api-rule-model"})
    assert listed.status_code == 200
    assert [row["unit_micro"] for row in listed.json()] == [1234]

    bad_kind = admin.post("/api/pricing/rules", json={**body, "kind": "nope"})
    assert bad_kind.status_code == 400

    free = admin.post(
        "/api/pricing/rules", json={**body, "kind": "per_second", "unit_micro": 0}
    )
    assert free.status_code == 400, "按次/按秒不允许 0 价，避免配成免费"


def test_api_resolve_reports_rules(admin):
    body = {
        "kind": "per_call",
        "provider": "*",
        "model": "api-resolve-model",
        "unit_micro": 999,
        "effective_from": 1000000000,
    }
    assert admin.post("/api/pricing/rules", json=body).status_code == 200
    preview = admin.get(
        "/api/pricing/resolve",
        params={"provider": "openai", "model": "api-resolve-model", "at": 1100000000},
    )
    assert preview.status_code == 200, preview.text
    payload = preview.json()
    assert payload["rules"]["per_call"]["unit_micro"] == 999
    assert payload["missing_rules"] == ["per_second", "long_context"]


def test_api_import_reports_rules(admin):
    imported = admin.post("/api/pricing/import", params={"dry_run": True})
    assert imported.status_code == 200, imported.text
    rules = imported.json()["rules"]
    assert rules["dry_run"] is True
    assert len(rules["pending"]) == 2
