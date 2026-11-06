"""F1 核心逻辑单测：配额扣减、429、注册任务状态流转。

运行：apps/control 目录下
  python -m unittest discover -s tests -v
"""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.adapters.base import ProviderError
from app.adapters.client import UnifiedApiClient, UpstreamReply
from app.adapters.opencode import OpenCodeAdapter
from app.adapters.sub2api import Sub2ApiAdapter
from app.adapters.sub2api_store import QuotaStore
from app.config import Settings
from app.services.grok_register import GrokRegisterService


def _settings(tmp: Path) -> Settings:
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
    )


class QuotaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = _settings(Path(self._tmp.name))
        self.store = QuotaStore(self.settings.db_path, allow_private_upstreams=True)

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_deduct_and_exhaust_returns_429(self) -> None:
        self.store.upsert_group("g1", quota_limit=100)
        account = self.store.add_account("g1", upstream_base="http://127.0.0.1:7899", api_key="k")

        self.assertEqual(self.store.require_group("g1")["quota_used"], 0)
        result = self.store.charge(
            "g1", account["id"], model="m", usage={"prompt_tokens": 10, "completion_tokens": 5}
        )
        self.assertEqual(result["cost"], 15)
        self.assertEqual(self.store.get_group("g1")["quota_used"], 15)

        # 超额后确定性 429，不再选中账号
        self.store.charge("g1", account["id"], model="m", usage={"prompt_tokens": 90, "completion_tokens": 0})
        with self.assertRaises(ProviderError) as ctx:
            self.store.require_group("g1")
        self.assertEqual(ctx.exception.status_code, 429)

        reset = self.store.update_group("g1", reset_quota=True)
        self.assertEqual(reset["quota_used"], 0)
        self.assertEqual(self.store.require_group("g1")["quota_used"], 0)

    async def test_adapter_returns_429_when_pool_empty(self) -> None:
        self.store.upsert_group("g1", quota_limit=100)
        adapter = Sub2ApiAdapter(self.settings)
        adapter.configure(options={"group": "g1"})
        with self.assertRaises(ProviderError) as ctx:
            await adapter.chat_completion({"model": "m", "messages": []}, stream=False)
        self.assertEqual(ctx.exception.status_code, 429)

        # 空池拒绝必须留痕，否则对账会出现缺口
        rows = self.store.ledger("g1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], 429)
        self.assertEqual(rows[0]["reason"], "no_available_account")
        self.assertIsNone(rows[0]["account_id"])
        self.assertEqual(rows[0]["cost"], 0)

    async def test_adapter_429_when_all_accounts_cooling(self) -> None:
        self.store.upsert_group("g1", quota_limit=0)
        account = self.store.add_account("g1", upstream_base="http://127.0.0.1:7899")
        self.store.cool_down(account["id"], 120)

        adapter = Sub2ApiAdapter(self.settings)
        adapter.configure(options={"group": "g1"})
        with self.assertRaises(ProviderError) as ctx:
            await adapter.messages({"model": "m", "messages": []}, stream=False)
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(self.store.ledger("g1")[0]["reason"], "no_available_account")

    async def test_account_cooldown_rotates_to_next_account(self) -> None:
        self.store.upsert_group("g1", quota_limit=0)
        first = self.store.add_account("g1", name="a", upstream_base="http://127.0.0.1:7899", weight=2)
        second = self.store.add_account("g1", name="b", upstream_base="http://127.0.0.1:7899", weight=1)

        picked = self.store.pick_account_credentials("g1")
        self.assertEqual(picked["id"], first["id"])
        self.store.cool_down(first["id"], 60)
        picked = self.store.pick_account_credentials("g1")
        self.assertEqual(picked["id"], second["id"])


class OpenCodeNetworkRetryTests(unittest.IsolatedAsyncioTestCase):
    """网络层错误（连接失败/超时/协议中断）必须和可重试状态码一样换 key 退避。"""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = _settings(Path(self._tmp.name))
        self._adapters: list[OpenCodeAdapter] = []

    async def asyncTearDown(self) -> None:
        # 适配器缓存的 sqlite 连接必须先关，否则 Windows 删不掉临时目录（WinError 32）
        for adapter in self._adapters:
            adapter.close()
        self._tmp.cleanup()

    def _adapter(self, *, max_attempts: int = 2) -> OpenCodeAdapter:
        adapter = OpenCodeAdapter(self.settings, "http://127.0.0.1:7899")
        adapter.configure(api_key="bad", options={"extra_keys": ["good"], "max_attempts": max_attempts})
        self._adapters.append(adapter)
        return adapter

    async def test_network_error_rotates_key_then_succeeds(self) -> None:
        adapter = self._adapter()
        calls: list[str] = []

        async def fake_forward(self, path, payload, *, stream):
            calls.append(self.api_key)
            if self.api_key == "bad":
                raise httpx.ConnectError("connection refused")
            return UpstreamReply(status_code=200, headers={}, body=b'{"ok":true}')

        with patch.object(UnifiedApiClient, "forward", fake_forward), patch(
            "app.adapters.opencode.asyncio.sleep", new=AsyncMock()
        ):
            reply = await adapter.chat_completion({"model": "m", "messages": []}, stream=False)

        self.assertEqual(reply.status_code, 200)
        self.assertEqual(calls, ["bad", "good"])

    async def test_network_errors_exhaust_attempts_and_raise_clear_error(self) -> None:
        for exc_type in (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError):
            with self.subTest(exc=exc_type.__name__):
                adapter = self._adapter(max_attempts=4)
                calls: list[str] = []

                async def fake_forward(self, path, payload, *, stream, _exc=exc_type):
                    calls.append(self.api_key)
                    raise _exc("boom")

                with patch.object(UnifiedApiClient, "forward", fake_forward), patch(
                    "app.adapters.opencode.asyncio.sleep", new=AsyncMock()
                ):
                    with self.assertRaises(ProviderError) as ctx:
                        await adapter.chat_completion({"model": "m", "messages": []}, stream=False)

                # 有次数上限，不会无限重试；失败必须显式抛出而不是给出空响应
                self.assertEqual(ctx.exception.status_code, 502)
                self.assertEqual(len(calls), 4)
                self.assertEqual(len(set(calls)), 2, "4 次尝试必须轮换到两把 key，不能全打同一把坏 key")
                self.assertIn("仍未连通", ctx.exception.message)
                self.assertIn("连接失败", ctx.exception.message)


class GrokTaskTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = _settings(Path(self._tmp.name))
        self.service = GrokRegisterService(self.settings)

    async def asyncTearDown(self) -> None:
        await self.service.stop()
        self._tmp.cleanup()

    async def _drain(self) -> None:
        running = list(self.service._running.values())
        if running:
            await asyncio.gather(*running, return_exceptions=True)

    async def test_success_transition(self) -> None:
        async def ok(task):
            return {"created": task["count"], "accounts": ["u1"]}

        self.service.set_executor(ok)
        task = self.service.enqueue(count=2, workers=1)
        self.assertEqual(task["status"], "queued")
        self.assertEqual(await self.service.run_once(), 1)
        await self._drain()
        done = self.service.get(task["id"])
        self.assertEqual(done["status"], "succeeded")
        self.assertEqual(done["attempts"], 1)
        self.assertEqual(json.loads(done["result_json"])["created"], 2)

    async def test_failure_retries_then_failed(self) -> None:
        async def boom(task):
            raise RuntimeError("browser crashed")

        self.service.set_executor(boom)
        task = self.service.enqueue(max_attempts=2)
        self.assertEqual(await self.service.run_once(), 1)
        await self._drain()
        queued = self.service.get(task["id"])
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["attempts"], 1)
        self.assertIn("browser crashed", queued["error"])

        # 把退避时间拨到当下，模拟重试窗口到期
        conn = self.service._db.connect()
        try:
            conn.execute("UPDATE grok_tasks SET next_run_at = 0 WHERE id = ?", (task["id"],))
            conn.commit()
        finally:
            conn.close()

        self.assertEqual(await self.service.run_once(), 1)
        await self._drain()
        failed = self.service.get(task["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["attempts"], 2)

        retried = self.service.retry(task["id"])
        self.assertEqual(retried["status"], "queued")
        self.assertEqual(retried["attempts"], 0)

    async def test_cancel_and_concurrency_limit(self) -> None:
        async def slow(task):
            await asyncio.sleep(0.2)
            return {"ok": True}

        self.service.set_executor(slow)
        first = self.service.enqueue()
        second = self.service.enqueue()
        self.assertEqual(await self.service.run_once(), 1)
        self.assertEqual(len(self.service._running), 1)
        self.assertEqual(self.service.stats()["max_concurrency"], 1)

        cancelled = self.service.cancel(second["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        await self._drain()

        # 第一个任务正常完成；第二个从未被领取
        self.assertEqual(self.service.get(first["id"])["status"], "succeeded")
        self.assertEqual(self.service.get(second["id"])["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
