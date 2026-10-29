# -*- coding: utf-8 -*-
"""在工作台里直接登录网盘，登录成功那一刻自动把凭证抓出来。

为什么要有这个模块
------------------
要求用户「F12 → Network → 复制 Cookie」是不合理的：那是开发者的动作，
普通用户既不知道 F12 在哪，也不知道 Cookie 是什么。

但也不能反过来让用户随便粘贴一段东西 —— 真正的解法是让「登录」这件事
回到它本来的样子：给用户一个浏览器窗口，他自己扫码，剩下的我们自己取。

做法
----
服务器上起一个无头 Chromium，把画面截成 JPEG 连续推给工作台前端；
用户在前端点一下（坐标原样转发回服务器），扫二维码；
登录成功那一刻从浏览器上下文里把 Cookie / refresh_token 抽出来写进 .env。

线程模型（重要）
----------------
Playwright 的 sync API 与线程绑定，Flask 是多线程处理请求的，
直接在请求线程里调 Playwright 会出现 "It looks like you are using Playwright Sync API
inside the asyncio loop" 或跨线程 InvalidStateError。

所以每个会话配一个**专属 worker 线程** + 指令队列：
请求线程只负责投递指令、等结果，永远不碰 Playwright 对象。
"""
import base64
import os
import queue
import re
import shutil
import threading
import time

VIEWPORT = {"width": 1120, "height": 720}
JPEG_QUALITY = 58
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 会话闲置多久自动关（秒）。浏览器进程占内存，不能让它一直悬着。
IDLE_TIMEOUT = 8 * 60
# 同时只允许一个浏览器。一个 Chromium 就要 8~11 个进程，
# 两个实例在 2G 内存的机器上直接把可用内存吃光。
MAX_SESSIONS = 1

# 平台 → 登录入口。三个站点未登录时默认就落在扫码页，用户点一下就能扫。
LOGIN_ENTRY = {
    "quark": "https://pan.quark.cn/",
    "baidu": ("https://passport.baidu.com/v2/?login&tpl=netdisk"
              "&u=https%3A%2F%2Fpan.baidu.com%2Fdisk%2Fmain"),
    "xunlei": "https://pan.xunlei.com/login/",
}

# 平台 → (Cookie 域名特征, 登录态必须存在的 Cookie 名)
COOKIE_RULE = {
    "quark": ("quark.cn", ("__puus", "__pus")),
    "baidu": ("baidu.com", ("BDUSS",)),
    "xunlei": ("xunlei.com", ("xlly_s", "usrlogin")),
}

# 平台 → 要写入的 .env 键
CRED_KEY = {
    "quark": "QUARK_COOKIE",
    "baidu": "BAIDU_COOKIE",
    "xunlei": "XUNLEI_REFRESH_TOKEN",
}

LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-blink-features=AutomationControlled",
    "--window-size=1120,720",
]

# 隐藏最常见的自动化特征，降低被风控要求短信验证的概率
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh']});
"""


# ===========================================================================
# 纯函数：凭证组装与登录判定（可单测，不依赖浏览器）
# ===========================================================================
def find_browser():
    """找一个能用的 Chromium。优先系统装的，最后退回 Playwright 自带。"""
    env = (os.getenv("BROWSER_EXECUTABLE") or "").strip()
    if env and os.path.exists(env):
        return env
    for name in ("chromium", "chromium-browser", "google-chrome",
                 "google-chrome-stable", "microsoft-edge"):
        path = shutil.which(name)
        if path:
            return path
    return None      # None → 交给 Playwright 自己找


def cookie_string(cookies, domain_hint="", names=None):
    """把 Playwright 的 cookie 列表拼成请求头那一行。

    只保留目标域名的 cookie —— 浏览器上下文里同名 cookie 会在多个域上
    各存一份（.baidu.com 和 passport.baidu.com），全拼进去会互相覆盖。

    ``names`` 给定时，只保留这些名字里的（用于 xunlei 这类只需要个别字段的场景）；
    但整串 Cookie 才是各平台 API 要的东西，所以默认全要。
    """
    pairs, seen = [], set()
    for c in cookies or []:
        name = (c.get("name") or "").strip()
        if not name or name in seen:
            continue
        if domain_hint and domain_hint not in (c.get("domain") or ""):
            continue
        if names and name not in names:
            continue
        value = c.get("value")
        if value is None:
            continue
        seen.add(name)
        pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def _dig_refresh_token(obj, depth=0):
    """在任意 JSON 结构里递归找 refresh_token。"""
    if depth > 6:
        return ""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("refresh_token", "refreshToken") and isinstance(v, str) and v:
                return v.strip()
        for v in obj.values():
            got = _dig_refresh_token(v, depth + 1)
            if got:
                return got
    elif isinstance(obj, list):
        for v in obj[:20]:
            got = _dig_refresh_token(v, depth + 1)
            if got:
                return got
    return ""


def extract_refresh_token(text):
    """从未知来源的文本（localStorage 值 / 响应体）里抠 refresh_token。"""
    text = (text or "").strip()
    if not text:
        return ""
    if len(text) < 8:
        return ""
    import json
    try:
        got = _dig_refresh_token(json.loads(text))
        if got:
            return got
    except Exception:  # noqa: BLE001  不是 JSON 就继续用正则
        pass
    # 兜底：refresh_token 出现在非 JSON 文本里（localStorage 裸串、form 体、query）
    m = re.search(r'["\']?refresh_?[Tt]oken["\']?\s*[:=]\s*["\']?'
                  r'([A-Za-z0-9_\-\.]{16,})', text)
    return m.group(1).strip() if m else ""


def _find_key(obj, key, depth=0):
    """在任意嵌套结构里按 key 找第一个字符串值。"""
    if depth > 8:
        return ""
    if isinstance(obj, dict):
        v = obj.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
        for v in obj.values():
            got = _find_key(v, key, depth + 1)
            if got:
                return got
    elif isinstance(obj, list):
        for v in obj[:30]:
            got = _find_key(v, key, depth + 1)
            if got:
                return got
    return ""


def _find_dict_key(obj, key, depth=0):
    """在任意嵌套结构里找名为 key 的 dict（迅雷的 curUser 就藏在 users 里）。"""
    if depth > 8:
        return None
    if isinstance(obj, dict):
        v = obj.get(key)
        if isinstance(v, dict):
            return v
        for v in obj.values():
            got = _find_dict_key(v, key, depth + 1)
            if got:
                return got
    elif isinstance(obj, list):
        for v in obj[:30]:
            got = _find_dict_key(v, key, depth + 1)
            if got:
                return got
    return None


def _xl_log(msg):
    """把迅雷登录排障信息落到文件。

    服务在 nohup / setsid 下跑，print 出去的东西抓不到；登录这种事
    出问题只能靠现场留下的痕迹，不然每次都只能猜。
    """
    try:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "logs", "xunlei_login.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        line = time.strftime("%m-%d %H:%M:%S ") + str(msg).replace("\n", " ")[:1200]
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def mask_token(t):
    """日志里只留头尾，别把整串凭据写进文件。"""
    t = str(t or "")
    if len(t) <= 16:
        return t or "-"
    return f"{t[:10]}…{t[-6:]}(len={len(t)})"


def parse_xunlei_state(raw):
    """从页面 dump 出来的 store/localStorage 里解析迅雷登录态。

    为什么不按固定路径取：迅雷是 Nuxt + Vuex，curUser 挂在
    ``store.state.users.curUser`` 上，但登录页和网盘主页是两个应用，
    字段名（refresh_token / refreshToken）、外层结构都改过版。
    这里改成「递归找 curUser，再递归找字段」，结构变一次不用再改代码。

    返回 {"refresh_token", "access_token", "user_id", "logged_in"}。
    """
    out = {"refresh_token": "", "access_token": "", "user_id": "", "logged_in": False}
    if not isinstance(raw, dict):
        return out

    cur = _find_dict_key(raw, "curUser")
    scope = cur if isinstance(cur, dict) else raw
    if isinstance(cur, dict):
        uid = str(cur.get("userId") or cur.get("uid") or cur.get("user_id") or "").strip()
        out["user_id"] = "" if uid in ("0", "None") else uid
        out["logged_in"] = bool(out["user_id"])
    out["refresh_token"] = (_find_key(scope, "refresh_token")
                            or _find_key(scope, "refreshToken"))
    out["access_token"] = (_find_key(scope, "access_token")
                           or _find_key(scope, "accessToken"))
    if not out["refresh_token"]:
        # localStorage 里存的是「键 → JSON 字符串」，得逐条再解析一遍
        for val in (raw.get("storage") or {}).values():
            got = extract_refresh_token(str(val))
            if got:
                out["refresh_token"] = got
                break
    if out["refresh_token"] and not out["logged_in"]:
        out["logged_in"] = True
    return out


def judge_login(platform, cookies, captured_token="", extra_tokens=(),
                page_logged=False):
    """判断是否已登录，并给出要写入的凭证值。

    返回 dict：
        ok          是否拿到了可直接使用的凭证
        logged_in   是否已登录（可能已登录但拿不到需要的字段）
        key         要写入的 .env 键
        value       凭证值
        candidates  候选凭证（迅雷 refresh_token 会被网页轮换，可能第一个已失效）
        message     给用户看的一句话
    """
    key = CRED_KEY.get(platform, "")
    domain, need = COOKIE_RULE.get(platform, ("", ()))
    names = {c.get("name") for c in cookies or [] if c.get("name")}
    full = cookie_string(cookies, domain)

    if platform == "xunlei":
        cands = []
        # 页面 store / localStorage 里的值通常最新（页面自己刷新过 token 会写回），
        # 先试它；抓到的响应体可能是登录那一瞬的、已被页面换掉的旧值。
        for t in [*list(extra_tokens or []), captured_token]:
            t = (t or "").strip()
            if t and t not in cands:
                cands.append(t)
        if not cands:
            for c in cookies or []:
                if "xunlei" in (c.get("domain") or "") and "token" in (c.get("name") or "").lower():
                    t = extract_refresh_token(c.get("value") or "")
                    if t and t not in cands:
                        cands.append(t)
        if cands:
            return {"ok": True, "logged_in": True, "key": key, "value": cands[0],
                    "candidates": cands, "message": "已拿到 refresh_token"}
        # 迅雷新版登录成功后不落 xlly_s / usrlogin 这类老 cookie，
        # 只看 cookie 名会一直判成「未登录」—— 页面明明已经在网盘主页里了。
        # 所以以页面自己认到的登录态（curUser 有 userId）为准。
        if page_logged or any(n in names for n in need):
            return {"ok": False, "logged_in": True, "key": key, "value": "",
                    "candidates": [], "message": "浏览器里已经登录，但没读到 refresh_token —— "
                                                 "请点「重新加载」让页面刷新后再试"}
        return {"ok": False, "logged_in": False, "key": key, "value": "",
                "candidates": [], "message": "尚未登录"}

    if not names:
        return {"ok": False, "logged_in": False, "key": key, "value": "",
                "candidates": [], "message": "尚未登录"}

    hit = [n for n in need if n in names]
    if not hit:
        return {"ok": False, "logged_in": False, "key": key, "value": "",
                "candidates": [], "message": "尚未登录"}
    if not full:
        return {"ok": False, "logged_in": False, "key": key, "value": "",
                "candidates": [], "message": "已登录但没读到 Cookie"}

    return {"ok": True, "logged_in": True, "key": key, "value": full,
            "candidates": [full], "message": f"已拿到 Cookie（{len(full)} 字符）"}


# ===========================================================================
# 会话：一个 worker 线程 + 一个指令队列
# ===========================================================================
class BrowserSession:
    def __init__(self, sid, platform):
        self.sid = sid
        self.platform = platform
        self.created_at = time.time()
        self.touched_at = time.time()
        self.error = ""
        self.page_url = ""
        self.page_title = ""
        self.captured_token = ""
        # 页面登录后会不止一次换票，每次响应里的 refresh_token 都不同。
        # 只留最后一个的话，很容易留下那个"刚被页面自己消费掉"的作废值。
        self.captured_tokens = []
        # 百度：扫码会话活着时在页面里取到的 bdstoken（任务清理要用）
        self.baidu_bdstoken = ""
        self._xunlei_moves = 0          # 自动跳网盘主页的次数（最多 2 次）
        self._xunlei_moved_at = 0.0     # 上次自动跳转的时间，用于「正在读取」提示
        self._xunlei_logged = False     # 页面自己认到的登录态
        self._baidu_moved = False
        self._q = queue.Queue()
        self._ready = threading.Event()
        self._closed = False
        self._thread = threading.Thread(target=self._run, name=f"browser-{sid[:6]}",
                                        daemon=True)
        self._thread.start()
        # 首次启动含进程拉起 + 首屏加载，给足时间。等不到就明确判失败 ——
        # 之前这里忽略返回值，结果是接口假装成功、后面每个操作都超时 30 秒。
        if not self._ready.wait(60):
            self.error = self.error or "浏览器启动超时（60 秒内没有就绪）"

    # ---------- 对外接口（都在请求线程里调） ----------
    def call(self, cmd, *args, timeout=30):
        if self._closed:
            raise RuntimeError("浏览器会话已关闭")
        holder = {"event": threading.Event(), "result": None, "error": None}
        self._q.put((cmd, args, holder))
        if not holder["event"].wait(timeout):
            raise TimeoutError(f"浏览器操作超时：{cmd}")
        if holder["error"]:
            raise RuntimeError(holder["error"])
        return holder["result"]

    def touch(self):
        self.touched_at = time.time()

    def close(self, wait=True):
        if self._closed:
            return
        self._closed = True
        holder = {"event": threading.Event(), "result": None, "error": None}
        self._q.put(("__stop__", (), holder))
        if wait:
            holder["event"].wait(5)
            # 等 worker 真正退出，确保 Chromium 进程被回收（否则内存会一直挂着）
            self._thread.join(8)

    # ---------- worker ----------
    def _run(self):
        # import 必须放在 try 里：缺 playwright 时如果直接冒出去，
        # _ready 永远不会 set，调用方只能干等 60 秒且看不到原因。
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # noqa: BLE001
            self.error = (f"缺少 playwright（{exc}）。请在项目虚拟环境里执行 "
                          f"pip install playwright")
            self._ready.set()
            self._closed = True
            return

        exe = find_browser()
        try:
            with sync_playwright() as p:
                kwargs = dict(headless=True, args=LAUNCH_ARGS)
                if exe:
                    kwargs["executable_path"] = exe
                browser = p.chromium.launch(**kwargs)
                ctx = browser.new_context(viewport=VIEWPORT, locale="zh-CN",
                                          user_agent=UA, device_scale_factor=1)
                ctx.add_init_script(STEALTH_JS)
                page = ctx.new_page()
                page.on("response", self._on_response)
                page.on("request", self._on_request)
                self._ctx, self._page = ctx, page
                self._goto(LOGIN_ENTRY[self.platform], initial=True)
                self._ready.set()
                self._loop()
                try:
                    browser.close()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc).strip().splitlines()[0][:300]
            self._ready.set()
        finally:
            self._closed = True

    def _loop(self):
        while True:
            cmd, args, holder = self._q.get()
            if cmd == "__stop__":
                holder["event"].set()
                return
            try:
                holder["result"] = getattr(self, f"_do_{cmd}")(*args)
            except Exception as exc:  # noqa: BLE001
                holder["error"] = str(exc).strip().splitlines()[0][:300]
            finally:
                holder["event"].set()

    def _goto(self, url, initial=False):
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=45000)
            if initial:
                self._page.wait_for_timeout(2500)   # 二维码要等 JS 渲染
            self._sync_meta()
        except Exception as exc:  # noqa: BLE001
            if initial:
                raise
            self.error = str(exc).strip().splitlines()[0][:200]

    def _sync_meta(self):
        try:
            self.page_url = self._page.url
            self.page_title = (self._page.title() or "")[:80]
        except Exception:  # noqa: BLE001
            pass

    def _on_request(self, req):
        """记下页面自己换票用的请求参数。

        token 只有配对的 client_id 才能刷新。我们用常量里的那个，
        页面用哪个，出问题时必须能对得上 —— 否则永远只能猜。
        """
        try:
            url = (req.url or "").lower()
            if "auth/token" not in url or req.method != "POST":
                return
            body = (req.post_data or "")[:400]
            body = re.sub(r'(refresh_token["\']?\s*[:=]\s*["\']?)([A-Za-z0-9_\-\.]{12,})',
                          lambda m: m.group(1) + mask_token(m.group(2)), body)
            _xl_log(f"request: {req.url} {body}")
        except Exception:  # noqa: BLE001
            pass

    def _on_response(self, resp):
        """抓登录瞬间的 token 响应体。回调在 Playwright 线程里跑，必须快且不抛。"""
        try:
            url = (resp.url or "").lower()
            if "token" not in url and "xunlei" not in url:
                return
            # 只翻 XHR/fetch 的 JSON —— 迅雷登录成功后那一份响应里就带着
            # refresh_token，比事后去页面 store 里捞更早也更稳。
            if "json" not in (resp.headers or {}).get("content-type", ""):
                return
            body = resp.text()
            if "refresh_token" not in body and "refreshToken" not in body:
                return
            token = extract_refresh_token(body)
            if token:
                # 留一份结构：字段名改过一次，下次再改能一眼看出来
                brief = re.sub(r'([A-Za-z0-9_\-\.]{24,})', '«tok»', body)[:600]
                _xl_log(f"response: {resp.url} → {token[:10]}… len={len(token)} body={brief}")
                self.captured_token = token
                if token not in self.captured_tokens:
                    self.captured_tokens.insert(0, token)     # 最新的排最前
                    del self.captured_tokens[6:]
        except Exception:  # noqa: BLE001
            pass

    # ---------- 具体指令 ----------
    def _do_shot(self):
        self._sync_meta()
        return self._page.screenshot(type="jpeg", quality=JPEG_QUALITY,
                                     animations="disabled", timeout=15000)

    def _do_click(self, x, y):
        x = max(0.0, min(float(x), VIEWPORT["width"] - 1))
        y = max(0.0, min(float(y), VIEWPORT["height"] - 1))
        self._page.mouse.move(x, y, steps=3)
        self._page.wait_for_timeout(80)
        self._page.mouse.click(x, y)
        self._page.wait_for_timeout(350)
        return True

    def _do_type(self, text):
        # 先清空当前输入框内容，避免"点错了再补一段"变成拼接
        try:
            self._page.keyboard.press("Control+A")
            self._page.keyboard.press("Backspace")
        except Exception:  # noqa: BLE001
            pass
        self._page.keyboard.type(str(text), delay=20)
        return True

    def _do_key(self, name):
        self._page.keyboard.press(str(name))
        self._page.wait_for_timeout(200)
        return True

    def _do_scroll(self, dy):
        self._page.mouse.wheel(0, float(dy))
        self._page.wait_for_timeout(250)
        return True

    # 在页面上下文里刷新 token：origin / cookie / 指纹都是迅雷自己的环境，
    # 比服务器端 requests 更接近"网页自己换票"的情形。CORS 挡住的概率很低
    # （这个地址本来就是页面自己在调），失败就由调用方退回服务器端。
    _REFRESH_JS = """async (tok) => {
      try {
        const r = await fetch('https://xluser-ssl.xunlei.com/v1/auth/token', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({client_id: 'Xqp0kJBXWhwaTpB6',
                                grant_type: 'refresh_token',
                                refresh_token: tok})
        });
        const t = await r.text();
        return {status: r.status, body: (t || '').slice(0, 3000)};
      } catch (e) { return {status: 0, body: String(e)}; }
    }"""

    # 全量扫一遍页面里所有可能存放 refresh_token 的角落。
    # 只认固定路径（users.curUser.refresh_token）已经被现实打脸两次：
    # 迅雷改过结构，登录页那个 Nuxt 实例登录成功后也未必同步。
    # 这里改成"能翻到就翻"，代价是多花几十毫秒，换来的是不再猜。
    _ALL_TOKENS_JS = """() => {
      const out = [], seen = new Set();
      const add = (v) => {
        v = String(v || '').trim();
        if (v.length >= 16 && v.length <= 400 && !seen.has(v)) { seen.add(v); out.push(v); }
      };
      const KEY = /refresh_?token/i;
      const TOKRE = /refresh_?token["']?\s*[:=]\s*["']?([A-Za-z0-9_\-\.]{16,})/i;
      const scan = (v) => {
        if (typeof v !== 'string' && typeof v !== 'number') return;
        const m = String(v).match(TOKRE);
        if (m) add(m[1]);
      };
      const walk = (o, d) => {
        if (d > 7 || o == null || out.length > 15) return;
        if (typeof o === 'string') { scan(o); return; }
        if (typeof o !== 'object') return;
        let ks = [];
        try { ks = Object.keys(o); } catch (e) { return; }
        for (const k of ks) {
          let v;
          try { v = o[k]; } catch (e) { continue; }
          if (KEY.test(k) && (typeof v === 'string' || typeof v === 'number')) add(v);
          else if (typeof v === 'object' || typeof v === 'string') walk(v, d + 1);
          if (out.length > 15) return;
        }
      };
      try {
        const el = document.querySelector('#__nuxt');
        const vm = el && el.__vue__;
        if (vm && vm.$store) walk(vm.$store.state, 0);
        if (window.$nuxt && window.$nuxt.$store) walk(window.$nuxt.$store.state, 0);
        if (window.__NUXT__) walk(window.__NUXT__, 0);
      } catch (e) {}
      try { for (let i = 0; i < localStorage.length; i++) scan(localStorage.getItem(localStorage.key(i))); } catch (e) {}
      try { for (let i = 0; i < sessionStorage.length; i++) scan(sessionStorage.getItem(sessionStorage.key(i))); } catch (e) {}
      try { scan(document.cookie); } catch (e) {}
      return out.slice(0, 15);
    }"""

    def _do_tokens(self):
        """返回页面里找到的所有疑似 refresh_token（新的排前面由调用方决定）。"""
        try:
            got = self._page.evaluate(self._ALL_TOKENS_JS)
        except Exception as exc:  # noqa: BLE001
            _xl_log(f"tokens: 扫描失败 {exc}")
            return []
        got = [str(x) for x in (got or []) if x]
        _xl_log(f"tokens: 页面扫到 {len(got)} 个 → {[mask_token(t) for t in got]}")
        return got

    def _do_resync(self):
        """重载页面，逼它自己重新换一次票，返回重载后抓到的新候选。

        道理很简单：页面活着的时候登录态是有效的（能看到文件列表），
        只是它把 token 存在我们翻不到的地方。刷新一次，它就得重新
        走一遍"用 Cookie 静默登录 / 换票"，那一瞬的响应我们能抓到。
        """
        try:
            # 用 goto 主页而不是 reload：登录后 URL 常常还停在 /login（SPA 没换地址），
            # 原地刷新只会再画一张二维码；直接进网盘才会触发静默登录和换票。
            self._page.goto("https://pan.xunlei.com/", wait_until="domcontentloaded",
                            timeout=30000)
            self._page.wait_for_timeout(4500)
            self._sync_meta()
        except Exception as exc:  # noqa: BLE001
            _xl_log(f"resync: 重载失败 {exc}")
            return []
        _xl_log(f"resync: 重载后 url={self.page_url} captured={[mask_token(t) for t in self.captured_tokens]}")
        return list(self.captured_tokens)

    def _do_refresh(self, token):
        """用页面自己的环境换一次 access_token，返回 {ok, refresh_token, message}。"""
        import json as _json
        token = str(token or "").strip()
        if not token:
            return {"ok": False, "refresh_token": "", "message": "候选为空"}
        try:
            raw = self._page.evaluate(self._REFRESH_JS, token)
        except Exception as exc:  # noqa: BLE001
            _xl_log(f"refresh: 页面内执行失败 {exc}")
            return {"ok": False, "refresh_token": "", "message": str(exc)[:120]}
        body = (raw or {}).get("body") or ""
        try:
            j = _json.loads(body)
        except Exception:  # noqa: BLE001
            _xl_log(f"refresh: 返回非 JSON status={(raw or {}).get('status')} body={body[:200]}")
            return {"ok": False, "refresh_token": "", "message": body[:120]}
        if j.get("access_token"):
            new_tok = str(j.get("refresh_token") or "")
            _xl_log(f"refresh: 页面内换票成功 in={mask_token(token)} out={mask_token(new_tok)}")
            return {"ok": True, "refresh_token": new_tok, "message": "ok"}
        err = str(j.get("error_description") or j.get("error") or "未知错误")
        _xl_log(f"refresh: 页面内换票失败 {err} token={mask_token(token)}")
        return {"ok": False, "refresh_token": "", "message": err[:120]}

    def _do_reload(self):
        self.captured_token = ""          # 换二维码 = 重新开始，旧 token 不能留着
        self.captured_tokens = []
        self._xunlei_moves = 0
        self._xunlei_moved_at = 0.0
        self._xunlei_logged = False
        self._baidu_moved = False
        self.baidu_bdstoken = ""
        self._goto(LOGIN_ENTRY[self.platform])
        self._page.wait_for_timeout(1200)
        return True

    def _do_goto(self, url):
        """只能在白名单里跳转，避免这个接口变成任意网页代理。"""
        if not allowed_url(url):
            raise RuntimeError("只允许访问网盘登录页")
        self._goto(url)
        return True

    def _do_cred(self):
        cookies = self._ctx.cookies()
        # 百度：扫码成功时 passport 只给 BDUSS，云盘会话 PANPSC/BDCLND
        # 要等页面真正落到 pan.baidu.com 才下发。缺了它，后续
        # gettemplatevariable / share / transfer 全部返回 errno=-6。
        if self.platform == "baidu":
            if "BDUSS" in {c.get("name") for c in cookies}:
                self._ensure_baidu_session()
                cookies = self._ctx.cookies()
        extra, page_logged = [], False
        if self.platform == "xunlei":
            extra, page_logged = self._xunlei_store_tokens(cookies)
            if extra or self.captured_token:
                # 只在确实抓到东西时记一行，避免每 1.8 秒刷一行噪音
                _xl_log(f"cred: url={self.page_url} store={[mask_token(t) for t in extra]} "
                        f"captured={mask_token(self.captured_token)} logged={page_logged}")
        res = judge_login(self.platform, cookies, self.captured_token, extra,
                          page_logged=page_logged)
        # 刚跳主页那一瞬，curUser 可能还没被接口填好。这时别报红吓人，
        # 下一次轮询（1.8 秒后）通常就有了。
        if (self.platform == "xunlei" and res.get("logged_in")
                and not res.get("ok")
                and time.time() - (self._xunlei_moved_at or 0) < 12):
            res["message"] = "已登录，正在读取登录凭证，稍等 2~3 秒…"
        if self.platform == "baidu" and self.baidu_bdstoken:
            res["bdstoken"] = self.baidu_bdstoken
        self._sync_meta()
        res.update(page_url=self.page_url, page_title=self.page_title)
        return res

    def _ensure_baidu_session(self):
        """扫码成功后补一次「进网盘」，让服务器下发云盘会话 cookie。

        passport 登录只给 BDUSS；PANPSC / BDCLND 是 pan.baidu.com 自己下发的，
        页面没真正落到网盘之前不会有。缺了它们，无论 api/list 能不能用，
        gettemplatevariable（拿 bdstoken）一定返回 errno=-6，
        于是分享、转存全链路失败 —— 而界面还显示"登录成功"。

        只跳一次：status 接口每 1.8 秒轮询，反复导航会把用户眼前的画面晃瞎。
        """
        if self._baidu_moved:
            return
        self._baidu_moved = True
        self._do_baidu_session()

    def _do_baidu_session(self):
        """进一次网盘，等 PANPSC 真正落地。

        只等固定 4 秒是不够的：passport → pan 的 sso 跳转要走好几跳，
        PANPSC 由 pan.baidu.com 在会话校验通过后才下发。等不到就直接存，
        存下来的就是「BDUSS 有效、云盘会话空」的半残 Cookie ——
        界面上绿灯，转存时 bdstoken 拿不到，报 errno=-6。
        """
        try:
            self._page.goto("https://pan.baidu.com/disk/main",
                            wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:  # noqa: BLE001
            _xl_log(f"baidu: 进网盘失败 {exc}")
            return False
        got = False
        for _ in range(15):                       # 最多等 12 秒
            self._page.wait_for_timeout(800)
            cks = {c.get("name"): (c.get("value") or "") for c in self._ctx.cookies()}
            if cks.get("PANPSC"):
                got = True
                self._page.wait_for_timeout(1200)  # 让 bdstoken 那批 XHR 跑完
                break
        if got:
            self._grab_baidu_bdstoken()
        self._sync_meta()
        _xl_log(f"baidu: 补会话 PANPSC={'有' if got else '仍缺'} "
                f"bdstoken={mask_token(self.baidu_bdstoken)} url={self.page_url}")
        return got

    # 服务器端调 gettemplatevariable 已经拿不到 bdstoken（接口对非浏览器
    # 请求恒返 -6），但页面自己调用不受影响。趁会话活着，在页面上下文里
    # 取一份存进 .env，任务时的清理用它，不依赖服务器端网页会话。
    _BAIDU_BDSTOKEN_JS = """async () => {
      try {
        const r = await fetch('/api/gettemplatevariable?clienttype=0&app_id=250528' +
                              '&web=1&fields=%5B%22bdstoken%22%5D',
                              {credentials: 'include'});
        const j = await r.json();
        const hit = (j.result || []).find(x => x.field === 'bdstoken');
        return {errno: j.errno, bdstoken: (hit && hit.value) || ''};
      } catch (e) { return {errno: -999, bdstoken: String(e)}; }
    }"""

    def _grab_baidu_bdstoken(self):
        try:
            j = self._page.evaluate(self._BAIDU_BDSTOKEN_JS)
        except Exception as exc:  # noqa: BLE001
            _xl_log(f"baidu: 页面内取 bdstoken 失败 {exc}")
            return
        tok = str((j or {}).get("bdstoken") or "").strip()
        if (j or {}).get("errno") == 0 and tok:
            self.baidu_bdstoken = tok
        else:
            _xl_log(f"baidu: 页面内取 bdstoken errno={(j or {}).get('errno')}")

    # 迅雷的 token 只活在网页的 Vuex 内存里（localStorage 里没有），
    # 而且登录页和网盘主页是两个应用：得等它跳到主页，curUser 才有值。
    # 这里不按固定路径取，整块 dump 回来交给 parse_xunlei_state 递归找 ——
    # 迅雷改一次版式就得改一次 JS 的亏，之前吃过。
    _XL_STATE_JS = """() => {
      const dump = (o) => { try { return JSON.parse(JSON.stringify(o)); }
                            catch (e) { return null; } };
      const out = {url: location.href, users: null, user: null,
                   nuxtUsers: null, storage: {}};
      let st = null;
      try {
        const el = document.querySelector('#__nuxt');
        const vm = el && el.__vue__;
        if (vm && vm.$store) st = vm.$store.state;
        if (!st && window.$nuxt && window.$nuxt.$store) st = window.$nuxt.$store.state;
      } catch (e) {}
      try {
        if (st) { out.users = dump(st.users); out.user = dump(st.user); }
        if (!out.users && window.__NUXT__ && window.__NUXT__.state) {
          out.nuxtUsers = dump(window.__NUXT__.state.users);
        }
      } catch (e) {}
      try {
        // 迅雷的 SSO SDK 把 {access_token, refresh_token, expires_at} 整串
        // 塞进 localStorage，键名不固定（未必带 token 字样），所以全量扫，
        // 只截前 8000 字符控制体积。
        const grab = (store, tag) => {
          for (let i = 0; i < store.length; i++) {
            const k = store.key(i) || '';
            let v = '';
            try { v = String(store.getItem(k) || '').slice(0, 8000); } catch (e) {}
            if (v) out.storage[tag + k] = v;
          }
        };
        grab(localStorage, 'ls:');
        grab(sessionStorage, 'ss:');
      } catch (e) {}
      return out;
    }"""

    def _read_xunlei_state(self):
        """读页面里的登录态；读不到就返回空 dict，不抛。"""
        try:
            raw = self._page.evaluate(self._XL_STATE_JS)
        except Exception:  # noqa: BLE001
            return {}
        return raw if isinstance(raw, dict) else {}

    def _xunlei_store_tokens(self, cookies):
        """返回 (候选 refresh_token 列表, 页面是否已登录)。

        只在页面确实还没登录时，才替它走一步到网盘主页 —— 之前用 cookie
        名当门槛，新版登录不写那几个 cookie，于是永远不跳、永远读不到 token。
        """
        st = parse_xunlei_state(self._read_xunlei_state() or {})
        tok = st.get("refresh_token") or ""
        logged = bool(st.get("logged_in"))

        if not tok and not logged and self._xunlei_moves < 2:
            # 扫码成功后页面还停在登录页，这里替它走一步到网盘主页。
            # 超时压在 12 秒内 —— 上层指令只等 20 秒，超了会连累后面的轮询排队。
            if "/login" in (self.page_url or ""):
                self._xunlei_moves += 1
                self._xunlei_moved_at = time.time()
                try:
                    self._page.goto("https://pan.xunlei.com/", wait_until="domcontentloaded",
                                    timeout=12000)
                    self._page.wait_for_timeout(3000)
                    self._sync_meta()
                except Exception:  # noqa: BLE001
                    pass
                st = parse_xunlei_state(self._read_xunlei_state() or {})
                tok = st.get("refresh_token") or ""
                logged = bool(st.get("logged_in"))

        self._xunlei_logged = logged
        if tok:
            print("[login] xunlei 从页面 store/本地存储取到 refresh_token", flush=True)
            return [tok], True
        print(f"[login] xunlei 没读到 refresh_token（页面登录态={logged}）", flush=True)
        return [], logged


# ===========================================================================
# 会话管理
# ===========================================================================
_sessions = {}
_lock = threading.Lock()

ALLOWED_HOSTS = (
    "pan.quark.cn", "quark.cn", "pan.baidu.com", "passport.baidu.com",
    "wappass.baidu.com", "baidu.com", "pan.xunlei.com", "xunlei.com",
    "open-api-drive.xunlei.com", "login.xunlei.com",
)


def allowed_url(url):
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return False
    return any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


def _gc():
    """回收闲置超时或线程已退出的会话。浏览器进程占内存，不能一直留着。"""
    now = time.time()
    for sid, s in list(_sessions.items()):
        if s.touched_at + IDLE_TIMEOUT < now or s._closed:
            if not s._closed:
                s.close(wait=False)
            _sessions.pop(sid, None)


def start(platform):
    if platform not in LOGIN_ENTRY:
        raise ValueError(f"不支持的平台：{platform}")

    with _lock:
        _gc()
        # 同一平台复用已有会话，避免用户连点两下起两个浏览器
        for s in _sessions.values():
            if s.platform == platform and not s._closed:
                s.touch()
                return s
        if len(_sessions) >= MAX_SESSIONS:
            oldest = min(_sessions.values(), key=lambda x: x.touched_at)
            oldest.close(wait=False)
            _sessions.pop(oldest.sid, None)

    sid = base64.b16encode(os.urandom(8)).decode().lower()
    s = BrowserSession(sid, platform)
    if s.error:
        s.close(wait=False)
        raise RuntimeError(s.error)
    with _lock:
        _sessions[sid] = s
    return s


def get(sid):
    with _lock:
        s = _sessions.get(sid)
    if not s:
        raise KeyError("会话不存在或已过期，请重新打开登录窗口")
    s.touch()
    return s


def stop(sid):
    with _lock:
        s = _sessions.pop(sid, None)
    if s:
        s.close(wait=False)
    return True


def stop_all():
    with _lock:
        items = list(_sessions.values())
        _sessions.clear()
    for s in items:
        s.close(wait=False)
