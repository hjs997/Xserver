#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XServer GAME 自动登录和续期脚本 - Patchright 终极过盾版
修复了缩进、超时加载与 Turnstile 强力点击逻辑
"""

import asyncio
import os
import re
import datetime
from datetime import timezone, timedelta
import requests
from patchright.async_api import async_playwright

# =====================================================================
#                          配置区域
# =====================================================================

WAIT_TIMEOUT = int(os.getenv("WAIT_TIMEOUT", "15"))
JUMPVPS_TIMEOUT = int(os.getenv("JUMPVPS_TIMEOUT", "60"))

# 代理配置 - 可选，例如 socks5://127.0.0.1:1080
PROXY_SERVER = os.getenv("PROXY_SERVER", "")

# XServer 登录凭据
LOGIN_EMAIL = os.getenv("XSERVER_EMAIL", "")
LOGIN_PASSWORD = os.getenv("XSERVER_PASSWORD", "")
TARGET_URL = "https://secure.xserver.ne.jp/xapanel/login/xmgame"

# Telegram 配置
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# 面板上报配置
PANEL_URL = os.getenv("PANEL_URL", "")
SERVER_NAME = os.getenv("SERVER_NAME", "")

GAME_PANEL_URL_HINTS = (
    "xmgame/game/index",
    "xmgame/game/",
    "/game/freeplan/",
)

# =====================================================================
#                     Telegram 推送模块
# =====================================================================

class TelegramNotifier:
    def __init__(self, bot_token=None, chat_id=None):
        self.bot_token = bot_token or TELEGRAM_BOT_TOKEN
        self.chat_id = chat_id or TELEGRAM_CHAT_ID
        self.enabled = bool(self.bot_token and self.chat_id)
        if not self.enabled:
            print("ℹ️ Telegram 推送未启用(缺少 BOT_TOKEN 或 CHAT_ID)")

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

    def send_photo(self, photo_path, caption=None):
        if not self.enabled or not os.path.exists(photo_path):
            return False
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendPhoto"
            with open(photo_path, "rb") as f:
                files = {"photo": f}
                payload = {"chat_id": self.chat_id}
                if caption:
                    payload["caption"] = caption
                response = requests.post(url, data=payload, files=files, timeout=20)
                return response.json().get("ok", False)
        except Exception as e:
            print(f"❌ Telegram 图片发送异常: {e}")
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
            message += f"💡 提示: 剩余时间充足,无需续期\n"
        elif status == "Failed":
            message += f"📊 续期结果: <b>❌ 失败</b>\n"
            message += f"🕛 到期时间: <code>{old_time}</code>\n"
            message += f"⚠️ 请检查日志或手动处理\n"
        else:
            message += f"📊 续期结果: <b>❓ 未知</b>\n"
            message += f"🕛 到期时间: <code>{old_time}</code>\n"

        return self.send_message(message)

# =====================================================================
#                  XServer 自动化执行主逻辑 (Async)
# =====================================================================

class XServerAutomation:
    def __init__(self):
        self.email = LOGIN_EMAIL
        self.password = LOGIN_PASSWORD
        self.target_url = TARGET_URL
        self.telegram = TelegramNotifier()
        
        self.old_expiry_time = None
        self.new_expiry_time = None
        self.renewal_status = "Unknown"
        self.remaining_seconds = 0

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

    def report_status(self, remaining_seconds):
        if not PANEL_URL:
            return
        try:
            payload = {"server_name": SERVER_NAME, "remaining_time": remaining_seconds, "status": "up"}
            resp = requests.post(PANEL_URL, json=payload, timeout=10)
            print(f"✅ 状态上报成功: {resp.json()}")
        except Exception as e:
            print(f"❌ 状态上报失败: {e}")

    async def run(self):
        if not self.email or not self.password:
            print("❌ 错误: 未设置 XSERVER_EMAIL 或 XSERVER_PASSWORD 环境变量!")
            return False

        print("🚀 开始 XServer GAME 自动续期流程 (Patchright 终极版)...")

        async with async_playwright() as p:
            proxy_config = {"server": PROXY_SERVER} if PROXY_SERVER else None

            browser = await p.chromium.launch(
                headless=True,
                channel="chrome",
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--window-size=1920,1080"
                ]
            )

            context = await browser.new_context(
                proxy=proxy_config,
                viewport={"width": 1920, "height": 1080},
                timezone_id="Asia/Tokyo"
            )

            page = await context.new_page()

            try:
                print(f"🌐 正在访问登录页: {self.target_url}")
                await page.goto(self.target_url, wait_until="domcontentloaded", timeout=60000)
                await page.screenshot(path="step_01_login_page.png")

                # 1. 填写账号密码
                print("📝 正在填写登录信息...")
                await page.fill('input[name="member_id"], input[name="memberid"]', self.email)
                await page.fill('input[name="user_password"]', self.password)

                # 2. 寻找并点击 Cloudflare Turnstile 验证框
                print("⏳ 寻找并点击 Cloudflare Turnstile 验证框...")
                try:
                    turnstile_clicked = False
                    for frame in page.frames:
                        if "challenges.cloudflare.com" in frame.url:
                            print(f"🎯 锁定 Turnstile Frame: {frame.url}")
                            checkbox = frame.locator('input[type="checkbox"], label, .cb-lb, span.mark')
                            if await checkbox.count() > 0:
                                await checkbox.first.click()
                                print("🖱️ 成功点击 Turnstile 验证复选框！")
                                turnstile_clicked = True
                                break
                    
                    if not turnstile_clicked:
                        iframe = page.frame_locator('iframe[src*="challenges.cloudflare.com"]')
                        await iframe.locator('body').click(position={"x": 28, "y": 28})
                        print("🖱️ 通过相对坐标点击了 Turnstile 区域")
                except Exception as e:
                    print(f"⚠️ 自动点击验证框提示: {e}")

                # 3. 轮询等待 cf-turnstile-response 签发
                print("⏳ 等待 Cloudflare 验证 Token 生成 (最多 30 秒)...")
                token_passed = False
                for i in range(30):
                    token = await page.evaluate(
                        '() => { const el = document.querySelector("input[name=\\"cf-turnstile-response\\"], input[name=\\"g-recaptcha-response\\"]"); return el ? el.value : ""; }'
                    )
                    if token and len(token) > 20:
                        token_passed = True
                        print(f"🎉 Cloudflare Turnstile 在第 {i+1} 秒成功签发 Token！")
                        await asyncio.sleep(2)  # 等待 JS 回调绑定
                        break
                    await asyncio.sleep(1)

                if not token_passed:
                    print("❌ Cloudflare 验证 Token 获取超时！")
                    await page.screenshot(path="step_02_cf_blocked.png")
                    return False

                # 4. 提交登录
                print("🖱️ 点击登录按钮...")
                await page.click('text="ログインする"')
                await asyncio.sleep(5)

                # 5. 处理跳转 / 进入游戏面板
                current_url = page.url
                print(f"🔍 当前页面 URL: {current_url}")

                if "jumpvps" in current_url:
                    print("🔄 检测到 jumpvps 中间页，尝试自动触发跳转...")
                    for _ in range(3):
                        try:
                            btn = page.locator('text="進む", text="続行", text="ゲーム管理"').first
                            if await btn.count() > 0:
                                await btn.click()
                        except Exception:
                            pass
                        await asyncio.sleep(2)
                        if any(hint in page.url for hint in GAME_PANEL_URL_HINTS):
                            break

                if not any(hint in page.url for hint in GAME_PANEL_URL_HINTS):
                    print("🔍 查找并点击'ゲーム管理'入口...")
                    game_btn = page.locator('text="ゲーム管理"').first
                    if await game_btn.count() > 0:
                        await game_btn.click()
                        await asyncio.sleep(3)
                        pages = context.pages
                        if len(pages) > 1:
                            page = pages[-1]

                print(f"✅ 成功到达游戏管理面板: {page.url}")
                await page.screenshot(path="step_03_game_panel.png")

                # 6. 获取当前服务器剩余时间和到期时间
                body_text = await page.content()
                remaining_match = re.search(r"残り(\d+時間\d+分)", body_text)
                if remaining_match:
                    rem_str = remaining_match.group(1)
                    print(f"⏰ 当前剩余时间: {rem_str}")
                    self.remaining_seconds = self.parse_remaining_seconds(rem_str)

                expiry_match = re.search(r"\((\d{4}-\d{2}-\d{2}[^)]*)まで\)", body_text)
                if expiry_match:
                    self.old_expiry_time = expiry_match.group(1).strip()
                    print(f"📅 当前到期时间: {self.old_expiry_time}")

                # 7. 点击升级/续期按钮
                print("📄 正在点击升级/期限延长按钮...")
                upgrade_btn = page.locator('text="アップグレード・期限延長", text="期限延長", a[href*="freeplan/extend"]').first
                if await upgrade_btn.count() == 0:
                    raise RuntimeError("未找到升级/续期按钮")
                
                await upgrade_btn.click()
                await asyncio.sleep(3)

                # 8. 检查是否在限制期内
                current_html = await page.content()
                if "更新をご希望の場合は" in current_html or "残り契約時間が24時間を切るまで" in current_html:
                    print("ℹ️ 尚未到可续期时间（剩余时间充足）")
                    self.renewal_status = "Unexpired"
                else:
                    print("📄 寻找'期限を延長する'并点击...")
                    ext_btn = page.locator('text="期限を延長する"').first
                    await ext_btn.click()
                    await asyncio.sleep(3)

                    print("📄 点击'確認画面に進む'...")
                    conf_btn = page.locator('button[type="submit"]:has-text("確認画面に進む"), text="確認画面に進む"').first
                    await conf_btn.click()
                    await asyncio.sleep(3)

                    try:
                        time_cell = page.locator('tr:has-text("延長後の期限") td').first
                        if await time_cell.count() > 0:
                            self.new_expiry_time = (await time_cell.inner_text()).strip()
                            print(f"🕡 预计续期后到期时间: {self.new_expiry_time}")
                    except Exception:
                        pass

                    print("📄 点击最终确认续期按钮...")
                    final_btn = page.locator('button[type="submit"]:has-text("期限を延長する"), text="期限を延長する"').first
                    await final_btn.click()
                    await asyncio.sleep(4)

                    success_text = "期限を延長しました。" in await page.content()
                    if success_text or "extend/do" in page.url:
                        print("🎉 续期操作成功！")
                        self.renewal_status = "Success"
                        await page.screenshot(path="renewal_success_tg.png")
                    else:
                        print("❌ 续期结果验证未通过")
                        self.renewal_status = "Failed"

                self.generate_report_file()
                self.push_telegram_notifications()
                return True

            except Exception as e:
                print(f"❌ 运行过程中发生异常: {e}")
                self.renewal_status = "Failed"
                self.generate_report_file()
                self.push_telegram_notifications()
                return False
            finally:
                await browser.close()

    def generate_report_file(self):
        try:
            beijing_time = datetime.datetime.now(timezone(timedelta(hours=8)))
            current_time = beijing_time.strftime("%Y-%m-%d %H:%M:%S")

            content = f"**最后运行时间**: `{current_time}`\n\n"
            content += "**运行结果**: <br>\n"
            content += "🖥️服务器:`🇯🇵Xserver(MC)`<br>\n"

            if self.renewal_status == "Success":
                content += "📊续期结果:✅Success<br>\n"
                content += f"🕛️旧到期时间: `{self.old_expiry_time or 'Unknown'}`<br>\n"
                content += f"🕡️新到期时间: `{self.new_expiry_time or 'Unknown'}`<br>\n"
            elif self.renewal_status == "Unexpired":
                content += "📊续期结果:ℹ️Unexpired<br>\n"
                content += f"🕛️旧到期时间: `{self.old_expiry_time or 'Unknown'}`<br>\n"
            else:
                content += "📊续期结果:❌Failed<br>\n"
                content += f"🕛️旧到期时间: `{self.old_expiry_time or 'Unknown'}`<br>\n"

            with open("report-notify.md", "w", encoding="utf-8") as f:
                f.write(content)
            print("✅ report-notify.md 生成成功")
        except Exception as e:
            print(f"⚠️ 生成 report 失败: {e}")

    def push_telegram_notifications(self):
        beijing_time = datetime.datetime.now(timezone(timedelta(hours=8)))
        current_time = beijing_time.strftime("%Y-%m-%d %H:%M:%S")

        self.telegram.send_renewal_result(
            status=self.renewal_status,
            old_time=self.old_expiry_time or "Unknown",
            new_time=self.new_expiry_time,
            run_time=current_time
        )

        if self.renewal_status == "Success" and os.path.exists("renewal_success_tg.png"):
            self.telegram.send_photo("renewal_success_tg.png", caption=f"XServer 续期成功截图\n{current_time}")

        if self.remaining_seconds > 0:
            self.report_status(self.remaining_seconds)


if __name__ == "__main__":
    success = asyncio.run(XServerAutomation().run())
    raise SystemExit(0 if success else 1)
