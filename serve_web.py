#!/usr/bin/env python3
"""工作台生产入口（单进程、无 debug/reloader、PID 可控）。

用法:
    python serve_web.py          # 默认 5003
    PORT=8080 python serve_web.py

访问控制由 web/auth.py 处理：.env 里配了 WEB_PASSWORD 就要登录，
没配则只允许本机访问。
"""
import os

from web.web_app import app, ensure_dirs
from web import auth


def main():
    port = int(os.getenv("PORT", "5003"))
    ensure_dirs()
    auth.install(app)

    guarded = bool((os.getenv("WEB_PASSWORD") or "").strip())
    print("=" * 60)
    print(f"网盘转存工作台: http://0.0.0.0:{port}")
    if guarded:
        print(f"访问控制: 已启用（用户名 {(os.getenv('WEB_USER') or 'admin')}）")
    else:
        print("访问控制: 未设密码 —— 仅本机可访问，外网会收到 403")
        print("          要开放外网请先在 .env 设置 WEB_PASSWORD")
    print("=" * 60)

    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False,
            threaded=True)


if __name__ == "__main__":
    main()
