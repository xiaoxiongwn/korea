import time
import os
import json
import re
import random
import requests

# 智能环境配置：仅在未设置时才应用默认值
# 这样兼容 GitHub Actions 的 xvfb-run (会自动设置 DISPLAY) 和 Docker 环境
if "DISPLAY" not in os.environ:
    os.environ["DISPLAY"] = ":1"
    
if "XAUTHORITY" not in os.environ:
    # 仅当路径存在时才设置，避免在 GitHub Runner (home/runner) 中报错
    if os.path.exists("/home/headless/.Xauthority"):
        os.environ["XAUTHORITY"] = "/home/headless/.Xauthority"

print(f"[DEBUG] Env DISPLAY: {os.environ.get('DISPLAY')}")
print(f"[DEBUG] Env XAUTHORITY: {os.environ.get('XAUTHORITY')}")

from seleniumbase import SB
from selenium.common.exceptions import WebDriverException

# ================= 配置区域 =================
# 代理配置
PROXY_URL = os.getenv("PROXY", "")
TG_TOKEN = os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")
NUM = os.getenv("NUM")
COOKIE = os.getenv("COOKIE")

# 目标 URL
URL_APP_PANEL = f"https://hub.weirdhost.xyz/server/{NUM}"
# ===========================================

class WeirdHostRenewal:
    def __init__(self):
        self.BASE_DIR = os.path.dirname(os.path.abspath(__file__))
        self.screenshot_dir = os.path.join(self.BASE_DIR, "artifacts")
        if not os.path.exists(self.screenshot_dir):
            os.makedirs(self.screenshot_dir)

    def log(self, msg):
        timestamp = time.strftime('%H:%M:%S')
        print(f"[{timestamp}] [INFO] {msg}", flush=True)

    def human_wait(self, min_s=6, max_s=10):
        """随机模拟人类等待时间"""
        time.sleep(random.uniform(min_s, max_s))

    def move_mouse_human(self, sb):
        """模拟人类鼠标晃动预热"""
        try:
            # 在页面不同位置“晃悠”一下鼠标，打破机器人直线模式
            for _ in range(3):
                x = random.randint(100, 800)
                y = random.randint(100, 600)
                sb.slow_click(f"body", force=True) # 借用 slow_click 的移动特性，或者直接用 move_to
                time.sleep(random.uniform(0.5, 1.2))
        except: pass

    def send_telegram_notify(self, message, photo_path=None):
        """发送 Telegram 通知 (带图片)"""
        if not TG_TOKEN or not TG_CHAT_ID:
            self.log("⚠️ 未配置 TG_TOKEN 或 TG_CHAT_ID，跳过推送。")
            return
        
        try:
            if photo_path and os.path.exists(photo_path):
                url = f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto"
                with open(photo_path, 'rb') as f:
                    # caption 参数用于发送带文字的图片
                    requests.post(url, data={'chat_id': TG_CHAT_ID, 'caption': message}, files={'photo': f})
            else:
                url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
                requests.post(url, data={'chat_id': TG_CHAT_ID, 'text': message})
            
            self.log("✅ TG 推送已发送")
        except Exception as e:
            self.log(f"❌ TG 推送失败: {e}")

    def check_token(self, sb):
        token = sb.execute_script("""
        let el = document.querySelector('input[name="cf-turnstile-response"]');
        return el ? el.value : null;
        """)
        if token:
            return True
        else:
            return False

    def get_turnstile_token(self, sb):
        try:
            token = sb.execute_script("""
            let tokens = [];
            // 1. 主页面扫描
            document.querySelectorAll(
                'input, textarea'
            ).forEach(el => {
                let v = el.value || "";
                if (v.length > 50) {
                    tokens.push(v);
                }
            });
            // 2. iframe扫描
            document.querySelectorAll(
                "iframe"
            ).forEach(frame => {
                try {
                    let doc =
                        frame.contentDocument ||
                        frame.contentWindow.document;
                    if (doc) {
                        doc.querySelectorAll(
                            'input, textarea'
                        ).forEach(el => {
                            let v = el.value || "";
                            if (v.length > 50) {
                                tokens.push(v);
                            }
                        });
                    }
                } catch(e) {}
            });
            // 3. 返回最长token
            if (tokens.length) {
                tokens.sort(
                    (a,b)=>b.length-a.length
                );
                return tokens[0];
            }
            return "";
            """)
            if token and len(token) > 50:
                return token
        except Exception as e:
            self.log(f"⚠️ Token扫描异常: {e}")
        return None    

    def run(self):
        self.log("=" * 40)
        self.log("🚀 WeirdHost - Renew流程")
        self.log("=" * 40)
        self.log("🎯 正在启动 Chrome 浏览器...")
        
        # 使用 headed=True 强制有头模式渲染到 VNC
        with SB(
            uc=True,            # 启用反检测模式
            headed=True,        # 关键：强制有头模式
            headless=False,     # 明确禁用 headless
            xvfb=False,         # 禁用内部虚拟显示器，使用系统 DISPLAY
            chromium_arg="--no-sandbox,--disable-dev-shm-usage,--window-position=0,0,--start-maximized",
            proxy=PROXY_URL if PROXY_URL else None
        ) as sb:
            try:
                self.log("✅ 浏览器已启动！")
                
                # ... (省略中间步骤，保持原有逻辑不变) ...
                
                # 1. IP 检测
                self.log("🌍 正在检测出口 IP...")
                try:
                    sb.open("https://api.ipify.org?format=json")
                    ip_val = json.loads(re.search(r'\{.*\}', sb.get_text("body")).group(0)).get('ip', 'Unknown')
                    parts = ip_val.split('.')
                    self.log(f"✅ 当前出口 IP: {parts[0]}.{parts[1]}.***.{parts[-1]}")
                except:
                    self.log("⚠️ IP 检测跳过...")

                # 2. 访问主页并注入 Cookie
                self.log("🔗 正在访问入口页面...")
                sb.uc_open_with_reconnect("https://hub.weirdhost.xyz/auth/login", reconnect_time=5)
                self.log("⏳ 等待页面 JS 渲染...")
                time.sleep(5)

                # 3. 全页Cloudflare挑战
                self.log("⏳ 全页Cloudflare挑战")
                cf_indicators = [
                    "verify you are human",
                    "确认您是真人",
                    "troubleshoot",
                    "just a moment"
                ]
                for i in range(2): # 尝试2次
                    sb.uc_gui_click_captcha()
                    time.sleep(15)
                    page_lower = sb.get_page_source().lower()
                    if any(x in page_lower for x in cf_indicators):
                        sb.uc_gui_handle_captcha()
                        time.sleep(15)
                        page_lower = sb.get_page_source().lower()
                    if not any(x in page_lower for x in cf_indicators):
                        self.log("✅Cloudflare验证已通过")
                        break
                
                # 4. 注入Cookies
                sb.add_cookie({
                    "name": "remember_web_59ba36addc2b2f9401580f014c7f58ea4e30989d",
                    "value": COOKIE,
                    "domain": "hub.weirdhost.xyz",
                    "path": "/",
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                    "expires": int(time.time()) + 3600 * 24 * 365
                })
                self.log("✅ Cookie 注入成功！")

                #cf_screenshot = f"{self.screenshot_dir}/cf.png"
                #sb.save_screenshot(cf_screenshot)
                #self.send_telegram_notify("通过Cloudflare整页挑战", cf_screenshot)

                # 5. 进入服务器面板
                self.log(f"📂 正在进入服务器面板...")
                sb.uc_open_with_reconnect(URL_APP_PANEL, reconnect_time=5)
                self.human_wait(6, 10)
                #server_screenshot = f"{self.screenshot_dir}/server.png"
                #sb.save_screenshot(server_screenshot)
                #self.send_telegram_notify(f"进入服务器 {NUM} 面板", server_screenshot)

                # 6. 判断续期按钮是否可点并点击
                sb.scroll_to_bottom() # 滚动到底部
                self.log("⏳ 开始检查续期按钮是否可以点击")
                status_element = sb.find_element('p[class*="StatusText"]')
                status_text = status_element.text.strip()

                if not "지금 연장이 가능해요" in status_text:
                    page_text = sb.get_text("body")
                    timestamp_pattern = r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}'
                    match_days = re.search(timestamp_pattern, page_text, re.IGNORECASE)
                    timestamp = match_days.group(0)
                    final_screenshot = f"{self.screenshot_dir}/final.png"
                    sb.save_screenshot(final_screenshot)
                    msg = f"🎉WeirdHost-家宽 续期按钮冷却中\n⏳服务器到期时间：{timestamp}"
                    self.log(msg)
                    self.send_telegram_notify(msg, final_screenshot)
                    return

                self.log("✅ 续期按钮可点击")
                btn = '//button[contains(normalize-space(.), "연장하기")]'
                sb.wait_for_element_visible(btn, timeout=10)
                sb.click(btn)
                self.log("✅ 已点击 연장하기")
                time.sleep(10)
                #renew_screenshot = f"{self.screenshot_dir}/renew.png"
                #sb.save_screenshot(renew_screenshot)
                #self.send_telegram_notify("已点击续期", renew_screenshot)
                    
                # 7. 局部Cloudflare挑战
                time.sleep(3)
                self.log("⏳ 局部Cloudflare挑战")
                token = None
                for i in range(2):
                    self.log(f"第 {i+1} 次尝试")
                    self.move_mouse_human(sb)
                    sb.uc_gui_click_captcha()
                    # 等待CF完成
                    for n in range(45):
                        token = self.get_turnstile_token(sb)
                        if token:
                            self.log(f"✅ 获取Turnstile Token 长度={len(token)}")
                            break
                        # 检查是否已经通过CF
                        try:
                            page_lower = (
                                sb.get_page_source()
                                .lower()
                            )
                            cf_words = [
                                "verify you are human",
                                "just a moment",
                                "checking your browser",
                                "cloudflare"
                            ]
                            still_cf = any(
                                x in page_lower
                                for x in cf_words
                            )
                            if not still_cf:
                                self.log("✅ CF挑战页面已消失")
                                break
                        except:
                            pass
                        time.sleep(2)
                    if token:
                        break
                    self.log("⚠️ 未获取token，尝试handle")
                    self.move_mouse_human(sb)
                    try:
                        sb.uc_gui_handle_captcha()
                    except Exception as e:
                        self.log(f"handle异常: {e}")
                    time.sleep(5)
                    token = self.get_turnstile_token(sb)
                    if token:
                        self.log(f"✅ handle后获取Token 长度={len(token)}")
                        break
                # 最终判断
                if token:
                    self.log("🎉 CF验证完成")
                else:
                    self.log("⚠️ 未发现Token，继续检查页面状态")
              
                final_screenshot = f"{self.screenshot_dir}/final.png"
                sb.save_screenshot(final_screenshot)
                #self.send_telegram_notify("已点击续期并通过cf挑战", final_screenshot)
                    
                # 8. 再次进入管理面板
                self.log(f"📂 再次进入面板...")
                sb.uc_open_with_reconnect(URL_APP_PANEL, reconnect_time=5)
                self.human_wait(6, 10)
                sb.scroll_to_bottom() # 滚动到底部
                page_text = sb.get_text("body")
                timestamp_pattern = r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}'
                match_days = re.search(timestamp_pattern, page_text, re.IGNORECASE)
                timestamp = match_days.group(0)
                msg = f"🎉WeirdHost-语言版-家宽 续期成功\n🕒服务器到期时间为: {timestamp}\n"
                self.send_telegram_notify(msg, final_screenshot)

            except Exception as e:
                self.log(f"❌ 运行异常: {e}")
                import traceback
                traceback.print_exc()
                sb.save_screenshot(f"{self.screenshot_dir}/error.png")


if __name__ == "__main__":
    WeirdHostRenewal().run()
