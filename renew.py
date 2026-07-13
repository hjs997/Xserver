#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XServer GAME 自动登录和续期脚本

修复要点:
1. jumpvps 中间页：主动提交跳转表单 / 处理新标签 / 更长等待
2. 页面导航中截图、读时间导致 context destroyed
3. 未到达游戏管理页时不再盲目点升级按钮
4. 登录成功 URL 放宽匹配（允许 query/trailing）
"""

# =====================================================================
#                          导入依赖
# =====================================================================

import asyncio
import time
import re
import datetime
from datetime import timezone, timedelta
import os
import json
import requests
from playwright.async_api import async_playwright, Playwright, Browser, BrowserContext, Page
from playwright_stealth import stealth_async

# =====================================================================
#                          配置区域
# =====================================================================

# 浏览器配置
IS_GITHUB_ACTIONS = os.getenv("GITHUB_ACTIONS") == "true"
USE_HEADLESS = IS_GITHUB_ACTIONS or os.getenv("USE_HEADLESS", "false").lower() == "true"
WAIT_TIMEOUT = int(os.getenv("WAIT_TIMEOUT", "15000"))  # 页面元素等待超时时间(毫秒)
PAGE_LOAD_DELAY = int(os.getenv("PAGE_LOAD_DELAY", "3"))  # 页面加载延迟时间(秒)
JUMPVPS_TIMEOUT = int(os.getenv("JUMPVPS_TIMEOUT", "60"))  # jumpvps 等待秒数

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

# =====================================================================
#                        Telegram 推送模块
# =====================================================================

class TelegramNotifier:
    """Telegram 通知推送类"""

    def __init__(self, bot_token=None, chat_id=None):
        self.bot_token = bot_token or TELEGRAM_BOT_TOKEN
        self.chat_id = chat_id or TELEGRAM_CHAT_ID
        self.enabled = bool(self.bot_token and self.chat_id)

        if not self.enabled:
            print("ℹ️ Telegram 推送未启用(缺少 BOT_TOKEN 或 CHAT_ID)")

    def send_photo(self, photo_path, caption=None):
        """发送 Telegram 图片"""
        if not self.enabled:
            return False

        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendPhoto"
            with open(photo_path, "rb") as f:
                files = {"photo": f}
                payload = {"chat_id": self.chat_id}
                if caption:
                    payload["caption"] = caption

                response = requests.post(url, data=payload, files=files, timeout=20)
                result = response.json()

                if result.get("ok"):
                    print(f"✅ Telegram 图片发送成功: {photo_path}")
                    return True
                else:
                    print(f"❌ Telegram 图片发送失败: {result.get('description')}")
                    return False
        except Exception as e:
            print(f"❌ Telegram 推送图片异常: {e}")
            return False

    def send_message(self, message, parse_mode="HTML"):
        """发送 Telegram 消息"""
        if not self.enabled:
            print("⚠️ Telegram 推送未启用,跳过发送")
            return False

        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": parse_mode,
            }

            response = requests.post(url, json=payload, timeout=10)
            result = response.json()

            if result.get("ok"):
                print("✅ Telegram 消息发送成功")
                return True
            else:
                print(f"❌ Telegram 消息发送失败: {result.get('description')}")
                return False

        except Exception as e:
            print(f"❌ Telegram 推送异常: {e}")
            return False

    def send_renewal_result(self, status, old_time, new_time=None, run_time=None):
        """发送续期结果通知"""
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
    """XServer GAME 自动登录主类 - Playwright版本"""

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

        # 续期状态跟踪
        self.old_expiry_time = None
        self.new_expiry_time = None
        self.renewal_status = "Unknown"
        self.remaining_seconds = 0

        # Telegram 推送器
        self.telegram = TelegramNotifier()

    def report_status(self, remaining_seconds):
        """上报状态到面板"""
        if not PANEL_URL:
            print("ℹ️ 未配置 PANEL_URL，跳过上报")
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
        """解析剩余时间字符串为秒数，例如: '30時間57分' -> 111420"""
        try:
            hours = 0
            minutes = 0

            h_match = re.search(r"(\d+)時間", time_str)
            if h_match:
                hours = int(h_match.group(1))

            m_match = re.search(r"(\d+)分", time_str)
            if m_match:
                minutes = int(m_match.group(1))

            total_seconds = (hours * 3600) + (minutes * 60)
            return total_seconds
        except Exception as e:
            print(f"⚠️ 解析剩余秒数失败: {e}")
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

    # =================================================================
    #                       1. 浏览器管理模块
    # =================================================================

    async def setup_browser(self):
        """设置并启动 Playwright 浏览器"""
        try:
            self.playwright = await async_playwright().start()

            browser_args = [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-notifications",
                "--window-size=1920,1080",
                "--lang=ja-JP",
                "--accept-lang=ja-JP,ja,en-US,en",
            ]

            if USE_PROXY and PROXY_SERVER:
                print(f"🌐 使用代理: {PROXY_SERVER}")
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
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            }

            if USE_PROXY and PROXY_SERVER:
                context_options["proxy"] = {"server": PROXY_SERVER}

            self.context = await self.browser.new_context(**context_options)
            self.page = await self.context.new_page()
            self.page.set_default_timeout(self.wait_timeout)

            await stealth_async(self.page)
            print("✅ Stealth 插件已应用")

            if USE_PROXY:
                print(f"✅ Playwright 浏览器初始化成功 (使用代理: {PROXY_SERVER})")
            else:
                print("✅ Playwright 浏览器初始化成功")
            return True

        except Exception as e:
            print(f"❌ Playwright 浏览器初始化失败: {e}")
            return False

    async def safe_wait_load(self, timeout=15000):
        """等待页面加载稳定，忽略导航中的异常"""
        if not self.page:
            return
        for state in ("domcontentloaded", "load"):
            try:
                await self.page.wait_for_load_state(state, timeout=timeout)
            except Exception:
                pass

    async def take_screenshot(self, step_name=""):
        """截图功能 - 用于可视化调试"""
        try:
            if not self.page:
                return
            await self.safe_wait_load(timeout=10000)
            self.screenshot_count += 1
            beijing_time = datetime.datetime.now(timezone(timedelta(hours=8)))
            timestamp = beijing_time.strftime("%H%M%S")
            filename = f"step_{self.screenshot_count:02d}_{timestamp}_{step_name}.png"
            filename = re.sub(r'[<>:"/\\|?*]', "_", filename)
            # full_page 在跳转中容易 30s 超时，先试 full_page，失败再视口截图
            try:
                await self.page.screenshot(path=filename, full_page=True, timeout=15000)
            except Exception:
                await self.page.screenshot(path=filename, full_page=False, timeout=10000)
            print(f"📸 截图已保存: {filename}")
        except Exception as e:
            print(f"⚠️ 截图失败: {e}")

    def validate_config(self):
        """验证配置信息"""
        if not self.email or not self.password:
            print("❌ 邮箱或密码未设置!")
            return False
        print("✅ 配置信息验证通过")
        return True

    async def cleanup(self):
        """清理资源"""
        try:
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
            print("🧹 浏览器已关闭")
        except Exception as e:
            print(f"⚠️ 清理资源时出错: {e}")

    # =================================================================
    #                       2. 页面导航模块
    # =================================================================

    async def navigate_to_login(self):
        """导航到登录页面"""
        try:
            print(f"🌐 正在访问: {self.target_url}")
            await self.page.goto(self.target_url, wait_until="load", timeout=60000)
            await self.page.wait_for_selector("body", timeout=self.wait_timeout)
            print("✅ 页面加载成功")
            await self.take_screenshot("login_page_loaded")
            return True
        except Exception as e:
            print(f"❌ 导航失败: {e}")
            return False

    # =================================================================
    #                       3. 登录表单处理模块
    # =================================================================

    async def find_login_form(self):
        """查找登录表单元素"""
        try:
            print("🔍 正在查找登录表单...")
            await asyncio.sleep(self.page_load_delay)

            email_selector = "input[name='memberid']"
            await self.page.wait_for_selector(email_selector, timeout=self.wait_timeout)
            print("✅ 找到邮箱输入框")

            password_selector = "input[name='user_password']"
            await self.page.wait_for_selector(password_selector, timeout=self.wait_timeout)
            print("✅ 找到密码输入框")

            login_button_selector = "input[value='ログインする']"
            await self.page.wait_for_selector(login_button_selector, timeout=self.wait_timeout)
            print("✅ 找到登录按钮")

            return email_selector, password_selector, login_button_selector

        except Exception as e:
            print(f"❌ 查找登录表单时出错: {e}")
            return None, None, None

    async def human_type(self, selector, text):
        """模拟人类输入行为"""
        for char in text:
            await self.page.type(selector, char, delay=100)
            await asyncio.sleep(0.05)

    async def perform_login(self):
        """执行登录操作"""
        try:
            print("🎯 开始执行登录操作...")

            email_selector, password_selector, login_button_selector = await self.find_login_form()
            if not email_selector or not password_selector:
                return False

            print("📝 正在填写登录信息...")

            await self.page.fill(email_selector, "")
            await self.human_type(email_selector, self.email)
            print("✅ 邮箱已填写")

            await asyncio.sleep(2)

            await self.page.fill(password_selector, "")
            await self.human_type(password_selector, self.password)
            print("✅ 密码已填写")

            await asyncio.sleep(2)

            if login_button_selector:
                print("🖱️ 点击登录按钮...")
                await self.page.click(login_button_selector)
            else:
                print("⌨️ 使用回车键提交...")
                await self.page.press(password_selector, "Enter")

            print("✅ 登录表单已提交")
            await self.safe_wait_load(timeout=30000)
            await asyncio.sleep(3)
            return True

        except Exception as e:
            print(f"❌ 登录操作失败: {e}")
            return False

    # =================================================================
    #                   jumpvps / 游戏管理页跳转
    # =================================================================

    async def kick_jumpvps_redirect(self):
        """
        jumpvps 中间页通常靠 JS 自动 form.submit() 跳转。
        Headless / 慢网环境下自动提交可能不触发，这里主动推动跳转。
        """
        try:
            result = await self.page.evaluate(
                """() => {
                    // 1) 优先提交表单（XServer 常见 SSO 跳转方式）
                    const forms = Array.from(document.querySelectorAll('form'));
                    for (const form of forms) {
                        try {
                            form.submit();
                            return { ok: true, method: 'form.submit', action: form.action || '' };
                        } catch (e) {}
                    }

                    // 2) 点击看起来像继续的链接/按钮
                    const texts = ['進む', '続行', 'こちら', 'click', 'Click', 'ゲーム管理', '管理画面'];
                    const clickables = Array.from(document.querySelectorAll('a, button, input[type=submit]'));
                    for (const el of clickables) {
                        const t = (el.innerText || el.value || el.textContent || '').trim();
                        if (texts.some(x => t.includes(x))) {
                            el.click();
                            return { ok: true, method: 'click', text: t };
                        }
                    }

                    // 3) 带 game / jump 的链接
                    const link = document.querySelector('a[href*="game"], a[href*="jump"], a[href*="xmgame"]');
                    if (link && link.href) {
                        window.location.href = link.href;
                        return { ok: true, method: 'location.href', href: link.href };
                    }

                    // 4) meta refresh
                    const meta = document.querySelector('meta[http-equiv="refresh" i]');
                    if (meta) {
                        const content = meta.getAttribute('content') || '';
                        const m = content.match(/url=(.+)/i);
                        if (m && m[1]) {
                            window.location.href = m[1].trim().replace(/^['"]|['"]$/g, '');
                            return { ok: true, method: 'meta.refresh', href: m[1] };
                        }
                    }

                    return { ok: false, method: 'none' };
                }"""
            )
            if result and result.get("ok"):
                print(f"🔄 已推动 jumpvps 跳转: {result}")
                return True
            print(f"⚠️ jumpvps 页未找到可提交的跳转元素: {result}")
            return False
        except Exception as e:
            print(f"⚠️ jumpvps 推动跳转失败(可能正在导航): {e}")
            return False

    async def wait_for_game_panel(self, timeout_sec=None):
        """等待离开 jumpvps 并进入游戏管理相关页面"""
        timeout_sec = timeout_sec or JUMPVPS_TIMEOUT
        deadline = time.time() + timeout_sec
        kicked_at = 0
        kick_count = 0

        print(f"🔄 等待进入游戏管理页面 (最多 {timeout_sec}s)...")

        while time.time() < deadline:
            try:
                url = self.page.url
            except Exception:
                await asyncio.sleep(0.5)
                continue

            if self.is_game_panel_url(url):
                await self.safe_wait_load(timeout=20000)
                # 再确认一次 URL（防止中间页误匹配）
                try:
                    url = self.page.url
                except Exception:
                    pass
                if self.is_game_panel_url(url):
                    print(f"✅ 已到达游戏管理相关页面: {url}")
                    return True

            if "jumpvps" in (url or ""):
                # 每 8 秒尝试一次推动跳转，最多 3 次
                now = time.time()
                if kick_count < 3 and (now - kicked_at) >= 8:
                    print(f"🔄 仍在 jumpvps，尝试主动跳转 ({kick_count + 1}/3)...")
                    await self.take_screenshot(f"jumpvps_retry_{kick_count + 1}")
                    await self.kick_jumpvps_redirect()
                    kick_count += 1
                    kicked_at = now
            else:
                # 不在 jumpvps 也不在 game panel：可能还在 xapanel 其它页
                print(f"ℹ️ 当前中间 URL: {url}")

            await asyncio.sleep(1)

        try:
            print(f"⚠️ 等待游戏管理页超时，最终 URL: {self.page.url}")
        except Exception:
            print("⚠️ 等待游戏管理页超时，且无法读取 URL")
        return False

    async def open_game_management(self):
        """点击ゲーム管理，处理同页跳转 / 新标签 / jumpvps"""
        print("🔍 正在查找ゲーム管理按钮...")
        game_button_selector = "a:has-text('ゲーム管理')"
        await self.page.wait_for_selector(game_button_selector, timeout=self.wait_timeout)
        print("✅ 找到ゲーム管理按钮")

        # 同时监听：同页导航 + 新标签页
        popup_task = asyncio.create_task(
            self.context.wait_for_event("page", timeout=12000)
        )

        nav_error = None
        try:
            async with self.page.expect_navigation(wait_until="domcontentloaded", timeout=30000):
                await self.page.click(game_button_selector)
            print("✅ 已点击ゲーム管理按钮 (当前页导航)")
        except Exception as e:
            nav_error = e
            # 可能是新标签打开，没有当前页 navigation
            try:
                await self.page.click(game_button_selector)
            except Exception:
                pass
            print(f"ℹ️ 当前页 navigation 未完成(可能新标签打开): {nav_error}")

        # 检查是否开了新标签
        try:
            new_page = await asyncio.wait_for(asyncio.shield(popup_task), timeout=0.5)
        except Exception:
            # 再等一会儿拿 popup
            try:
                new_page = await popup_task
            except Exception:
                new_page = None
                if not popup_task.done():
                    popup_task.cancel()

        if new_page is not None:
            print(f"✅ 检测到新标签页: {new_page.url}")
            try:
                await stealth_async(new_page)
            except Exception:
                pass
            self.page = new_page
            self.page.set_default_timeout(self.wait_timeout)
            await self.safe_wait_load(timeout=30000)

        await asyncio.sleep(2)
        try:
            current_url = self.page.url
        except Exception:
            current_url = ""
        print(f"🔍 点击后 URL: {current_url}")

        if "jumpvps" in current_url:
            print("🔄 检测到中间跳转页面 (jumpvps)")
            # 先等 3 秒看是否自己跳
            for _ in range(3):
                await asyncio.sleep(1)
                try:
                    if self.is_game_panel_url(self.page.url):
                        break
                except Exception:
                    pass
            else:
                # 仍在 jumpvps，主动推一次
                await self.kick_jumpvps_redirect()

        ok = await self.wait_for_game_panel(timeout_sec=JUMPVPS_TIMEOUT)
        final_url = ""
        try:
            final_url = self.page.url
        except Exception:
            pass
        print(f"🔍 最终页面URL: {final_url}")

        if not ok:
            print("❌ 未能进入游戏管理页面，中止后续续期步骤")
            self.renewal_status = "Failed"
            await self.take_screenshot("jumpvps_or_panel_failed")
            return False

        print("✅ 成功到达游戏管理页面")
        await self.take_screenshot("game_page_loaded")
        await self.get_server_time_info()
        await self.click_upgrade_button()
        return True

    # =================================================================
    #                       4. 登录结果处理模块
    # =================================================================

    async def handle_login_result(self):
        """处理登录结果"""
        try:
            print("🔍 正在检查登录结果...")
            await self.safe_wait_load(timeout=30000)
            await asyncio.sleep(2)

            current_url = self.page.url
            print(f"🔍 当前URL: {current_url}")

            # 登录后有时会直接到 jumpvps 或 game panel
            if self.is_game_panel_url(current_url):
                print("✅ 已直接进入游戏管理页面")
                await self.take_screenshot("game_page_loaded")
                await self.get_server_time_info()
                await self.click_upgrade_button()
                return True

            if "jumpvps" in current_url:
                print("🔄 登录后直接进入 jumpvps，继续处理跳转...")
                await self.kick_jumpvps_redirect()
                if await self.wait_for_game_panel():
                    await self.take_screenshot("game_page_loaded")
                    await self.get_server_time_info()
                    await self.click_upgrade_button()
                    return True
                self.renewal_status = "Failed"
                await self.take_screenshot("jumpvps_after_login_failed")
                return False

            if self.is_login_success_url(current_url):
                print("✅ 登录成功!已跳转到XServer GAME管理页面")
                await asyncio.sleep(2)
                try:
                    return await self.open_game_management()
                except Exception as e:
                    print(f"❌ 查找或点击ゲーム管理按钮时出错: {e}")
                    self.renewal_status = "Failed"
                    await self.take_screenshot("game_button_error")
                    return False

            print("❌ 登录失败!当前URL不是预期的成功页面")
            print("   预期包含: xapanel/xmgame/index 或游戏管理页")
            print(f"   实际URL: {current_url}")
            await self.take_screenshot("login_failed")
            return False

        except Exception as e:
            print(f"❌ 检查登录结果时出错: {e}")
            return False

    # =================================================================
    #                    5A. 服务器信息获取模块
    # =================================================================

    async def get_server_time_info(self):
        """获取服务器时间信息（带导航重试）"""
        print("🕒 正在获取服务器时间信息...")

        for attempt in range(1, 4):
            try:
                await self.safe_wait_load(timeout=15000)
                await asyncio.sleep(1)

                elements = await self.page.locator(r"text=/残り\d+時間\d+分/").all()

                for element in elements:
                    element_text = await element.text_content()
                    element_text = element_text.strip() if element_text else ""

                    if (
                        element_text
                        and len(element_text) < 200
                        and "残り" in element_text
                        and "時間" in element_text
                    ):
                        print(f"✅ 找到时间元素: {element_text}")

                        remaining_match = re.search(r"残り(\d+時間\d+分)", element_text)
                        if remaining_match:
                            remaining_raw = remaining_match.group(1)
                            remaining_formatted = self.format_remaining_time(remaining_raw)
                            print(f"⏰ 剩余时间: {remaining_formatted}")
                            self.remaining_seconds = self.parse_remaining_seconds(
                                remaining_formatted
                            )

                        expiry_match = re.search(
                            r"\((\d{4}-\d{2}-\d{2}[^)]*)まで\)", element_text
                        )
                        if expiry_match:
                            expiry_raw = expiry_match.group(1).strip()
                            expiry_formatted = self.format_expiry_date(expiry_raw)
                            print(f"📅 查找到的到期时间: {expiry_formatted}")
                            if self.old_expiry_time is None:
                                self.old_expiry_time = expiry_formatted
                                print("✅ 已记录原到期时间")

                        return

                # 宽松再扫一遍 body
                body_text = await self.page.locator("body").inner_text()
                remaining_match = re.search(r"残り(\d+時間\d+分)", body_text)
                if remaining_match:
                    remaining_formatted = self.format_remaining_time(remaining_match.group(1))
                    print(f"⏰ 剩余时间(body): {remaining_formatted}")
                    self.remaining_seconds = self.parse_remaining_seconds(remaining_formatted)
                    expiry_match = re.search(
                        r"\((\d{4}-\d{2}-\d{2}[^)]*)まで\)", body_text
                    )
                    if expiry_match and self.old_expiry_time is None:
                        self.old_expiry_time = self.format_expiry_date(
                            expiry_match.group(1).strip()
                        )
                        print(f"📅 到期时间(body): {self.old_expiry_time}")
                    return

                print(f"ℹ️ 第 {attempt} 次未找到时间信息，稍后重试...")
                await asyncio.sleep(2)

            except Exception as e:
                msg = str(e)
                print(f"⚠️ 获取时间信息失败(第{attempt}次): {e}")
                if "Execution context was destroyed" in msg or "navigation" in msg.lower():
                    await asyncio.sleep(2)
                    continue
                break

    def format_remaining_time(self, time_str):
        return time_str

    def format_expiry_date(self, date_str):
        return date_str

    # =================================================================
    #                    5B. 续期页面导航模块
    # =================================================================

    async def click_upgrade_button(self):
        """点击升级延长按钮"""
        try:
            print("📄 正在查找アップグレード・期限延長按钮...")
            await self.safe_wait_load(timeout=15000)

            # 多种选择器兼容
            candidates = [
                "a:has-text('アップグレード・期限延長')",
                "text=アップグレード・期限延長",
                "a:has-text('期限延長')",
                "a[href*='freeplan/extend']",
            ]

            clicked = False
            last_error = None
            for upgrade_selector in candidates:
                try:
                    await self.page.wait_for_selector(upgrade_selector, timeout=self.wait_timeout)
                    print(f"✅ 找到升级按钮: {upgrade_selector}")
                    await self.page.click(upgrade_selector)
                    clicked = True
                    break
                except Exception as e:
                    last_error = e
                    continue

            if not clicked:
                raise last_error or RuntimeError("未找到升级按钮")

            print("✅ 已点击アップグレード・期限延長按钮")
            await self.safe_wait_load(timeout=30000)
            await asyncio.sleep(2)
            await self.verify_upgrade_page()

        except Exception as e:
            print(f"❌ 点击升级按钮失败: {e}")
            self.renewal_status = "Failed"
            await self.take_screenshot("upgrade_button_failed")

    async def verify_upgrade_page(self):
        """验证升级页面"""
        try:
            current_url = self.page.url
            expected_url = "https://secure.xserver.ne.jp/xmgame/game/freeplan/extend/index"

            print(f"🔍 升级页面URL: {current_url}")

            if expected_url in current_url or "freeplan/extend" in current_url:
                print("✅ 成功跳转到升级页面")
                await self.check_extension_restriction()
            else:
                print("❌ 升级页面跳转失败")
                print(f"   预期URL: {expected_url}")
                print(f"   实际URL: {current_url}")
                self.renewal_status = "Failed"

        except Exception as e:
            print(f"❌ 验证升级页面失败: {e}")

    async def check_extension_restriction(self):
        """检查期限延长限制信息 (智能识别新旧版提示)"""
        try:
            print("🔍 正在检测期限延长限制提示...")
            await asyncio.sleep(2)

            body_text = await self.page.locator("body").inner_text()

            match = re.search(
                r"更新をご希望の場合は、(.+?)以降にお試しください。", body_text
            )

            if match and match.group(1):
                next_time = match.group(1).strip()
                print("✅ 成功匹配到新版期限延长限制信息！")
                print(f"📋 下次可续期时间为: {next_time}")
                self.old_expiry_time = f"未到期 (下次续期开放时间: {next_time})"
                self.renewal_status = "Unexpired"
                return True

            elif "残り契約時間が24時間を切るまで" in body_text:
                print("✅ 匹配到旧版期限延长限制信息（未满24小时）")
                self.renewal_status = "Unexpired"
                return True

            else:
                print("ℹ️ 未找到任何期限延长限制信息，说明已经开放续期，准备进行延长操作...")
                await self.perform_extension_operation()
                return False

        except Exception as e:
            print(f"❌ 检测期限延长限制失败: {e}")
            return True

    # =================================================================
    #                    5C. 续期操作执行模块
    # =================================================================

    async def perform_extension_operation(self):
        """执行期限延长操作"""
        try:
            print("📄 开始执行期限延长操作...")
            await self.click_extension_button()
        except Exception as e:
            print(f"❌ 执行期限延长操作失败: {e}")

    async def click_extension_button(self):
        """点击期限延长按钮 (优化选择器版)"""
        try:
            print("🔍 正在查找'期限を延長する'按钮...")

            extension_selector = "text='期限を延長する'"
            await self.page.wait_for_selector(extension_selector, timeout=self.wait_timeout)
            print("✅ 找到'期限を延長する'按钮")

            await self.page.click(extension_selector)
            print("✅ 已点击'期限を延長する'按钮")

            print("⏰ 等待页面跳转...")
            await self.safe_wait_load(timeout=30000)
            await asyncio.sleep(2)

            await self.verify_extension_input_page()
            return True

        except Exception as e:
            print(f"❌ 点击期限延长按钮失败: {e}")
            return False

    async def verify_extension_input_page(self):
        """验证是否成功跳转到期限延长输入页面"""
        try:
            current_url = self.page.url
            expected_url = "https://secure.xserver.ne.jp/xmgame/game/freeplan/extend/input"

            print(f"🔍 当前页面URL: {current_url}")

            if expected_url in current_url:
                print("🎉 成功跳转到期限延长输入页面!")
                await self.take_screenshot("extension_input_page")
                await self.click_confirmation_button()
                return True
            else:
                print("❌ 页面跳转失败")
                print(f"   预期URL: {expected_url}")
                print(f"   实际URL: {current_url}")
                return False

        except Exception as e:
            print(f"❌ 验证期限延长输入页面失败: {e}")
            return False

    async def click_confirmation_button(self):
        """点击確認画面に進む按钮"""
        try:
            print("🔍 正在查找'確認画面に進む'按钮...")

            confirmation_selector = "button[type='submit']:has-text('確認画面に進む')"
            await self.page.wait_for_selector(confirmation_selector, timeout=self.wait_timeout)
            print("✅ 找到'確認画面に進む'按钮")

            await self.page.click(confirmation_selector)
            print("✅ 已点击'確認画面に進む'按钮")

            print("⏰ 等待页面跳转...")
            await self.safe_wait_load(timeout=30000)
            await asyncio.sleep(2)

            await self.verify_extension_conf_page()
            return True

        except Exception as e:
            print(f"❌ 点击確認画面に進む按钮失败: {e}")
            return False

    async def verify_extension_conf_page(self):
        """验证是否成功跳转到期限延长确认页面"""
        try:
            current_url = self.page.url
            expected_url = "https://secure.xserver.ne.jp/xmgame/game/freeplan/extend/conf"

            print(f"🔍 当前页面URL: {current_url}")

            if expected_url in current_url:
                print("🎉 成功跳转到期限延长确认页面!")
                await self.take_screenshot("extension_conf_page")
                await self.record_extension_time()
                await self.find_final_extension_button()
                return True
            else:
                print("❌ 页面跳转失败")
                print(f"   预期URL: {expected_url}")
                print(f"   实际URL: {current_url}")
                return False

        except Exception as e:
            print(f"❌ 验证期限延长确认页面失败: {e}")
            return False

    async def record_extension_time(self):
        """记录续期后的时间信息"""
        try:
            print("📅 正在获取续期后的时间信息...")

            time_selector = "tr:has(th:has-text('延長後の期限'))"
            time_element = await self.page.wait_for_selector(
                time_selector, timeout=self.wait_timeout
            )
            print("✅ 找到续期后时间信息")

            td_element = await time_element.query_selector("td")
            if td_element:
                extension_time = await td_element.text_content()
                extension_time = extension_time.strip()
                print(f"📅 续期后的期限: {extension_time}")
                self.new_expiry_time = extension_time
            else:
                print("❌ 未找到时间内容")

        except Exception as e:
            print(f"❌ 记录续期后时间失败: {e}")

    async def find_final_extension_button(self):
        """查找并点击最终的期限延长按钮"""
        try:
            print("🔍 正在查找最终的'期限を延長する'按钮...")

            final_button_selector = "button[type='submit']:has-text('期限を延長する')"
            await self.page.wait_for_selector(
                final_button_selector, timeout=self.wait_timeout
            )
            print("✅ 找到最终的'期限を延長する'按钮")

            await self.page.click(final_button_selector)
            print("✅ 已点击最终续期按钮")

            print("⏰ 等待续期操作完成...")
            await self.safe_wait_load(timeout=30000)
            await asyncio.sleep(2)

            await self.verify_extension_success()
            return True

        except Exception as e:
            print(f"❌ 执行最终期限延长操作失败: {e}")
            return False

    async def verify_extension_success(self):
        """验证续期操作是否成功"""
        try:
            print("🔍 正在验证续期操作结果...")

            current_url = self.page.url
            expected_url = "https://secure.xserver.ne.jp/xmgame/game/freeplan/extend/do"

            print(f"🔍 当前页面URL: {current_url}")

            url_success = expected_url in current_url

            text_success = False
            try:
                success_text_selector = "p:has-text('期限を延長しました。')"
                await self.page.wait_for_selector(success_text_selector, timeout=5000)
                success_text = await self.page.query_selector(success_text_selector)
                if success_text:
                    text_content = await success_text.text_content()
                    print(f"✅ 找到成功提示文字: {text_content.strip()}")
                    text_success = True
            except Exception:
                print("ℹ️ 未找到成功提示文字")

            if url_success or text_success:
                print("🎉 续期操作成功!")
                self.renewal_status = "Success"
                await self.take_screenshot("extension_success")
                try:
                    await self.page.screenshot(
                        path="renewal_success_tg.png", full_page=True, timeout=15000
                    )
                except Exception:
                    await self.page.screenshot(
                        path="renewal_success_tg.png", full_page=False, timeout=10000
                    )
                return True
            else:
                print("❌ 续期操作可能失败")
                self.renewal_status = "Failed"
                await self.take_screenshot("extension_failed")
                return False

        except Exception as e:
            print(f"❌ 验证续期结果失败: {e}")
            self.renewal_status = "Failed"
            return False

    # =================================================================
    #                    5D. 结果记录与报告模块
    # =================================================================

    def generate_report_notify(self):
        """生成report-notify.md文件记录续期情况"""
        try:
            print("📝 正在生成report-notify.md文件...")

            beijing_time = datetime.datetime.now(timezone(timedelta(hours=8)))
            current_time = beijing_time.strftime("%Y-%m-%d %H:%M:%S")

            readme_content = f"**最后运行时间**: `{current_time}`\n\n"
            readme_content += "**运行结果**: <br>\n"
            readme_content += "🖥️服务器:`🇯🇵Xserver(MC)`<br>\n"

            if self.renewal_status == "Success":
                readme_content += "📊续期结果:✅Success<br>\n"
                readme_content += f"🕛️旧到期时间: `{self.old_expiry_time or 'Unknown'}`<br>\n"
                readme_content += f"🕡️新到期时间: `{self.new_expiry_time or 'Unknown'}`<br>\n"
            elif self.renewal_status == "Unexpired":
                readme_content += "📊续期结果:ℹ️Unexpired<br>\n"
                readme_content += f"🕛️旧到期时间: `{self.old_expiry_time or 'Unknown'}`<br>\n"
            elif self.renewal_status == "Failed":
                readme_content += "📊续期结果:❌Failed<br>\n"
                readme_content += f"🕛️旧到期时间: `{self.old_expiry_time or 'Unknown'}`<br>\n"
            else:
                readme_content += "📊续期结果:❓Unknown<br>\n"
                readme_content += f"🕛️旧到期时间: `{self.old_expiry_time or 'Unknown'}`<br>\n"

            with open("report-notify.md", "w", encoding="utf-8") as f:
                f.write(readme_content)

            print("✅ report-notify.md文件生成成功")
            print(f"📄 续期状态: {self.renewal_status}")
            print(f"📅 原到期时间: {self.old_expiry_time or 'Unknown'}")
            if self.new_expiry_time:
                print(f"📅 新到期时间: {self.new_expiry_time}")

            self.push_to_telegram(current_time)

        except Exception as e:
            print(f"❌ 生成report-notify.md文件失败: {e}")

    def push_to_telegram(self, run_time=None):
        """推送结果到 Telegram"""
        try:
            print("📱 正在推送结果到 Telegram...")

            result = self.telegram.send_renewal_result(
                status=self.renewal_status,
                old_time=self.old_expiry_time or "Unknown",
                new_time=self.new_expiry_time,
                run_time=run_time,
            )

            if self.renewal_status == "Success" and os.path.exists("renewal_success_tg.png"):
                print("📸 正在推送续期成功截图...")
                self.telegram.send_photo(
                    "renewal_success_tg.png",
                    caption=f"XServer 续期成功截图\n{run_time}",
                )

            if result:
                print("✅ Telegram 推送成功")
            else:
                print("⚠️ Telegram 推送失败或未启用")

        except Exception as e:
            print(f"❌ Telegram 推送异常: {e}")

    # =================================================================
    #                       6. 主流程控制模块
    # =================================================================

    async def run(self):
        """运行自动登录流程"""
        try:
            print("🚀 开始 XServer GAME 自动登录流程...")

            if not self.validate_config():
                return False

            if not await self.setup_browser():
                return False

            if not await self.navigate_to_login():
                return False

            if not await self.perform_login():
                return False

            if not await self.handle_login_result():
                print("⚠️ 登录或进入游戏管理页失败,请检查邮箱密码 / jumpvps 跳转")
                self.generate_report_notify()
                return False

            print("🎉 XServer GAME 自动登录流程完成!")
            await self.take_screenshot("login_completed")

            if self.renewal_status == "Success":
                print("🔄 续期成功，重新获取最新剩余时间...")
                try:
                    game_url = "https://secure.xserver.ne.jp/xmgame/game/index"
                    await self.page.goto(game_url, wait_until="load", timeout=60000)
                    await asyncio.sleep(3)
                    await self.get_server_time_info()
                    print(f"✅ 已刷新剩余时间: {self.remaining_seconds} 秒")
                except Exception as e:
                    print(f"⚠️ 刷新时间失败，将使用续期前的时间上报: {e}")

            if self.remaining_seconds > 0:
                print("📡 正在上报最终状态到面板...")
                self.report_status(self.remaining_seconds)

            self.generate_report_notify()

            print("⏰ 浏览器将在 10 秒后关闭...")
            await asyncio.sleep(10)

            return True

        except Exception as e:
            print(f"❌ 自动登录流程出错: {e}")
            self.generate_report_notify()
            return False

        finally:
            await self.cleanup()


# =====================================================================
#                          主程序入口
# =====================================================================

async def main():
    """主函数"""
    print("=" * 60)
    print("XServer GAME 自动登录脚本 - Playwright版本")
    print("基于 Playwright + stealth (jumpvps 修复版)")
    print("=" * 60)
    print()

    print("📋 当前配置:")
    print(f"   XServer邮箱: {LOGIN_EMAIL}")
    print(f"   XServer密码: {'*' * len(LOGIN_PASSWORD) if LOGIN_PASSWORD else 'None'}")
    print(f"   目标网站: {TARGET_URL}")
    print(f"   无头模式: {USE_HEADLESS}")
    print(f"   jumpvps超时: {JUMPVPS_TIMEOUT}s")
    if USE_PROXY and PROXY_SERVER:
        print(f"   代理服务器: {PROXY_SERVER}")
    else:
        print("   代理服务器: 未使用")
    if PANEL_URL:
        print(f"   面板上报: {PANEL_URL} (SERVER_NAME={SERVER_NAME})")
    else:
        print("   面板上报: 未配置，跳过上报")
    print()

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        print("📱 Telegram推送配置:")
        print(
            f"   Bot Token: {TELEGRAM_BOT_TOKEN[:10]}"
            f"{'*' * (len(TELEGRAM_BOT_TOKEN) - 10) if len(TELEGRAM_BOT_TOKEN) > 10 else ''}"
        )
        print(f"   Chat ID: {TELEGRAM_CHAT_ID}")
    else:
        print("ℹ️ Telegram推送未配置(可选功能)")
    print()

    if (
        not LOGIN_EMAIL
        or not LOGIN_PASSWORD
        or LOGIN_EMAIL == "your_email@example.com"
        or LOGIN_PASSWORD == "your_password"
    ):
        print("❌ 请先设置正确的邮箱和密码!")
        print("   可以通过环境变量 XSERVER_EMAIL 和 XSERVER_PASSWORD 设置")
        return

    print("🚀 配置验证通过,自动开始登录...")

    auto_login = XServerAutoLogin()
    success = await auto_login.run()

    if success:
        print("✅ 登录流程执行成功!")
        raise SystemExit(0)
    else:
        print("❌ 登录流程执行失败!")
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
