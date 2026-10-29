"""工作台访问控制（表单登录 + Cookie 会话，默认不裸奔）。

为什么不用 HTTP Basic Auth：
    实测「发布为应用」的网关会剥掉 `Authorization` 请求头，
    导致 Basic 认证永远失败（应用侧收到的 request.authorization 是 None）。
    因此改用表单登录，凭 Session Cookie 维持会话 —— Cookie 能正常透传。

规则：
  1. 配了 `WEB_PASSWORD` → 所有请求都要先登录
  2. 没配密码 → 只允许本机回环地址访问，外网一律 403 并提示怎么配

反向代理说明：前面挂了网关时 `request.remote_addr` 是代理的内网地址。
那种情况下必须配置 `WEB_PASSWORD`，不要依赖"只允许本机"这一层。
"""
import hmac
import os
import secrets

from flask import (Response, jsonify, redirect, render_template_string,
                   request, session, url_for)

REALM = "netdisk2weixin"
SESSION_DAYS = 7


def _env(key, default=""):
    return (os.environ.get(key) or default).strip()


def _eq(a, b):
    """定长比较，避免通过响应时间猜密码；bytes 比较兼容中文。"""
    return hmac.compare_digest(str(a).encode("utf-8"),
                               str(b).encode("utf-8"))


def _is_loopback(addr):
    if not addr:
        return False
    return addr in ("::1", "localhost") or addr.startswith("127.")


def _secret_key():
    """会话签名密钥，落盘持久化，避免重启后所有人被登出。"""
    path = os.path.join("data", ".web_secret")
    try:
        if os.path.exists(path):
            key = open(path, encoding="utf-8").read().strip()
            if key:
                return key
        os.makedirs("data", exist_ok=True)
        key = secrets.token_hex(32)
        with open(path, "w", encoding="utf-8") as f:
            f.write(key)
        os.chmod(path, 0o600)
        return key
    except OSError:
        # 磁盘不可写时退回内存密钥：能跑，但重启后需重新登录
        return secrets.token_hex(32)


LOGIN_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>登录 · 网盘转存工作台</title>
<style>
:root{--bg:#f4f6fb;--card:#fff;--line:#e2e8f2;--ink:#1a2540;--sub:#6b7891;--brand:#2c46b8}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:var(--bg);font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;color:var(--ink)}
.box{width:100%;max-width:360px;margin:24px;background:var(--card);border:1px solid var(--line);
border-radius:14px;padding:32px 28px;box-shadow:0 8px 30px rgba(26,37,64,.08)}
h1{margin:0 0 4px;font-size:19px}
p.sub{margin:0 0 22px;color:var(--sub);font-size:13px}
label{display:block;font-size:12px;color:var(--sub);margin-bottom:6px}
input{width:100%;padding:11px 12px;border:1px solid var(--line);border-radius:9px;
font-size:14px;outline:none;background:#fbfcfe}
input:focus{border-color:var(--brand);background:#fff}
button{width:100%;margin-top:16px;padding:11px;border:0;border-radius:9px;
background:var(--brand);color:#fff;font-size:14px;font-weight:600;cursor:pointer}
button:hover{filter:brightness(1.08)}
.err{margin-top:12px;padding:9px 11px;border-radius:8px;background:#fef3f2;
color:#b42318;font-size:13px;border:1px solid #fecdca}
.tip{margin-top:18px;color:var(--sub);font-size:12px;line-height:1.7}
</style></head><body>
<form class="box" method="post">
  <h1>网盘转存工作台</h1>
  <p class="sub">请输入访问密码</p>
  <label for="password">密码</label>
  <input id="password" name="password" type="password" autofocus
         autocomplete="current-password" required>
  <button type="submit">进入</button>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <div class="tip">密码在服务器的 <code>data/.web_password</code> 文件里。</div>
</form></body></html>"""

NO_PASSWORD_HINT = (
    "403 工作台未设置访问密码，仅允许本机访问。\n\n"
    "要开放外网访问，请在 .env 中设置：\n"
    "    WEB_USER=admin\n"
    "    WEB_PASSWORD=你的密码\n"
    "然后重启：bash stop.sh && bash start.sh\n"
)


def install(app):
    """把访问控制挂到 Flask app 上。"""
    app.secret_key = _secret_key()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * SESSION_DAYS,
    )

    def _safe_next(target):
        return target if target.startswith("/") and not target.startswith("//") \
            else "/"

    @app.route("/login", methods=["GET", "POST"])
    def _login():  # noqa: ANN202
        nxt = _safe_next(request.args.get("next", "/"))
        if request.method == "POST":
            if _eq(request.form.get("password", ""), _env("WEB_PASSWORD")):
                session.clear()
                session["auth"] = True
                session.permanent = True
                return redirect(nxt)
            return render_template_string(LOGIN_HTML, error="密码错误"), 401
        return render_template_string(LOGIN_HTML, error="")

    @app.route("/logout")
    def _logout():  # noqa: ANN202
        session.clear()
        return redirect(url_for("_login"))

    @app.before_request
    def _guard():  # noqa: ANN202
        if request.endpoint in ("_login", "_logout", "static"):
            return None

        password = _env("WEB_PASSWORD")

        if not password:
            if _is_loopback(request.remote_addr):
                return None
            return Response(NO_PASSWORD_HINT, 403,
                            mimetype="text/plain; charset=utf-8")

        if session.get("auth"):
            return None

        # 接口返回 JSON，避免前端把登录页当数据解析
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "code": "unauthorized",
                            "message": "请先登录"}), 401

        return redirect(url_for("_login", next=request.full_path
                                if request.query_string else request.path))

    return app
