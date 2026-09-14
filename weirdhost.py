import time
import os
import json
import re
import random
import traceback
from urllib.parse import urlparse

import requests
from seleniumbase import SB

# ================= 环境配置 =================
# 仅在未设置时使用默认 DISPLAY，兼容 GitHub Actions / Docker / VNC。
if "DISPLAY" not in os.environ:
    os.environ["DISPLAY"] = ":1"

if "XAUTHORITY" not in os.environ and os.path.exists("/home/headless/.Xauthority"):
    os.environ["XAUTHORITY"] = "/home/headless/.Xauthority"

print(f"[DEBUG] Env DISPLAY: {os.environ.get('DISPLAY')}")
print(f"[DEBUG] Env XAUTHORITY: {os.environ.get('XAUTHORITY')}")

# ================= 配置区域 =================
PROXY_URL = os.getenv("PROXY", "").strip()
TG_TOKEN = os.getenv("TG_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()
NUM = os.getenv("NUM", "").strip()
COOKIE = os.getenv("COOKIE", "").strip()

LOGIN_URL = "https://hub.weirdhost.xyz/auth/login"
URL_APP_PANEL = f"https://hub.weirdhost.xyz/server/{NUM}" if NUM else ""

RENEW_BUTTON = '//button[contains(normalize-space(.), "연장하기")]'
TIMESTAMP_PATTERN = r'\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}'
# 冷却提示的真实文案是"N일/N시간/N분 후에 연장할 수 있어요"（N天/N小时/N分钟后可以续期）。
# 页面在"可以续期"时会显示"지금 연장이 가능해요"这句话（已由截图确认真实存在），
# 之前的代码错误地认为这句话不会出现，于是删掉了这个最直接的正向判断依据，
# 改成"只要没匹配到冷却提示就当作可以续期"的间接推断——
# 这在页面文本还没渲染/更新完成、残留旧的冷却文案片段时，会被误判为"冷却中"。
# 现在把"지금 연장이 가능해요"加回来，作为第一优先级的正向判断依据。
AVAILABLE_TEXT = "지금 연장이 가능해요"
COOLDOWN_PATTERN = r'(\d+)\s*(일|시간|분)\s*후에\s*연장할\s*수\s*있어요'


class WeirdHostRenewal:
    def __init__(self):
        self.base_dir = os.path.dirname(os.path.abspath(__file__))
        self.screenshot_dir = os.path.join(self.base_dir, "artifacts")
        os.makedirs(self.screenshot_dir, exist_ok=True)

    def log(self, msg):
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] [INFO] {msg}", flush=True)

    def human_wait(self, min_s=6, max_s=10):
        time.sleep(random.uniform(min_s, max_s))

    def validate_config(self):
        missing = []
        if not NUM:
            missing.append("NUM")
        if not COOKIE:
            missing.append("COOKIE")

        if missing:
            raise RuntimeError(
                "缺少环境变量：" + ", ".join(missing)
                + "。请在 GitHub Secrets/环境变量中配置。"
            )

    def safe_screenshot(self, sb, filename):
        path = os.path.join(self.screenshot_dir, filename)
        try:
            sb.save_screenshot(path)
            return path
        except Exception as e:
            self.log(f"⚠️ 截图失败：{e}")
            return None

    def send_telegram_notify(self, message, photo_path=None):
        if not TG_TOKEN or not TG_CHAT_ID:
            self.log("⚠️ 未配置 TG_TOKEN 或 TG_CHAT_ID，跳过推送。")
            return

        try:
            if photo_path and os.path.exists(photo_path):
                url = f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto"
                with open(photo_path, "rb") as f:
                    response = requests.post(
                        url,
                        data={"chat_id": TG_CHAT_ID, "caption": message},
                        files={"photo": f},
                        timeout=30,
                    )
            else:
                url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
                response = requests.post(
                    url,
                    data={"chat_id": TG_CHAT_ID, "text": message},
                    timeout=30,
                )

            response.raise_for_status()
            self.log("✅ TG 推送已发送")
        except Exception as e:
            self.log(f"❌ TG 推送失败: {e}")

    def get_page_timestamp(self, sb):
        """安全提取页面中的到期时间，找不到时返回“未找到”。"""
        try:
            page_text = sb.get_text("body")
            match = re.search(TIMESTAMP_PATTERN, page_text, re.IGNORECASE)
            if match:
                return match.group(0)
        except Exception as e:
            self.log(f"⚠️ 读取到期时间失败：{e}")

        self.log("⚠️ 页面中未找到 YYYY-MM-DD HH:MM:SS 格式的时间")
        return "未找到"

    def open_with_retry(self, sb, url, name, retries=3, reconnect_time=5):
        """
        打开页面并重试。

        之前只用 `sb.get_current_url()` 是否非空来判断"打开成功"，
        但哪怕导航实际失败（网络抖动、DNS 问题、Chrome 报错页、about:blank 等），
        get_current_url() 也几乎总会返回点什么，导致这个检查形同虚设——
        表面上"打开成功"了，实际上浏览器根本没停在目标网站上。
        紧接着的 Cloudflare 检测会因为页面里没有相关关键词而被误判为"已通过"，
        再往后 inject_cookie 指定域名设置 Cookie 时就会因为当前文档域名对不上
        而报 invalid cookie domain。

        这里改成额外校验当前页面域名是否真的落在目标域名下，
        域名对不上就当成打开失败，走重试逻辑。
        """
        last_error = None
        expected_host = urlparse(url).netloc

        for attempt in range(1, retries + 1):
            try:
                self.log(f"🌐 正在访问{name}（第 {attempt}/{retries} 次）...")
                sb.uc_open_with_reconnect(url, reconnect_time=reconnect_time)
                time.sleep(3)

                current_url = sb.get_current_url() or ""
                current_host = urlparse(current_url).netloc

                if current_host and current_host == expected_host:
                    self.log(f"✅ {name}已打开")
                    return True
                else:
                    last_error = f"当前页面域名为「{current_host or '空'}」，与目标域名「{expected_host}」不符（当前地址：{current_url or '空'}）"
                    self.log(f"⚠️ {name}打开后域名校验未通过：{last_error}")
            except Exception as e:
                last_error = e
                self.log(f"⚠️ {name}打开失败：{e}")

            if attempt < retries:
                wait_time = 5 * attempt
                self.log(f"⏳ {wait_time} 秒后重试...")
                time.sleep(wait_time)

        raise RuntimeError(f"{name}连续 {retries} 次打开失败：{last_error}")

    def move_mouse_human(self, sb):
        """保留原脚本的页面交互预热逻辑。"""
        try:
            for _ in range(3):
                sb.slow_click("body")
                time.sleep(random.uniform(0.5, 1.2))
        except Exception as e:
            self.log(f"⚠️ 页面交互预热失败，继续执行：{e}")

    def get_turnstile_token(self, sb):
        try:
            # uc=True 这种反检测模式下，execute_script 不会自动把代码包成函数，
            # 顶层直接写 return 会报 "Illegal return statement"。
            # 用立即执行函数 (function(){...})() 包一层，不管哪种模式下 return 都合法。
            token = sb.execute_script("""
                (function() {
                    let tokens = [];
                    document.querySelectorAll('input, textarea').forEach(el => {
                        let v = el.value || "";
                        if (v.length > 50) tokens.push(v);
                    });

                    if (tokens.length) {
                        tokens.sort((a, b) => b.length - a.length);
                        return tokens[0];
                    }
                    return "";
                })();
            """)

            if token and len(token) > 50:
                return token
        except Exception as e:
            self.log(f"⚠️ Token扫描异常: {e}")

        return None

    def has_turnstile_widget(self, sb):
        """检测页面上是否存在 Cloudflare Turnstile 验证组件（那个打勾框）。"""
        try:
            return bool(sb.execute_script("""
                return !!(
                    document.querySelector('.cf-turnstile') ||
                    document.querySelector('iframe[src*="challenges.cloudflare.com"]') ||
                    document.querySelector('[data-sitekey]')
                );
            """))
        except Exception:
            return False

    def wait_cloudflare(self, sb, max_attempts=2, poll_seconds=90):
        """
        等待 Cloudflare 验证结束；超时后不直接判定续期失败，由后续面板结果确认。

        这里要区分两种完全不同的场景：
        1. 登录页那种整页拦截式挑战——页面上会出现
           "just a moment" / "verify you are human" 之类的文案，
           这些文案消失就说明整页挑战通过了。
        2. 点击"연장하기"续期按钮后弹出的 Turnstile 打勾框——它并不是整页拦截，
           页面文字里从头到尾都不会出现上面那些关键词。
           之前的代码只要"没搜到那几个关键词"就判定"挑战已通过"，
           对第 2 种场景来说，这个判断在打勾框刚出现、还没来得及自动验证完成时
           就会误判为"已通过"，导致脚本提前往下走，而真正的验证 Token
           还没生成，续期请求自然没有真正发出去。

        修复：如果页面上检测到 Turnstile 组件（不管整页文案在不在），
        就必须等到拿到验证 Token 才算真正通过；
        只有页面上压根没有 Turnstile 组件、也没有整页拦截文案时，
        才能直接判定为"已通过/无需验证"。
        """
        cf_words = [
            "verify you are human",
            "just a moment",
            "checking your browser",
            "troubleshoot",
        ]

        for attempt in range(1, max_attempts + 1):
            self.log(f"⏳ Cloudflare 检查，第 {attempt}/{max_attempts} 次")
            try:
                sb.uc_gui_click_captcha()
            except Exception as e:
                self.log(f"⚠️ 点击挑战控件失败：{e}")

            deadline = time.time() + poll_seconds
            widget_seen = False
            while time.time() < deadline:
                try:
                    page_lower = sb.get_page_source().lower()
                except Exception as e:
                    self.log(f"⚠️ 检查 Cloudflare 状态失败：{e}")
                    page_lower = ""

                interstitial_present = any(word in page_lower for word in cf_words)
                widget_present = self.has_turnstile_widget(sb)
                if widget_present:
                    widget_seen = True

                if widget_present:
                    token = self.get_turnstile_token(sb)
                    if token:
                        self.log(f"✅ 检测到 Turnstile 验证组件，已获取 Token（长度={len(token)}），验证完成")
                        return True
                    # 组件还在，但 token 还没生成，说明还没验证完，不能提前判定通过
                elif not interstitial_present:
                    self.log("✅ 未检测到 Cloudflare 挑战页面或验证组件，视为已通过")
                    return True

                time.sleep(3)

            if widget_seen:
                self.log("⚠️ 检测到 Turnstile 组件但等待超时，仍未拿到 Token")

            if attempt < max_attempts:
                try:
                    self.log("⚠️ 等待超时，尝试备用处理...")
                    self.move_mouse_human(sb)
                    sb.uc_gui_handle_captcha()
                except Exception as e:
                    self.log(f"⚠️ 备用处理失败：{e}")
                time.sleep(8)

        self.log("⚠️ Cloudflare 等待超时，继续通过最终页面状态判断是否成功")
        return False

    def inject_cookie(self, sb):
        if not COOKIE:
            raise RuntimeError("COOKIE 为空，无法登录。")

        sb.add_cookie({
            "name": "remember_web_59ba36addc2b2f9401580f014c7f58ea4e30989d",
            "value": COOKIE,
            "domain": "hub.weirdhost.xyz",
            "path": "/",
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
            "expires": int(time.time()) + 3600 * 24 * 365,
        })
        self.log("✅ Cookie 注入成功")

    def renewal_available(self, sb):
        """
        判断是否可以续期，按优先级依次检测：

        1. 页面文字里直接出现"지금 연장이 가능해요"（此文案真实存在，见用户截图确认）
           —— 这是"可以续期"最直接、最可靠的正向依据，命中就直接判定 available。
        2. 出现"N일/N시간/N분 후에 연장할 수 있어요"这句冷却提示
           —— 说明还在冷却期，判定 cooldown。
        3. 两者都没出现：不能再像之前那样默认当成"可以续期"，
           因为页面文本可能还没渲染完成、或残留了旧状态的文字片段，
           贸然当作"可以续期"点击按钮反而可能白点或点错时机。
           这里再确认页面基本内容（到期时间/续期按钮文字）是否存在：
           - 都没有：页面状态异常（如 Cloudflare 没过、Cookie 失效），判定 unknown。
           - 有，但既没有正向文案也没有冷却文案：状态不明，同样判定 unknown，
             交给上层重试/等待，而不是盲目点击。

        注意：不能用"续期按钮是否可见"来判断——冷却期间按钮其实也一直显示在页面上，
        只是点了没用，"看得见"不等于"能点"，这也是之前版本误判的根源之一。
        """
        try:
            page_text = sb.get_text("body")
        except Exception as e:
            self.log(f"⚠️ 读取页面文字失败：{e}")
            page_text = ""

        if AVAILABLE_TEXT in page_text:
            self.log(f"✅ 页面中检测到「{AVAILABLE_TEXT}」，判断为可以续期")
            return "available"

        match = re.search(COOLDOWN_PATTERN, page_text)
        if match:
            remaining = f"{match.group(1)}{match.group(2)}"
            self.log(f"⏳ 检测到冷却提示：{remaining}后可以续期 -> 时间尚未达到")
            return "cooldown"

        # 既没有正向文案，也没有冷却文案，先确认页面本身是否加载出了真实内容
        # （比如还卡在 Cloudflare 验证页、或者没登录成功）。
        page_looks_valid = ("연장하기" in page_text) or ("유통기한" in page_text)
        if not page_looks_valid:
            self.log("⚠️ 页面中既没有可续期/冷却提示，也没有找到到期时间/续期按钮相关文字，判断为页面状态异常")
            return "unknown"

        self.log("⚠️ 页面内容看起来正常，但既未检测到可续期提示，也未检测到冷却提示，判断为状态不明，本次跳过")
        return "unknown"

    def solve_panel_turnstile(self, sb, timeout=60):
        """
        点击"연장하기"之后，页面会在按钮上方插入一个 Cloudflare Turnstile 验证框
        （先显示"正在验证..."转圈，再变成待勾选的"请验证您是真人"），
        必须等它被勾选验证通过（变成"성공/成功"绿勾）之后，
        网站才会自动把按钮切换成"연장 중..."并真正提交续期请求。
        这是从实测截图逐帧确认的真实顺序：验证框是点击"연장하기"之后才出现的，
        不是页面一开始（点击前）就带着的。

        之前脚本完全没处理这个验证框，点完"연장하기"就直接往下走，
        点击本身不会报错，但因为验证框没过，服务器端根本不认这次续期请求——
        这正是"日志显示点击成功，但到期时间三次确认都没变化"的真正原因。

        这里在点击"연장하기"之后调用：先等验证框渲染出来（给个短暂等待，
        因为它不是瞬间出现的），检测到了就点击解决；
        如果等了一阵子始终没出现验证框，说明这次续期本来就不需要验证，直接跳过。
        """
        widget_appear_deadline = time.time() + 10
        widget_seen = False
        while time.time() < widget_appear_deadline:
            if self.has_turnstile_widget(sb):
                widget_seen = True
                break
            time.sleep(1)

        if not widget_seen:
            self.log("ℹ️ 未检测到点击后出现的 Turnstile 验证框，视为无需验证")
            return

        self.log("🔒 检测到续期按钮上方出现的 Turnstile 验证框，尝试勾选验证...")
        try:
            sb.uc_gui_click_captcha()
        except Exception as e:
            self.log(f"⚠️ 点击验证框失败：{e}")

        deadline = time.time() + timeout
        while time.time() < deadline:
            token = self.get_turnstile_token(sb)
            if token:
                self.log(f"✅ Turnstile 验证已通过，获取到 Token（长度={len(token)}）")
                return
            time.sleep(2)

        self.log("⚠️ Turnstile 验证等待超时，仍未拿到 Token，续期结果以后续确认步骤为准")

    def click_renew(self, sb):
        self.log("⏳ 等待续期按钮...")
        sb.wait_for_element_visible(RENEW_BUTTON, timeout=30)
        sb.scroll_to(RENEW_BUTTON)
        time.sleep(1)

        try:
            sb.click(RENEW_BUTTON)
        except Exception as e:
            self.log(f"⚠️ 普通点击失败，尝试 JS 点击：{e}")
            sb.execute_script("""
                const buttons = [...document.querySelectorAll('button')];
                const btn = buttons.find(b => b.innerText.includes('연장하기'));
                if (!btn) throw new Error('未找到 연장하기 按钮');
                btn.click();
            """)

        self.log("✅ 已点击 연장하기")
        self.confirm_dialog_if_present(sb)

        # 点击"연장하기"之后，页面会在按钮上方插入一个 Cloudflare Turnstile 验证框
        # （"请验证您是真人"），必须勾选验证通过（变成"성공/成功"）之后，
        # 网站才会自动把按钮切换成"연장 중..."并真正提交续期请求。
        # 这是从实测截图逐帧确认的真实顺序：验证框是点击后才出现的，
        # 不是页面一开始就带着的。
        self.solve_panel_turnstile(sb)

    def confirm_dialog_if_present(self, sb):
        """
        很多网站的"연장하기"按钮点一下并不会立刻真正续期，而是先弹出一个二次确认弹窗
        （比如"정말 연장하시겠습니까?"配一个"확인/예/네"按钮），需要再点一次确认按钮，
        真正的续期请求才会发出去。

        之前脚本点完主按钮就直接当作完成了，从没处理过这种二次确认弹窗——
        这正好能解释"日志显示已点击成功，但服务器端到期时间完全没变、
        用户自己上网页看也没续期"这种现象：主按钮点击只是弹出了确认框，
        真正的确认动作从来没有发生过。

        这里做一次尽量通用的探测：短暂等待后看页面上是否出现了弹窗/对话框，
        出现的话优先在弹窗范围内找常见确认文案的按钮点掉；
        找不到明确文案就退而求其次点弹窗里的第一个按钮；
        如果压根没有弹窗，就什么也不做——说明这个网站本来就没有二次确认这一步，
        点主按钮就是最终动作。
        """
        time.sleep(1.5)

        modal_selectors = [
            ".swal2-popup",
            "[role='dialog']",
            ".modal.show",
            ".modal.in",
            ".ReactModal__Content",
        ]

        modal_found = False
        matched_selector = None
        for sel in modal_selectors:
            try:
                if sb.is_element_visible(sel):
                    modal_found = True
                    matched_selector = sel
                    break
            except Exception:
                continue

        if not modal_found:
            self.log("ℹ️ 未检测到二次确认弹窗，视为点击主按钮后直接生效")
            return

        self.log(f"ℹ️ 检测到疑似确认弹窗（{matched_selector}），尝试点击确认按钮...")
        self.safe_screenshot(sb, "confirm_dialog.png")

        confirm_words = ["확인", "예", "네", "동의", "계속하기", "계속", "확인하기", "구매", "결제", "Confirm", "OK", "Yes"]
        clicked = False
        for word in confirm_words:
            xpath = f'//button[contains(normalize-space(.), "{word}")]'
            try:
                if sb.is_element_visible(xpath):
                    sb.click(xpath)
                    self.log(f"✅ 已点击确认弹窗中的「{word}」按钮")
                    clicked = True
                    break
            except Exception:
                continue

        if not clicked:
            try:
                sb.execute_script("""
                    const modal = document.querySelector('.swal2-popup, [role="dialog"], .modal.show, .modal.in, .ReactModal__Content');
                    if (modal) {
                        const btn = modal.querySelector('button');
                        if (btn) btn.click();
                    }
                """)
                self.log("⚠️ 未匹配到明确的确认文案，已尝试点击弹窗内第一个按钮，请结合截图核实")
            except Exception as e:
                self.log(f"❌ 弹窗确认按钮点击失败：{e}")

        time.sleep(1.5)

    def run(self):
        self.log("=" * 50)
        self.log("🚀 WeirdHost 自动续期任务开始")
        self.log("=" * 50)

        self.validate_config()
        self.log(f"🎯 服务器编号：{NUM}")
        self.log(f"🔗 目标面板：{URL_APP_PANEL}")
        self.log("🎯 正在启动 Chrome 浏览器...")

        with SB(
            uc=True,
            headed=True,
            headless=False,
            xvfb=False,
            chromium_arg=(
                "--no-sandbox,"
                "--disable-dev-shm-usage,"
                "--window-position=0,0,"
                "--start-maximized"
            ),
            proxy=PROXY_URL if PROXY_URL else None,
        ) as sb:
            try:
                self.log("✅ 浏览器已启动")

                # 1. 检测出口 IP（失败不影响续期）
                self.log("🌍 正在检测出口 IP...")
                try:
                    sb.open("https://api.ipify.org?format=json")
                    body = sb.get_text("body")
                    match = re.search(r"\{.*\}", body)
                    if not match:
                        raise ValueError(f"IP 接口返回异常：{body[:200]}")

                    ip_val = json.loads(match.group(0)).get("ip", "Unknown")
                    if "." in ip_val:
                        parts = ip_val.split(".")
                        masked_ip = f"{parts[0]}.{parts[1]}.***.{parts[-1]}"
                    else:
                        masked_ip = ip_val
                    self.log(f"✅ 当前出口 IP: {masked_ip}")
                except Exception as e:
                    self.log(f"⚠️ IP 检测跳过：{e}")

                # 2. 打开登录页
                self.open_with_retry(sb, LOGIN_URL, "登录页面")
                self.log("⏳ 等待页面 JS 渲染...")
                time.sleep(5)

                # 3. Cloudflare 页面检查
                self.wait_cloudflare(sb, max_attempts=2, poll_seconds=60)

                # 4. 注入 Cookie 后直接打开目标面板
                self.inject_cookie(sb)
                self.open_with_retry(sb, URL_APP_PANEL, "服务器面板")
                self.human_wait(6, 10)
                sb.scroll_to_bottom()
                time.sleep(2)

                # 5. 判断是否可以续期
                self.log("⏳ 开始检查续期状态...")
                renewal_status = self.renewal_available(sb)

                if renewal_status == "cooldown":
                    timestamp = self.get_page_timestamp(sb)
                    screenshot = self.safe_screenshot(sb, "cooldown.png")
                    msg = (
                        "⏳ 时间尚未达到，暂不能续期\n"
                        f"当前服务器到期时间：{timestamp}"
                    )
                    self.log(msg)
                    self.send_telegram_notify(msg, screenshot)
                    return

                if renewal_status == "unknown":
                    screenshot = self.safe_screenshot(sb, "unknown_state.png")
                    msg = (
                        "❌ 页面状态异常，未能确认续期按钮/到期时间\n"
                        "可能是 Cloudflare 验证没通过，或者 Cookie 已失效导致没登录成功，"
                        "本次跳过，不去点击不存在的按钮。请查看截图确认。"
                    )
                    self.log(msg)
                    self.send_telegram_notify(msg, screenshot)
                    return

                # 6. 点击续期前，先记录当前的到期时间，等下用来验证续期是否真的生效
                before_timestamp = self.get_page_timestamp(sb)
                self.log(f"🕒 续期前到期时间：{before_timestamp}")

                self.log("✅ 检测到可以续期")
                self.click_renew(sb)

                # 给页面切换/弹窗渲染留出时间
                time.sleep(5)

                # 7. 等待可能出现的页面验证
                self.log("⏳ 等待续期后的页面验证完成...")
                self.wait_cloudflare(sb, max_attempts=2, poll_seconds=120)

                # 8. 重新进入面板，并循环确认到期时间/状态已刷新
                self.log("📂 正在重新进入服务器面板确认续期结果...")
                confirmed = False
                final_timestamp = "未找到"

                for attempt in range(1, 4):
                    self.log(f"🔄 第 {attempt}/3 次确认续期结果")
                    self.open_with_retry(
                        sb,
                        URL_APP_PANEL,
                        f"服务器面板确认页 {attempt}",
                        retries=2,
                    )
                    self.human_wait(5, 8)
                    sb.scroll_to_bottom()
                    time.sleep(2)

                    final_timestamp = self.get_page_timestamp(sb)

                    # 真正可靠的验证方式：到期时间是不是真的往后推了。
                    # 之前用"页面上是不是还显示某句固定文案"来判断，
                    # 但那句文案在实际页面里根本不存在，导致永远误判成功。
                    if (
                        final_timestamp != "未找到"
                        and before_timestamp != "未找到"
                        and final_timestamp != before_timestamp
                    ):
                        confirmed = True
                        break

                    self.log(
                        "⚠️ 到期时间暂未变化，"
                        f"续期前：{before_timestamp}，当前：{final_timestamp}，继续等待..."
                    )
                    time.sleep(10)

                final_screenshot = self.safe_screenshot(sb, "final.png")

                if confirmed:
                    msg = (
                        "🎉 WeirdHost-语言版-家宽 续期成功\n"
                        f"🕒 服务器到期时间为：{final_timestamp}"
                    )
                    self.log(msg)
                    self.send_telegram_notify(msg, final_screenshot)
                else:
                    msg = (
                        "⚠️ WeirdHost 已执行续期操作，但到期时间暂未检测到变化\n"
                        f"🕒 续期前到期时间：{before_timestamp}\n"
                        f"🕒 当前检测到的到期时间：{final_timestamp}\n"
                        "请查看截图确认，也可能是页面刷新较慢，实际已续期成功。"
                    )
                    self.log(msg)
                    self.send_telegram_notify(msg, final_screenshot)

            except Exception as e:
                self.log(f"❌ 运行异常: {e}")
                traceback.print_exc()

                error_screenshot = self.safe_screenshot(sb, "error.png")
                self.send_telegram_notify(
                    f"❌ WeirdHost 自动续期运行异常\n{type(e).__name__}: {e}",
                    error_screenshot,
                )


if __name__ == "__main__":
    WeirdHostRenewal().run()
