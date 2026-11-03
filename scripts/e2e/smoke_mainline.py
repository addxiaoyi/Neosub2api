"""Playwright 主线冒烟：登录 → 建密钥 → Playground 发一次 chat → 删密钥。

默认 headless、可重复跑（每次新建一个带时间戳的密钥，结束删掉）。
失败时把截图与页面 HTML 落到 artifacts 目录，便于定位是哪一步断了。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from pathlib import Path

from playwright.sync_api import Page, sync_playwright


def _step(msg: str) -> None:
    print(f"[e2e] {msg}", flush=True)


def _shot(page: Page, artifacts: Path, name: str) -> Path:
    artifacts.mkdir(parents=True, exist_ok=True)
    path = artifacts / name
    page.screenshot(path=str(path), full_page=True)
    _step(f"截图：{path}")
    return path


def run(web_url: str, artifacts: Path, headed: bool) -> None:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    key_name = f"e2e-keys-{stamp}"
    errors: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        page = context.new_page()
        page.set_default_timeout(20000)
        # 前端运行时异常单独收集：页面上看不出错时靠它定位
        page.on("pageerror", lambda exc: errors.append(str(exc)))

        try:
            _step(f"打开 {web_url}/login/")
            page.goto(f"{web_url}/login/", wait_until="domcontentloaded")
            page.fill("#username", "admin")
            page.fill("#password", "test")
            page.click("button[type=submit]")
            # 登录后是客户端路由跳转，不会触发 load 事件，按 pathname 轮询更稳
            page.wait_for_function("() => location.pathname.startsWith('/dashboard')", timeout=30000)
            _step("登录成功，进入 /dashboard")

            _step("进入 /keys 新建密钥")
            page.goto(f"{web_url}/keys/", wait_until="domcontentloaded")
            page.get_by_role("button", name="新建密钥").first.click()
            dialog = page.locator('[role="dialog"]').last
            dialog.locator("input").fill(key_name)
            dialog.get_by_role("button", name="创建").click()

            issued = page.locator('[role="dialog"]').last
            issued.get_by_text("密钥创建成功").wait_for()
            api_key = issued.locator("code").first.inner_text().strip()
            assert api_key.startswith("u2a_"), f"未拿到完整密钥：{api_key!r}"
            issued.get_by_role("button", name="我已保存").click()
            page.locator("tbody tr").filter(has_text=key_name).wait_for()
            _step(f"密钥已创建：{key_name}")

            _step("进入 /playground 发送一次 chat 请求")
            page.goto(f"{web_url}/playground/", wait_until="domcontentloaded")
            page.fill("#pg-key", api_key)
            # 提交按钮在 keyDraft 落进 React state 之前是 disabled 的，点它会自动等可用；
            # 直接按 Enter 有竞态（keydown 闭包可能还拿到空 state）
            page.get_by_role("button", name="保存并加载模型").click()
            page.wait_for_selector("textarea:enabled")
            with page.expect_response(
                lambda r: "/v1/chat/completions" in r.url, timeout=30000
            ) as hit:
                page.fill("textarea", "ping")
                page.press("textarea", "Enter")
            upstream = hit.value
            assert upstream.status == 200, f"chat 请求失败：{upstream.status} {upstream.text()[:200]}"

            bubble = page.locator("text=[fake] model=").first
            bubble.wait_for(timeout=30000)
            answer = bubble.inner_text()
            assert "[fake] model=fake-chat-1" in answer, f"响应内容不符：{answer[:120]!r}"
            _step(f"收到响应：{answer.strip()[:80]}")

            _step("回到 /keys 删除该密钥")
            page.goto(f"{web_url}/keys/", wait_until="domcontentloaded")
            row = page.locator("tbody tr").filter(has_text=key_name)
            row.locator('button[title="删除"]').click()
            confirm = page.locator('[role="alertdialog"]').last
            confirm.get_by_role("button", name="删除").click()
            row.wait_for(state="detached")
            _step("列表已移除该密钥")

            listed = page.request.get(f"{web_url}/api/keys")
            assert listed.status == 200, f"/api/keys 返回 {listed.status}"
            names = [item["name"] for item in listed.json()]
            assert key_name not in names, "界面上删掉了，后端仍然存在"
            _step("后端已确认删除")

            assert not errors, f"页面出现运行时异常：{errors[:3]}"
            _shot(page, artifacts, "smoke-final.png")
            _step("主线冒烟通过")
        except Exception:
            try:
                _shot(page, artifacts, f"failure-{stamp}.png")
                (artifacts / f"failure-{stamp}.html").write_text(page.content(), encoding="utf-8")
                _step(f"失败页面 URL：{page.url}")
                _step(f"运行时异常：{errors[:5]}")
            finally:
                traceback.print_exc()
            raise
        finally:
            context.close()
            browser.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--web-url",
        default=os.getenv("E2E_WEB_URL", "http://127.0.0.1:18111"),
        help="前端入口（默认控制面直出静态导出）",
    )
    parser.add_argument(
        "--artifacts",
        default=str(Path(__file__).resolve().parent / "artifacts"),
        help="截图/HTML 输出目录",
    )
    parser.add_argument("--headed", action="store_true", default=os.getenv("E2E_HEADED") == "1")
    args = parser.parse_args()

    try:
        run(args.web_url.rstrip("/"), Path(args.artifacts), args.headed)
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
