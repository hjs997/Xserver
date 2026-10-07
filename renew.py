import asyncio
import os
import sys
from datetime import datetime
import requests
from patchright.async_api import async_playwright

# ==================== 用户原版配置区域 ====================
# 代理配置 - 可选，不填则不使用代理
PROXY_SERVER = os.getenv("PROXY_SERVER") or ""
USE_PROXY = bool(PROXY_SERVER)  # 如果有代理地址则启用

# XServer登录配置 - 可以直接填写或使用环境变量
LOGIN_EMAIL = os.getenv("XSERVER_EMAIL") or ""
LOGIN_PASSWORD = os.getenv("XSERVER_PASSWORD") or ""
TARGET_URL = "https://secure.xserver.ne.jp/xapanel/login/xmgame"

# Telegram配置 - 可选，不填则不推送
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or ""
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or ""

# 面板上报配置 - 可选，不填则不上报
PANEL_URL = os.getenv("PANEL_URL", "")
SERVER_NAME = os.getenv("SERVER_NAME", "")

# 游戏管理页 URL 特征
GAME_PANEL_URL_HINTS = (
    "xmgame/game/index",
    "xmgame/game/",
    "/game/freeplan/",
)
# =========================================================


def send_telegram_msg(message: str):
    """发送 Telegram 机器人通知"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"⚠️ 发送 Telegram 通知失败: {e}")


def get_screenshot_filename(step_name: str) -> str:
    timestamp = datetime.now().strftime("%H%M%S")
    return f"{step_name}_{timestamp}.png"


async def main():
    print("============================================================")
    print("XServer GAME 自动续期脚本 (Patchright 抗检测加固版)")
    print("============================================================")

    if not LOGIN_EMAIL or not LOGIN_PASSWORD:
        print("❌ 错误：未配置 XSERVER_EMAIL 或 XSERVER_PASSWORD！")
        send_telegram_msg("❌ <b>XServer 自动续期失败</b>\n原因：缺少账号密码配置。")
        sys.exit(1)

    async with async_playwright() as p:
        browser_args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--window-size=1920,1080",
        ]

        launch_options = {
            "headless": False,  # 配合 Workflow 中的 Xvfb 虚拟桌面
            "args": browser_args
        }

        print("✅ 正在启动 Chromium 浏览器...")
        browser = await p.chromium.launch(**launch_options)

        context_options = {
            "viewport": {"width": 1920, "height": 1080},
            "locale": "ja-JP",
            "timezone_id": "Asia/Tokyo",
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        }

        # 读取代理配置
        if USE_PROXY and PROXY_SERVER.strip():
            print(f"🌐 使用代理服务器: {PROXY_SERVER}")
            context_options["proxy"] = {"server": PROXY_SERVER.strip()}
        else:
            print("🌐 未配置代理或代理被禁用，使用直连网络模式")

        context = await browser.new_context(**context_options)
        page = await context.new_page()

        # 防检测脚本注入
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        """)

        try:
            print(f"🌐 正在打开登录页面: {TARGET_URL}")
            await page.goto(TARGET_URL, wait_until="networkidle", timeout=60000)
            await page.screenshot(path=get_screenshot_filename("step_01_login_page_loaded"))

            print("🎯 正在填入账号密码...")
            email_input = page.locator('input[name="member_id"], input[name="email"], #member_id').first
            password_input = page.locator('input[name="user_password"], input[name="password"], #user_password').first

            await email_input.fill(LOGIN_EMAIL)
            await page.wait_for_timeout(500)
            await password_input.fill(LOGIN_PASSWORD)
            await page.wait_for_timeout(500)
            print("✅ 账号密码已填充完毕")

            # ==================== Cloudflare Turnstile 抗检测处理 ====================
            # 关键：绝对不要调用 click() 去强行点击 Turnstile iframe
            print("⏳ 等待 Cloudflare Turnstile 自动完成验证 (最多 30 秒)...")
            token_passed = False
            for i in range(30):
                has_token = await page.evaluate("""() => {
                    const el = document.querySelector('input[name="cf-turnstile-response"], input[name="g-recaptcha-response"]');
                    return el && el.value && el.value.length > 20;
                }""")
                if has_token:
                    token_passed = True
                    print(f"🎉 Cloudflare Turnstile 在第 {i+1} 秒自动验证通过！")
                    break
                await asyncio.sleep(1)

            await page.screenshot(path=get_screenshot_filename("step_02_form_filled"))

            if not token_passed:
                print("⚠️ Turnstile 未能在预期时间内自动就绪，尝试平滑移动鼠标模拟真实人手...")
                await page.mouse.move(300, 300)
                await page.mouse.move(320, 350)
                await asyncio.sleep(3)

            print("⌨️ 正在点击登录按钮...")
            submit_button = page.locator('button[type="submit"], input[type="submit"], .btn_login').first
            await submit_button.click()

            print("⏳ 等待提交后页面响应...")
            await page.wait_for_load_state("networkidle", timeout=30000)
            await asyncio.sleep(3)

            current_url = page.url
            print(f"🔍 提交后页面 URL: {current_url}")
            await page.screenshot(path=get_screenshot_filename("step_03_after_login_attempt"))

            # 校验登录状态
            if "login" in current_url:
                print("❌ 登录失败：页面仍然停留在 login 路径上，请根据上传的截图排查原因。")
                send_telegram_msg("❌ <b>XServer 自动续期失败</b>\n原因：登录失败，未能跳转后台。")
                sys.exit(1)

            print("🎉 登录成功！检测管理页面...")

            # 检测匹配游戏管理页特征或点击续期按钮
            renew_executed = False
            renew_selectors = [
                'a:has-text("更新")',
                'button:has-text("更新")',
                'a:has-text("延長")',
                'button:has-text("延長")',
                'a:has-text("契約更新")',
            ]

            for selector in renew_selectors:
                renew_btn = page.locator(selector).first
                if await renew_btn.is_visible():
                    print(f"🎯 找到续期按钮 ({selector})，正在执行点击...")
                    await renew_btn.click()
                    await page.wait_for_load_state("networkidle")
                    await asyncio.sleep(2)
                    renew_executed = True
                    await page.screenshot(path=get_screenshot_filename("step_04_renew_action_done"))
                    print("✅ 已点击续期按钮！")
                    break

            if not renew_executed:
                print("ℹ️ 未发现需手动点击的续期按钮，服务可能正处于有效状态。")

            await page.screenshot(path=get_screenshot_filename("step_05_final_success"))

            msg = (
                f"✅ <b>XServer GAME 自动续期成功</b>\n"
                f"⏰ 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"🔗 页面: {current_url}"
            )
            print(msg)
            send_telegram_msg(msg)

        except Exception as e:
            print(f"❌ 运行报错: {e}")
            await page.screenshot(path=get_screenshot_filename("step_error"))
            send_telegram_msg(f"❌ <b>XServer 自动续期异常</b>\n报错: {e}")
            sys.exit(1)
        finally:
            print("🧹 清理浏览器资源...")
            await context.close()
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
