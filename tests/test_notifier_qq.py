"""QQ 群（OneBot）推送通道的单元测试。

不碰真网络：全部 mock requests.post，只验协议格式、开关逻辑与失败分支。
"""
import unittest
from unittest import mock

from core import notifier as nt


def _resp(payload, status=200):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


def _cfg(**kw):
    qq = {"enabled": True, "api_url": "http://127.0.0.1:3000",
          "group_id": "123456789", "access_token": ""}
    qq.update(kw)
    return {"wechat": {"webhook_url": ""}, "feishu": {"enabled": False,
                                                      "webhook_url": ""},
            "qq": qq}


SHARES = [{"note": "示例资源", "share_url": "https://pan.quark.cn/s/abc"}]


class TestQqTarget(unittest.TestCase):
    def test_disabled_returns_empty(self):
        url, group, hdr = nt.qq_target(_cfg(enabled=False))
        self.assertEqual((url, group, hdr), ("", "", {}))

    def test_missing_group_returns_empty(self):
        url, group, _ = nt.qq_target(_cfg(group_id=""))
        self.assertEqual((url, group), ("", ""))

    def test_missing_url_returns_empty(self):
        url, _, _ = nt.qq_target(_cfg(api_url=""))
        self.assertEqual(url, "")

    def test_url_gets_send_group_msg_suffix(self):
        url, group, _ = nt.qq_target(_cfg())
        self.assertEqual(url, "http://127.0.0.1:3000/send_group_msg")
        self.assertEqual(group, "123456789")

    def test_full_endpoint_not_doubled(self):
        url, _, _ = nt.qq_target(
            _cfg(api_url="http://127.0.0.1:3000/send_group_msg"))
        self.assertEqual(url, "http://127.0.0.1:3000/send_group_msg")

    def test_trailing_slash_trimmed(self):
        url, _, _ = nt.qq_target(_cfg(api_url="http://127.0.0.1:3000/"))
        self.assertEqual(url, "http://127.0.0.1:3000/send_group_msg")

    def test_access_token_becomes_bearer(self):
        _, _, hdr = nt.qq_target(_cfg(access_token="secret-token"))
        self.assertEqual(hdr["Authorization"], "Bearer secret-token")

    def test_no_token_no_header(self):
        _, _, hdr = nt.qq_target(_cfg(access_token=""))
        self.assertEqual(hdr, {})


class TestPayload(unittest.TestCase):
    def test_payload_shape(self):
        p = nt._onebot_payload("hello", "123456789")
        self.assertEqual(p["group_id"], "123456789")
        self.assertTrue(p["auto_escape"])          # 别让 [标题] 被当 CQ 码解析
        self.assertEqual(p["message"],
                         [{"type": "text", "data": {"text": "hello"}}])


class TestResponseCheck(unittest.TestCase):
    def test_retcode_zero_ok(self):
        nt._check_onebot_response(_resp({"status": "ok", "retcode": 0}))

    def test_nonzero_retcode_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            nt._check_onebot_response(_resp({"retcode": 100, "message": "群不存在"}))
        self.assertIn("100", str(ctx.exception))

    def test_non_json_raises(self):
        r = _resp(None)
        r.json.side_effect = ValueError("not json")
        with self.assertRaises(RuntimeError):
            nt._check_onebot_response(r)


class TestSendQq(unittest.TestCase):
    def test_success_posts_once(self):
        with mock.patch.object(nt.requests, "post",
                               return_value=_resp({"retcode": 0})) as post:
            res = nt.send_qq_notification(_cfg(), SHARES, kw="国漫")
        self.assertTrue(res["success"])
        self.assertEqual(res["chunks"], 1)
        url = post.call_args[0][0]
        self.assertTrue(url.endswith("/send_group_msg"))
        body = post.call_args[1]["json"]
        self.assertEqual(body["group_id"], "123456789")
        text = body["message"][0]["data"]["text"]
        self.assertIn("示例资源", text)
        self.assertIn("【国漫搜索结果 · 1条】", text)

    def test_failure_reports_error(self):
        with mock.patch.object(nt.requests, "post",
                               return_value=_resp({"retcode": 100})):
            res = nt.send_qq_notification(_cfg(), SHARES)
        self.assertFalse(res["success"])

    def test_network_error_does_not_raise(self):
        with mock.patch.object(nt.requests, "post",
                               side_effect=RuntimeError("connection refused")):
            res = nt.send_qq_notification(_cfg(), SHARES)
        self.assertFalse(res["success"])

    def test_disabled_returns_message(self):
        res = nt.send_qq_notification(_cfg(enabled=False), SHARES)
        self.assertFalse(res["success"])
        self.assertIn("未启用", res["message"])

    def test_no_shares_short_circuits(self):
        res = nt.send_qq_notification(_cfg(), [])
        self.assertFalse(res["success"])

    def test_long_text_is_chunked(self):
        shares = [{"note": "长" * 100, "share_url": "https://pan.quark.cn/s/x"}
                  for _ in range(60)]
        with mock.patch.object(nt.requests, "post",
                               return_value=_resp({"retcode": 0})) as post:
            res = nt.send_qq_notification(_cfg(), shares)
        self.assertGreater(res["chunks"], 1)
        self.assertEqual(post.call_count, res["chunks"])


class TestFanout(unittest.TestCase):
    def test_send_notifications_includes_qq(self):
        with mock.patch.object(nt.requests, "post",
                               return_value=_resp({"retcode": 0})):
            out = nt.send_notifications(_cfg(), SHARES)
        self.assertIsNotNone(out["qq"])
        self.assertTrue(out["qq"]["success"])

    def test_qq_none_when_disabled(self):
        out = nt.send_notifications(_cfg(enabled=False), SHARES)
        self.assertIsNone(out["qq"])

    def test_raw_text_goes_to_qq(self):
        with mock.patch.object(nt.requests, "post",
                               return_value=_resp({"retcode": 0})) as post:
            out = nt.send_raw_text(_cfg(), "入口：https://pan.quark.cn/s/abc")
        self.assertIsNotNone(out["qq"])
        self.assertIn("入口", post.call_args[1]["json"]["message"][0]["data"]["text"])

    def test_wechat_and_qq_both_sent(self):
        cfg = _cfg()
        cfg["wechat"] = {"webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x"}
        with mock.patch.object(nt.requests, "post",
                               return_value=_resp({"retcode": 0, "errcode": 0})) as post:
            out = nt.send_notifications(cfg, SHARES)
        self.assertEqual(post.call_count, 2)
        self.assertIsNotNone(out["wechat"])
        self.assertIsNotNone(out["qq"])


if __name__ == "__main__":
    unittest.main()
