"""从「随便粘一段东西」里认出这是哪个平台的什么凭证。

用户从浏览器开发者工具复制出来的内容至少有这么几种形状：

    1. 纯 Cookie 串     __pus=xx; __puus=yy; b-user-id=zz
    2. Bash cURL        curl 'https://pan.quark.cn/...' -H 'cookie: ...'
    3. Windows cURL     curl "https://..." -H "cookie: ..." ^
    4. 多行请求头块     cookie: ...\\nuser-agent: ...\\nreferer: ...
    5. JSON 响应        {"token_type":"Bearer","refresh_token":"..."}
    6. Webhook URL      https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...

这里把它们统一收敛成 (platform, key, value)。纯函数，不碰网络，方便单测。
"""
import json
import re

MAX_INPUT = 200_000

# 平台 → (环境变量名, 中文名, 凭证类型)
PLATFORM_META = {
    "quark": ("QUARK_COOKIE", "夸克网盘", "cookie"),
    "baidu": ("BAIDU_COOKIE", "百度网盘", "cookie"),
    "xunlei": ("XUNLEI_REFRESH_TOKEN", "迅雷网盘", "token"),
    "wechat": ("WECHAT_WEBHOOK_URL", "企业微信", "webhook"),
    "feishu": ("FEISHU_WEBHOOK_URL", "飞书", "webhook"),
}

# host 片段 → 平台（长的放前面，避免被短片段抢先匹配）
_HOST_RULES = [
    ("qyapi.weixin.qq.com", "wechat"),
    ("open.feishu.cn", "feishu"),
    ("pan.quark.cn", "quark"),
    ("quark.cn", "quark"),
    ("pan.baidu.com", "baidu"),
    ("baidu.com", "baidu"),
    ("pan.xunlei.com", "xunlei"),
    ("xunlei.com", "xunlei"),
    ("feishu.cn", "feishu"),
]

# Cookie 字段特征 → 平台（URL 认不出来时的兜底）
_COOKIE_FEATURES = [
    ("quark", ("__puus=", "__pus=", "b-user-id=", "__kp=")),
    ("baidu", ("BDUSS=", "STOKEN=", "BAIDUID=")),
    ("xunlei", ("xlly_s=", "usrlogin=", "xlly_"))
]

_CURL_HEADER = re.compile(r"(?:-H|--header)\s+(['\"])(.*?)\1", re.S)
_HEADER_LINE = re.compile(r"^\s*([A-Za-z0-9\-_]+)\s*:\s*(.+?)\s*$")
_URL_IN_TEXT = re.compile(r"https?://[^\s'\"\\]+")
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), None)


def _clean(text):
    return (text or "").translate(_ZERO_WIDTH).strip()


def _host(text):
    m = _URL_IN_TEXT.search(text or "")
    if not m:
        return ""
    url = m.group(0)
    url = re.sub(r"^https?://", "", url)
    return url.split("/")[0].split("?")[0].lower()


def _platform_from_host(host):
    for frag, pf in _HOST_RULES:
        if frag in host:
            return pf
    return None


def _platform_from_cookie(value):
    upper = value or ""
    for pf, marks in _COOKIE_FEATURES:
        if any(m in upper or m.lower() in upper.lower() for m in marks):
            return pf
    return None


def _strip_wrapping(value):
    """去掉包裹引号、行尾续行符、以及误带的 `cookie:` 前缀。"""
    v = _clean(value)
    v = re.sub(r"^\s*cookie\s*:\s*", "", v, flags=re.I)
    v = v.rstrip("\\^").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        v = v[1:-1]
    return v.strip()


def _looks_like_cookie(value):
    if not value or "\n" in value:
        return False
    if "=" not in value:
        return False
    # 至少要有一个 key=value 的形状
    return bool(re.search(r"[^\s;=]+=[^;]*", value))


def _extract_cookie_from_headers(text):
    """从 cURL 或请求头块里抽 cookie 值。"""
    # 1) cURL 的 -H 'cookie: ...'
    for _, payload in _CURL_HEADER.findall(text):
        head = _HEADER_LINE.match(payload)
        if head and head.group(1).lower() == "cookie":
            return _strip_wrapping(head.group(2))
    # 2) 直接以 cookie: 开头的行
    for line in text.splitlines():
        line = line.rstrip("\\^").strip()
        m = re.match(r"^cookie\s*:\s*(.+)$", line, re.I)
        if m:
            return _strip_wrapping(m.group(1))
    return ""


def _fmt_len(v):
    return f"{len(v)} 字符"


def _result(platform, value, kind, confidence, notes, warnings):
    meta = PLATFORM_META.get(platform)
    return {
        "ok": bool(meta and value),
        "platform": platform,
        "platform_name": meta[1] if meta else "",
        "key": meta[0] if meta else "",
        "value": value,
        "kind": kind,
        "confidence": confidence,
        "notes": notes,
        "warnings": warnings,
        "length": len(value),
        "preview": (value[:14] + "…" + value[-6:]) if len(value) > 24 else value,
    }


def _fail(message, hint=""):
    return {"ok": False, "platform": None, "platform_name": "", "key": "",
            "value": "", "kind": "unknown", "confidence": "low",
            "notes": [message] + ([hint] if hint else []),
            "warnings": [], "length": 0, "preview": ""}


def parse(text):
    """把粘贴内容解析成可写入的凭证。"""
    raw = _clean(text)
    if not raw:
        return _fail("粘贴的内容是空的")
    if len(raw) > MAX_INPUT:
        return _fail("内容太长（超过 200KB），请只复制 Cookie 那一行")

    notes, warnings = [], []

    # ---------- 1) JSON（迅雷 token / 某些接口返回体）----------
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            token = obj.get("refresh_token") or (obj.get("data") or {}).get("refresh_token")
            if token:
                notes.append("从 JSON 里取到 refresh_token")
                return _result("xunlei", str(token).strip(), "token", "high", notes, warnings)
            # 可能是企微/飞书发消息返回体，不是凭证
            if "errcode" in obj or "code" in obj:
                return _fail("这是一段接口返回结果，不是凭证",
                             "请复制请求头里的 cookie，或 auth/token 响应里的 refresh_token")

    # ---------- 2) Webhook 地址 ----------
    if re.match(r"^https?://", raw):
        pf = _platform_from_host(_host(raw))
        if pf in ("wechat", "feishu"):
            notes.append("识别为群机器人 Webhook 地址")
            return _result(pf, raw.split()[0], "webhook", "high", notes, warnings)
        return _fail("这是个网址，但不是企业微信/飞书的 Webhook",
                     "Webhook 应形如 https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...")

    # ---------- 3) cURL 或多行请求头 ----------
    is_curl = raw.lower().lstrip().startswith("curl") or "-h " in raw.lower() \
        or "--header" in raw.lower()
    has_header_lines = bool(re.search(r"^\s*cookie\s*:", raw, re.I | re.M))

    if is_curl or has_header_lines:
        cookie = _extract_cookie_from_headers(raw)
        if cookie:
            if not _looks_like_cookie(cookie):
                return _fail("在请求头里找到了 cookie，但内容不像 Cookie 串",
                             f"取到的开头是：{cookie[:40]}")
            pf = _platform_from_host(_host(raw)) or _platform_from_cookie(cookie)
            if pf not in ("quark", "baidu", "xunlei"):
                return _fail("找到了 Cookie，但认不出是哪个网盘",
                             "请确认复制的是 pan.quark.cn / pan.baidu.com / pan.xunlei.com 的请求")
            notes.append("从 cURL/请求头里提取")
            if is_curl:
                notes.append("已剥离命令行参数，只保留 cookie 值")
            _check_cookie_quality(pf, cookie, warnings)
            return _result(pf, cookie, "cookie", "high", notes, warnings)
        if is_curl:
            return _fail("这段 cURL 里没有 cookie",
                         "这条请求可能不需要登录，换一条请求再复制")

    # ---------- 4) 纯 Cookie 串 ----------
    value = _strip_wrapping(raw)
    if "\n" in value:
        # 有些浏览器复制出来是每行一个 k=v，合并成标准 Cookie 串
        parts = [p.strip().rstrip(";") for p in value.splitlines() if p.strip()]
        value = "; ".join(parts)
        notes.append("多行内容已合并为一行")
    if _looks_like_cookie(value):
        pf = _platform_from_cookie(value)
        if not pf:
            return _fail("看着像 Cookie，但认不出平台",
                         "夸克要含 __pus / __puus；百度要含 BDUSS；迅雷要含 xlly_s")
        notes.append("按 Cookie 字段特征识别")
        _check_cookie_quality(pf, value, warnings)
        return _result(pf, value, "cookie", "medium", notes, warnings)

    # ---------- 5) 裸 token（迅雷）----------
    if 20 <= len(value) <= 512 and re.fullmatch(r"[A-Za-z0-9._\-]+", value):
        notes.append("像是一段 token，按迅雷 refresh_token 处理")
        warnings.append("无法从内容确认平台，请核对后保存")
        return _result("xunlei", value, "token", "low", notes, warnings)

    return _fail("没认出这是什么",
                 "支持：Cookie 整行、cURL 命令、请求头块、refresh_token 的 JSON、Webhook 地址")


def _check_cookie_quality(platform, cookie, warnings):
    """给出「可能不完整」的提示，但不阻断保存。"""
    if len(cookie) < 40:
        warnings.append("长度偏短，可能只复制了一部分")
    if platform == "quark" and "__puus" not in cookie:
        warnings.append("没看到 __puus 字段，夸克通常需要它，可能不完整")
    if platform == "baidu" and "BDUSS" not in cookie:
        warnings.append("没看到 BDUSS 字段，百度通常需要它，可能不完整")
    if "\n" in cookie:
        warnings.append("内容里还有换行，可能导致请求失败")
    if cookie != cookie.strip():
        warnings.append("首尾有空白字符，已自动去掉")
