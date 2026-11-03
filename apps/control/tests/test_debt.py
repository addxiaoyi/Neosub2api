"""技术债回归：流式 usage 解析、网关写库不阻塞/不丢、settings 分组、usage_records.key_id NULL。"""

import asyncio
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.billing import record_usage, usage_by_key, usage_from_stream, usage_in_chunk
from app.db import Database, set_json_setting
from app.routers import settings as settings_router
from app.routers import v1


class UsageParseTests(unittest.TestCase):
    def test_text_mentioning_usage_is_not_counted(self):
        chunk = b'data: {"choices":[{"delta":{"content":"usage"}}]}\n\n'
        self.assertIsNone(usage_in_chunk(chunk))
        self.assertIsNone(usage_from_stream([chunk]))

    def test_openai_final_chunk_usage(self):
        chunk = b'data: {"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":7}}\n\ndata: [DONE]\n\n'
        self.assertEqual(usage_in_chunk(chunk), {"prompt_tokens": 11, "completion_tokens": 7})

    def test_anthropic_start_and_delta_merged(self):
        start = b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":25}}}\n\n'
        delta = b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":15}}\n\n'
        self.assertEqual(usage_from_stream([start, delta]), {"input_tokens": 25, "output_tokens": 15})

    def test_broken_json_is_skipped(self):
        chunk = b'data: {"usage": broken}\n\ndata: not-json\n\n'
        self.assertIsNone(usage_from_stream([chunk]))


class _DbCase:
    """临时 DB + 假 request 的 mixin，不继承 TestCase，避免与 IsolatedAsyncio 抢 MRO。"""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "unified.db")
        self.request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=self.db)))

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self, sql: str) -> list[sqlite3.Row]:
        conn = self.db.connect()
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()


class RecordTests(_DbCase, unittest.IsolatedAsyncioTestCase):
    async def test_record_safe_writes_null_key(self):
        await v1._record_safe(
            self.request,
            key={"id": None, "name": "anonymous"},
            provider="p", model="m", status=200, usage={"prompt_tokens": 1},
            latency_ms=5, stream=False, error=None,
        )
        row = self.rows("SELECT * FROM usage_records")[0]
        self.assertIsNone(row["key_id"])
        self.assertEqual(row["key_name"], "anonymous")

    async def test_record_survives_stream_cancel(self):
        real = v1._record
        finished: list[bool] = []

        def slow_record(request, **fields):
            time.sleep(0.1)
            real(request, **fields)
            finished.append(True)

        with patch.object(v1, "_record", slow_record):
            task = asyncio.create_task(
                v1._record_safe(
                    self.request, key={"id": 3, "name": "k"}, provider="p", model="m",
                    status=200, usage=None, latency_ms=1, stream=True, error="client disconnected",
                )
            )
            await asyncio.sleep(0.02)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0.3)

        self.assertTrue(finished, "取消后写盘线程应继续跑完")
        self.assertEqual(len(self.rows("SELECT * FROM usage_records")), 1)

    def test_usage_by_key_buckets_null_as_unattributed(self):
        conn = self.db.connect()
        try:
            common = dict(provider="p", model="m", status=200, stream=False, usage=None, latency_ms=1,
                          ts=int(time.time()))
            record_usage(conn, key_id=None, key_name="", **common)
            record_usage(conn, key_id=2, key_name="k2", **common)
            conn.commit()
            buckets = {item["key_name"]: item["requests"] for item in usage_by_key(conn)}
        finally:
            conn.close()
        self.assertEqual(buckets["未归属"], 1)
        self.assertEqual(buckets["k2"], 1)


class SettingsGroupTests(_DbCase, unittest.TestCase):
    def test_new_groups_always_present(self):
        cfg = settings_router._read_config(self.request)
        for group in ("gateway", "model_map", "provider_config"):
            self.assertIsInstance(cfg[group], dict)

    def test_detached_groups_fall_back_to_real_settings(self):
        conn = self.db.connect()
        try:
            set_json_setting(conn, "gateway", {"allow_anonymous": True})
            set_json_setting(conn, "model_map", {"a": "b"})
            set_json_setting(conn, "provider_config", {"opencode": {"api_key": "sk-live-1234567890"}})
        finally:
            conn.close()
        cfg = settings_router._read_config(self.request)
        self.assertTrue(cfg["gateway"]["allow_anonymous"])
        self.assertEqual(cfg["model_map"], {"a": "b"})
        # S-01：读接口只给掩码，明文不出现在响应里
        entry = cfg["provider_config"]["opencode"]
        self.assertNotIn("api_key", entry)
        self.assertEqual(entry["api_key_masked"], "sk-l…7890")
        self.assertTrue(entry["has_api_key"])

    def test_plain_groups_default_empty(self):
        cfg = settings_router._read_config(self.request)
        self.assertEqual(cfg["schedule"], {})
