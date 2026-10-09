#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XServer GAME 自动登录和续期脚本 - DrissionPage 真实 Chrome 版

修复与优化要点:
1. 采用 DrissionPage + 真实 Chrome (Xvfb) 穿透 Cloudflare Turnstile
2. 通过 ChromiumOptions 命令行参数设置 SOCKS5 代理，消除 SOCKS 代理设置报错
3. 优化 Turnstile 验证后的缓冲等待（time.sleep(2)），确保前端 JavaScript 完成事件绑定
4. 修复登录按钮及各项页面元素的选择器定位
5. 完整保留原版所有业务逻辑：JumpVPS 跳转处理、多步续期导航、Telegram 推送、面板状态上报
"""

import asyncio
import time
import re
import datetime
from datetime import timezone, timedelta
import os
import json
import requests
from DrissionPage import ChromiumPage, ChromiumOptions

# =====================================================================
#                          配置区域
# =====================================================================

IS_GITHUB_ACTIONS = os.getenv("GITHUB_ACTIONS") == "true"
WAIT_TIMEOUT = int(os.getenv("WAIT_TIMEOUT", "15"))  # 页面元素等待超时时间(秒)
PAGE_LOAD_DELAY = int(os.getenv("PAGE_LOAD_DELAY", "3"))  # 页面加载延迟时间(秒)
JUMPVPS_TIMEOUT = int(os.getenv("JUMPVPS_TIMEOUT", "60"))  # jumpvps 等待秒数

# 代理配置 - 可选，不填则不使用代理
PROXY_SERVER = os.getenv("PROXY_SERVER") or ""
USE_PROXY = bool(PROXY_SERVER)

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
#                        XServer 自动登录主类
# =====================================================================

class XServerAutoLogin:
    """XServer GAME 自动登录主类 - DrissionPage 版本"""

    def __init__(self):
        self.page = None
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

    def setup_browser(self):
        """设置并启动 DrissionPage 真实 Chrome 浏览器"""
        try:
            co = ChromiumOptions()
            co.set_argument("--no-sandbox")
            co.set_argument("--disable-dev-shm-usage")
            co.set_argument("--disable-gpu")
            co.set_argument("--window-size=1920,1080")
            co.set_argument("--lang=ja-JP")

            # 使用 Chrome 进程命令行参数传入代理，完美兼容 SOCKS5 等协议
            if USE_PROXY and PROXY_SERVER:
                print(f"🌐 使用代理: {PROXY_SERVER}")
                co.set_argument(f"--proxy-server={PROXY_SERVER}")

            # 保持 headless=False，在 Xvfb 虚拟屏幕中显示运行以穿透 Cloudflare
            self.page = ChromiumPage(co)
            self.page.set.timeouts(base=self.wait_timeout)

            print("✅ DrissionPage 浏览器初始化成功 (真实 Chrome + Xvfb)")
            return True

        except Exception as e:
            print(f"❌ DrissionPage 浏览器初始化失败: {e}")
            return False

    def take_screenshot(self, step_name=""):
        """截图功能"""
        try:
            if not self.page:
                return
            self.screenshot_count += 1
            beijing_time = datetime.datetime.now(timezone(timedelta(hours=8)))
            timestamp = beijing_time.strftime("%H%M%S")
            filename = f"step_{self.screenshot_count:02d}_{timestamp}_{step_name}.png"
            filename = re.sub(r'[<>:"/\\|?*]', "_", filename)
            self.page.get_screenshot(path=filename)
            print(f"📸 截图已保存: {filename}")
        except Exception as e:
            print(f"⚠️ 截图失败: {e}")

    def validate_config(self):
        if not self.email or not self.password:
            print("❌ 邮箱或密码未设置!")
            return False
        print("✅ 配置信息验证通过")
        return True

    def cleanup(self):
        try:
            if self.page:
                self.page.quit()
            print("🧹 浏览器已关闭")
        except Exception as e:
            print(f"⚠️ 清理资源时出错: {e}")

    # =================================================================
    #                       2. 页面导航模块
    # =================================================================

    def navigate_to_login(self):
        try:
            print(f"🌐 正在访问: {self.target_url}")
            self.page.get(self.target_url)
            print("✅ 页面加载成功")
            self.take_screenshot("login_page_loaded")
            return True
        except Exception as e:
            print(f"❌ 导航失败: {e}")
            return False

    # =================================================================
    #                       3. 登录表单处理模块
    # =================================================================

    def find_login_form(self):
        try:
            print("🔍 正在查找登录表单...")
            time.sleep(self.page_load_delay)

            email_ele = self.page.ele('css:input[name="member_id"], input[name="memberid"], input[type="text"], input[type="email"]')
            if not email_ele:
                print("❌ 未找到账号输入框")
                return None, None, None
            print("✅ 找到账号/邮箱输入框")

            password_ele = self.page.ele('css:input[name="user_password"], input[type="password"]')
            if not password_ele:
                print("❌ 未找到密码输入框")
                return None, None, None
            print("✅ 找到密码输入框")

            # 使用正确的文本选择方式精确匹配“ログインする”按钮
            login_btn_ele = self.page.ele('text:ログインする') or self.page.ele('css:button[type="submit"], input[type="submit"]')
            if login_btn_ele:
                print("✅ 找到登录按钮")

            return email_ele, password_ele, login_btn_ele

        except Exception as e:
            print(f"❌ 查找登录表单时出错: {e}")
            return None, None, None

    def perform_login(self):
        try:
            print("🎯 开始执行登录操作...")

            email_ele, password_ele, login_btn_ele = self.find_login_form()
            if not email_ele or not password_ele:
                return False

            print("📝 正在填写登录信息...")
            email_ele.input(self.email, clear=True)
            print("✅ 邮箱已填写")

            time.sleep(1)

            password_ele.input(self.password, clear=True)
            print("✅ 密码已填写")

            time.sleep(1)

            print("⏳ 检查并等待 Cloudflare Turnstile 自动完成验证 (最多 30 秒)...")
            token_passed = False
            for i in range(30):
                res = self.page.run_js(
                    'const el = document.querySelector("input[name=\\"cf-turnstile-response\\"], input[name=\\"g-recaptcha-response\\"]"); return el ? el.value : "";'
                )
                if res and len(res) > 20:
                    token_passed = True
                    print(f"🎉 Cloudflare Turnstile 在第 {i+1} 秒自动验证通过！")
                    # 关键增加：验证成功后多等待 2 秒，确保前端 JavaScript 完成回调绑定
                    time.sleep(2)
                    break
                time.sleep(1)

            self.take_screenshot("form_filled")

            if login_btn_ele:
                print("🖱️ 点击登录按钮...")
                login_btn_ele.click()
            else:
                print("⌨️ 使用回车键提交...")
                password_ele.input("\n")

            print("✅ 登录表单已提交")
            time.sleep(3)
            return True

        except Exception as e:
            print(f"❌ 登录操作失败: {e}")
            return False

    # =================================================================
    #                   jumpvps / 游戏管理页跳转
    # =================================================================

    def kick_jumpvps_redirect(self):
        try:
            result = self.page.run_js(
                """() => {
                    const forms = Array.from(document.querySelectorAll('form'));
                    for (const form of forms) {
                        try {
                            form.submit();
                            return { ok: true, method: 'form.submit', action: form.action || '' };
                        } catch (e) {}
                    }

                    const texts = ['進む', '続行', 'こちら', 'click', 'Click', 'ゲーム管理', '管理画面'];
                    const clickables = Array.from(document.querySelectorAll('a, button, input[type=submit]'));
                    for (const el of clickables) {
                        const t = (el.innerText || el.value || el.textContent || '').trim();
                        if (texts.some(x => t.includes(x))) {
                            el.click();
                            return { ok: true, method: 'click', text: t };
                        }
                    }

                    const link = document.querySelector('a[href*="game"], a[href*="jump"], a[href*="xmgame"]');
                    if (link && link.href) {
                        window.location.href = link.href;
                        return { ok: true, method: 'location.href', href: link.href };
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
            print(f"⚠️ jumpvps 推动跳转失败: {e}")
            return False

    def wait_for_game_panel(self, timeout_sec=None):
        timeout_sec = timeout_sec or JUMPVPS_TIMEOUT
        deadline = time.time() + timeout_sec
        kicked_at = 0
        kick_count = 0

        print(f"🔄 等待进入游戏管理页面 (最多 {timeout_sec}s)...")

        while time.time() < deadline:
            url = self.page.url

            if self.is_game_panel_url(url):
                time.sleep(2)
                url = self.page.url
                if self.is_game_panel_url(url):
                    print(f"✅ 已到达游戏管理相关页面: {url}")
                    return True

            if "jumpvps" in (url or ""):
                now = time.time()
                if kick_count < 3 and (now - kicked_at) >= 8:
                    print(f"🔄 仍在 jumpvps，尝试主动跳转 ({kick_count + 1}/3)...")
                    self.take_screenshot(f"jumpvps_retry_{kick_count + 1}")
                    self.kick_jumpvps_redirect()
                    kick_count += 1
                    kicked_at = now
            else:
                print(f"ℹ️ 当前中间 URL: {url}")

            time.sleep(1)

        print(f"⚠️ 等待游戏管理页超时，最终 URL: {self.page.url}")
        return False

    def open_game_management(self):
        print("🔍 正在查找ゲーム管理按钮...")
        game_btn = self.page.ele("text:ゲーム管理")
        if game_btn:
            print("✅ 找到ゲーム管理按钮，执行点击...")
            game_btn.click()

        time.sleep(3)
        self.page = self.page.latest_tab

        current_url = self.page.url
        print(f"🔍 点击后 URL: {current_url}")

        if "jumpvps" in current_url:
            print("🔄 检测到中间跳转页面 (jumpvps)")
            for _ in range(3):
                time.sleep(1)
                if self.is_game_panel_url(self.page.url):
                    break
            else:
                self.kick_jumpvps_redirect()

        ok = self.wait_for_game_panel(timeout_sec=JUMPVPS_TIMEOUT)
        print(f"🔍 最终页面URL: {self.page.url}")

        if not ok:
            print("❌ 未能进入游戏管理页面，中止后续续期步骤")
            self.renewal_status = "Failed"
            self.take_screenshot("jumpvps_or_panel_failed")
            return False

        print("✅ 成功到达游戏管理页面")
        self.take_screenshot("game_page_loaded")
        self.get_server_time_info()
        self.click_upgrade_button()
        return True

    # =================================================================
    #                       4. 登录结果处理模块
    # =================================================================

    def handle_login_result(self):
        try:
            print("🔍 正在检查登录结果...")
            time.sleep(3)

            current_url = self.page.url
            print(f"🔍 当前URL: {current_url}")

            if self.is_game_panel_url(current_url):
                print("✅ 已直接进入游戏管理页面")
                self.take_screenshot("game_page_loaded")
                self.get_server_time_info()
                self.click_upgrade_button()
                return True

            if "jumpvps" in current_url:
                print("🔄 登录后直接进入 jumpvps，继续处理跳转...")
                self.kick_jumpvps_redirect()
                if self.wait_for_game_panel():
                    self.take_screenshot("game_page_loaded")
                    self.get_server_time_info()
                    self.click_upgrade_button()
                    return True
                self.renewal_status = "Failed"
                self.take_screenshot("jumpvps_after_login_failed")
                return False

            if self.is_login_success_url(current_url):
                print("✅ 登录成功!已跳转到XServer GAME管理页面")
                time.sleep(2)
                try:
                    return self.open_game_management()
                except Exception as e:
                    print(f"❌ 查找或点击ゲーム管理按钮时出错: {e}")
                    self.renewal_status = "Failed"
                    self.take_screenshot("game_button_error")
                    return False

            print("❌ 登录失败!当前URL不是预期的成功页面")
            print(f"   实际URL: {current_url}")
            self.take_screenshot("login_failed")
            return False

        except Exception as e:
            print(f"❌ 检查登录结果时出错: {e}")
            return False

    # =================================================================
    #                    5A. 服务器信息获取模块
    # =================================================================

    def get_server_time_info(self):
        print("🕒 正在获取服务器时间信息...")

        for attempt in range(1, 4):
            try:
                time.sleep(1)
                body_text = self.page.html

                remaining_match = re.search(r"残り(\d+時間\d+分)", body_text)
                if remaining_match:
                    remaining_formatted = remaining_match.group(1)
                    print(f"⏰ 剩余时间: {remaining_formatted}")
                    self.remaining_seconds = self.parse_remaining_seconds(remaining_formatted)

                    expiry_match = re.search(r"\((\d{4}-\d{2}-\d{2}[^)]*)まで\)", body_text)
                    if expiry_match and self.old_expiry_time is None:
                        self.old_expiry_time = expiry_match.group(1).strip()
                        print(f"📅 到期时间: {self.old_expiry_time}")
                    return

                print(f"ℹ️ 第 {attempt} 次未找到时间信息，稍后重试...")
                time.sleep(2)

            except Exception as e:
                print(f"⚠️ 获取时间信息失败(第{attempt}次): {e}")

    # =================================================================
    #                    5B. 续期页面导航模块
    # =================================================================

    def click_upgrade_button(self):
        try:
            print("📄 正在查找アップグレード・期限延長按钮...")
            time.sleep(2)

            upgrade_btn = (
                self.page.ele("text:アップグレード・期限延長")
                or self.page.ele("text:期限延長")
                or self.page.ele('css:a[href*="freeplan/extend"]')
            )

            if not upgrade_btn:
                raise RuntimeError("未找到升级按钮")

            print("✅ 找到升级按钮，执行点击...")
            upgrade_btn.click()
            time.sleep(3)
            self.verify_upgrade_page()

        except Exception as e:
            print(f"❌ 点击升级按钮失败: {e}")
            self.renewal_status = "Failed"
            self.take_screenshot("upgrade_button_failed")

    def verify_upgrade_page(self):
        try:
            current_url = self.page.url
            expected_url = "https://secure.xserver.ne.jp/xmgame/game/freeplan/extend/index"

            print(f"🔍 升级页面URL: {current_url}")

            if expected_url in current_url or "freeplan/extend" in current_url:
                print("✅ 成功跳转到升级页面")
                self.check_extension_restriction()
            else:
                print("❌ 升级页面跳转失败")
                self.renewal_status = "Failed"

        except Exception as e:
            print(f"❌ 验证升级页面失败: {e}")

    def check_extension_restriction(self):
        try:
            print("🔍 正在检测期限延长限制提示...")
            time.sleep(2)

            body_text = self.page.html

            match = re.search(r"更新をご希望の場合は、(.+?)以降にお試しください。", body_text)

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
                self.perform_extension_operation()
                return False

        except Exception as e:
            print(f"❌ 检测期限延长限制失败: {e}")
            return True

    # =================================================================
    #                    5C. 续期操作执行模块
    # =================================================================

    def perform_extension_operation(self):
        try:
            print("📄 开始执行期限延长操作...")
            self.click_extension_button()
        except Exception as e:
            print(f"❌ 执行期限延长操作失败: {e}")

    def click_extension_button(self):
        try:
            print("🔍 正在查找'期限を延長する'按钮...")
            ext_btn = self.page.ele("text:期限を延長する")
            if not ext_btn:
                return False

            print("✅ 找到'期限を延長する'按钮并点击")
            ext_btn.click()

            time.sleep(3)
            self.verify_extension_input_page()
            return True

        except Exception as e:
            print(f"❌ 点击期限延长按钮失败: {e}")
            return False

    def verify_extension_input_page(self):
        try:
            current_url = self.page.url
            expected_url = "https://secure.xserver.ne.jp/xmgame/game/freeplan/extend/input"

            if expected_url in current_url:
                print("🎉 成功跳转到期限延长输入页面!")
                self.take_screenshot("extension_input_page")
                self.click_confirmation_button()
                return True
            else:
                print(f"❌ 页面跳转失败, 实际URL: {current_url}")
                return False

        except Exception as e:
            print(f"❌ 验证期限延长输入页面失败: {e}")
            return False

    def click_confirmation_button(self):
        try:
            print("🔍 正在查找'確認画面に進む'按钮...")
            conf_btn = self.page.ele('css:button[type="submit"]:contains("確認画面に進む")') or self.page.ele("text:確認画面に進む")
            if conf_btn:
                conf_btn.click()
                print("✅ 已点击'確認画面に進む'按钮")

            time.sleep(3)
            self.verify_extension_conf_page()
            return True

        except Exception as e:
            print(f"❌ 点击確認画面に進む按钮失败: {e}")
            return False

    def verify_extension_conf_page(self):
        try:
            current_url = self.page.url
            expected_url = "https://secure.xserver.ne.jp/xmgame/game/freeplan/extend/conf"

            if expected_url in current_url:
                print("🎉 成功跳转到期限延长确认页面!")
                self.take_screenshot("extension_conf_page")
                self.record_extension_time()
                self.find_final_extension_button()
                return True
            else:
                print(f"❌ 页面跳转失败, 实际URL: {current_url}")
                return False

        except Exception as e:
            print(f"❌ 验证期限延长确认页面失败: {e}")
            return False

    def record_extension_time(self):
        try:
            time_ele = self.page.ele('css:tr:has(th:contains("延長後の期限")) td')
            if time_ele:
                extension_time = time_ele.text.strip()
                print(f"📅 续期后的期限: {extension_time}")
                self.new_expiry_time = extension_time
        except Exception as e:
            print(f"❌ 记录续期后时间失败: {e}")

    def find_final_extension_button(self):
        try:
            final_btn = self.page.ele('css:button[type="submit"]:contains("期限を延長する")') or self.page.ele("text:期限を延長する")
            if final_btn:
                final_btn.click()
                print("✅ 已点击最终续期按钮")

            time.sleep(3)
            self.verify_extension_success()
            return True

        except Exception as e:
            print(f"❌ 执行最终期限延长操作失败: {e}")
            return False

    def verify_extension_success(self):
        try:
            current_url = self.page.url
            expected_url = "https://secure.xserver.ne.jp/xmgame/game/freeplan/extend/do"

            url_success = expected_url in current_url
            text_success = "期限を延長しました。" in self.page.html

            if url_success or text_success:
                print("🎉 续期操作成功!")
                self.renewal_status = "Success"
                self.take_screenshot("extension_success")
                try:
                    self.page.get_screenshot(path="renewal_success_tg.png")
                except Exception:
                    pass
                return True
            else:
                print("❌ 续期操作可能失败")
                self.renewal_status = "Failed"
                self.take_screenshot("extension_failed")
                return False

        except Exception as e:
            print(f"❌ 验证续期结果失败: {e}")
            self.renewal_status = "Failed"
            return False

    # =================================================================
    #                    5D. 结果记录与报告模块
    # =================================================================

    def generate_report_notify(self):
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
            self.push_to_telegram(current_time)

        except Exception as e:
            print(f"❌ 生成report-notify.md文件失败: {e}")

    def push_to_telegram(self, run_time=None):
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

    def run(self):
        try:
            print("🚀 开始 XServer GAME 自动登录流程 (DrissionPage 版)...")

            if not self.validate_config():
                return False

            if not self.setup_browser():
                return False

            if not self.navigate_to_login():
                return False

            if not self.perform_login():
                return False

            if not self.handle_login_result():
                print("⚠️ 登录或进入游戏管理页失败")
                self.generate_report_notify()
                return False

            print("🎉 XServer GAME 自动登录流程完成!")
            self.take_screenshot("login_completed")

            if self.remaining_seconds > 0:
                print("📡 正在上报最终状态到面板...")
                self.report_status(self.remaining_seconds)

            self.generate_report_notify()
            return True

        except Exception as e:
            print(f"❌ 自动登录流程出错: {e}")
            self.generate_report_notify()
            return False

        finally:
            self.cleanup()


def main():
    print("=" * 60)
    print("XServer GAME 自动登录脚本 - DrissionPage 版")
    print("=" * 60)

    if not LOGIN_EMAIL or not LOGIN_PASSWORD:
        print("❌ 请先设置正确的邮箱和密码!")
        return

    auto_login = XServerAutoLogin()
    success = auto_login.run()

    if success:
        print("✅ 登录流程执行成功!")
        raise SystemExit(0)
    else:
        print("❌ 登录流程执行失败!")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
