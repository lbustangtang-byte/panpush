"""凭证在线校验的判定逻辑。

这里出错的代价很隐蔽：凭证其实是好的，但界面报"已失效"，用户就会反复重登，
最后把好凭证换掉。所以每个平台的"什么算成功"必须钉死。

真实返回样例（2026-10 实测）：
  夸克  {"success":true,"code":"OK","data":{"nickname":"..."}}   ← code 是字符串，不是 0
  百度  errno=0 + netdisk_name                                    ← gettemplatevariable 恒返回 -6
  迅雷  invalid_grant / 4126 invalid refresh token
"""
import unittest
from unittest import mock

import web.workbench_api as wb


def _resp(payload, status=200):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = payload
    return r


class TestQuarkVerify(unittest.TestCase):
    def test_code_is_string_ok(self):
        """夸克把 code 写成字符串 "OK"，只认数字 0 会把有效凭证判成失效。"""
        with mock.patch("httpx.get", return_value=_resp(
                {"success": True, "code": "OK", "data": {"nickname": "不拘小节的木棉花"}})):
            ok, msg = wb.verify_credential("QUARK_COOKIE", "ctoken=x")
        self.assertTrue(ok)
        self.assertIn("不拘小节的木棉花", msg)

    def test_legacy_numeric_zero(self):
        with mock.patch("httpx.get", return_value=_resp(
                {"code": 0, "data": {"nickname": "老接口"}})):
            ok, _ = wb.verify_credential("QUARK_COOKIE", "ctoken=x")
        self.assertTrue(ok)

    def test_expired(self):
        with mock.patch("httpx.get", return_value=_resp(
                {"success": False, "code": 31001, "message": "require login"})):
            ok, msg = wb.verify_credential("QUARK_COOKIE", "ctoken=x")
        self.assertFalse(ok)
        self.assertIn("31001", msg)

    def test_ok_without_nickname_is_not_trusted(self):
        with mock.patch("httpx.get", return_value=_resp(
                {"success": True, "code": "OK", "data": {}})):
            ok, _ = wb.verify_credential("QUARK_COOKIE", "ctoken=x")
        self.assertFalse(ok)

    def test_kuake_cookie_uses_same_rule(self):
        with mock.patch("httpx.get", return_value=_resp(
                {"success": True, "code": "OK", "data": {"nickname": "n"}})):
            ok, _ = wb.verify_credential("KUAKE_COOKIE", "ctoken=x")
        self.assertTrue(ok)


class TestBaiduVerify(unittest.TestCase):
    """2026-10-29 起只查 uinfo 一级。

    原来的两级判定（uinfo + gettemplatevariable 拿 bdstoken）作废：
    ① gettemplatevariable 对服务器端请求恒返 -6，接口事实废弃；
    ② 网页会话（PANPSC）由百度风控管理，扫码后半小时内就可能被作废，
       服务器端测不出「当下是否可用」（实测 Cookie 放回真实浏览器也被踢）；
    ③ 转存/分享走 xpan 接口只认 BDUSS，不依赖 bdstoken。
    """

    def test_uinfo_ok_is_green(self):
        with mock.patch("httpx.get", side_effect=[
                _resp({"errno": 0, "netdisk_name": "是陈陈诶", "baidu_name": "布衣女子w"}),
        ]) as g:
            ok, msg = wb.verify_credential("BAIDU_COOKIE", "BDUSS=x")
        self.assertTrue(ok)
        self.assertIn("是陈陈诶", msg)
        self.assertEqual(len(g.call_args_list), 1)      # 只打一个接口
        self.assertIn("xpan/nas", g.call_args_list[0].args[0])

    def test_no_gettemplatevariable_call(self):
        """绝不能再从服务器端打网页层接口 —— 测不准，还可能触发风控。"""
        with mock.patch("httpx.get", side_effect=[
                _resp({"errno": 0, "netdisk_name": "n"}),
        ]) as g:
            wb.verify_credential("BAIDU_COOKIE", "BDUSS=x; PANPSC=y")
        self.assertTrue(all("gettemplatevariable" not in c.args[0]
                            for c in g.call_args_list))

    def test_pansc_present_mentions_cleanup(self):
        ok, msg = None, None
        with mock.patch("httpx.get", side_effect=[
                _resp({"errno": 0, "netdisk_name": "是陈陈诶"}),
        ]):
            ok, msg = wb.verify_credential("BAIDU_COOKIE", "BDUSS=x; PANPSC=y")
        self.assertTrue(ok)

    def test_falls_back_to_baidu_name(self):
        with mock.patch("httpx.get", side_effect=[
                _resp({"errno": 0, "baidu_name": "布衣女子w"}),
        ]):
            ok, msg = wb.verify_credential("BAIDU_COOKIE", "BDUSS=x")
        self.assertTrue(ok)
        self.assertIn("布衣女子w", msg)

    def test_expired(self):
        with mock.patch("httpx.get", return_value=_resp({"errno": -6})):
            ok, msg = wb.verify_credential("BAIDU_COOKIE", "BDUSS=x")
        self.assertFalse(ok)
        self.assertIn("-6", msg)


class TestXunleiProbe(unittest.TestCase):
    def setUp(self):
        # verify_credential 现在会记录结果，别让它写真实的 data/cred_status.json
        import os, tempfile
        from pathlib import Path
        fd, self._path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self._path)
        self._p = mock.patch.object(wb, "CRED_STATUS_PATH", Path(self._path))
        self._p.start()

    def tearDown(self):
        import os
        self._p.stop()
        try:
            os.unlink(self._path)
        except OSError:
            pass

    def test_empty_token(self):
        ok, tok, why = wb.probe_xunlei_token("")
        self.assertFalse(ok)
        self.assertEqual(tok, "")
        self.assertIn("空", why)

    def test_refresh_error_is_failure(self):
        from adapters.xunlei import XunleiError
        with mock.patch("adapters.xunlei.XunleiClient.ensure_tokens",
                        side_effect=XunleiError("刷新 access_token 失败: invalid refresh token")):
            ok, tok, why = wb.probe_xunlei_token("a1.dead")
        self.assertFalse(ok)
        self.assertEqual(tok, "")
        self.assertIn("invalid refresh token", why)

    def test_success_returns_rotated_token(self):
        client = mock.Mock()
        client._refresh_token = "a1.rotated"
        client._user_name = "陈陈"
        with mock.patch("adapters.xunlei.XunleiClient", return_value=client):
            ok, tok, who = wb.probe_xunlei_token("a1.old")
        self.assertTrue(ok)
        self.assertEqual(tok, "a1.rotated")     # 轮换后的新值，旧的会被作废
        self.assertEqual(who, "陈陈")
        client.ensure_tokens.assert_called_once()

    def test_verify_uses_probe(self):
        with mock.patch.object(wb, "probe_xunlei_token", return_value=(True, "n", "陈陈")):
            ok, msg = wb.verify_credential("XUNLEI_REFRESH_TOKEN", "a1.x")
        self.assertTrue(ok)
        self.assertIn("陈陈", msg)

    def test_verify_failure_message(self):
        with mock.patch.object(wb, "probe_xunlei_token", return_value=(False, "", "invalid")):
            ok, msg = wb.verify_credential("XUNLEI_REFRESH_TOKEN", "a1.x")
        self.assertFalse(ok)
        self.assertIn("invalid", msg)


class TestHealthCredStates(unittest.TestCase):
    """界面上的灯必须说实话：填了 ≠ 能用，验过才算。"""

    def setUp(self):
        from web.web_app import app
        self.client = app.test_client()

    def _health(self, env, status):
        with mock.patch.object(wb, "env_read", return_value=env), \
             mock.patch.object(wb, "load_cred_status", return_value=status), \
             mock.patch.object(wb, "load_config", return_value={}), \
             mock.patch.object(wb, "load_pool", return_value=[]), \
             mock.patch("httpx.get", side_effect=RuntimeError("no pansou")):
            return self.client.get("/api/workbench/health").get_json()

    Q = "ctoken=abcdefghijklmn"

    def test_verified_true(self):
        d = self._health({"QUARK_COOKIE": self.Q},
                         {"QUARK_COOKIE": {"ok": True, "msg": "夸克登录有效：小明",
                                           "fp": wb._cred_fp(self.Q), "at": 1}})
        c = d["creds"]["quark"]["cookie"]
        self.assertTrue(c["ok"])
        self.assertIs(c["verified"], True)
        self.assertIn("小明", c["message"])

    def test_verified_false(self):
        d = self._health({"QUARK_COOKIE": self.Q},
                         {"QUARK_COOKIE": {"ok": False, "msg": "code=31001",
                                           "fp": wb._cred_fp(self.Q), "at": 1}})
        self.assertIs(d["creds"]["quark"]["cookie"]["verified"], False)

    def test_never_verified_is_none(self):
        d = self._health({"QUARK_COOKIE": self.Q}, {})
        self.assertIsNone(d["creds"]["quark"]["cookie"]["verified"])

    def test_value_changed_invalidates_status(self):
        """换了 Cookie 就不能沿用上一次的结论。"""
        d = self._health({"QUARK_COOKIE": self.Q},
                         {"QUARK_COOKIE": {"ok": True, "msg": "老的", "fp": "deadbeef0000", "at": 1}})
        c = d["creds"]["quark"]["cookie"]
        self.assertIsNone(c["verified"])
        self.assertEqual(c["message"], "")

    def test_empty_value_is_off(self):
        d = self._health({"QUARK_COOKIE": ""}, {})
        c = d["creds"]["quark"]["cookie"]
        self.assertFalse(c["ok"])
        self.assertIsNone(c["verified"])


class TestVerifyRoute(unittest.TestCase):
    def setUp(self):
        from web.web_app import app
        self.client = app.test_client()

    def test_unknown_key_rejected(self):
        r = self.client.post("/api/workbench/credentials/verify", json={"key": "PATH"})
        self.assertEqual(r.status_code, 400)

    def test_not_configured(self):
        with mock.patch.object(wb, "env_read", return_value={}):
            r = self.client.post("/api/workbench/credentials/verify",
                                 json={"key": "BAIDU_COOKIE"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("还没配置", r.get_json()["message"])

    def test_verifies_current_env_value(self):
        with mock.patch.object(wb, "env_read", return_value={"BAIDU_COOKIE": "BDUSS=x"}), \
             mock.patch.object(wb, "verify_credential", return_value=(True, "百度登录有效：陈")) as v:
            d = self.client.post("/api/workbench/credentials/verify",
                                 json={"key": "BAIDU_COOKIE"}).get_json()
        self.assertTrue(d["verified"])
        v.assert_called_once_with("BAIDU_COOKIE", "BDUSS=x")

    def test_xunlei_rotated_token_written_back(self):
        """迅雷校验会轮换 token，新的必须回写，否则 .env 里留的是作废值。"""
        from adapters.xunlei import TokenStore
        with mock.patch.object(wb, "env_read", return_value={"XUNLEI_REFRESH_TOKEN": "a1.old"}), \
             mock.patch.object(wb, "verify_credential", return_value=(True, "迅雷登录有效")), \
             mock.patch.object(TokenStore, "load", return_value="a1.new"), \
             mock.patch.object(wb, "env_write") as ew, \
             mock.patch.object(wb, "save_cred_status"):
            self.client.post("/api/workbench/credentials/verify",
                             json={"key": "XUNLEI_REFRESH_TOKEN"})
        ew.assert_called_once_with({"XUNLEI_REFRESH_TOKEN": "a1.new"})


class TestXunleiStoreRead(unittest.TestCase):
    """迅雷的 token 只在页面 store 里；登录态也不再靠 cookie 名判断。"""

    def _sess(self, page, moves=0, url="https://pan.xunlei.com/"):
        from core import browser_login as bl
        s = object.__new__(bl.BrowserSession)
        s.platform = "xunlei"
        s._page = page
        s.page_url = url
        s.page_title = ""
        s._xunlei_moves = moves
        s._xunlei_moved_at = 0.0
        s._xunlei_logged = False
        page.url = url
        page.title.return_value = ""
        return s

    def test_reads_token_even_without_legacy_cookie(self):
        """新版登录不写 xlly_s / usrlogin，不能因为没这几个 cookie 就不读 store。"""
        page = mock.Mock()
        page.evaluate.return_value = {"users": {"curUser": {
            "userId": "123", "refreshToken": "a1.refresh"}}}
        s = self._sess(page)
        toks, logged = s._xunlei_store_tokens([{"name": "XLA_CI"}])
        self.assertEqual(toks, ["a1.refresh"])
        self.assertTrue(logged)
        page.goto.assert_not_called()

    def test_guest_navigates_to_main_once(self):
        page = mock.Mock()
        page.evaluate.side_effect = [{"users": {"curUser": {"userId": "0"}}},
                                     {"users": {"curUser": {
                                         "userId": "123", "refreshToken": "a1.new"}}}]
        s = self._sess(page, url="https://pan.xunlei.com/login/")
        toks, logged = s._xunlei_store_tokens([{"name": "usrlogin"}])
        self.assertEqual(toks, ["a1.new"])
        self.assertTrue(logged)
        page.goto.assert_called_once()

    def test_does_not_navigate_twice(self):
        """status 每 1.8 秒轮询一次，绝不能每次都跳转。"""
        page = mock.Mock()
        page.evaluate.return_value = {"users": {"curUser": {"userId": "0"}}}
        s = self._sess(page, moves=2, url="https://pan.xunlei.com/login/")
        toks, logged = s._xunlei_store_tokens([{"name": "xlly_s"}])
        self.assertEqual(toks, [])
        self.assertFalse(logged)
        page.goto.assert_not_called()

    def test_does_not_navigate_when_already_on_main(self):
        page = mock.Mock()
        page.evaluate.return_value = {"users": {"curUser": {"userId": "0"}}}
        s = self._sess(page)
        s._xunlei_store_tokens([])
        page.goto.assert_not_called()

    def test_logged_in_without_token_reports_logged(self):
        page = mock.Mock()
        page.evaluate.return_value = {"users": {"curUser": {"userId": "123"}}}
        s = self._sess(page)
        toks, logged = s._xunlei_store_tokens([])
        self.assertEqual(toks, [])
        self.assertTrue(logged)          # 已登录但没令牌，界面要给提示而不是「尚未登录」


if __name__ == "__main__":
    unittest.main()
