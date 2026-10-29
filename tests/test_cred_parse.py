"""智能粘贴识别测试：把各种复制格式收敛成 (平台, 配置项, 值)。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.cred_parse import parse  # noqa: E402

# 真实 Cookie 长度在几百到上千，这里造一个够长的样本，避免触发"偏短"提示
QUARK_COOKIE = (
    "__pus=4f8a2c1e9b7d3a5f6e0c8b2d4a9f1e3c; __puus=7b2e4d6f8a0c1e3d5f7a9b2c4e6d8f0a1b3c5d7e9f; "
    "b-user-id=9d8c7b6a5f4e3d2c1b0a; __kp=20261029; __uid=abc123def456ghi"
)
BAIDU_COOKIE = "BDUSS=5xY2ZkMzQ1NzY4OWFiY2RlZmdoaWo7; STOKEN=0e1d2c3b4a5968; BAIDUID=A1B2C3D4:FG=1"


class TestCookiePlain(unittest.TestCase):
    def test_quark(self):
        r = parse(QUARK_COOKIE)
        self.assertTrue(r["ok"])
        self.assertEqual(r["platform"], "quark")
        self.assertEqual(r["key"], "QUARK_COOKIE")
        self.assertEqual(r["value"], QUARK_COOKIE)
        self.assertFalse(r["warnings"], "完整 Cookie 不应有警告")

    def test_baidu(self):
        r = parse(BAIDU_COOKIE)
        self.assertTrue(r["ok"])
        self.assertEqual(r["platform"], "baidu")
        self.assertEqual(r["key"], "BAIDU_COOKIE")

    def test_cookie_prefix_stripped(self):
        r = parse("Cookie: " + QUARK_COOKIE)
        self.assertTrue(r["ok"])
        self.assertEqual(r["value"], QUARK_COOKIE)

    def test_wrapping_quotes_stripped(self):
        r = parse('"' + QUARK_COOKIE + '"')
        self.assertTrue(r["ok"])
        self.assertEqual(r["value"], QUARK_COOKIE)

    def test_trailing_whitespace_trimmed(self):
        r = parse("  " + QUARK_COOKIE + "   \n\n")
        self.assertTrue(r["ok"])
        self.assertEqual(r["value"], QUARK_COOKIE)

    def test_multiline_cookie_merged(self):
        r = parse("__pus=aaa111; __puus=bbb222\nb-user-id=ccc333")
        self.assertTrue(r["ok"])
        self.assertNotIn("\n", r["value"])
        self.assertIn(";", r["value"])


class TestCurl(unittest.TestCase):
    def test_bash_single_quote(self):
        txt = ("curl 'https://pan.quark.cn/1/clouddrive/file/sort?pr=ucpro' "
               "-H 'accept: */*' -H 'cookie: " + QUARK_COOKIE + "' "
               "-H 'referer: https://pan.quark.cn/'")
        r = parse(txt)
        self.assertTrue(r["ok"])
        self.assertEqual(r["platform"], "quark")
        self.assertEqual(r["value"], QUARK_COOKIE)

    def test_windows_cmd_caret(self):
        txt = ('curl "https://pan.baidu.com/api/list?dir=/" ^\n'
               '  -H "cookie: ' + BAIDU_COOKIE + '" ^\n'
               '  -H "user-agent: Mozilla/5.0"')
        r = parse(txt)
        self.assertTrue(r["ok"])
        self.assertEqual(r["platform"], "baidu")
        self.assertEqual(r["value"], BAIDU_COOKIE)

    def test_curl_without_cookie(self):
        txt = "curl 'https://pan.quark.cn/1/clouddrive/file/sort' -H 'accept: */*'"
        r = parse(txt)
        self.assertFalse(r["ok"])
        self.assertIn("没有 cookie", "".join(r["notes"]))


class TestHeaderBlock(unittest.TestCase):
    def test_multiline_headers(self):
        txt = ("accept: */*\n"
               "accept-language: zh-CN,zh;q=0.9\n"
               "cookie: " + QUARK_COOKIE + "\n"
               "referer: https://pan.quark.cn/")
        r = parse(txt)
        self.assertTrue(r["ok"])
        self.assertEqual(r["platform"], "quark")
        self.assertEqual(r["value"], QUARK_COOKIE)


class TestTokenAndWebhook(unittest.TestCase):
    def test_xunlei_json(self):
        txt = ('{"token_type":"Bearer","access_token":"at_123",'
               '"refresh_token":"rt_9f8e7d6c5b4a3210","expires_in":7200}')
        r = parse(txt)
        self.assertTrue(r["ok"])
        self.assertEqual(r["platform"], "xunlei")
        self.assertEqual(r["key"], "XUNLEI_REFRESH_TOKEN")
        self.assertEqual(r["value"], "rt_9f8e7d6c5b4a3210")

    def test_wechat_webhook(self):
        url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc-123-def"
        r = parse(url)
        self.assertTrue(r["ok"])
        self.assertEqual(r["platform"], "wechat")
        self.assertEqual(r["value"], url)

    def test_feishu_webhook(self):
        url = "https://open.feishu.cn/open-apis/bot/v2/hook/9a8b7c6d"
        r = parse(url)
        self.assertTrue(r["ok"])
        self.assertEqual(r["platform"], "feishu")

    def test_bare_token_low_confidence(self):
        r = parse("rt_9f8e7d6c5b4a3210abcdef123456")
        self.assertTrue(r["ok"])
        self.assertEqual(r["platform"], "xunlei")
        self.assertEqual(r["confidence"], "low")
        self.assertTrue(r["warnings"], "猜出来的平台必须提示")


class TestReject(unittest.TestCase):
    def test_empty(self):
        self.assertFalse(parse("   ")["ok"])

    def test_garbage(self):
        self.assertFalse(parse("这是一段随便写的文字，什么都不是")["ok"])

    def test_api_response_body(self):
        self.assertFalse(parse('{"errcode":0,"errmsg":"ok","access_token":"x"}')["ok"])

    def test_oversize(self):
        self.assertFalse(parse("a=b; " * 60000)["ok"])

    def test_unknown_cookie(self):
        r = parse("some_random_key=value; another_key=value2")
        self.assertFalse(r["ok"])
        self.assertIn("认不出平台", "".join(r["notes"]))


class TestWarnings(unittest.TestCase):
    def test_short_cookie_warns(self):
        r = parse("__pus=ab; __puus=cd")
        self.assertTrue(r["ok"])
        self.assertTrue(any("偏短" in w for w in r["warnings"]))

    def test_quark_missing_puus(self):
        r = parse("__pus=" + "a1b2c3d4e5f6" * 5 + "; b-user-id=xyz")
        self.assertTrue(r["ok"])
        self.assertTrue(any("__puus" in w for w in r["warnings"]))


if __name__ == "__main__":
    unittest.main()
