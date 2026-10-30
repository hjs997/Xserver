#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XServer GAME 自动登录和续期脚本
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

PROXY_SERVER = os.getenv("PROXY_SERVER", "")
LOGIN_EMAIL = os.getenv("XSERVER_EMAIL", "")
LOGIN_PASSWORD = os.getenv("XSERVER_PASSWORD", "")
TARGET_URL = "https://secure.xserver.ne.jp/xapanel/login/xmgame"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

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
            message += f"💡 提示: 剩余时间大于 4 小时,暂不能续期\n"
        else:
            message += f"📊 续期结果: <b>❌ 失败</b>\n"
            message += f"🕛 到期时间: <code>{old_time}</code>\n"

        return self.send_message(message)

# =====================================================================
#                Cloudflare Turnstile 智能过盾核心函数
# =====================================================================

async def _get_turnstile_token(page):
    try:
        return await page.evaluate(
            """
            () => {
                try {
                    if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
                        let t = null;
                        try { t = window.turnstile.getResponse(); } catch (e) {}
                        if (t && t.length > 20) return t;
                        for (let i = 0; i < 5; i++) {
                            try { t = window.turnstile.getResponse(String(i)); } catch (e) { t = null; }
                            if (t && t.length > 20) return t;
                        }
                    }
                } catch (e) {}
                const inputs = document.querySelectorAll(
                    'input[name="cf-turnstile-response"], input[name="g-recaptcha-response"]'
                );
                for (const el of inputs) {
                    if (el.value && el.value.length > 20) return el.value;
                }
                return null;
            }
            """
        )
    except Exception:
        return None

async def solve_modal_turnstile(page, timeout=60):
    print("🛡️ 开始智能处理 Cloudflare Turnstile 验证...")
    start_time = asyncio.get_event_loop().time()
    last_click = 0
    clicks = 0

    while asyncio.get_event_loop().time() - start_time < timeout:
        token = await _get_turnstile_token(page)
        if token:
            print("🎉 Cloudflare Turnstile 验证通过，成功获取 Token！")
            return True

        if asyncio.get_event_loop().time() - last_click > 5:
            try:
                clicked = False
                for frame in page.frames:
                    if "challenges.cloudflare.com" in (frame.url or ""):
                        checkbox = frame.locator('input[type="checkbox"], label, .cb-lb').first
                        if await checkbox.count() > 0:
                            await checkbox.click(timeout=2000)
                            print(f"🖱️ 第 {clicks + 1} 次：直接点击了复选框")
                            clicked = True
                            break

                if not clicked:
                    frames_handles = await page.query_selector_all('iframe[src*="challenges.cloudflare.com"]')
                    for handle in frames_handles:
                        box = await handle.bounding_box()
                        if box and box['width'] > 10 and box['height'] > 10:
                            x = box['x'] + import_random_offset(20, 40)
                            y = box['y'] + box['height'] / 2 + import_random_offset(-5, 5)
                            
                            await page.mouse.move(x - 40, y + import_random_offset(-10, 10))
                            await asyncio.sleep(0.2)
                            await page.mouse.move(x, y)
                            await asyncio.sleep(0.1)
                            await page.mouse.down()
                            await asyncio.sleep(0.08)
                            await page.mouse.up()
                            
                            print(f"🖱️ 第 {clicks + 1} 次：通过拟人化坐标偏移点击 (x={int(x)}, y={int(y)})")
                            clicked = True
                            break

                if clicked:
                    clicks += 1
                    last_click = asyncio.get_event_loop().time()
            except Exception as e:
                print(f"⚠️ 点击尝试提示: {e}")

        await asyncio.sleep(1)

    if await _get_turnstile_token(page):
        return True
    print("❌ Cloudflare 验证 Token 获取超时。")
    return False

def import_random_offset(a, b):
    import random
    return random.uniform(a, b)

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
            requests.post(PANEL_URL, json=payload, timeout=10)
        except Exception:
            pass

    async def run(self):
        if not self.email or not self.password:
            print("❌ 错误: 未设置 XSERVER_EMAIL 或 XSERVER_PASSWORD 环境变量!")
            return False

        print("🚀 开始 XServer GAME 自动续期流程 (终极修复版)...")

        async with async_playwright() as p:
            proxy_config = {"server": PROXY_SERVER} if PROXY_SERVER else None

            browser = await p.chromium.launch(
                headless=False,
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

                # 2. 智能过 Turnstile 验证
                await solve_modal_turnstile(page, timeout=45)

                # 3. 提交登录
                print("🖱️ 点击登录按钮...")
                await page.click('text="ログインする"')
                await asyncio.sleep(5)

                # 4. 处理跳转 / 进入游戏面板
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

                # 5. 获取当前服务器剩余时间和到期时间
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

                # 6. 点击升级/期限延长按钮 (Step 1)
                print("📄 步骤1：正在点击主页面的'アップグレード・期限延長'按钮...")
                upgrade_btn = page.locator('text="アップグレード・期限延長"').first
                if await upgrade_btn.count() == 0:
                    upgrade_btn = page.locator('a:has-text("アップグレード・期限延長"), button:has-text("アップグレード・期限延長")').first
                
                if await upgrade_btn.count() == 0:
                    raise RuntimeError("未找到主页面的升级/续期按钮")
                
                await upgrade_btn.click()
                print("🖱️ 成功点击主页面按钮")
                await asyncio.sleep(3)

                # 7. 检查是否在限制期内（完美兼容 4 小时限制提示）
                current_html = await page.content()
                if any(keyword in current_html for keyword in ["4時間を切るまで", "更新をご希望の場合は", "残り契約時間が", "行えません"]):
                    print("ℹ️ 尚未到可续期时间（契约剩余时间大于 4 小时）")
                    self.renewal_status = "Unexpired"
                else:
                    # Step 2: 点击套餐详情页中的 '期限を延長する'
                    print("📄 步骤2：寻找并点击'期限を延長する'按钮...")
                    ext_btn = page.locator('text="期限を延長する", button:has-text("期限を延長する"), a:has-text("期限を延長する")').first
                    if await ext_btn.count() > 0:
                        await ext_btn.click()
                        print("🖱️ 成功点击'期限を延長する'")
                        await asyncio.sleep(3)
                    else:
                        raise RuntimeError("未找到套餐详情页的'期限を延長する'按钮")

                    # Step 3: 点击 '確認画面に進む'
                    print("📄 步骤3：点击'確認画面に進む'...")
                    conf_btn = page.locator('button[type="submit"]:has-text("確認画面に進む"), text="確認画面に進む", a:has-text("確認画面に進む")').first
                    if await conf_btn.count() > 0:
                        await conf_btn.click()
                        print("🖱️ 成功点击'確認画面に進む'")
                        await asyncio.sleep(3)

                    try:
                        time_cell = page.locator('tr:has-text("延長後の期限") td').first
                        if await time_cell.count() > 0:
                            self.new_expiry_time = (await time_cell.inner_text()).strip()
                            print(f"🕡 预计续期后到期时间: {self.new_expiry_time}")
                    except Exception:
                        pass

                    # Step 4: 点击最终确认续期按钮
                    print("📄 步骤4：点击最终确认续期按钮...")
                    final_clicked = False
                    for btn_text in ["確定する", "設定する", "実行する", "期限を延長する"]:
                        final_btn = page.locator(f'button:has-text("{btn_text}"), text="{btn_text}", input[value*="{btn_text}"]')
                        if await final_btn.count() > 0:
                            await final_btn.first.click()
                            print(f"🎉 成功点击最终确认按钮: {btn_text}")
                            final_clicked = True
                            await asyncio.sleep(4)
                            break
                    
                    if not final_clicked:
                        print("⚠️ 未直接匹配到标准确认按钮，尝试查找页面提交按钮...")
                        fallback_btn = page.locator('button[type="submit"], input[type="submit"]').first
                        if await fallback_btn.count() > 0:
                            await fallback_btn.click()
                            await asyncio.sleep(4)

                    success_text = "期限を延長しました。" in await page.content()
                    if success_text or "extend/do" in page.url or "complete" in page.url:
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
        except Exception:
            pass

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
