import time
import os
import requests
import platform
from datetime import datetime

if "DISPLAY" not in os.environ:
    if platform.system().lower() == "linux":
        try:
            from pyvirtualdisplay import Display
            display = Display(visible=False, size=(1920, 1080))
            display.start()
            os.environ["DISPLAY"] = display.new_display_var
        except:
            pass

from seleniumbase import SB

# ================= 配置区域 =================
PROXY = os.getenv("PROXY") or None
TG_TOKEN = os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")
ACCOUNTS = os.getenv("BYTENUT", "")

URL_LOGIN_PANEL = "https://www.bytenut.com/auth/login"
URL_HOMEPAGE = "https://www.bytenut.com/homepage"
API_SERVER_LIST = "https://www.bytenut.com/game-panel/api/gpPanelServer/user/servers"
API_EXTENSION_INFO = "https://www.bytenut.com/game-panel/api/gp-free-server/extension-info/{}"
API_START_STATUS = "https://www.bytenut.com/game-panel/api/serverStartQueue/status/{}"

RENEW_MENU = '//li[contains(., "RENEW SERVER")]'
EXTEND_BTN = "button.extend-btn"
START_BTN = "button.start-btn"
START_VERIFY_DIALOG = "div.el-dialog"
MANAGEMENT_MENU = '//li[contains(@class,"el-sub-menu")]//span[text()="Management"]'
CONSOLE_MENU_ITEM = '//li[contains(@class,"el-menu-item")]//span[text()="Console"]'
PAGE_READY_INDICATOR = '//li[contains(@class,"el-menu-item")]'


def parse_accounts(raw: str):
    accounts = []
    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line or "-----" not in line:
            continue
        parts = line.split("-----", 1)
        if len(parts) == 2:
            accounts.append((parts[0].strip(), parts[1].strip()))
    return accounts


class BytenutRenewal:

    def __init__(self):
        self.BASE_DIR = os.path.dirname(os.path.abspath(__file__))
        self.screenshot_dir = os.path.join(self.BASE_DIR, "artifacts")
        os.makedirs(self.screenshot_dir, exist_ok=True)

    # ========== 脱敏工具 ==========
    def mask_account(self, u):
        if not u:
            return "Unknown"
        u = u.strip()
        if "@" in u:
            local, domain = u.split("@", 1)
            masked_local = (
                local[:2] + "*" * (len(local) - 2)
                if len(local) > 2
                else local[0] + "*"
            )
            return f"{masked_local}@{domain}"
        return u[:2] + "*" * (len(u) - 2) if len(u) > 2 else u[0] + "*"

    def mask_server_id(self, sid):
        return "[server]"

    def log(self, msg):
        print(f"[{time.strftime('%H:%M:%S')}] [INFO] {msg}", flush=True)

    def shot(self, sb, name):
        path = os.path.join(self.screenshot_dir, name)
        sb.save_screenshot(path)
        return path

    # ========== TG 通知 ==========
    def send_tg(self, icon, title, account_name, server_id,
                state_str, expiry_str, extra="", screenshot=None):
        if not TG_TOKEN or not TG_CHAT_ID:
            return
        msg = (
            f"{icon} {title}\n\n"
            f"账号: {account_name}\n"
            f"服务器: {server_id}\n"
            f"状态: {state_str}\n"
            f"到期时间: {expiry_str}\n"
        )
        if extra:
            msg += f"\n{extra}\n"
        msg += "\nByteNut Auto Renew"
        try:
            if screenshot and os.path.exists(screenshot):
                url = f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto"
                with open(screenshot, "rb") as f:
                    requests.post(
                        url,
                        data={"chat_id": TG_CHAT_ID, "caption": msg},
                        files={"photo": f},
                    )
            else:
                url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
                requests.post(url, data={"chat_id": TG_CHAT_ID, "text": msg})
        except Exception as e:
            self.log(f"TG发送失败: {e}")

    # ========== 浏览器内 fetch（变量嵌入脚本）==========
    def fetch_api(self, sb, url, method="GET", referer=None):
        """
        在浏览器上下文执行 fetch，变量直接嵌入脚本字符串。
        返回解析后的 data，失败返回 None。
        """
        if referer is None:
            referer = URL_HOMEPAGE

        # 用 json.dumps 确保字符串正确转义
        import json
        url_js = json.dumps(url)
        method_js = json.dumps(method)
        referer_js = json.dumps(referer)

        script = f"""
        var callback = arguments[0];
        var token = localStorage.getItem('yl-token')
                 || sessionStorage.getItem('yl-token') || '';
        var headers = {{
            'Accept': 'application/json, text/plain, */*',
            'Referer': {referer_js}
        }};
        if (token) {{ headers['Yl-Token'] = token; }}
        fetch({url_js}, {{
            method: {method_js},
            headers: headers,
            credentials: 'include'
        }})
        .then(function(r) {{ return r.json(); }})
        .then(function(data) {{ callback({{ok: true, data: data}}); }})
        .catch(function(e) {{ callback({{ok: false, error: e.toString()}}); }});
        """
        try:
            result = sb.execute_async_script(script)
            if result and result.get("ok"):
                resp = result["data"]
                if resp.get("code") == 200:
                    return resp.get("data")
                self.log(f"API 业务错误: {resp.get('message')}")
            else:
                err = result.get("error") if result else "None"
                self.log(f"fetch 失败: {err}")
        except Exception as e:
            self.log(f"fetch_api 异常: {e}")
        return None

    def fetch_api_post(self, sb, url, referer=None):
        """POST 版本"""
        if referer is None:
            referer = URL_HOMEPAGE

        import json
        url_js = json.dumps(url)
        referer_js = json.dumps(referer)

        script = f"""
        var callback = arguments[0];
        var token = localStorage.getItem('yl-token')
                 || sessionStorage.getItem('yl-token') || '';
        var headers = {{
            'Accept': 'application/json, text/plain, */*',
            'Content-Type': 'application/x-www-form-urlencoded',
            'Referer': {referer_js}
        }};
        if (token) {{ headers['Yl-Token'] = token; }}
        fetch({url_js}, {{
            method: 'POST',
            headers: headers,
            credentials: 'include'
        }})
        .then(function(r) {{ return r.json(); }})
        .then(function(data) {{ callback({{ok: true, data: data}}); }})
        .catch(function(e) {{ callback({{ok: false, error: e.toString()}}); }});
        """
        try:
            result = sb.execute_async_script(script)
            if result and result.get("ok"):
                resp = result["data"]
                if resp.get("code") == 200:
                    return resp.get("data")
                self.log(f"API 业务错误: {resp.get('message')}")
            else:
                err = result.get("error") if result else "None"
                self.log(f"fetch POST 失败: {err}")
        except Exception as e:
            self.log(f"fetch_api_post 异常: {e}")
        return None

    # ========== API 封装 ==========
    def get_servers_data(self, sb):
        return self.fetch_api(sb, API_SERVER_LIST, referer=URL_HOMEPAGE)

    def get_extension_data(self, sb, server_id):
        ref = f"https://www.bytenut.com/free-gamepanel/{server_id}"
        return self.fetch_api(sb, API_EXTENSION_INFO.format(server_id),
                              referer=ref)

    def get_start_status(self, sb, server_id):
        ref = f"https://www.bytenut.com/free-gamepanel/{server_id}"
        return self.fetch_api(sb, API_START_STATUS.format(server_id),
                              referer=ref)

    # ========== 等待页面就绪 ==========
    def wait_for_panel_ready(self, sb, server_id, timeout=30):
        self.log("⏳ 等待页面加载...")
        try:
            sb.wait_for_element_present(PAGE_READY_INDICATOR, timeout=timeout)
        except Exception:
            self.log("⚠️ 侧边栏未出现，继续...")

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if sb.is_element_present(RENEW_MENU):
                    self.log("✅ 页面就绪（RENEW SERVER 可见）")
                    return True
            except Exception:
                pass
            self.remove_overlay_ads(sb)
            time.sleep(1)
        self.log("⚠️ RENEW SERVER 等待超时")
        return False

    # ========== 轮询开机队列 ==========
    def poll_start_status(self, sb, server_id, timeout=300, interval=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = self.get_start_status(sb, server_id)
            if data:
                in_queue = data.get("inQueue", True)
                can_start = data.get("canStart", False)
                pos = data.get("queuePosition", 0)
                wait_sec = data.get("estimatedWaitSeconds")
                msg = data.get("statusMessage", "")
                self.log(f"  队列: inQueue={in_queue}, pos={pos}, "
                         f"wait={wait_sec}s, msg={msg}")
                if not in_queue and can_start:
                    self.log("✅ 服务器启动成功（队列完成）")
                    return True, "running"
            time.sleep(interval)
        return False, "timeout"

    def wait_until_running(self, sb, server_id, timeout=120, interval=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            servers = self.get_servers_data(sb)
            if servers:
                for srv in servers:
                    if str(srv.get("id")) == str(server_id):
                        state = (srv.get("serverInfo") or {}).get(
                            "state", "unknown")
                        self.log(f"  server state: {state}")
                        if state == "running":
                            return True, state
            time.sleep(interval)
        return False, "unknown"

    def wait_until_not_expired(self, sb, server_id, timeout=120, interval=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            ext_info = self.get_extension_data(sb, server_id)
            if ext_info and ext_info.get("minutesUntilExpiration", 0) > 0:
                return True
            time.sleep(interval)
        return False

    # ========== 广告清理 ==========
    def remove_overlay_ads(self, sb):
        try:
            sb.execute_script("""
                (function(){
                    var a = document.getElementById('ez-accept-all');
                    if (a) a.click();
                    var keep = ['turnstile','cf-turnstile','extend-btn',
                                'adsterra-rewarded','Claim Reward','Watch Ad',
                                'start-btn','Start','Continue',
                                'RENEW SERVER','el-menu'];
                    ['ins.adsbygoogle','iframe[id^="aswift"]',
                     'div[id^="google_ads"]',
                     'div[class*="ad-"]:not([class*="adsterra-rewarded"])',
                     'div[class*="ads-"]',
                     'div[id*="ad-"]:not([id*="adsterra"])',
                     'div[id*="ads-"]','.ad-container','.ads-wrapper',
                     '.fixed-bottom-banner','.ezoic-floating-bottom',
                     '.fc-ab-root'
                    ].forEach(function(s){
                        document.querySelectorAll(s).forEach(function(el){
                            if (keep.some(function(k){
                                return el.innerHTML.indexOf(k) !== -1;
                            })) return;
                            el.style.cssText += 'display:none!important;'
                                + 'visibility:hidden!important;'
                                + 'height:0!important;width:0!important;';
                        });
                    });
                    document.body.style.overflow = 'auto';
                    document.body.style.position = 'static';
                })();
            """)
        except Exception:
            pass
    
    # ========== Turnstile ==========
    def is_turnstile_present(self, sb):
        try:
            return sb.execute_script("""
                return !!(document.querySelector('.cf-turnstile')
                    || document.querySelector(
                        'iframe[src*="challenges.cloudflare"]')
                    || document.querySelector(
                        'input[name="cf-turnstile-response"]'));
            """)
        except Exception:
            return False

    def wait_turnstile(self, sb, timeout=90):
        if not self.is_turnstile_present(sb):
            self.log("ℹ️ 无 Turnstile 验证")
            return True
        self.log("⏳ 等待 Turnstile 验证...")
        start = time.time()
        last_click = 0
        while time.time() - start < timeout:
            self.remove_overlay_ads(sb)
            try:
                sb.execute_script("""
                    var e = document.querySelector('.cf-turnstile');
                    if (e) e.scrollIntoView({block:'center'});
                """)
            except Exception:
                pass
            try:
                val = sb.execute_script(
                    "return document.querySelector("
                    "\"input[name='cf-turnstile-response']\")?.value || '';"
                )
                if len(val) > 20:
                    self.log("✅ Turnstile 完成")
                    return True
            except Exception:
                pass
            now = time.time()
            if now - last_click > 3:
                try:
                    sb.uc_gui_click_captcha()
                    last_click = now
                except Exception:
                    try:
                        sb.find_element(".cf-turnstile").click()
                        last_click = now
                    except Exception:
                        pass
            time.sleep(1)
        self.log("⚠️ Turnstile 超时")
        return False

    def _wait_dialog_turnstile(self, sb, timeout=30):
        self.log("⏳ 等待弹窗 Turnstile（最多 30s）...")
        start = time.time()
        last_click = 0
        while time.time() - start < timeout:
            self.remove_overlay_ads(sb)
            if sb.execute_script(
                    "return !document.querySelector('div.el-dialog');"):
                self.log("✅ 弹窗已消失，验证自动完成")
                return True
            if sb.execute_script("""
                var btn = document.querySelector(
                    'div.el-dialog__footer button.el-button--primary');
                return btn && !btn.disabled
                    && !btn.classList.contains('is-disabled');
            """):
                self.log("✅ Continue 已启用，Turnstile 自动完成")
                return True
            try:
                val = sb.execute_script("""
                    var d = document.querySelector('div.el-dialog');
                    if (!d) return '';
                    var i = d.querySelector(
                        'input[name="cf-turnstile-response"]');
                    return i ? i.value : '';
                """)
                if val and len(val) > 20:
                    self.log("✅ 弹窗 Turnstile token 已填充")
                    return True
            except Exception:
                pass
            now = time.time()
            if now - last_click > 3:
                try:
                    sb.uc_gui_click_captcha()
                    last_click = now
                except Exception:
                    try:
                        sb.execute_script("""
                            var d = document.querySelector('div.el-dialog');
                            if (d) {
                                var ts = d.querySelector('.cf-turnstile');
                                if (ts) ts.click();
                            }
                        """)
                        last_click = now
                    except Exception:
                        pass
            time.sleep(1)

        # 超时后最终检查
        if sb.execute_script(
                "return !document.querySelector('div.el-dialog');"):
            self.log("✅ 超时后弹窗已消失")
            return True
        if sb.execute_script("""
            var btn = document.querySelector(
                'div.el-dialog__footer button.el-button--primary');
            return btn && !btn.disabled
                && !btn.classList.contains('is-disabled');
        """):
            self.log("✅ 超时后 Continue 已启用")
            return True
        self.log("⚠️ Turnstile 等待结束，尝试继续")
        return True

    # ========== 广告验证弹窗 ==========
    def handle_ad_verification(self, sb):
        try:
            if not sb.execute_script(
                "return !!document.querySelector("
                "'div.adsterra-rewarded-dialog');"
            ):
                return True
            self.log("🛡️ 处理广告验证...")
            time.sleep(1)
            sb.execute_script("""
                var btn = document.querySelector(
                    'div.adsterra-rewarded-dialog button.el-button--primary');
                if (btn) btn.click();
            """)
            time.sleep(3)
            orig = sb.driver.current_window_handle
            if len(sb.driver.window_handles) > 1:
                for h in sb.driver.window_handles:
                    if h != orig:
                        sb.driver.switch_to.window(h)
                        break
                time.sleep(12)
                sb.driver.close()
                sb.driver.switch_to.window(orig)
                time.sleep(2)
            sb.execute_script("""
                var btn = document.querySelector(
                    'div.adsterra-rewarded-dialog button.el-button--success');
                if (btn) btn.click();
            """)
            time.sleep(3)
            self.log("✅ 广告验证完成")
            return True
        except Exception as e:
            self.log(f"广告验证异常: {e}")
            return True

    # ========== 导航 + 等待就绪 ==========
    def navigate_to_panel(self, sb, server_id):
        url = f"https://www.bytenut.com/free-gamepanel/{server_id}"
        sb.uc_open_with_reconnect(url, reconnect_time=6)
        time.sleep(5)
        self.remove_overlay_ads(sb)
        return self.wait_for_panel_ready(sb, server_id, timeout=30)

    # ========== 点击 RENEW SERVER（带重试）==========
    def click_renew_menu(self, sb, server_id, idx, max_retry=3):
        for attempt in range(1, max_retry + 1):
            try:
                sb.wait_for_element_present(RENEW_MENU, timeout=15)
                sb.wait_for_element_visible(RENEW_MENU, timeout=10)
                self.remove_overlay_ads(sb)
                sb.click(RENEW_MENU)
                time.sleep(3)
                self.log(f"✅ RENEW SERVER 已点击 (attempt {attempt})")
                return True
            except Exception as e:
                self.log(f"⚠️ RENEW SERVER 失败 (attempt {attempt}): {e}")
                if attempt < max_retry:
                    self.shot(sb, f"renew_fail_{idx}_a{attempt}.png")
                    self.log("🔄 重新导航...")
                    self.navigate_to_panel(sb, server_id)
        self.log("❌ RENEW SERVER 最终失败")
        return False

    # ========== 续期 ==========
    def try_extend_and_verify(self, sb, server_id, old_expiry):
        if not self.wait_turnstile(sb):
            return False, ""
        self.remove_overlay_ads(sb)
        self.log("⏳ 点击续期按钮...")
        try:
            if sb.is_element_visible(EXTEND_BTN):
                sb.execute_script("arguments[0].click();",
                                  sb.find_element(EXTEND_BTN))
            else:
                self.log("⚠️ 续期按钮不可见")
                return False, ""
        except Exception as e:
            self.log(f"续期按钮点击失败: {e}")
            return False, ""

        time.sleep(2)
        self.handle_ad_verification(sb)
        time.sleep(5)

        for _ in range(6):
            new_ext = self.get_extension_data(sb, server_id)
            if new_ext:
                new_expiry = new_ext.get("expiredTime", "")
                if new_expiry and new_expiry != old_expiry:
                    self.log(f"✅ 续期生效: {self.format_expiry(new_expiry)}")
                    return True, self.format_expiry(new_expiry)
            time.sleep(5)

        if (sb.is_element_present(EXTEND_BTN)
                and not sb.is_element_enabled(EXTEND_BTN)):
            return "cooldown", ""
        return False, ""

    # ========== UI 开机 ==========
    def ui_start_server(self, sb, server_id, idx):
        self.log("🖥️ 导航到 Console 页面...")
        self.navigate_to_panel(sb, server_id)

        # Step 1: 展开 Management
        self.log("📂 展开 Management...")
        try:
            sb.click(MANAGEMENT_MENU)
            time.sleep(2)
        except Exception:
            try:
                sb.execute_script("""
                    document.querySelectorAll('.el-sub-menu__title span')
                    .forEach(function(el){
                        if (el.textContent.trim() === 'Management')
                            el.closest('.el-sub-menu__title').click();
                    });
                """)
                time.sleep(2)
            except Exception as e:
                self.log(f"Management 展开失败: {e}")
                return False, "management_fail"

        # Step 2: 点击 Console
        self.log("🖥️ 点击 Console...")
        try:
            sb.click(CONSOLE_MENU_ITEM)
            time.sleep(3)
        except Exception:
            try:
                sb.execute_script("""
                    document.querySelectorAll('.el-menu-item span')
                    .forEach(function(el){
                        if (el.textContent.trim() === 'Console')
                            el.closest('.el-menu-item').click();
                    });
                """)
                time.sleep(3)
            except Exception as e:
                self.log(f"Console 点击失败: {e}")

        # Step 3: 等待 Start 按钮
        try:
            sb.wait_for_element_present(START_BTN, timeout=15)
            self.log("✅ Console 页面就绪")
        except Exception as e:
            self.log(f"⚠️ 等待 Start 超时: {e}")
            self.shot(sb, f"no_start_btn_{idx}.png")
            return False, "no_start_btn"

        # Step 4: 点击 Start
        self.log("▶️ 点击 Start...")
        self.remove_overlay_ads(sb)
        try:
            btn = sb.find_element(START_BTN)
            if btn.get_attribute("disabled"):
                self.log("⚠️ Start disabled")
                return False, "start_disabled"
            sb.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", btn)
            time.sleep(0.5)
            sb.execute_script("arguments[0].click();", btn)
            self.log("  Start 已点击")
            time.sleep(2)
        except Exception as e:
            self.log(f"Start 点击失败: {e}")
            return False, "start_click_fail"

        # Step 5: 等待验证弹窗（最多 10s）
        self.log("⏳ 等待验证弹窗...")
        dialog_appeared = False
        for _ in range(10):
            try:
                if sb.is_element_visible(START_VERIFY_DIALOG):
                    dialog_appeared = True
                    break
            except Exception:
                pass
            data = self.get_start_status(sb, server_id)
            if data and not data.get("inQueue") and data.get("canStart"):
                self.log("✅ 无弹窗，直接开机成功")
                return True, "running"
            time.sleep(1)

        if not dialog_appeared:
            self.log("⚠️ 弹窗未出现，轮询状态...")
            ok, state = self.poll_start_status(sb, server_id, timeout=60)
            return (True, state) if ok else (False, "dialog_not_appeared")

        self.log("✅ 验证弹窗出现")

        # Step 6: 等待 Turnstile
        self._wait_dialog_turnstile(sb, timeout=30)

        # Step 7: 点击 Continue（最多 60s）
        self.log("▶️ 等待并点击 Continue...")
        continue_clicked = False
        for attempt in range(30):
            if sb.execute_script(
                    "return !document.querySelector('div.el-dialog');"):
                self.log("✅ 弹窗已自动消失")
                continue_clicked = True
                break
            if sb.execute_script("""
                var btn = document.querySelector(
                    'div.el-dialog__footer button.el-button--primary');
                return btn && !btn.disabled
                    && !btn.classList.contains('is-disabled');
            """):
                sb.execute_script("""
                    document.querySelector(
                        'div.el-dialog__footer button.el-button--primary'
                    ).click();
                """)
                self.log(f"  Continue 已点击 (attempt {attempt + 1})")
                continue_clicked = True
                break
            if attempt % 5 == 0:
                self.log(f"  等待 Continue 启用... ({attempt + 1}/30)")
            time.sleep(2)

        if not continue_clicked:
            self.log("❌ Continue 未启用")
            self.shot(sb, f"continue_fail_{idx}.png")
            return False, "continue_fail"

        time.sleep(3)

        # Step 8: 处理排队弹窗
        self._handle_queue_dialog(sb)

        # Step 9: 轮询开机状态
        self.log("⏳ 轮询开机状态...")
        ok, state = self.poll_start_status(
            sb, server_id, timeout=300, interval=5)
        if ok:
            self.log("⏳ 确认运行状态...")
            is_running, final_state = self.wait_until_running(
                sb, server_id, timeout=120, interval=10)
            return True, "running" if is_running else f"started({final_state})"
        return False, "start_timeout"

    def _handle_queue_dialog(self, sb):
        try:
            has_q = False
            for _ in range(5):
                has_q = sb.execute_script(
                    "return !!document.querySelector("
                    "'div.el-message-box.queue-dialog-styled');"
                )
                if has_q:
                    break
                time.sleep(1)
            if has_q:
                self.log("📋 排队弹窗，点击 OK...")
                sb.execute_script("""
                    document.querySelectorAll(
                        'div.el-message-box.queue-dialog-styled '
                        '.el-message-box__btns button'
                    ).forEach(function(btn){
                        if (btn.textContent.trim() === 'OK') btn.click();
                    });
                """)
                time.sleep(2)
                self.log("✅ 排队弹窗已关闭")
            else:
                self.log("ℹ️ 无排队弹窗")
        except Exception as e:
            self.log(f"排队弹窗异常: {e}")

    def format_expiry(self, dt_str):
        if not dt_str:
            return ""
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(dt_str, fmt).strftime(
                    "%b %d, %Y, %I:%M %p UTC")
            except ValueError:
                continue
        return dt_str

    # ========== 主流程 ==========
    def run(self):
        self.log("🚀 开始执行 ByteNut 续期与开机")
        accounts = parse_accounts(ACCOUNTS)
        if not accounts:
            self.log("❌ 无账号")
            return

        for idx, (user, pwd) in enumerate(accounts, 1):
            masked_user = self.mask_account(user)
            self.log(f"==== 账号 [{idx}] {masked_user} ====")

            with SB(
                uc=True, test=True, headed=True,
                chromium_arg=(
                    "--no-sandbox,--disable-dev-shm-usage,"
                    "--disable-gpu,--window-size=1280,753"
                ),
                proxy=PROXY,
            ) as sb:
                try:
                    # --- 登录 ---
                    sb.uc_open_with_reconnect(URL_LOGIN_PANEL, reconnect_time=5)
                    sb.wait_for_element_visible(
                        'input[placeholder="Username"]', timeout=25)
                    sb.type('input[placeholder="Username"]', user)
                    sb.type('input[placeholder="Password"]', pwd)
                    sb.click('//button[contains(., "Sign In")]')
                    time.sleep(5)
                    if "/auth/login" in sb.get_current_url():
                        self.send_tg("❌", "登录失败", user, "未知",
                                     "未知", "",
                                     screenshot=self.shot(
                                         sb, f"login_fail_{idx}.png"))
                        continue
                    self.log("✅ 登录成功")

                    # 停留 homepage 让 CF cookie 稳定
                    sb.uc_open_with_reconnect(URL_HOMEPAGE, reconnect_time=6)
                    time.sleep(8)

                    # --- 获取服务器信息 ---
                    servers = self.get_servers_data(sb)
                    if not servers:
                        self.send_tg("⚠️", "警告", user, "未知",
                                     "未知", "API 请求失败",
                                     screenshot=self.shot(
                                         sb, f"no_server_{idx}.png"))
                        continue

                    server = servers[0]
                    server_id = str(server.get("id") or "")
                    server_info = server.get("serverInfo") or {}
                    state = server_info.get("state", "running")
                    expired_time = server.get("expiredTime") or ""
                    expiry_str = self.format_expiry(expired_time)
                    log_sid = self.mask_server_id(server_id)
                    self.log(f"服务器 {log_sid}: 状态={state}, 到期={expiry_str}")

                    if not server_id:
                        self.send_tg("❌", "失败", user, "未知",
                                     state, expiry_str, "服务器ID无效",
                                     screenshot=self.shot(
                                         sb, f"invalid_id_{idx}.png"))
                        continue

                    ext_info = self.get_extension_data(sb, server_id)
                    if not ext_info:
                        self.send_tg("❌", "失败", user, server_id,
                                     state, expiry_str,
                                     extra="无法获取扩展信息",
                                     screenshot=self.shot(
                                         sb, f"ext_info_fail_{idx}.png"))
                        continue

                    can_extend = ext_info.get("canExtend", False)
                    cooldown_min = ext_info.get("minutesUntilNextExtension", 0)
                    mins_until_exp = ext_info.get("minutesUntilExpiration", 9999)
                    expired = mins_until_exp <= 0
                    self.log(f"可续期={can_extend}, 冷却={cooldown_min}分, "
                             f"距过期={mins_until_exp}分")

                    # ===== 离线处理 =====
                    if state == "offline":
                        if can_extend:
                            self.log("🔴 离线可续期，先续期再开机...")
                            ready = self.navigate_to_panel(sb, server_id)
                            if not ready:
                                self.send_tg("❌", "面板加载失败", user,
                                             server_id, "offline", expiry_str,
                                             screenshot=self.shot(
                                                 sb, f"panel_fail_{idx}.png"))
                                continue
                            if not self.click_renew_menu(sb, server_id, idx):
                                self.send_tg("❌", "续期菜单失败", user,
                                             server_id, "offline", expiry_str,
                                             screenshot=self.shot(
                                                 sb, f"renew_fail_{idx}.png"))
                                continue
                            result, new_time = self.try_extend_and_verify(
                                sb, server_id, expired_time)
                            if result is True:
                                if not self.wait_until_not_expired(
                                        sb, server_id):
                                    self.send_tg(
                                        "⚠️", "续期成功但状态未更新",
                                        user, server_id, "offline", expiry_str,
                                        "无法开机，请稍后重试",
                                        screenshot=self.shot(
                                            sb, f"start_fail_{idx}.png"))
                                    continue
                                ok, final = self.ui_start_server(
                                    sb, server_id, idx)
                                self.send_tg(
                                    "✅" if ok else "⚠️",
                                    "续期并开机成功" if ok else "续期成功，开机未确认",
                                    user, server_id,
                                    f"offline -> {final}",
                                    f"{expiry_str} -> {new_time}",
                                    screenshot=self.shot(sb, f"ok_{idx}.png"))
                            elif result == "cooldown":
                                self.send_tg("⏳", "续期后冷却", user,
                                             server_id, "offline", expiry_str,
                                             screenshot=self.shot(
                                                 sb, f"cooldown_{idx}.png"))
                            else:
                                self.send_tg("❌", "续期失败", user,
                                             server_id, "offline", expiry_str,
                                             screenshot=self.shot(
                                                 sb, f"extend_fail_{idx}.png"))
                        else:
                            if expired:
                                self.send_tg(
                                    "🚫", "无法操作", user, server_id,
                                    state, expiry_str,
                                    "服务器已过期且处于冷却期",
                                    screenshot=self.shot(
                                        sb, f"expired_cooldown_{idx}.png"))
                            else:
                                self.log("🔴 离线冷却中，直接开机（UI）")
                                ok, final = self.ui_start_server(
                                    sb, server_id, idx)
                                self.send_tg(
                                    "✅" if ok else "❌",
                                    "开机成功" if ok else "开机失败",
                                    user, server_id,
                                    f"offline -> {final}", expiry_str,
                                    screenshot=self.shot(
                                        sb,
                                        f"{'started' if ok else 'start_fail'}"
                                        f"_{idx}.png"))
                        continue

                    # ===== 运行中处理 =====
                    if not can_extend:
                        extra = "服务器已过期但处于冷却期" if expired else ""
                        self.log(f"⏳ 冷却中 ({cooldown_min}分钟)")
                        self.send_tg("⏳", "冷却中", user, server_id,
                                     state, expiry_str, extra,
                                     screenshot=self.shot(
                                         sb, f"cooldown_{idx}.png"))
                        continue

                    self.log("✅ 可续期，执行续期")
                    ready = self.navigate_to_panel(sb, server_id)
                    if not ready:
                        self.send_tg("❌", "面板加载失败", user, server_id,
                                     state, expiry_str,
                                     screenshot=self.shot(
                                         sb, f"panel_fail_{idx}.png"))
                        continue
                    if not self.click_renew_menu(sb, server_id, idx):
                        self.send_tg("❌", "续期菜单失败", user, server_id,
                                     state, expiry_str,
                                     screenshot=self.shot(
                                         sb, f"renew_fail_{idx}.png"))
                        continue
                    result, new_time = self.try_extend_and_verify(
                        sb, server_id, expired_time)
                    if result is True:
                        self.send_tg("✅", "续期成功", user, server_id,
                                     state, f"{expiry_str} -> {new_time}",
                                     screenshot=self.shot(sb, f"ok_{idx}.png"))
                    elif result == "cooldown":
                        self.send_tg("⏳", "续期后冷却", user, server_id,
                                     state, expiry_str,
                                     screenshot=self.shot(
                                         sb, f"cooldown_{idx}.png"))
                    else:
                        self.send_tg("❌", "续期失败", user, server_id,
                                     state, expiry_str,
                                     screenshot=self.shot(
                                         sb, f"extend_fail_{idx}.png"))

                except Exception as e:
                    self.log(f"❌ 异常: {e}")
                    try:
                        self.send_tg("❌", "异常", user, "未知",
                                     "未知", str(e),
                                     screenshot=self.shot(
                                         sb, f"error_{idx}.png"))
                    except Exception:
                        self.send_tg("❌", "异常", user, "未知",
                                     "未知", str(e))

        self.log("✅ 所有账号处理完毕")


if __name__ == "__main__":
    BytenutRenewal().run()
