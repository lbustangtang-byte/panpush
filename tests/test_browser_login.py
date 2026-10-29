# -*- coding: utf-8 -*-
"""浏览器内登录（core/browser_login.py）的单元测试。

这个功能最怕两件事，所以重点测这两件：

1. **凭证拼错** —— 把 passport.baidu.com 域上的同名 Cookie 也拼进去，
   百度 API 会因为 BDUSS 被覆盖而直接判定未登录。所以 cookie_string 必须按域过滤。
2. **误判登录成功** —— 只看到任意 cookie 就认为登录了，会把一堆垃圾写进 .env，
   然后用户跑任务时 401，还找不到原因。judge_login 必须认准特征字段。

真浏览器不在这里起（沙箱/CI 里可能没有 Chromium），
只测纯函数 + 用 mock 测路由结构。
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import browser_login as bl


def ck(name, value, domain=".baidu.com"):
    return {"name": name, "value": value, "domain": domain}


class TestCookieString(unittest.TestCase):
    def test_join_and_filter_by_domain(self):
        cookies = [
            ck("BDUSS", "aaa", ".baidu.com"),
            ck("STOKEN", "bbb", ".pan.baidu.com"),
            ck("BDUSS", "ccc", ".passport.baidu.com"),   # 同名的脏数据，必须被挡掉
            ck("other", "ddd", ".example.com"),
        ]
        s = bl.cookie_string(cookies, "baidu.com")
        self.assertIn("BDUSS=aaa", s)
        self.assertIn("STOKEN=bbb", s)
        self.assertNotIn("ccc", s)        # passport 域那份不能进来
        self.assertNotIn("example.com", s)
        self.assertNotIn("other=", s)

    def test_dedup_keeps_first(self):
        cookies = [ck("A", "1"), ck("A", "2")]
        self.assertEqual(bl.cookie_string(cookies, "baidu.com"), "A=1")

    def test_empty_and_none(self):
        self.assertEqual(bl.cookie_string([], "baidu.com"), "")
        self.assertEqual(bl.cookie_string(None, "baidu.com"), "")

    def test_names_filter(self):
        cookies = [ck("xlly_s", "x", ".xunlei.com"), ck("usrlogin", "y", ".xunlei.com"),
                   ck("junk", "z", ".xunlei.com")]
        s = bl.cookie_string(cookies, "xunlei.com", names=("xlly_s",))
        self.assertEqual(s, "xlly_s=x")

    def test_empty_value_kept_as_blank(self):
        # 空值也是有效 cookie（有些平台靠"存在但为空"判断），不能丢
        self.assertEqual(bl.cookie_string([ck("A", "")], "baidu.com"), "A=")


class TestExtractRefreshToken(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(
            bl.extract_refresh_token('{"token_type":"Bearer","refresh_token":"abc123456789012345"}'),
            "abc123456789012345")

    def test_nested_json(self):
        body = '{"code":0,"data":{"auth":{"refresh_token":"deep0000000000000"}}}'
        self.assertEqual(bl.extract_refresh_token(body), "deep0000000000000")

    def test_camel_case(self):
        self.assertEqual(bl.extract_refresh_token('{"refreshToken":"cam000000000000000"}'),
                         "cam000000000000000")

    def test_query_style_text(self):
        self.assertEqual(bl.extract_refresh_token("refresh_token=nested000000000001&x=1"),
                         "nested000000000001")

    def test_garbage(self):
        self.assertEqual(bl.extract_refresh_token(""), "")
        self.assertEqual(bl.extract_refresh_token("hello"), "")
        self.assertEqual(bl.extract_refresh_token('{"a":1}'), "")


class TestJudgeLogin(unittest.TestCase):
    def test_quark_ok(self):
        cookies = [ck("__puus", "p1", ".quark.cn"), ck("b-user-id", "u1", ".quark.cn")]
        r = bl.judge_login("quark", cookies)
        self.assertTrue(r["ok"])
        self.assertEqual(r["key"], "QUARK_COOKIE")
        self.assertIn("__puus=p1", r["value"])
        self.assertIn("b-user-id=u1", r["value"])

    def test_quark_not_logged_in(self):
        r = bl.judge_login("quark", [ck("__kp", "x", ".quark.cn")])
        self.assertFalse(r["ok"])
        self.assertFalse(r["logged_in"])

    def test_quark_ignores_baidu_cookies(self):
        # 浏览器里同时登录了百度，不能把百度的 cookie 当成夸克的
        r = bl.judge_login("quark", [ck("BDUSS", "b", ".baidu.com")])
        self.assertFalse(r["ok"])

    def test_baidu_ok(self):
        cookies = [ck("BDUSS", "bd1"), ck("STOKEN", "st1", ".pan.baidu.com")]
        r = bl.judge_login("baidu", cookies)
        self.assertTrue(r["ok"])
        self.assertEqual(r["key"], "BAIDU_COOKIE")
        self.assertIn("BDUSS=bd1", r["value"])
        self.assertIn("STOKEN=st1", r["value"])

    def test_baidu_without_bduss_is_not_logged_in(self):
        # 只有 BAIDUID 是游客态，绝不能当登录成功
        r = bl.judge_login("baidu", [ck("BAIDUID", "guest")])
        self.assertFalse(r["ok"])
        self.assertFalse(r["logged_in"])

    def test_xunlei_uses_captured_token(self):
        r = bl.judge_login("xunlei", [ck("xlly_s", "x", ".xunlei.com")],
                           captured_token="tok000000000000000")
        self.assertTrue(r["ok"])
        self.assertEqual(r["key"], "XUNLEI_REFRESH_TOKEN")
        self.assertEqual(r["value"], "tok000000000000000")

    def test_xunlei_logged_in_but_no_token(self):
        r = bl.judge_login("xunlei", [ck("xlly_s", "x", ".xunlei.com")])
        self.assertFalse(r["ok"])
        self.assertTrue(r["logged_in"])       # 登录了，但拿不到 refresh_token
        self.assertIn("refresh_token", r["message"])

    def test_xunlei_page_logged_in_without_any_cookie(self):
        # 新版迅雷登录后不写 xlly_s / usrlogin，只看 cookie 会永远判「未登录」
        r = bl.judge_login("xunlei", [ck("XLA_CI", "x", ".xunlei.com")],
                           page_logged=True)
        self.assertFalse(r["ok"])
        self.assertTrue(r["logged_in"])

    def test_xunlei_guest_stays_not_logged_in(self):
        r = bl.judge_login("xunlei", [ck("XLA_CI", "x", ".xunlei.com")])
        self.assertFalse(r["ok"])
        self.assertFalse(r["logged_in"])

    def test_xunlei_token_from_cookie(self):
        cookies = [ck("refresh_token", '{"refresh_token":"ck0000000000000001"}', ".xunlei.com")]
        r = bl.judge_login("xunlei", cookies)
        self.assertTrue(r["ok"])
        self.assertEqual(r["value"], "ck0000000000000001")


class TestParseXunleiState(unittest.TestCase):
    """页面 store 的结构改过版，解析要认得各种嵌套。"""

    def test_cur_user_with_snake_case(self):
        raw = {"users": {"curUser": {"userId": "123", "refresh_token": "r" * 32,
                                     "accessToken": "a" * 32}}}
        r = bl.parse_xunlei_state(raw)
        self.assertEqual(r["user_id"], "123")
        self.assertEqual(r["refresh_token"], "r" * 32)
        self.assertEqual(r["access_token"], "a" * 32)
        self.assertTrue(r["logged_in"])

    def test_cur_user_with_camel_case(self):
        raw = {"users": {"curUser": {"userId": 9, "refreshToken": "R" * 32}}}
        r = bl.parse_xunlei_state(raw)
        self.assertEqual(r["refresh_token"], "R" * 32)
        self.assertTrue(r["logged_in"])

    def test_deeply_nested(self):
        raw = {"a": {"b": [{"curUser": {"userId": "7", "refresh_token": "x" * 32}}]}}
        self.assertEqual(bl.parse_xunlei_state(raw)["refresh_token"], "x" * 32)

    def test_guest(self):
        r = bl.parse_xunlei_state({"users": {"curUser": {"userId": "0"}}})
        self.assertFalse(r["logged_in"])
        self.assertEqual(r["refresh_token"], "")

    def test_token_in_local_storage_only(self):
        raw = {"storage": {"oauth_sign_in_Xqp0kJBXWhwaTpB6":
                           '{"refresh_token":"%s"}' % ("z" * 32)}}
        r = bl.parse_xunlei_state(raw)
        self.assertEqual(r["refresh_token"], "z" * 32)

    def test_garbage_input(self):
        for bad in (None, {}, [], "x"):
            r = bl.parse_xunlei_state(bad)
            self.assertEqual(r["refresh_token"], "")


class TestUrlWhitelist(unittest.TestCase):
    def test_allowed(self):
        for u in ("https://pan.quark.cn/", "https://pan.baidu.com/disk/main",
                  "https://passport.baidu.com/v2/?login",
                  "https://pan.xunlei.com/login/"):
            self.assertTrue(bl.allowed_url(u), u)

    def test_rejected(self):
        for u in ("http://127.0.0.1:8080/admin", "https://example.com/",
                  "file:///etc/passwd", "https://pan.quark.cn.evil.com/", ""):
            self.assertFalse(bl.allowed_url(u), u)


class TestCredKey(unittest.TestCase):
    def test_every_platform_has_entry_and_rule(self):
        for pf in bl.LOGIN_ENTRY:
            self.assertIn(pf, bl.CRED_KEY)
            self.assertIn(pf, bl.COOKIE_RULE)


class TestRoutes(unittest.TestCase):
    """路由层：用 mock 顶掉真浏览器，只验接口契约。"""

    def setUp(self):
        from web.web_app import app
        self.client = app.test_client()

    def _fake_session(self, cred=None, refresh=None):
        s = mock.Mock()
        s.sid = "deadbeef"
        s.platform = "quark"
        s.page_url = "https://pan.quark.cn/"
        default_cred = {"ok": True, "logged_in": True, "key": "QUARK_COOKIE",
                        "value": "__puus=x; b-user-id=y", "message": "已拿到 Cookie",
                        "page_url": "https://pan.quark.cn/", "page_title": "夸克"}

        def _call(cmd, *a, **kw):
            if cmd == "shot":
                return b"\xff\xd8\xff"
            if cmd == "cred":
                return cred or default_cred
            if cmd == "refresh":
                # 默认模拟「页面内换票这条路走不通」，让用例继续走服务器端 probe
                return refresh or {"ok": False, "refresh_token": "",
                                   "message": "Failed to fetch"}
            if cmd in ("tokens", "resync"):
                return []
            return True

        s.call = mock.Mock(side_effect=_call)
        return s

    def test_start_rejects_unknown_platform(self):
        r = self.client.post("/api/workbench/browser/start",
                             json={"platform": "aliyun"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["success"])

    def test_start_ok(self):
        with mock.patch.object(bl, "start", return_value=self._fake_session()):
            r = self.client.post("/api/workbench/browser/start",
                                 json={"platform": "quark"})
        d = r.get_json()
        self.assertTrue(d["success"])
        self.assertEqual(d["sid"], "deadbeef")
        self.assertIn("viewport", d)

    def test_frame_returns_jpeg(self):
        with mock.patch.object(bl, "get", return_value=self._fake_session()):
            r = self.client.get("/api/workbench/browser/frame?sid=deadbeef")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "image/jpeg")
        self.assertTrue(r.data.startswith(b"\xff\xd8\xff"))

    def test_frame_unknown_session(self):
        with mock.patch.object(bl, "get", side_effect=KeyError("会话不存在")):
            r = self.client.get("/api/workbench/browser/frame?sid=nope")
        self.assertEqual(r.status_code, 404)

    def test_status_shape(self):
        with mock.patch.object(bl, "get", return_value=self._fake_session()):
            r = self.client.get("/api/workbench/browser/status?sid=deadbeef")
        d = r.get_json()
        self.assertTrue(d["alive"])
        self.assertTrue(d["logged_in"])
        self.assertTrue(d["ok"])
        self.assertEqual(d["key"], "QUARK_COOKIE")

    def test_click_forwards_rounded_coords(self):
        s = self._fake_session()
        with mock.patch.object(bl, "get", return_value=s):
            r = self.client.post("/api/workbench/browser/click",
                                 json={"sid": "deadbeef", "x": 12.7, "y": 40.2})
        self.assertTrue(r.get_json()["success"])
        s.call.assert_called_with("click", 12.7, 40.2)

    def test_save_writes_env(self):
        s = self._fake_session()
        with mock.patch.object(bl, "get", return_value=s), \
             mock.patch.object(bl, "stop") as stopper, \
             mock.patch.object(__import__("web.workbench_api", fromlist=["x"]),
                               "env_write") as ew, \
             mock.patch.object(__import__("web.workbench_api", fromlist=["x"]),
                               "verify_credential", return_value=(True, "夸克登录有效：某某")):
            r = self.client.post("/api/workbench/browser/save",
                                 json={"sid": "deadbeef"})
        d = r.get_json()
        self.assertTrue(d["success"])
        self.assertTrue(d["verified"])
        keys = {k for c in ew.call_args_list for k in c.args[0]}
        self.assertEqual(keys, {"QUARK_COOKIE", "KUAKE_COOKIE"})
        stopper.assert_called_once_with("deadbeef")

    def test_save_refuses_when_not_ok(self):
        s = self._fake_session(cred={"ok": False, "logged_in": False,
                                     "key": "QUARK_COOKIE", "value": "",
                                     "message": "尚未登录"})
        with mock.patch.object(bl, "get", return_value=s), \
             mock.patch.object(__import__("web.workbench_api", fromlist=["x"]),
                               "env_write") as ew:
            r = self.client.post("/api/workbench/browser/save",
                                 json={"sid": "deadbeef"})
        self.assertEqual(r.status_code, 400)
        ew.assert_not_called()

    # ---- 迅雷：抓到的 token 常常已被网页轮换，必须逐个试 ----
    def _xunlei_session(self, candidates):
        return self._fake_session(cred={
            "ok": True, "logged_in": True, "key": "XUNLEI_REFRESH_TOKEN",
            "value": candidates[0], "candidates": candidates,
            "message": "已拿到 refresh_token"})

    def test_save_xunlei_picks_first_working_candidate(self):
        wb = __import__("web.workbench_api", fromlist=["x"])
        s = self._xunlei_session(["dead01", "good02"])
        seen = []

        def probe(tok, timeout=25):
            seen.append(tok)
            return (tok == "good02", "newtok02" if tok == "good02" else "", "invalid refresh token")

        with mock.patch.object(bl, "get", return_value=s), \
             mock.patch.object(bl, "stop"), \
             mock.patch.object(wb, "probe_xunlei_token", side_effect=probe), \
             mock.patch.object(wb, "env_write") as ew, \
             mock.patch.object(wb, "save_cred_status") as scs, \
             mock.patch.object(wb, "verify_credential") as vf:
            r = self.client.post("/api/workbench/browser/save", json={"sid": "deadbeef"})
        d = r.get_json()
        self.assertTrue(d["success"])
        self.assertEqual(seen, ["dead01", "good02"])       # 第一个失败才试第二个
        self.assertEqual(ew.call_args.args[0]["XUNLEI_REFRESH_TOKEN"], "newtok02")
        self.assertFalse(any("KUAKE_COOKIE" in c.args[0] for c in ew.call_args_list))
        # 不能再验一遍：迅雷每验一次就轮换，刚写进去的值会立刻作废
        vf.assert_not_called()
        scs.assert_called_once()

    def test_save_xunlei_all_candidates_dead(self):
        wb = __import__("web.workbench_api", fromlist=["x"])
        s = self._xunlei_session(["dead01", "dead02"])
        with mock.patch.object(bl, "get", return_value=s), \
             mock.patch.object(bl, "stop") as stopper, \
             mock.patch.object(wb, "probe_xunlei_token", return_value=(False, "", "invalid")), \
             mock.patch.object(wb, "env_write") as ew:
            r = self.client.post("/api/workbench/browser/save", json={"sid": "deadbeef"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("换个二维码", r.get_json()["message"])
        ew.assert_not_called()                             # 坏 token 不许落盘
        stopper.assert_not_called()                        # 留会话，重扫不用再等冷启动

    def test_save_xunlei_prefers_inpage_refresh(self):
        """页面内换票成功就不该再打服务器端接口 —— 每多换一次就多废一个 token。"""
        wb = __import__("web.workbench_api", fromlist=["x"])
        s = self._xunlei_session(["fresh01"])
        def _call(cmd, *a, **kw):
            if cmd == "refresh":
                return {"ok": True, "refresh_token": "rotated01", "message": "ok"}
            if cmd == "tokens":
                return ["deep01"]        # 页面全量扫描还翻到一个，但页面内换票已成功
            return {"ok": True, "logged_in": True, "key": "XUNLEI_REFRESH_TOKEN",
                    "value": "fresh01", "candidates": ["fresh01"], "message": "已拿到"}

        s.call = mock.Mock(side_effect=_call)
        with mock.patch.object(bl, "get", return_value=s), \
             mock.patch.object(bl, "stop"), \
             mock.patch.object(wb, "probe_xunlei_token") as probe, \
             mock.patch.object(wb, "env_write") as ew, \
             mock.patch.object(wb, "save_cred_status"):
            r = self.client.post("/api/workbench/browser/save", json={"sid": "deadbeef"})
        self.assertTrue(r.get_json()["success"])
        probe.assert_not_called()
        self.assertEqual(ew.call_args.args[0]["XUNLEI_REFRESH_TOKEN"], "rotated01")

    def test_save_xunlei_uses_deep_scan_candidate(self):
        """固定路径取到的值是废的，但页面里还翻得出一个有效的 —— 要能救回来。"""
        wb = __import__("web.workbench_api", fromlist=["x"])
        s = self._fake_session(cred={"ok": True, "logged_in": True,
                                     "key": "XUNLEI_REFRESH_TOKEN", "value": "dead01",
                                     "candidates": ["dead01"], "message": "已拿到"})

        def _call(cmd, *a, **kw):
            if cmd == "cred":
                return {"ok": True, "logged_in": True, "key": "XUNLEI_REFRESH_TOKEN",
                        "value": "dead01", "candidates": ["dead01"], "message": "已拿到"}
            if cmd == "tokens":
                return ["good09"]
            if cmd == "resync":
                return []
            if cmd == "refresh":
                tok = a[0]
                return {"ok": tok == "good09",
                        "refresh_token": "rotated09" if tok == "good09" else "",
                        "message": "ok" if tok == "good09" else "invalid refresh token"}
            return {}

        s.call = mock.Mock(side_effect=_call)
        with mock.patch.object(bl, "get", return_value=s), \
             mock.patch.object(bl, "stop"), \
             mock.patch.object(wb, "env_write") as ew, \
             mock.patch.object(wb, "save_cred_status"):
            r = self.client.post("/api/workbench/browser/save", json={"sid": "deadbeef"})
        self.assertTrue(r.get_json()["success"])
        self.assertEqual(ew.call_args.args[0]["XUNLEI_REFRESH_TOKEN"], "rotated09")

    def test_save_xunlei_reports_no_token_honestly(self):
        """读不到 token 和 token 被拒是两回事，提示不能混着说。"""
        wb = __import__("web.workbench_api", fromlist=["x"])
        s = self._fake_session(cred={"ok": True, "logged_in": True,
                                     "key": "XUNLEI_REFRESH_TOKEN", "value": "",
                                     "candidates": [], "message": "已登录"})
        with mock.patch.object(bl, "get", return_value=s), \
             mock.patch.object(wb, "env_write") as ew:
            r = self.client.post("/api/workbench/browser/save", json={"sid": "deadbeef"})
        msg = r.get_json()["message"]
        self.assertEqual(r.status_code, 400)
        self.assertIn("没从页面里读到", msg)
        ew.assert_not_called()



if __name__ == "__main__":
    unittest.main()
