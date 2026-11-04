"""主线端到端验收（API 级）：登录 → 建密钥 → chat 流/非流 → messages → responses → 日志/统计 → 删密钥。

对已启动的隔离控制面执行 7 步主线，只用 httpx、不依赖浏览器。
所有客户端 trust_env=False：本机 HTTP_PROXY 会劫持 127.0.0.1 请求导致超时。

v1/responses 只有 codar/qoder 声明能力位，跑第 5 步时临时把 codar 指到假上游，
结束后按 tests/test_error_contract.py 的 codar_on_fake 同款方式恢复原配置。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid

import httpx

ANTHROPIC_HEADERS = {
    "anthropic-version": "2023-06-01",
    "content-type": "application/json",
}


class StepFail(Exception):
    def __init__(self, step: str, detail: str) -> None:
        super().__init__(f"[{step}] {detail}")
        self.step = step


def ensure(cond: bool, step: str, detail: str) -> None:
    if not cond:
        raise StepFail(step, detail)


def say(msg: str) -> None:
    print(f"[mainline] {msg}", flush=True)


def brief(resp: httpx.Response) -> str:
    return f"status={resp.status_code} body={resp.text[:400]!r}"


def step1_login(client: httpx.Client, user: str, password: str) -> None:
    r = client.post("/api/login", json={"username": user, "password": password})
    ensure(r.status_code == 200, "1-login", f"登录失败 {brief(r)}")
    ensure(len(client.cookies) > 0, "1-login", "登录响应未设置会话 cookie")
    say(f"STEP1 login status=200 session_cookie={list(client.cookies.keys())}")


def step2_create_key(client: httpx.Client) -> tuple[str, int, str]:
    name = f"e2e-mainline-{time.strftime('%m%d%H%M%S')}-{uuid.uuid4().hex[:4]}"
    r = client.post("/api/keys", json={"name": name})
    ensure(r.status_code == 200, "2-key", f"创建密钥失败 {brief(r)}")
    issued = r.json()
    key = issued.get("key")
    ensure(
        isinstance(key, str) and key.startswith("u2a_"),
        "2-key",
        f"一次性明文缺失或前缀不对 {r.text[:400]!r}",
    )
    key_id = issued.get("id")
    ensure(isinstance(key_id, int), "2-key", f"返回体缺 id {r.text[:400]!r}")

    lst = client.get("/api/keys")
    ensure(lst.status_code == 200, "2-key", f"列表查询失败 {brief(lst)}")
    items = lst.json()
    ensure(isinstance(items, list), "2-key", f"GET /api/keys 非列表 {lst.text[:300]!r}")
    ensure(key not in lst.text, "2-key", "明文密钥出现在列表响应里（应只在创建时回一次）")
    mine = [it for it in items if it.get("id") == key_id]
    ensure(len(mine) == 1, "2-key", f"列表里找不到新建的密钥 id={key_id}")
    prefix = mine[0].get("prefix")
    ensure(
        prefix == key[:10],
        "2-key",
        f"列表前缀 {prefix!r} != 明文前 10 位 {key[:10]!r}",
    )
    say(
        f"STEP2 key id={key_id} name={name} one_time_prefix={key[:6]}… "
        f"listed_prefix={prefix} plaintext_in_list=False"
    )
    return key, key_id, name


def _chat_payload() -> dict:
    return {"model": "fake-chat-1", "messages": [{"role": "user", "content": "ping"}]}


def step3_chat(client: httpx.Client, key: str) -> None:
    hdr = {"Authorization": f"Bearer {key}", "x-provider": "codebuddy"}

    r = client.post("/v1/chat/completions", headers=hdr, json=_chat_payload())
    ensure(r.status_code == 200, "3-chat", f"非流式失败 {brief(r)}")
    data = r.json()
    ensure(data.get("object") == "chat.completion", "3-chat", f"非 chat.completion {r.text[:300]!r}")
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    ensure(
        "[fake] model=fake-chat-1" in content,
        "3-chat",
        f"响应内容不符 {content[:200]!r}",
    )
    say(f"STEP3a non-stream status=200 content={content.strip()[:80]!r}")

    frames: list[str] = []
    arrivals: list[tuple[float, str]] = []
    t0 = time.perf_counter()
    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers=hdr,
        json={**_chat_payload(), "stream": True},
    ) as resp:
        ensure(resp.status_code == 200, "3-chat", f"流式状态码 {resp.status_code}")
        for line in resp.iter_lines():
            if not line.startswith("data:"):
                continue
            payload_txt = line[len("data:"):].strip()
            arrivals.append((round((time.perf_counter() - t0) * 1000, 1), payload_txt[:60]))
            frames.append(payload_txt)

    chunks = [f for f in frames if f != "[DONE]"]
    ensure(
        len(chunks) >= 2,
        "3-chat",
        f"流式 chunk 数 {len(chunks)} < 2，不是逐帧推送：{arrivals!r}",
    )
    ensure(
        frames and frames[-1] == "[DONE]",
        "3-chat",
        f"末帧不是 [DONE]：{frames[-3:]!r}",
    )
    say(
        "STEP3b stream status=200 "
        f"chunks={len(chunks)} done_marker=OK first_chunk_ms={arrivals[0][0]} "
        f"arrivals={arrivals}"
    )


def step4_messages(client: httpx.Client, key: str) -> None:
    hdr = {"x-api-key": key, "x-provider": "codebuddy", **ANTHROPIC_HEADERS}
    body = {"model": "fake-chat-1", "max_tokens": 64, "messages": [{"role": "user", "content": "ping"}]}

    r = client.post("/v1/messages", headers=hdr, json=body)
    ensure(r.status_code == 200, "4-messages", f"失败 {brief(r)}")
    data = r.json()
    ensure(data.get("role") == "assistant", "4-messages", f"缺 role=assistant {r.text[:300]!r}")
    ensure(isinstance(data.get("content"), list), "4-messages", f"content 不是数组 {r.text[:300]!r}")
    ensure(
        isinstance(data.get("stop_reason"), str) and data["stop_reason"],
        "4-messages",
        f"缺 stop_reason {r.text[:300]!r}",
    )
    ensure("choices" not in data, "4-messages", f"响应是 OpenAI 形状（含 choices）{r.text[:300]!r}")
    ensure("error" not in data, "4-messages", f"成功响应里混入 error 字段 {r.text[:300]!r}")
    text_parts = [b.get("text", "") for b in data["content"] if isinstance(b, dict)]
    say(
        f"STEP4a status=200 type={data.get('type')!r} role={data['role']!r} "
        f"content_len={len(data['content'])} stop_reason={data['stop_reason']!r} "
        f"text={(' '.join(text_parts)).strip()[:60]!r}"
    )

    # 显式 x-provider 走 MODE_EXPLICIT、不校验模型目录（model_resolve.py:225），
    # 要触发真实的解析错误必须去掉头，让请求落到目录自动匹配 → 404
    bad_hdr = {k: v for k, v in hdr.items() if k.lower() != "x-provider"}
    bad_body = {**body, "model": "no-such-model-e2e"}
    bad = client.post("/v1/messages", headers=bad_hdr, json=bad_body)
    ensure(
        bad.status_code in (400, 404),
        "4-messages",
        f"无效模型未被拒绝 {brief(bad)}",
    )
    err = bad.json()
    ensure(
        err.get("type") == "error",
        "4-messages",
        f"错误体不是 Anthropic 壳（缺 type=error，疑似 OpenAI 形状）{bad.text[:300]!r}",
    )
    ensure(
        isinstance(err.get("error"), dict) and err["error"],
        "4-messages",
        f"错误体 error 不是对象 {bad.text[:300]!r}",
    )
    say(
        f"STEP4b error-shell status={bad.status_code} type={err.get('type')!r} "
        f"error={err['error']!r}"
    )


def _find_provider(client: httpx.Client, pid: str) -> dict:
    r = client.get("/api/providers")
    ensure(r.status_code == 200, "5-responses", f"读 providers 失败 {brief(r)}")
    for item in r.json():
        if item.get("id") == pid:
            return item
    raise StepFail("5-responses", f"providers 里没有 {pid}")


def step5_responses(client: httpx.Client, key: str, fake_base: str) -> None:
    before = _find_provider(client, "codar")
    say(
        f"STEP5 codar before: enabled={before.get('enabled')} "
        f"base_url_source={before.get('base_url_source')} has_api_key={before.get('has_api_key')}"
    )
    patched = client.patch(
        "/api/providers/codar",
        json={"enabled": True, "base_url": fake_base, "api_key": "fake-key"},
    )
    ensure(patched.status_code == 200, "5-responses", f"启用 codar 失败 {brief(patched)}")
    try:
        r = client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {key}", "x-provider": "codar"},
            json={"model": "fake-chat-1", "input": "ping"},
        )
        ensure(r.status_code == 200, "5-responses", f"失败 {brief(r)}")
        data = r.json()
        ensure(
            data.get("object") == "response",
            "5-responses",
            f"非 response 对象 {r.text[:300]!r}",
        )
        ensure(isinstance(data.get("output"), list), "5-responses", f"缺 output 数组 {r.text[:300]!r}")
        ensure("choices" not in data, "5-responses", f"混入 chat 形状 {r.text[:300]!r}")
        say(
            f"STEP5 status=200 object={data.get('object')!r} id={data.get('id')!r} "
            f"output_items={len(data['output'])}"
        )
    finally:
        restore: dict = {"enabled": bool(before.get("enabled"))}
        restore["base_url"] = before["base_url"] if before.get("base_url_source") == "db" else ""
        if not before.get("has_api_key"):
            restore["api_key"] = ""
        rr = client.patch("/api/providers/codar", json=restore)
        say(f"STEP5-restore codar status={rr.status_code} body={rr.text[:200]!r}")


def step6_logs_stats(client: httpx.Client, key: str, key_id: int) -> int:
    logs = client.get("/api/logs", params={"key_id": key_id, "limit": 50})
    ensure(logs.status_code == 200, "6-logs", f"日志查询失败 {brief(logs)}")
    payload = logs.json()
    items = payload.get("items", [])
    total = int(payload.get("total", 0))
    ensure(total >= 4, "6-logs", f"该 key 日志只有 {total} 条，主线至少 4 条成功请求")
    wrong = [it for it in items if it.get("key_id") != key_id]
    ensure(not wrong, "6-logs", f"混入他 key 的日志 {wrong[:2]!r}")
    ok_rows = [it for it in items if int(it.get("status", 500)) < 400]
    ensure(len(ok_rows) >= 4, "6-logs", f"成功日志不足 {[(it.get('status'), it.get('error')) for it in items]!r}")
    stream_rows = [it for it in items if it.get("stream")]
    ensure(stream_rows, "6-logs", "流式请求没有以 stream=True 落日志")
    expect_prefix = f"u2a_{key[4:8]}****"
    bad_prefix = [it.get("key_prefix") for it in items if it.get("key_prefix") != expect_prefix]
    ensure(not bad_prefix, "6-logs", f"key_prefix 口径不符：{bad_prefix} != {expect_prefix}")
    say(f"STEP6a logs total={total} rows={[(it['provider'], it['model'], it['status'], it['stream'], it['latency_ms']) for it in items]}")

    stats = client.get("/api/stats/summary")
    ensure(stats.status_code == 200, "6-stats", f"统计查询失败 {brief(stats)}")
    s = stats.json()
    ensure(
        int(s.get("total_requests", 0)) >= 4,
        "6-stats",
        f"total_requests={s.get('total_requests')} 未计入本次请求 {s!r}",
    )
    ensure(int(s.get("active_keys", 0)) >= 1, "6-stats", f"active_keys={s.get('active_keys')}")
    say(
        f"STEP6b stats total_requests={s.get('total_requests')} "
        f"active_keys={s.get('active_keys')} top_model={s.get('top_model')!r} "
        f"failures={s.get('failures')}"
    )
    return total


def _viewer_probe(viewer: httpx.Client, key: str, key_id: int, key_name: str, phase: str) -> None:
    """非 admin 视角的泄露检查：他人 key_id 要 403/空集，全量列表 0 行，正文零泄露。"""
    by_id = viewer.get("/api/logs", params={"key_id": key_id, "limit": 50})
    ok_shape = by_id.status_code == 403 or (
        by_id.status_code == 200 and int(by_id.json().get("total", -1)) == 0
    )
    ensure(
        ok_shape,
        "6b-viewer",
        f"{phase} ?key_id= 应为 403 或空集，实际 {by_id.status_code} {by_id.text[:300]}",
    )
    listed = viewer.get("/api/logs", params={"limit": 50})
    ensure(listed.status_code == 200, "6b-viewer", f"{phase} viewer 全量日志失败 {brief(listed)}")
    ensure(
        int(listed.json().get("total", -1)) == 0,
        "6b-viewer",
        f"{phase} viewer 看到了不属于自己的行 {listed.text[:300]}",
    )
    needles = {
        "明文密钥": key,
        "key_name": key_name,
        "10位前缀": key[:10],
        "掩码前缀": f"u2a_{key[4:8]}****",
    }
    for label, needle in needles.items():
        ensure(needle not in listed.text, "6b-viewer", f"{phase} 列表泄露{label}")
        ensure(needle not in by_id.text, "6b-viewer", f"{phase} key_id 查询泄露{label}")
    say(
        f"STEP6b-{phase} viewer by_key_id={by_id.status_code} "
        f"unfiltered_total={listed.json().get('total')} leak=none"
    )


def step65_viewer_scope(client: httpx.Client, base: str, key: str, key_id: int, key_name: str) -> httpx.Client:
    viewer_name = f"e2e-viewer-{uuid.uuid4().hex[:6]}"
    created = client.post(
        "/api/users",
        json={"username": viewer_name, "password": "viewer-pass-1", "role": "viewer"},
    )
    ensure(created.status_code == 200, "6b-viewer", f"建 viewer 失败 {brief(created)}")
    viewer = httpx.Client(base_url=base, trust_env=False, timeout=httpx.Timeout(30.0, connect=5.0))
    login = viewer.post("/api/login", json={"username": viewer_name, "password": "viewer-pass-1"})
    ensure(login.status_code == 200, "6b-viewer", f"viewer 登录失败 {brief(login)}")
    _viewer_probe(viewer, key, key_id, key_name, "before-delete")
    return viewer


def step7_delete(
    client: httpx.Client, viewer: httpx.Client, key: str, key_id: int, key_name: str, before_total: int
) -> None:
    d = client.delete(f"/api/keys/{key_id}")
    ensure(d.status_code == 200, "7-delete", f"删除失败 {brief(d)}")

    after = client.get("/api/keys")
    ensure(after.status_code == 200, "7-delete", f"删除后列表查询失败 {brief(after)}")
    items = after.json()
    ensure(
        all(it.get("id") != key_id for it in items),
        "7-delete",
        "删除后 /api/keys 里仍能按 id 找到该密钥",
    )
    ensure(
        all(it.get("name") != key_name for it in items),
        "7-delete",
        "删除后 /api/keys 里仍能按 name 找到该密钥",
    )

    hdr = {"Authorization": f"Bearer {key}", "x-provider": "codebuddy"}
    retry = client.post("/v1/chat/completions", headers=hdr, json=_chat_payload())
    ensure(
        retry.status_code in (401, 403),
        "7-delete",
        f"删除后同一密钥仍可用 {brief(retry)}",
    )
    retry_anth = client.post(
        "/v1/messages",
        headers={"x-api-key": key, "x-provider": "codebuddy", **ANTHROPIC_HEADERS},
        json={"model": "fake-chat-1", "max_tokens": 16, "messages": [{"role": "user", "content": "ping"}]},
    )
    ensure(
        retry_anth.status_code in (401, 403),
        "7-delete",
        f"删除后 x-api-key 仍可用 {brief(retry_anth)}",
    )

    # 被删的 key 不得把新请求挂到自己的历史日志上：key_id 维度行数必须不变
    logs = client.get("/api/logs", params={"key_id": key_id, "limit": 50})
    ensure(logs.status_code == 200, "7-delete", f"删除后日志查询失败 {brief(logs)}")
    lp = logs.json()
    ensure(
        int(lp.get("total", 0)) == before_total,
        "7-delete",
        f"删除后该 key 的日志行数 {lp.get('total')} != 删除前 {before_total}（401 请求挂上了已删 key）",
    )
    # 写时固化：删 key 后历史行前缀必须原样保留（之前是读时 join，删完就变空串）
    expect_prefix = f"u2a_{key[4:8]}****"
    for it in lp.get("items", []):
        ensure(
            it.get("key_prefix") == expect_prefix,
            "7-delete",
            f"删 key 后历史行 key_prefix 应固化为 {expect_prefix}，实际 {it.get('key_prefix')!r}",
        )
    ensure(
        retry.status_code not in [it.get("status") for it in lp.get("items", []) if it.get("key_id") == key_id],
        "7-delete",
        "401 拒绝被记成了该 key 的新日志行",
    )
    _viewer_probe(viewer, key, key_id, key_name, "after-delete")
    say(
        f"STEP7 delete=200 keys_gone=True retry_chat={retry.status_code} "
        f"retry_messages={retry_anth.status_code} logs_total_before={before_total} "
        f"logs_total_after={lp.get('total')} key_prefix_frozen={expect_prefix}"
    )


def run(args: argparse.Namespace) -> None:
    base = args.base_url.rstrip("/")
    say(f"target={base} fake_upstream={args.fake_base}")
    with httpx.Client(
        base_url=base,
        trust_env=False,
        timeout=httpx.Timeout(30.0, connect=5.0),
    ) as client:
        step1_login(client, args.admin_user, args.admin_password)
        key, key_id, key_name = step2_create_key(client)
        step3_chat(client, key)
        step4_messages(client, key)
        step5_responses(client, key, args.fake_base)
        before_total = step6_logs_stats(client, key, key_id)
        viewer = step65_viewer_scope(client, base, key, key_id, key_name)
        try:
            step7_delete(client, viewer, key, key_id, key_name, before_total)
        finally:
            viewer.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="unified-2api 主线 E2E（API 级）")
    parser.add_argument("--base-url", default=os.getenv("E2E_CTRL_URL", "http://127.0.0.1:8210"))
    parser.add_argument("--fake-base", default=os.getenv("E2E_FAKE_URL", "http://127.0.0.1:8211"))
    parser.add_argument("--admin-user", default=os.getenv("UNIFIED_ADMIN_USER", "admin"))
    parser.add_argument("--admin-password", default=os.getenv("UNIFIED_ADMIN_PASSWORD", "test"))
    args = parser.parse_args()

    try:
        run(args)
    except StepFail as exc:
        print(f"MAINLINE FAIL {exc}", flush=True, file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"MAINLINE FAIL [http] {exc!r}", flush=True, file=sys.stderr)
        return 1
    say("MAINLINE PASS 全部 7 步断言通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
