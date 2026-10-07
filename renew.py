#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XServer GAME 自动登录和续期脚本 - Patchright防检测增强版

修复要点:
1. 增强 Turnstile 自动化检测避让（移除 webdriver 标记、添加真实 User-Agent）
2. 修复 Cloudflare 验证失败(検証に失敗しました)问题
3. 增加对 Turnstile token (cf-turnstile-response) 自动生成的轮询检测
4. jumpvps 中间页主动提交与多标签兼容
5. 完善错误捕获与 Telegram 运行结果通知
"""

import asyncio
import time
import re
import datetime
from datetime import timezone, timedelta
import os
import requests
from patchright.async_api import async_playwright

# =====================================================================
#                          配置区域
# =====================================================================

IS_GITHUB_ACTIONS = os.getenv("GITHUB_ACTIONS") == "true"
USE_HEADLESS = IS_GITHUB_ACTIONS or os.getenv("USE_HEADLESS", "false").lower() == "true"
WAIT_TIMEOUT = int(os.getenv("WAIT_TIMEOUT", "20000"))  # 页面元素等待超时时间(毫秒)
PAGE_LOAD_DELAY = int(os.getenv("PAGE_LOAD_DELAY", "3"))  # 页面加载延迟时间(秒)
JUMPVPS_TIMEOUT = int(os.getenv("JUMPVPS_TIMEOUT", "60"))  # jumpvps 等待秒数

# 代理配置
PROXY_SERVER = os.getenv("PROXY_SERVER") or ""
USE_PROXY = bool(PROXY_SERVER)

# XServer 登录配置
LOGIN_EMAIL = os.getenv("XSERVER_EMAIL") or ""
LOGIN_PASSWORD = os.getenv("XSERVER_PASSWORD") or ""
TARGET_URL = "https://secure.xserver.ne.jp/xapanel/login/xmgame"

# Telegram 配置
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or ""
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or ""

# 面板上报配置
PANEL_URL = os.getenv("PANEL_URL", "")
SERVER_NAME = os.getenv("SERVER_NAME", "")

GAME_PANEL_URL_HINTS = (
    "xmgame/game/index",
    "xmgame/game/",
    "/game/freeplan/",
)

# =====================================================================
#                        Telegram 推送模块
# =====================================================================

class TelegramNotifier:
    """Telegram 通知推送类"""

    def __init__(self, bot_token=None, chat_id=None):
        self.bot_token = bot_token or TELEGRAM_BOT_TOKEN
        self.chat_id = chat_id or TELEGRAM_CHAT_ID
        self.enabled = bool(self.bot_token and self.chat_id)

    def send_photo(self, photo_path, caption=None):
        if not self.enabled:
            return False
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendPhoto"
            with open(photo_path, "rb") as f:
                payload = {"chat_id": self.chat_id}
                if caption:
                    payload["caption"] = caption
                response = requests.post(url, data=payload, files={"photo": f}, timeout=20)
                return response.json().get("ok", False)
        except Exception as e:
            print(f"❌ Telegram 推送图片异常: {e}")
            return False

    def send_message(self, message, parse_mode="HTML"):
        if not self.enabled:
            return False
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            payload = {"chat_id": self.chat_id, "text": message, "parse_mode": parse_mode}
            response = requests.post(url, json=payload, timeout=10)
            return response.json().get("ok", False)
        except Exception as e:
            print(f"❌ Telegram 推送异常: {e}")
            return False

    def send_renewal_result(self, status, old_time, new_time=None, run_time=None):
        beijing_time = datetime.datetime.now(timezone(timedelta(hours=8)))
        timestamp = run_time or beijing_time.strftime("%Y-%m-%d %H:%M:%S")

        message = f"<b>🎮 XServer GAME 续期通知</b>\n\n"
        message += f"🕐 运行时间: <code>{timestamp}</code>\n"
        message += f"🖥 服务器: <code>🇯🇵 Xserver(MC)</code>\n\n"

        if status == "Success":
            message += f"📊 续期结果: <b>✅ 成功</b>\n"
            message += f"🕛 旧到期: <code>{old_time}</code>\n"
            message += f"🕡 新到期: <code>{new_time}</code>\n"
        elif status == "Unexpired":
            message += f"📊 续期结果: <b>ℹ️ 未到期</b>\n"
            message += f"🕛 到期时间: <code>{old_time}</code>\n"
            message += f"💡 提示: 剩余时间超过24小时,无需续期\n"
        elif status == "Failed":
            message += f"📊 续期结果: <b>❌ 失败</b>\n"
            message += f"🕛 到期时间: <code>{old_time}</code>\n"
            message += f"⚠️ 请检查日志或手动续期\n"
        else:
            message += f"📊 续期结果: <b>❓ 未知</b>\n"
            message += f"🕛 到期时间: <code>{old_time}</code>\n"

        return self.send_message(message)


# =====================================================================
#                        XServer 自动登录类
# =====================================================================

class XServerAutoLogin:
    """XServer GAME 自动登录主类"""

    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.headless = USE_HEADLESS
        self.email = LOGIN_EMAIL
        self.password = LOGIN_PASSWORD
        self.target_url = TARGET_URL
        self.wait_timeout = WAIT_TIMEOUT
        self.page_load_delay = PAGE_LOAD_DELAY
        self.screenshot_count = 0

        self.old_expiry_time = None
        self.new_expiry_time = None
        self.renewal_status = "Unknown"
        self.remaining_seconds = 0

        self.telegram = TelegramNotifier()

    def report_status(self, remaining_seconds):
        if not PANEL_URL:
            return
        try:
            payload = {
                "server_name": SERVER_NAME,
                "remaining_time": remaining_seconds,
                "status": "up",
            }
            resp = requests.post(PANEL_URL, json=payload, timeout=10)
            print(f"✅ 上报成功: {resp.json()}")
        except Exception as e:
            print(f"❌ 上报失败: {e}")

    def parse_remaining_seconds(self, time_str):
        try:
            hours, minutes = 0, 0
            h_match = re.search(r"(\d+)時間", time_str)
            if h_match:
                hours = int(h_match.group(1))
            m_match = re.search(r"(\d+)分", time_str)
            if m_match:
                minutes = int(m_match.group(1))
            return (hours * 3600) + (minutes * 60)
        except Exception:
            return 0

    @staticmethod
    def is_game_panel_url(url: str) -> bool:
        if not url:
            return False
        return any(hint in url for hint in GAME_PANEL_URL_HINTS)

    @staticmethod
    def is_login_success_url(url: str) -> bool:
        if not url:
            return False
        return "xapanel/xmgame/index" in url or url.rstrip("/").endswith("xmgame/index")

    async def setup_browser(self):
        try:
            self.playwright = await async_playwright().start()

            browser_args = [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--window-size=1920,1080",
                "--lang=ja-JP",
            ]

            if USE_PROXY and PROXY_SERVER:
                print(f"🌐 使用代理服务器: {PROXY_SERVER}")
                browser_args.append(f"--proxy-server={PROXY_SERVER}")

            self.browser = await self.playwright.chromium.launch(
                headless=self.headless,
                args=browser_args,
            )

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

            if USE_PROXY and PROXY_SERVER:
                context_options["proxy"] = {"server": PROXY_SERVER}

            self.context = await self.browser.new_context(**context_options)

            # 注入脚本：抹除 navigator.webdriver 特征
            await self.context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                window.navigator.chrome = { runtime: {} };
            """)

            self.page = await self.context.new_page()
            self.page.set_default_timeout(self.wait_timeout)
            print("✅ 浏览器初始化及抗检测配置成功")
            return True

        except Exception as e:
            print(f"❌ 浏览器初始化失败: {e}")
            return False

    async def safe_wait_load(self, timeout=15000):
        if not self.page:
            return
        for state in ("domcontentloaded", "load"):
            try:
                await self.page.wait_for_load_state(state, timeout=timeout)
            except Exception:
                pass

    async def take_screenshot(self, step_name=""):
        try:
            if not self.page:
                return
            await self.safe_wait_load(timeout=5000)
            self.screenshot_count += 1
            beijing_time = datetime.datetime.now(timezone(timedelta(hours=8)))
            timestamp = beijing_time.strftime("%H%M%S")
            filename = f"step_{self.screenshot_count:02d}_{timestamp}_{step_name}.png"
            filename = re.sub(r'[<>:"/\\|?*]', "_", filename)
            try:
                await self.page.screenshot(path=filename, full_page=True, timeout=10000)
            except Exception:
                await self.page.screenshot(path=filename, full_page=False, timeout=5000)
            print(f"📸 截图已保存: {filename}")
        except Exception as e:
            print(f"⚠️ 截图失败: {e}")

    def validate_config(self):
        if not self.email or not self.password:
            print("❌ 账号或密码未配置!")
            return False
        return True

    async def cleanup(self):
        try:
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
            print("🧹 浏览器资源已清理完毕")
        except Exception as e:
            print(f"⚠️ 清理出错: {e}")

    async def navigate_to_login(self):
        try:
            print(f"🌐 正在打开登录页面: {self.target_url}")
            await self.page.goto(self.target_url, wait_until="load", timeout=60000)
            await self.page.wait_for_selector("body", timeout=self.wait_timeout)
            await self.take_screenshot("login_page_loaded")
            return True
        except Exception as e:
            print(f"❌ 页面导航失败: {e}")
            return False

    async def perform_login(self):
        """核心登录方法：包含完整的 Turnstile 验证框防卡死处理"""
        try:
            print("🎯 开始填入账号密码...")
            await asyncio.sleep(self.page_load_delay)

            # 1. 模拟真实击键填充账号密码
            email_input = self.page.locator("input[name='memberid']")
            await email_input.wait_for(timeout=self.wait_timeout)
            await email_input.click()
            await email_input.fill(self.email)

            pwd_input = self.page.locator("input[name='user_password']")
            await pwd_input.wait_for(timeout=self.wait_timeout)
            await pwd_input.click()
            await pwd_input.fill(self.password)

            print("✅ 账号密码已填充完毕")
            await asyncio.sleep(2)

            # 2. 交互处理 Cloudflare Turnstile
            print("⏳ 正在定位并处理 Cloudflare Turnstile 验证...")
            turnstile_frame = None

            for _ in range(8):
                for frame in self.page.frames:
                    if "challenges.cloudflare.com" in frame.url or "turnstile" in frame.url:
                        turnstile_frame = frame
                        break
                if turnstile_frame:
                    break
                await asyncio.sleep(1)

            if turnstile_frame:
                print("🔍 检测到 Turnstile 验证码 iframe，尝试精准点击...")
                try:
                    await asyncio.sleep(2)
                    # 尝试点击复选框元素
                    cb = turnstile_frame.locator('input[type="checkbox"], .mark, #challenge-stage, body')
                    if await cb.count() > 0:
                        await cb.first.click(delay=150)
                        print("✅ 已点击 Turnstile 勾选框区域")
                except Exception as cf_err:
                    print(f"⚠️ 模拟点击 Turnstile 异常 (将靠隐式验证等待): {cf_err}")

            # 3. 轮询检测 token 是否生成
            print("⏳ 等待 Turnstile token 验证完成 (最多 15 秒)...")
            token_passed = False
            for _ in range(15):
                has_token = await self.page.evaluate("""() => {
                    const res = document.querySelector('input[name="cf-turnstile-response"], [name="g-recaptcha-response"]');
                    return res && res.value && res.value.length > 10;
                }""")
                if has_token:
                    token_passed = True
                    print("🎉 Cloudflare Turnstile 验证成功通过！")
                    break
                await asyncio.sleep(1)

            if not token_passed:
                print("⚠️ Turnstile 自动化 token 未能完全就绪，尝试强制触发提交...")

            await self.take_screenshot("form_filled")

            # 4. 点击登录按钮
            print("⌨️ 正在点击登录按钮...")
            login_btn = self.page.locator("input[value='ログインする'], button[type='submit']")
            if await login_btn.count() > 0:
                await login_btn.first.click()
            else:
                await self.page.evaluate("document.querySelector('form').submit()")

            try:
                await self.page.wait_for_navigation(timeout=15000, wait_until="domcontentloaded")
            except Exception:
                pass

            await asyncio.sleep(3)
            return True

        except Exception as e:
            print(f"❌ 执行登录遇到错误: {e}")
            return False

    async def kick_jumpvps_redirect(self):
        try:
            res = await self.page.evaluate("""() => {
                const forms = document.querySelectorAll('form');
                for (const form of forms) {
                    try { form.submit(); return true; } catch (e) {}
                }
                const btn = document.querySelector('a, button, input[type=submit]');
                if (btn) { btn.click(); return true; }
                return false;
            }""")
            return res
        except Exception:
            return False

    async def wait_for_game_panel(self, timeout_sec=None):
        timeout_sec = timeout_sec or JUMPVPS_TIMEOUT
        deadline = time.time() + timeout_sec
        kick_count = 0

        while time.time() < deadline:
            try:
                url = self.page.url
            except Exception:
                await asyncio.sleep(0.5)
                continue

            if self.is_game_panel_url(url):
                await self.safe_wait_load(timeout=20000)
                return True

            if "jumpvps" in (url or "") and kick_count < 3:
                print(f"🔄 当前卡在 jumpvps 跳转页，尝试第 {kick_count + 1} 次主动推动...")
                await self.kick_jumpvps_redirect()
                kick_count += 1

            await asyncio.sleep(2)
        return False

    async def open_game_management(self):
        print("🔍 正在查找 ゲーム管理 按钮...")
        btn = "a:has-text('ゲーム管理')"
        await self.page.wait_for_selector(btn, timeout=self.wait_timeout)

        popup_task = asyncio.create_task(self.context.wait_for_event("page", timeout=10000))

        try:
            await self.page.click(btn)
        except Exception:
            pass

        try:
            new_page = await asyncio.wait_for(popup_task, timeout=3)
            if new_page:
                self.page = new_page
                self.page.set_default_timeout(self.wait_timeout)
        except Exception:
            pass

        if await self.wait_for_game_panel():
            await self.take_screenshot("game_page_loaded")
            await self.get_server_time_info()
            await self.click_upgrade_button()
            return True
        return False

    async def handle_login_result(self):
        try:
            await self.safe_wait_load(timeout=20000)
            await asyncio.sleep(2)

            current_url = self.page.url
            print(f"🔍 提交后页面 URL: {current_url}")

            # 抓取页面红字提示
            err_msg = await self.page.evaluate("""() => {
                const el = document.querySelector('.err, .error, .mb10.red, p.red, div.red');
                return el ? el.innerText.trim() : '';
            }""")

            if err_msg:
                print(f"❌ 页面提示红字错误: {err_msg}")

            if self.is_game_panel_url(current_url):
                await self.take_screenshot("game_page_loaded")
                await self.get_server_time_info()
                await self.click_upgrade_button()
                return True

            if "jumpvps" in current_url:
                await self.kick_jumpvps_redirect()
                if await self.wait_for_game_panel():
                    await self.take_screenshot("game_page_loaded")
                    await self.get_server_time_info()
                    await self.click_upgrade_button()
                    return True

            if self.is_login_success_url(current_url):
                return await self.open_game_management()

            await self.take_screenshot("login_failed")
            return False

        except Exception as e:
            print(f"❌ 登录结果验证异常: {e}")
            return False

    async def get_server_time_info(self):
        print("🕒 正在获取服务器到期时间信息...")
        for _ in range(3):
            try:
                await self.safe_wait_load(timeout=10000)
                body_text = await self.page.locator("body").inner_text()
                match = re.search(r"残り(\d+時間\d+分)", body_text)
                if match:
                    rem_str = match.group(1)
                    print(f"⏰ 剩余时间: {rem_str}")
                    self.remaining_seconds = self.parse_remaining_seconds(rem_str)

                    exp_match = re.search(r"\((\d{4}-\d{2}-\d{2}[^)]*)まで\)", body_text)
                    if exp_match and not self.old_expiry_time:
                        self.old_expiry_time = exp_match.group(1).strip()
                        print(f"📅 记录原到期时间: {self.old_expiry_time}")
                    return
                await asyncio.sleep(2)
            except Exception:
                pass

    async def click_upgrade_button(self):
        try:
            print("📄 正在点击 期限延長 按钮...")
            upgrade_selector = "a:has-text('アップグレード・期限延長'), a[href*='freeplan/extend']"
            await self.page.wait_for_selector(upgrade_selector, timeout=self.wait_timeout)
            await self.page.click(upgrade_selector)
            await self.safe_wait_load(timeout=20000)
            await self.check_extension_restriction()
        except Exception as e:
            print(f"❌ 点击升级续期按钮失败: {e}")
            self.renewal_status = "Failed"

    async def check_extension_restriction(self):
        try:
            await asyncio.sleep(2)
            body_text = await self.page.locator("body").inner_text()

            match = re.search(r"更新をご希望の場合は、(.+?)以降にお試しください。", body_text)
            if match:
                next_time = match.group(1).strip()
                print(f"📋 未到 24 小时续期开放窗口，下次可续期时间: {next_time}")
                self.old_expiry_time = f"未到期 (下次开放: {next_time})"
                self.renewal_status = "Unexpired"
            elif "残り契約時間が24時間を切るまで" in body_text:
                print("📋 距离到期超过 24 小时，暂无需续期")
                self.renewal_status = "Unexpired"
            else:
                print("⚡ 已开放续期窗口，正在进行延长...")
                await self.perform_extension_operation()
        except Exception as e:
            print(f"❌ 检查可续期状态异常: {e}")

    async def perform_extension_operation(self):
        try:
            btn1 = "text='期限を延長する'"
            await self.page.wait_for_selector(btn1, timeout=self.wait_timeout)
            await self.page.click(btn1)
            await self.safe_wait_load(timeout=20000)

            btn2 = "button[type='submit']:has-text('確認画面に進む')"
            await self.page.wait_for_selector(btn2, timeout=self.wait_timeout)
            await self.page.click(btn2)
            await self.safe_wait_load(timeout=20000)

            # 抓取续期后的新到期时间
            try:
                td = await self.page.query_selector("tr:has(th:has-text('延長後の期限')) td")
                if td:
                    self.new_expiry_time = (await td.text_content()).strip()
            except Exception:
                pass

            btn3 = "button[type='submit']:has-text('期限を延長する')"
            await self.page.wait_for_selector(btn3, timeout=self.wait_timeout)
            await self.page.click(btn3)
            await self.safe_wait_load(timeout=20000)

            if "freeplan/extend/do" in self.page.url:
                print("🎉 续期请求成功完成！")
                self.renewal_status = "Success"
                await self.take_screenshot("extension_success")
            else:
                self.renewal_status = "Failed"
        except Exception as e:
            print(f"❌ 续期提交失败: {e}")
            self.renewal_status = "Failed"

    def generate_report_notify(self):
        try:
            beijing_time = datetime.datetime.now(timezone(timedelta(hours=8)))
            current_time = beijing_time.strftime("%Y-%m-%d %H:%M:%S")

            content = f"**最后运行时间**: `{current_time}`\n\n"
            content += f"**运行结果**: {self.renewal_status}\n"
            content += f"**旧到期时间**: `{self.old_expiry_time or 'Unknown'}`\n"
            if self.new_expiry_time:
                content += f"**新到期时间**: `{self.new_expiry_time}`\n"

            with open("report-notify.md", "w", encoding="utf-8") as f:
                f.write(content)

            self.telegram.send_renewal_result(
                status=self.renewal_status,
                old_time=self.old_expiry_time or "Unknown",
                new_time=self.new_expiry_time,
                run_time=current_time,
            )
        except Exception as e:
            print(f"❌ 报表/推送生成失败: {e}")

    async def run(self):
        try:
            if not self.validate_config() or not await self.setup_browser():
                return False

            if not await self.navigate_to_login():
                return False

            if not await self.perform_login():
                return False

            if not await self.handle_login_result():
                self.generate_report_notify()
                return False

            if self.remaining_seconds > 0:
                self.report_status(self.remaining_seconds)

            self.generate_report_notify()
            return True

        except Exception as e:
            print(f"❌ 运行发生未捕获异常: {e}")
            self.generate_report_notify()
            return False

        finally:
            await self.cleanup()


# =====================================================================
#                          入口函数
# =====================================================================

async def main():
    print("=" * 60)
    print("XServer GAME 自动续期脚本 (Patchright 抗检测加固版)")
    print("=" * 60)

    if not LOGIN_EMAIL or not LOGIN_PASSWORD:
        print("❌ 环境变量 XSERVER_EMAIL / XSERVER_PASSWORD 未配置！")
        return

    auto_login = XServerAutoLogin()
    success = await auto_login.run()

    if success:
        print("✅ 脚本顺利完成！")
        raise SystemExit(0)
    else:
        print("❌ 脚本执行遇到问题！")
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
