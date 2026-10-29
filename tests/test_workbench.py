"""工作台后端（web/workbench_api.py）的单元测试。

覆盖四类容易回归的行为：

1. ``env_write`` 按行替换——必须保留用户的注释与行序（交接文档"坑 5"）
2. 平台识别与资源池去重
3. 分享历史的解析与状态归类
4. 凭证白名单——不允许通过接口写任意 .env 项

所有写文件的测试都用临时目录 + mock，绝不碰真实 .env / 资源池。
"""
import json
import threading
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web import workbench_api as wb


class TestEnvWrite(unittest.TestCase):
    """env_write 是配置写入的唯一入口，写坏一次用户的注释就全没了。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = Path(self.tmp.name) / ".env"
        self.env.write_text(
            "# 网盘拉新唯一配置文件\n"
            "COUNT=5\n"
            "\n"
            "# ===== 夸克 =====\n"
            "QUARK_COOKIE=\n"
            "KUAKE_COOKIE=          # kuake CLI 专用\n"
            "WECHAT_WEBHOOK_URL=\n",
            encoding="utf-8",
        )
        self.patch = mock.patch.object(wb, "ENV_PATH", self.env)
        self.patch.start()
        self.env_patch = mock.patch.dict(os.environ, {}, clear=False)
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()
        self.patch.stop()
        self.tmp.cleanup()

    def test_preserve_comments_and_order(self):
        wb.env_write({"QUARK_COOKIE": "abc123"})
        text = self.env.read_text(encoding="utf-8")
        self.assertIn("# 网盘拉新唯一配置文件", text)
        self.assertIn("# ===== 夸克 =====", text)
        self.assertIn("QUARK_COOKIE=abc123", text)
        self.assertLess(text.index("COUNT=5"), text.index("QUARK_COOKIE=abc123"))

    def test_inline_comment_survives(self):
        wb.env_write({"KUAKE_COOKIE": "xyz"})
        text = self.env.read_text(encoding="utf-8")
        self.assertIn("KUAKE_COOKIE=xyz  # kuake CLI 专用", text)

    def test_no_duplicate_key(self):
        wb.env_write({"COUNT": "9"})
        text = self.env.read_text(encoding="utf-8")
        self.assertEqual(text.count("\nCOUNT="), 1)
        self.assertIn("COUNT=9", text)

    def test_append_missing_key(self):
        wb.env_write({"NEW_KEY": "v1"})
        lines = self.env.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[-1], "NEW_KEY=v1")

    def test_env_read_strips_comment(self):
        m = wb.env_read()
        self.assertEqual(m["KUAKE_COOKIE"], "")
        self.assertEqual(m["COUNT"], "5")
        self.assertEqual(m["WECHAT_WEBHOOK_URL"], "")

    def test_value_without_space_hash_kept(self):
        """Cookie 里偶尔带 #，只有 # 前是空白才当注释。"""
        wb.env_write({"BAIDU_COOKIE": "a#b"})
        self.assertEqual(wb.env_read()["BAIDU_COOKIE"], "a#b")


class TestDetectPlatform(unittest.TestCase):
    def test_four_kinds(self):
        self.assertEqual(wb.detect_platform("https://pan.quark.cn/s/abc"), "quark")
        self.assertEqual(wb.detect_platform("https://pan.uc.cn/s/abc"), "quark")
        self.assertEqual(wb.detect_platform("https://pan.xunlei.com/s/VP1A1?pwd=1"), "xunlei")
        self.assertEqual(wb.detect_platform("https://pan.baidu.com/s/1abc"), "baidu")
        self.assertEqual(wb.detect_platform("https://example.com/x"), "other")

    def test_empty(self):
        self.assertEqual(wb.detect_platform(""), "other")
        self.assertEqual(wb.detect_platform(None), "other")


class TestPool(unittest.TestCase):
    def test_dedupe_and_platform(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "资源池.json"
            p.write_text(json.dumps([
                {"index": 1, "note": "A", "url": "https://pan.quark.cn/s/1"},
                {"index": 2, "note": "A 重复", "url": "https://pan.quark.cn/s/1"},
                {"index": 3, "note": "B", "url": "https://pan.xunlei.com/s/2"},
            ], ensure_ascii=False), encoding="utf-8")
            with mock.patch.object(wb, "POOL_PATH", p):
                items = wb.load_pool(force=True)
            self.assertEqual(len(items), 2)
            self.assertEqual(items[0]["platform"], "quark")
            self.assertEqual(items[1]["platform"], "xunlei")

    def test_missing_file(self):
        with mock.patch.object(wb, "POOL_PATH", Path("/nonexistent/x.json")):
            self.assertEqual(wb.load_pool(force=True), [])


class TestHistory(unittest.TestCase):
    def test_parse_and_order(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "share_url_total.txt"
            p.write_text(
                "标题A  |  https://pan.quark.cn/s/a  |  DONE\n"
                "标题B  |  https://pan.xunlei.com/s/VB1  |  LINK_DEAD:分享不可用\n"
                "标题C  |  https://pan.baidu.com/s/1c  |  SAVE_FAIL\n",
                encoding="utf-8",
            )
            with mock.patch.object(wb, "HISTORY_PATH", p):
                rows = wb.load_history()
        self.assertEqual([r["note"] for r in rows], ["标题C", "标题B", "标题A"])
        self.assertEqual(rows[0]["kind"], "save_fail")
        self.assertEqual(rows[1]["kind"], "dead")
        self.assertEqual(rows[2]["kind"], "success")
        self.assertEqual(rows[1]["platform"], "xunlei")

    def test_missing_file(self):
        with mock.patch.object(wb, "HISTORY_PATH", Path("/nonexistent/h.txt")):
            self.assertEqual(wb.load_history(), [])


class TestTaskState(unittest.TestCase):
    def test_locked_reset_does_not_deadlock(self):
        """预览接口曾因持锁调用 _reset_task（Lock 不可重入）整站卡死。"""
        finished = []

        def work():
            with wb._TASK_LOCK:
                if not wb._task["running"]:
                    wb._reset_task_locked("quark", "kw")
            finished.append(True)

        t = threading.Thread(target=work, daemon=True)
        t.start()
        t.join(3)
        self.assertTrue(finished, "持锁路径卡住了，检查是否重复获取 _TASK_LOCK")

        snap = wb._task_snapshot()
        self.assertEqual(snap["platform"], "quark")
        self.assertEqual(snap["stage"], "idle")

    def test_snapshot_is_copy(self):
        wb._reset_task()
        snap = wb._task_snapshot()
        snap["logs"].append("脏数据")
        self.assertEqual(wb._task["logs"], [], "快照必须是拷贝，不能把内部列表暴露出去")


class TestCredentialGuard(unittest.TestCase):
    """接口只允许写白名单里的键。"""

    @classmethod
    def setUpClass(cls):
        from web.web_app import app
        cls.client = app.test_client()

    def test_reject_unknown_key(self):
        r = self.client.post("/api/workbench/credentials",
                             json={"values": {"EVIL_KEY": "x"}})
        self.assertEqual(r.status_code, 400)
        self.assertIn("不允许", r.get_json()["message"])

    def test_health_shape(self):
        r = self.client.get("/api/workbench/health")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertTrue(d["success"])
        for key in ("pansou", "creds", "pool", "history", "task"):
            self.assertIn(key, d)
        for pf in ("quark", "baidu", "xunlei"):
            self.assertIn(pf, d["creds"])

    def test_task_status_shape(self):
        r = self.client.get("/api/workbench/task/status")
        d = r.get_json()
        self.assertTrue(d["success"])
        for key in ("running", "stage", "logs", "result"):
            self.assertIn(key, d["task"])

    def test_pool_endpoint(self):
        r = self.client.get("/api/workbench/pool?page=1&size=5")
        d = r.get_json()
        self.assertTrue(d["success"])
        self.assertLessEqual(len(d["items"]), 5)
        self.assertGreaterEqual(d["pages"], 1)


class TestQuarkCookieSync(unittest.TestCase):
    """QUARK_COOKIE 与 KUAKE_COOKIE 必须一起变，包括清空。"""

    def test_clear_also_clears_kuake(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "web", "workbench_api.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn('"QUARK_COOKIE" in updates and updates["QUARK_COOKIE"]', src,
                         "清空夸克 Cookie 时 KUAKE_COOKIE 会残留旧值")
        self.assertIn('if "QUARK_COOKIE" in updates:', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
