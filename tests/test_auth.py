"""工作台访问控制测试：表单登录 + 默认不裸奔。"""
import os
import sys
import unittest

from flask import Flask

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web import auth  # noqa: E402


class AuthTestBase(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.get(k) for k in ("WEB_USER", "WEB_PASSWORD")}
        self.app = Flask(__name__, template_folder=None)
        self.app.add_url_rule("/", "index", lambda: "ok")
        self.app.add_url_rule("/api/x", "apix", lambda: {"ok": True})
        self.c = self.app.test_client()
        auth.install(self.app)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def get(self, path="/", addr="127.0.0.1"):
        return self.c.get(path, environ_base={"REMOTE_ADDR": addr})

    def login(self, pwd, addr="203.0.113.9"):
        return self.c.post("/login", data={"password": pwd},
                           environ_base={"REMOTE_ADDR": addr},
                           follow_redirects=False)


class TestNoPassword(AuthTestBase):
    """未设密码：只放行本机，公网直接 403。"""

    def setUp(self):
        super().setUp()
        os.environ.pop("WEB_PASSWORD", None)
        os.environ.pop("WEB_USER", None)

    def test_loopback_allowed(self):
        self.assertEqual(self.get("/").status_code, 200)

    def test_public_blocked(self):
        r = self.get("/", "203.0.113.9")
        self.assertEqual(r.status_code, 403)
        self.assertIn(b"WEB_PASSWORD", r.data, "应提示怎么配置密码")

    def test_empty_password_treated_as_unset(self):
        os.environ["WEB_PASSWORD"] = "   "
        self.assertEqual(self.get("/", "203.0.113.9").status_code, 403)


class TestLoginFlow(AuthTestBase):
    """设了密码：走表单登录，Session Cookie 维持会话。"""

    def setUp(self):
        super().setUp()
        os.environ["WEB_USER"] = "admin"
        os.environ["WEB_PASSWORD"] = "s3cret"

    def test_page_redirects_to_login(self):
        r = self.get("/", "203.0.113.9")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])

    def test_api_returns_json_401(self):
        r = self.get("/api/x", "203.0.113.9")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.get_json().get("code"), "unauthorized")

    def test_login_page_accessible(self):
        self.assertEqual(self.get("/login", "203.0.113.9").status_code, 200)

    def test_wrong_password(self):
        self.assertEqual(self.login("wrong").status_code, 401)

    def test_login_then_access(self):
        r = self.login("s3cret")
        self.assertEqual(r.status_code, 302)
        # 同一个 client 自动带上 session cookie
        self.assertEqual(self.get("/").status_code, 200)
        self.assertEqual(self.get("/api/x").status_code, 200)

    def test_logout_clears_session(self):
        self.login("s3cret")
        self.assertEqual(self.get("/").status_code, 200)
        self.c.get("/logout", environ_base={"REMOTE_ADDR": "203.0.113.9"})
        self.assertEqual(self.get("/", "203.0.113.9").status_code, 302)

    def test_unicode_password(self):
        """中文密码不能抛 TypeError（hmac 需要 bytes）。"""
        os.environ["WEB_PASSWORD"] = "中文密码"
        self.assertEqual(self.login("中文密码").status_code, 302)
        self.assertEqual(self.get("/").status_code, 200)

    def test_open_redirect_blocked(self):
        """next 参数不能被用来跳到外站。"""
        r = self.c.post("/login?next=//evil.com", data={"password": "s3cret"},
                        environ_base={"REMOTE_ADDR": "203.0.113.9"})
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("evil.com", r.headers["Location"])

    def test_basic_auth_header_still_rejected(self):
        """Basic 认证不再被接受（网关会剥掉该头），必须走表单。"""
        import base64
        raw = base64.b64encode(b"admin:s3cret").decode()
        r = self.c.get("/", headers={"Authorization": f"Basic {raw}"},
                       environ_base={"REMOTE_ADDR": "203.0.113.9"})
        self.assertEqual(r.status_code, 302)


if __name__ == "__main__":
    unittest.main()
