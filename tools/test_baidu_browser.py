"""实验：真实浏览器指纹能否绕过百度分享页风控。

当前 requests 打开分享页被 302 到 passport（机房 IP 风控）。
本实验用 Chromium（无头）注入 .env 的百度 cookie，打开分享页：
  1. 若正常渲染 → 从页面提取 shareid/uk/fs_id → 浏览器通道可行
  2. 若要提码 → 页面内 fetch POST /share/verify 拿 randsk → 种 BDCLND → 刷新
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright
from core.browser_login import LAUNCH_ARGS, UA, VIEWPORT, find_browser
from core.config import load_config

SHARE_URL = "https://pan.baidu.com/s/1B38d_d-cbrZX8xRPnBs6Zg"
PWD = "8888"


def parse_cookies(raw):
    """把 .env 的 cookie 串转成 playwright cookie 列表。"""
    out = []
    for part in raw.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        out.append({"name": k.strip(), "value": v.strip(),
                    "domain": ".baidu.com", "path": "/"})
    return out


def main():
    config = load_config()
    raw = config["baidu"]["cookie"]
    exe = find_browser()
    with sync_playwright() as p:
        kwargs = dict(headless=True, args=LAUNCH_ARGS)
        if exe:
            kwargs["executable_path"] = exe
        browser = p.chromium.launch(**kwargs)
        ctx = browser.new_context(viewport=VIEWPORT, locale="zh-CN",
                                  user_agent=UA, device_scale_factor=1)
        ctx.add_cookies(parse_cookies(raw))
        page = ctx.new_page()
        page.goto(SHARE_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)
        url_now = page.url
        title = page.title()
        print("step1 url:", url_now[:100])
        print("step1 title:", title[:60])
        if "passport" in url_now or "login" in url_now:
            print("RESULT: 浏览器也被 302 passport —— 此路不通")
            browser.close()
            return

        # 若是提码页，页面内 fetch verify
        if "请输入提取码" in title or "提取码" in page.content()[:3000]:
            print("需要提码，页面内 fetch verify ...")
            randsk = page.evaluate(
                """async (pwd) => {
                    const r = await fetch('/share/verify?surl=B38d_d-cbrZX8xRPnBs6Zg'
                        + '&t=' + Date.now() + '&channel=chunlei&web=1&clienttype=0', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/x-www-form-urlencoded',
                                  'X-Requested-With': 'XMLHttpRequest'},
                        body: 'pwd=' + pwd + '&vcode=&vcode_str=',
                        credentials: 'include',
                    });
                    const j = await r.json();
                    return j.randsk || ('errno=' + j.errno);
                }""", PWD)
            print("verify:", str(randsk)[:24])
            if isinstance(randsk, str) and not randsk.startswith("errno="):
                ctx.add_cookies([{"name": "BDCLND", "value": randsk,
                                  "domain": ".baidu.com", "path": "/"}])
                page.goto(SHARE_URL, wait_until="domcontentloaded", timeout=30000)
                time.sleep(3)
                print("step2 title:", page.title()[:60])

        html = page.content()
        import re
        m_sid = re.search(r'"shareid":(\d+?)["\s,]', html)
        m_uk = re.search(r'"share_uk":"(\d+?)"', html)
        fsids = re.findall(r'"fs_id":(\d+?)["\s,]', html)
        names = re.findall(r'"server_filename":"(.+?)","', html)
        print("shareid:", m_sid.group(1) if m_sid else None)
        print("share_uk:", m_uk.group(1) if m_uk else None)
        print("fs_id 数:", len(fsids), "样例文件:", names[:3] if names else None)
        # 顺手验证页面内 share/list 是否可用
        lst = page.evaluate(
            """async () => {
                const r = await fetch('/share/list?shareid=' + window.__sid
                    + '&uk=' + window.__uk + '&root=1&page=1&num=100&web=1'
                    + '&clienttype=0&channel=chunlei',
                    {method: 'POST', credentials: 'include'});
                const j = await r.json();
                return {errno: j.errno, n: (j.list || []).length,
                        names: (j.list || []).slice(0, 5).map(x => x.server_filename)};
            }""") if m_sid and m_uk else None
        if lst:
            print("页面内 share/list:", json.dumps(lst, ensure_ascii=False)[:200])
        browser.close()


if __name__ == "__main__":
    main()
