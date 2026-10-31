"""引流文件夹复制（三平台）单元测试。

覆盖：
  1. 路径规整与配置优先级（全局 / 平台覆盖）
  2. 插入计划的各种跳过条件
  3. insert() 的异常收口（引流失败不能影响主流程）
  4. 百度 copy_folder 的请求体与错误码处理
  5. 夸克 copy 在 CLI 缺失时的降级
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import yinliu_folder as yf  # noqa: E402


def _cfg(global_folder="", quark="", baidu="", xunlei="", enabled=True, skip=True):
    return {
        "yinliu_folder": {"enabled": enabled, "skip_exists": skip,
                          "default": global_folder},
        "quark": {"yinliu_folder": quark or global_folder},
        "baidu": {"yinliu_folder": baidu or global_folder},
        "xunlei": {"yinliu_folder": xunlei or global_folder},
    }


class TestPathNormalize(unittest.TestCase):
    def test_normalize(self):
        cases = {
            "": "",
            "/": "/",
            "引流": "/引流",
            "/引流/": "/引流",
            "//a//b//": "/a/b",
            " /a/b ": "/a/b",
            "a\\b": "/a/b",
        }
        for raw, want in cases.items():
            self.assertEqual(yf.normalize_remote_path(raw), want, raw)

    def test_folder_name(self):
        self.assertEqual(yf.folder_name("/转存资源/引流资料"), "引流资料")
        self.assertEqual(yf.folder_name("/引流资料/"), "引流资料")
        self.assertEqual(yf.folder_name(""), "")


class TestConfigPriority(unittest.TestCase):
    def test_global_fallback(self):
        c = _cfg(global_folder="/引流资料")
        for p in ("quark", "baidu", "xunlei"):
            self.assertEqual(yf.get_folder(c, p), "/引流资料")
            self.assertTrue(yf.is_enabled(c, p))

    def test_platform_override(self):
        c = _cfg(global_folder="/引流资料", quark="/我的引流")
        self.assertEqual(yf.get_folder(c, "quark"), "/我的引流")
        self.assertEqual(yf.get_folder(c, "baidu"), "/引流资料")

    def test_disabled(self):
        c = _cfg(global_folder="/引流资料", enabled=False)
        self.assertFalse(yf.is_enabled(c, "quark"))

    def test_no_folder(self):
        c = _cfg()
        self.assertFalse(yf.is_enabled(c, "baidu"))
        self.assertEqual(yf.get_folder(c, "baidu"), "")


class TestPlan(unittest.TestCase):
    def test_plan_ok(self):
        p = yf.plan(_cfg(global_folder="/引流/资料包"), "quark", "/转存资源/某课程")
        self.assertIsNotNone(p)
        self.assertEqual(p["src"], "/引流/资料包")
        self.assertEqual(p["dest"], "/转存资源/某课程")
        self.assertEqual(p["name"], "资料包")

    def test_skip_when_dest_is_src(self):
        """目标就是引流文件夹本身时跳过，避免自己复制自己。"""
        self.assertIsNone(
            yf.plan(_cfg(global_folder="/转存资源/引流"), "quark", "/转存资源/引流/"))

    def test_dest_may_be_src_parent(self):
        """复制的是子项，目标为源的父目录是合法的（不会产生同名壳）。"""
        p = yf.plan(_cfg(global_folder="/转存资源/引流"), "quark", "/转存资源")
        self.assertIsNotNone(p)

    def test_skip_when_no_dest(self):
        self.assertIsNone(yf.plan(_cfg(global_folder="/引流"), "quark", ""))

    def test_skip_when_not_configured(self):
        self.assertIsNone(yf.plan(_cfg(), "xunlei", "/转存资源/x"))


def _children(*names):
    return [{"name": n, "path": f"/引流/{n}", "dir": False} for n in names]


class TestInsert(unittest.TestCase):
    def test_not_configured_returns_skipped(self):
        calls = []
        res = yf.insert(_cfg(), "baidu", "/转存资源/x",
                        lambda s: [], lambda s, d: calls.append((s, d)) or {"ok": True})
        self.assertTrue(res.get("skipped"))
        self.assertEqual(calls, [])

    def test_copies_children_not_the_wrapper_folder(self):
        """核心语义：复制引流文件夹里面的子项，而不是文件夹本身。"""
        seen = []

        def copy_fn(src, dest):
            seen.append((src, dest))
            return {"ok": True, "skipped": False, "message": "已复制"}

        res = yf.insert(_cfg(global_folder="/替换引流文件"), "xunlei", "/推送/资源",
                        lambda s: _children("扫码加群.jpg", "更多浮力"), copy_fn)
        self.assertTrue(res["ok"])
        self.assertEqual(seen, [("/引流/扫码加群.jpg", "/推送/资源"),
                                ("/引流/更多浮力", "/推送/资源")])
        self.assertEqual(res["count"], 2)
        # 绝不能把 /替换引流文件 这个壳本身复制过去
        self.assertNotIn(("/替换引流文件", "/推送/资源"), seen)

    def test_empty_folder_skipped(self):
        called = []
        res = yf.insert(_cfg(global_folder="/引流"), "quark", "/资源",
                        lambda s: [], lambda s, d: called.append(s) or {"ok": True})
        self.assertTrue(res["skipped"])
        self.assertEqual(called, [])

    def test_partial_failure_still_counts_success(self):
        def copy_fn(src, dest):
            if src.endswith("坏的"):
                return {"ok": False, "message": "errno=2"}
            return {"ok": True}

        res = yf.insert(_cfg(global_folder="/引流"), "quark", "/资源",
                        lambda s: _children("好的", "坏的"), copy_fn)
        self.assertTrue(res["ok"])
        self.assertEqual(res["ok_count"], 1)
        self.assertEqual(len(res["errors"]), 1)

    def test_list_exception_is_swallowed(self):
        def boom(src):
            raise RuntimeError("网盘抽风")

        res = yf.insert(_cfg(global_folder="/引流"), "quark", "/转存资源/x",
                        boom, lambda s, d: {"ok": True})
        self.assertFalse(res["ok"])
        self.assertIn("网盘抽风", res["message"])

    def test_single_item_exception_does_not_stop_others(self):
        def copy_fn(src, dest):
            if src.endswith("炸"):
                raise RuntimeError("单项爆炸")
            return {"ok": True}

        res = yf.insert(_cfg(global_folder="/引流"), "baidu", "/资源",
                        lambda s: _children("炸", "好的"), copy_fn)
        self.assertEqual(res["ok_count"], 1)

    def test_failure_result(self):
        res = yf.insert(_cfg(global_folder="/引流"), "baidu", "/转存资源/x",
                        lambda s: _children("a"), lambda s, d: {"ok": False, "message": "errno=2"})
        self.assertFalse(res["ok"])


class TestBaiduCopy(unittest.TestCase):
    def _network(self, dest_entries, copy_errno=0):
        from adapters.baidu import Network
        n = Network()
        n.bdstoken = "tok"

        def fake_list(path):
            if path == "/转存资源/课程":
                return dest_entries
            return [{"server_filename": "引流", "isdir": 1, "fs_id": 42}]

        n.get_dir_list = fake_list
        resp = mock.Mock()
        resp.json.return_value = {"errno": copy_errno, "info": []}
        n.s = mock.Mock()
        n.s.post.return_value = resp
        return n

    def test_copy_calls_filemanager(self):
        n = self._network([])
        res = n.copy_folder("/引流", "/转存资源/课程")
        self.assertTrue(res["ok"], res)
        args, kwargs = n.s.post.call_args
        self.assertIn("/api/filemanager", kwargs["url"])
        self.assertEqual(kwargs["params"]["opera"], "copy")
        self.assertIn('"id": 42', kwargs["data"]["filelist"])
        self.assertIn('"dest": "/转存资源/课程"', kwargs["data"]["filelist"])

    def test_skip_when_exists(self):
        n = self._network([{"server_filename": "引流", "isdir": 1}])
        res = n.copy_folder("/引流", "/转存资源/课程")
        self.assertTrue(res["skipped"])
        n.s.post.assert_not_called()

    def test_errno_reported(self):
        n = self._network([], copy_errno=-10)
        res = n.copy_folder("/引流", "/转存资源/课程")
        self.assertFalse(res["ok"])
        self.assertIn("-10", res["message"])


class TestQuarkCopy(unittest.TestCase):
    def test_missing_cli(self):
        from adapters.quark import _kuake_copy_folder
        res = _kuake_copy_folder("", "/引流", "/转存资源/x")
        self.assertFalse(res["ok"])

    def test_entry_name_fallbacks(self):
        from adapters.quark import _kuake_entry_name
        self.assertEqual(_kuake_entry_name({"file_name": "a"}), "a")
        self.assertEqual(_kuake_entry_name({"name": "b"}), "b")
        self.assertEqual(_kuake_entry_name({"path": "/x/c"}), "c")
        self.assertEqual(_kuake_entry_name(None), "")


class TestCallOrder(unittest.TestCase):
    """复制必须发生在生成分享链接之前，否则分享里看不到引流内容。

    只做源码顺序断言——真实 API 调用需要凭证，跑不了。
    """

    def _read(self, name):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "adapters", name), encoding="utf-8") as f:
            return f.read()

    def test_quark_before_share(self):
        src = self._read("quark.py")
        start = src.find("for save_result in save_results")
        self.assertGreater(start, 0)
        i_copy = src.find("_insert_yinliu_for_quark(", start)
        i_share = src.find("manager.share_run(", start)
        self.assertGreater(i_copy, 0, "夸克缺少引流复制调用")
        self.assertGreater(i_share, 0)
        self.assertLess(i_copy, i_share, "夸克：复制应在分享之前")

    def test_baidu_before_share(self):
        src = self._read("baidu.py")
        start = src.find("for item in dir_list")
        i_copy = src.find("_insert_yinliu_baidu(", start)
        i_share = src.find('network.create_share(item["fs_id"]', start)
        self.assertGreater(i_copy, 0, "百度缺少引流复制调用")
        self.assertGreater(i_share, 0)
        self.assertLess(i_copy, i_share, "百度：复制应在分享之前")

    def test_xunlei_before_share(self):
        src = self._read("xunlei.py")
        start = src.find("# 2) 为转存结果创建自己的永久分享链接")
        i_copy = src.find("_insert_yinliu_xunlei(", start)
        i_share = src.find("client.create_share(fids=save_as_fids", start)
        self.assertGreater(i_copy, 0, "迅雷缺少引流复制调用")
        self.assertGreater(i_share, 0)
        self.assertLess(i_copy, i_share, "迅雷：复制应在分享之前")

    def test_quark_never_uploads_local_file(self):
        """本地引流已移除：夸克适配器里不该再有任何上传本地文件的调用。"""
        src = self._read("quark.py")
        self.assertNotIn("_upload_yinliu_files", src)
        self.assertNotIn("_upload_yinliu_pdf", src)
        self.assertNotIn("fallback_upload", src)


class TestNoLocalFallback(unittest.TestCase):
    """本地引流文件整体下线后的硬约束。

    以前「没配文件夹」会退回去上传 99999.pdf，现在这条路不存在了：
    没配就当作不做引流。留三条断言，防止哪天有人又把兜底加回来。
    """

    def test_no_fallback_api(self):
        self.assertFalse(hasattr(yf, "fallback_upload"),
                         "fallback_upload 已删除，不要再加回来")

    def test_get_yinliu_files_always_empty(self):
        from core.config import get_yinliu_files
        self.assertEqual(get_yinliu_files({"quark": {}}), [])
        self.assertEqual(get_yinliu_files({"quark": {"yinliu_files": ["a.pdf"]}}), [])

    def test_no_local_asset_in_project(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        assets = os.path.join(root, "assets")
        if not os.path.isdir(assets):
            return
        left = [f for f in os.listdir(assets) if f.lower().endswith((".pdf", ".mp4"))]
        self.assertEqual(left, [], f"assets 里不该再有本地引流素材: {left}")

    def test_insert_skipped_when_disabled(self):
        """关闭状态下 insert 直接跳过，不会去调 list_fn/copy_fn。"""
        called = []
        r = yf.insert(_cfg(enabled=False, quark="/引流"), "quark", "/资源",
                      lambda s: called.append(("list", s)) or _children("a"),
                      lambda s, d: called.append((s, d)) or {"ok": True})
        self.assertTrue(r.get("skipped"))
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
