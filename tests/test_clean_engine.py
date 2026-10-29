"""引流清理通用引擎测试。

重点保护三件事：
  1. 自己的引流文件夹不能被自己的规则删掉（排除名单）
  2. 带别人微信号的正课要「改名」而不是「删除」
  3. 目录递归、异常隔离、删除后不再下钻
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import clean_engine  # noqa: E402

CFG = {
    "enabled": True,
    "keywords": None,
    "extensions": None,
    "excludes": [],
    "max_small_file_bytes": 5 * 1024 * 1024,
    "aggressive_ext_check": False,
    "my_contact": "kds3566",
}


def _node(path, is_dir=False, size=1000):
    return {"path": path, "name": os.path.basename(path), "id": path,
            "size": size, "dir": is_dir}


class TestCleanTree(unittest.TestCase):
    def setUp(self):
        self.tmp = mock.patch("core.config.LOG_DIR", "/tmp/wb_test_logs")
        self.tmp.start()
        os.makedirs("/tmp/wb_test_logs", exist_ok=True)

    def tearDown(self):
        self.tmp.stop()

    def _run(self, tree, cfg=None, excludes=None):
        """tree: {路径: [子节点]}，自动按目录展开。"""
        deleted, renamed = [], []

        def ls_fn(node):
            return tree.get(node["path"], [])

        def delete_fn(node):
            deleted.append(node["path"])
            return True

        def rename_fn(node, new_name):
            renamed.append((node["path"], new_name))
            return True

        roots = [_node("/转存/x", is_dir=True)]
        res = clean_engine.clean_tree("测试", roots, cfg or CFG,
                                      ls_fn, delete_fn, rename_fn, excludes)
        return res, deleted, renamed

    def test_delete_by_keyword(self):
        tree = {"/转存/x": [_node("/转存/x/加微信领资料.jpg"), _node("/转存/x/第一课.mp4")]}
        res, deleted, renamed = self._run(tree)
        self.assertEqual(deleted, ["/转存/x/加微信领资料.jpg"])
        self.assertEqual(renamed, [])
        self.assertEqual(res["total"], 2)

    def test_exclude_protects_own_folder(self):
        tree = {"/转存/x": [_node("/转存/x/我的引流资料", is_dir=True)]}
        res, deleted, _ = self._run(tree, excludes=["我的引流资料"])
        self.assertEqual(deleted, [])

    # ── 空目录引流话术（2026-10-29 实测案例）──────────────────────
    def test_delete_empty_bait_dir(self):
        """原作者放的空文件夹「先保存，在观看避免被和谐」必须删掉。

        实测：转存「死亡回忆录」时它被原样分享出去，等于替对方导流。
        """
        tree = {"/转存/x": [_node("/转存/x/先保存，在观看避免被和谐", is_dir=True),
                            _node("/转存/x/死亡回忆录.pdf")]}
        res, deleted, _ = self._run(tree)
        self.assertEqual(deleted, ["/转存/x/先保存，在观看避免被和谐"])
        self.assertEqual(res["total"], 3)   # 目录本身也算检查一次

    def test_keep_nonempty_bait_named_dir(self):
        """非空目录名字再像也不删 —— 只按内容判定，防止误删正课。"""
        bait = "/转存/x/先保存，在观看避免被和谐"
        tree = {
            "/转存/x": [_node(bait, is_dir=True)],
            bait: [_node(bait + "/第1课.mp4")],
        }
        res, deleted, _ = self._run(tree)
        self.assertEqual(deleted, [])

    def test_keep_empty_normal_dir(self):
        """空目录但名字正常（如「课件」），不能删。"""
        tree = {"/转存/x": [_node("/转存/x/课件", is_dir=True)]}
        res, deleted, _ = self._run(tree)
        self.assertEqual(deleted, [])

    def test_empty_dir_with_contact_keyword_deleted(self):
        """空目录叫「加微信xxx」也算引流。"""
        tree = {"/转存/x": [_node("/转存/x/加微信领全套", is_dir=True)]}
        res, deleted, _ = self._run(tree)
        self.assertEqual(deleted, ["/转存/x/加微信领全套"])

    def test_empty_dir_homophone_bait(self):
        """「河蟹」是「和谐」的谐音，用来绕关键词，实测真实存在。

        案例：「若河蟹 没有了请看别的盘」—— 空目录，把人往他自己的盘引。
        """
        tree = {"/转存/x": [_node("/转存/x/若河蟹 没有了请看别的盘", is_dir=True)]}
        res, deleted, _ = self._run(tree)
        self.assertEqual(deleted, ["/转存/x/若河蟹 没有了请看别的盘"])

    def test_rename_contact_instead_of_delete(self):
        """正课带别人微信号 → 改名成自己的号，文件不能删。"""
        tree = {"/转存/x": [_node("/转存/x/43 《风筝》【更多好课加微信abc123】.pdf")]}
        res, deleted, renamed = self._run(tree)
        self.assertEqual(deleted, [])
        self.assertEqual(len(renamed), 1)
        old, new = renamed[0]
        self.assertIn("abc123", old)
        self.assertIn("kds3566", new)
        self.assertEqual(res["renamed"], [old])

    def test_recurse_into_subdir(self):
        tree = {
            "/转存/x": [_node("/转存/x/第1章", is_dir=True)],
            "/转存/x/第1章": [_node("/转存/x/第1章/扫码关注.png")],
        }
        res, deleted, _ = self._run(tree)
        self.assertEqual(deleted, ["/转存/x/第1章/扫码关注.png"])

    def test_deleted_dir_not_descended(self):
        tree = {
            "/转存/x": [_node("/转存/x/加群领福利", is_dir=True)],
            "/转存/x/加群领福利": [_node("/转存/x/加群领福利/a.mp4")],
        }
        res, deleted, _ = self._run(tree)
        self.assertEqual(deleted, ["/转存/x/加群领福利"])
        self.assertEqual(res["total"], 1, "被删目录内部的条目不应再扫描")

    def test_ls_exception_isolated(self):
        def ls_fn(node):
            if node["path"] == "/转存/x":
                return [_node("/转存/x/子", is_dir=True)]
            raise RuntimeError("接口抽风")

        res = clean_engine.clean_tree(
            "测试", [_node("/转存/x", is_dir=True)], CFG,
            ls_fn, lambda n: True, lambda n, x: True, None)
        self.assertEqual(res["total"], 1)

    def test_delete_failure_recorded_but_continues(self):
        tree = {"/转存/x": [_node("/转存/x/加微信.jpg"), _node("/转存/x/加qq群.jpg")]}
        deleted = []

        def delete_fn(node):
            if node["path"].endswith("加微信.jpg"):
                raise RuntimeError("权限不足")
            deleted.append(node["path"])
            return True

        def ls_fn(node):
            return tree.get(node["path"], [])

        res = clean_engine.clean_tree("测试", [_node("/转存/x", is_dir=True)], CFG,
                                      ls_fn, delete_fn, lambda n, x: True, None)
        self.assertEqual(deleted, ["/转存/x/加qq群.jpg"])
        self.assertEqual(res["deleted"], ["/转存/x/加qq群.jpg"])

    def test_record_file_written(self):
        tree = {"/转存/x": [_node("/转存/x/加微信.jpg")]}
        self._run(tree)
        files = [f for f in os.listdir("/tmp/wb_test_logs") if f.startswith("yinliu_clean_")]
        self.assertTrue(files)


class TestCleanCallOrder(unittest.TestCase):
    """清理必须在复制引流文件夹之前，否则自己的文件夹会被扫到。"""

    def _read(self, name):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "adapters", name), encoding="utf-8") as f:
            return f.read()

    def test_baidu_clean_before_insert(self):
        src = self._read("baidu.py")
        start = src.find("for item in dir_list")
        i_clean = src.find("_clean_yinliu_baidu(", start)
        i_insert = src.find("_insert_yinliu_baidu(", start)
        self.assertGreater(i_clean, 0, "百度缺少清理调用")
        self.assertLess(i_clean, i_insert, "百度：清理应在复制之前")

    def test_xunlei_clean_before_insert(self):
        src = self._read("xunlei.py")
        start = src.find("# 1.5) 先清理别人的引流")
        i_clean = src.find("_clean_yinliu_xunlei(", start)
        i_insert = src.find("_insert_yinliu_xunlei(", start)
        self.assertGreater(i_clean, 0, "迅雷缺少清理调用")
        self.assertLess(i_clean, i_insert, "迅雷：清理应在复制之前")

    def test_quark_clean_before_insert(self):
        src = self._read("quark.py")
        start = src.find("success_count = sum(")
        i_clean = src.find("clean_yinliu_after_save(", start)
        i_insert = src.find("_insert_yinliu_for_quark(", start)
        self.assertGreater(i_clean, 0, "夸克缺少清理调用")
        self.assertLess(i_clean, i_insert, "夸克：清理应在复制之前")

    def test_all_platforms_protect_own_folder(self):
        """三平台的自动白名单都必须包含自己的引流文件夹名。

        漏掉会导致重跑同一资源时，上次复制进去的文件夹被自己的清理规则删掉。
        """
        for name in ("quark.py", "baidu.py", "xunlei.py"):
            src = self._read(name)
            self.assertIn("yinliu_folder\") or \"\")", src.replace("'", '"'),
                          f"{name}: 未把引流文件夹名加入清理排除名单")


if __name__ == "__main__":
    unittest.main()
