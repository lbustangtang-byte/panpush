"""百度选择性转存（转存前引流过滤）测试。

背景：服务器 IP 对百度网页会话风控严，转存后删除引流经常 -6 失败。
方案：转存前先列分享内容（/share/list），把引流条目挡在 fsidlist 外，
让引流文件根本不进网盘 —— 从根上不再依赖被风控的删除接口。

红线（与 test_yinliu_clean 一致）：
  - 禁止把引流文件原样转存进网盘后再指望事后删除
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters import baidu as bd


def make_config():
    return {
        "clean_yinliu": {"enabled": True},
        "baidu": {"yinliu_folder": "追剧神器", "cookie": "BDUSS=abc",
                  "count": 1, "target_folder": "转存资源"},
        "target_folder": "转存资源",
        "count": 1,
        "cache_expire_hours": 24,
        "yinliu_folder": {"skip_exists": True},
    }


class FakeNetwork:
    """只实现选择性转存用到的原语。"""

    def __init__(self, share_root, share_children=None, dir_lists=None,
                 share_list_errno=None):
        # share_root: 分享根条目列表；share_children: {分享内路径: 条目列表}
        self.share_root = share_root
        self.share_children = share_children or {}
        self.share_list_errno = share_list_errno
        self.dir_lists = dir_lists or {}   # 网盘路径 -> 条目列表
        self.created_dirs = []
        self.transfers = []                # (params_list, folder_name)
        self.bdstoken = "t"
        self.headers = {"Cookie": "BDUSS=abc"}

    @staticmethod
    def _entry(name, fs_id, isdir=0, size=100, path=None):
        return {"server_filename": name, "fs_id": fs_id, "isdir": isdir,
                "size": size, "path": path or f"/{name}"}

    def get_bdstoken(self):
        return "t"

    def fetch_share_meta(self, surl):
        return None  # 默认走旧分享页通道

    def verify_pass_code(self, link_url, pass_code):
        return "randsk"

    def get_transfer_params(self, url):
        return "ok"

    def share_list(self, shareid, uk, dir_path=None):
        if self.share_list_errno is not None:
            return self.share_list_errno
        if dir_path is None:
            return self.share_root
        return self.share_children.get(dir_path, [])

    def get_dir_list(self, folder_name):
        return self.dir_lists.get(folder_name, [])

    def create_dir(self, folder_name):
        self.created_dirs.append(folder_name)
        return 0

    def transfer_file(self, params_list, folder_name):
        self.transfers.append((params_list, folder_name))
        return 0

    def transfer_file_full(self, params_list, folder_name):
        self.transfers.append((params_list, folder_name))
        return {"errno": 0,
                "info": [{"from_fs_id": int(f), "to_fs_id": int(f) + 1000,
                          "errno": 0} for f in params_list[2]]}


def res(name, fs_id, isdir=0, size=100, path=None):
    return FakeNetwork._entry(name, fs_id, isdir=isdir, size=size, path=path)


class TestSelectiveTransfer(unittest.TestCase):
    """平铺转存：引流条目不进 fsidlist；目录建不了就用顶层平铺+多文件分享。"""

    def test_root_yinliu_files_filtered(self):
        """分享根下的引流 txt/jpg 不进 fsidlist（遗留问题场景）。"""
        net = FakeNetwork(
            share_root=[res("第01集.mp4", 101, size=500 * 1024 * 1024),
                        res("不会解压的看这里！！！.txt", 102, size=3),
                        res("追剧浮力.jpg", 103, size=200 * 1024)],
        )
        out = bd._selective_transfer_baidu(net, make_config(), 1, 222,
                                           "转存资源", "C-剧", True)
        self.assertEqual(out["status"], "transferred")
        self.assertEqual(len(net.transfers), 1)
        fsids = net.transfers[0][0][2]
        self.assertEqual(fsids, ["101"])
        self.assertEqual(net.transfers[0][1], "转存资源")
        self.assertEqual(out["names"], ["第01集.mp4"])
        skipped = [p for p, _ in out["skipped"]]
        self.assertEqual(len(skipped), 2)
        self.assertTrue(any("追剧浮力" in p for p in skipped))

    def test_dirty_subdir_triggers_drilldown(self):
        """单根目录内部有引流 → 下钻：干净叶子上浮平铺到顶层。"""
        net = FakeNetwork(
            share_root=[res("C-剧", 200, isdir=1, path="/C-剧")],
            share_children={"/C-剧": [
                res("第01集.mp4", 201, size=500 * 1024 * 1024, path="/C-剧/第01集.mp4"),
                res("不会解压的看这里！！！.txt", 202, size=3, path="/C-剧/不会解压的看这里！！！.txt"),
            ]},
        )
        out = bd._selective_transfer_baidu(net, make_config(), 1, 222,
                                           "转存资源", "C-剧", True)
        self.assertEqual(out["status"], "transferred")
        self.assertEqual(out["mode"], "single")
        # 叶子上浮：不建子目录，直接平铺转存到顶层
        self.assertEqual(net.created_dirs, [])
        params, folder = net.transfers[0]
        self.assertEqual(params[2], ["201"])
        self.assertEqual(folder, "转存资源")
        self.assertEqual(out["names"], ["第01集.mp4"])

    def test_empty_bait_dir_skipped(self):
        """空目录 + 引流话术 → 不转存。"""
        net = FakeNetwork(
            share_root=[res("C-剧", 200, isdir=1, path="/C-剧")],
            share_children={"/C-剧": [
                res("先保存，在观看避免被和谐", 203, isdir=1,
                    path="/C-剧/先保存，在观看避免被和谐"),
            ]},
        )
        out = bd._selective_transfer_baidu(net, make_config(), 1, 222,
                                           "转存资源", "C-剧", True)
        self.assertEqual(out["status"], "transferred")
        self.assertEqual(net.transfers, [])
        self.assertEqual(out["names"], [])

    def test_clean_subdir_transferred_whole(self):
        """子目录干净 → 整目录一次转存。"""
        net = FakeNetwork(
            share_root=[res("C-剧", 200, isdir=1, path="/C-剧")],
            share_children={"/C-剧": [
                res("第一季", 204, isdir=1, path="/C-剧/第一季"),
            ]},
        )
        out = bd._selective_transfer_baidu(net, make_config(), 1, 222,
                                           "转存资源", "C-剧", True)
        self.assertEqual(out["status"], "transferred")
        self.assertEqual(net.transfers[0][0][2], ["200"])
        self.assertEqual(out["names"], ["C-剧"])

    def test_top_same_name_same_size_reused_in_flat(self):
        """顶层已有同名同大小条目（上次平铺过）→ 免转存直接并入分享。"""
        net = FakeNetwork(
            share_root=[res("1.mp4", 101, size=500 * 1024 * 1024,
                            path="/分享/1.mp4")],
            dir_lists={"/转存资源": [res("1.mp4", 900, size=500 * 1024 * 1024)]},
        )
        out = bd._selective_transfer_baidu(net, make_config(), 1, 222,
                                           "转存资源", "1.mp4", False)
        self.assertEqual(out["status"], "transferred")
        self.assertEqual(net.transfers, [])
        self.assertEqual(out["names"], ["1.mp4"])

    def test_top_same_name_diff_size_skipped(self):
        """顶层同名但大小不同 → 视为冲突跳过，不转存。"""
        net = FakeNetwork(
            share_root=[res("1.mp4", 101, size=500 * 1024 * 1024,
                            path="/分享/1.mp4")],
            dir_lists={"/转存资源": [res("1.mp4", 900, size=123)]},
        )
        out = bd._selective_transfer_baidu(net, make_config(), 1, 222,
                                           "转存资源", "1.mp4", False)
        self.assertEqual(out["status"], "transferred")
        self.assertEqual(net.transfers, [])
        self.assertEqual(out["names"], [])
        self.assertEqual(len(out["conflicts"]), 1)

    def test_share_list_unavailable_falls_back(self):
        """share/list 被风控 → unavailable，由 run() 回退全量转存。"""
        net = FakeNetwork(share_root=[], share_list_errno=-6)
        out = bd._selective_transfer_baidu(net, make_config(), 1, 222,
                                           "转存资源", "C-剧", True)
        self.assertEqual(out["status"], "unavailable")
        self.assertEqual(net.transfers, [])

    def test_existing_clean_dir_reused(self):
        """同名目录已存在且干净 → 直接复用（不重复转存）。"""
        net = FakeNetwork(
            share_root=[res("C-剧", 200, isdir=1, path="/C-剧")],
            share_children={"/C-剧": [res("第01集.mp4", 201, size=1 << 20,
                                          path="/C-剧/第01集.mp4")]},
            dir_lists={"/转存资源": [res("C-剧", 900, isdir=1, path="/转存资源/C-剧")],
                       "/转存资源/C-剧": [res("第01集.mp4", 901, size=1 << 20)]},
        )
        out = bd._selective_transfer_baidu(net, make_config(), 1, 222,
                                           "转存资源", "C-剧", True)
        self.assertEqual(out["status"], "reused")
        self.assertEqual(out["names"], ["C-剧"])
        self.assertEqual(net.transfers, [])

    def test_existing_dirty_dir_flattens(self):
        """同名目录残留引流（删除被风控的遗留场景）→ 平铺重转干净内容。"""
        net = FakeNetwork(
            share_root=[res("C-剧", 200, isdir=1, path="/C-剧")],
            share_children={"/C-剧": [
                res("第01集.mp4", 201, size=500 * 1024 * 1024, path="/C-剧/第01集.mp4"),
                res("追剧浮力.jpg", 202, size=200 * 1024, path="/C-剧/追剧浮力.jpg"),
            ]},
            dir_lists={"/转存资源": [res("C-剧", 900, isdir=1, path="/转存资源/C-剧")],
                       "/转存资源/C-剧": [
                           res("第01集.mp4", 901, size=1 << 20),
                           res("不会解压的看这里！！！.txt", 902, size=3)]},
        )
        out = bd._selective_transfer_baidu(net, make_config(), 1, 222,
                                           "转存资源", "C-剧", True)
        self.assertEqual(out["status"], "transferred")
        self.assertEqual(out["names"], ["第01集.mp4"])
        params, folder = net.transfers[0]
        self.assertEqual(params[2], ["201"])      # 引流 jpg 不进 fsidlist
        self.assertEqual(folder, "转存资源")       # 平铺到已存在的顶层，无需建目录

    def test_contact_tail_file_kept(self):
        """带联系方式尾巴的正课文件保留（改写交给事后流程，不能删课）。"""
        name = "43 《风筝》【更多好课加微信z13492263】.pdf"
        net = FakeNetwork(share_root=[res(name, 301, size=1 << 20)])
        out = bd._selective_transfer_baidu(net, make_config(), 1, 222,
                                           "转存资源", name, False)
        self.assertEqual(out["status"], "transferred")
        self.assertEqual(net.transfers[0][0][2], ["301"])

    def test_transfer_failed_reports(self):
        """转存报错 → failed 状态，run() 回退全量流程。"""
        net = FakeNetwork(share_root=[res("第01集.mp4", 101, size=1 << 20)])
        net.transfer_file_full = lambda params, folder: {"errno": 2}
        out = bd._selective_transfer_baidu(net, make_config(), 1, 222,
                                           "转存资源", "C-剧", True)
        self.assertEqual(out["status"], "failed")

    def test_clean_disabled_means_no_selective(self):
        """清理关闭时不做选择性转存（run() 直接走全量）。"""
        cfg = make_config()
        cfg["clean_yinliu"]["enabled"] = False
        # run() 的判断分支：enabled=False 时不会调用 _selective_transfer_baidu
        self.assertFalse(cfg["clean_yinliu"]["enabled"])


class TestRunIntegration(unittest.TestCase):
    """run() 层面的接线：selective 失败要回退全量转存。"""

    def test_unavailable_triggers_full_transfer(self):
        cfg = make_config()
        cfg["wechat"] = {"webhook_url": ""}
        net = FakeNetwork(share_root=[], share_list_errno=-6)

        with mock.patch.object(bd, "load_config", return_value=cfg), \
             mock.patch.object(bd.cache, "fetch_api_data",
                               return_value=[{"url": "https://pan.baidu.com/s/1abc",
                                              "password": "code", "note": "测试"}]), \
             mock.patch.object(bd, "Network", return_value=net), \
             mock.patch.object(bd, "parse_url_and_code",
                               return_value=("https://pan.baidu.com/s/1abc", "code")), \
             mock.patch.object(bd, "normalize_link",
                               return_value="https://pan.baidu.com/s/1abc code"), \
             mock.patch.object(bd, "parse_response",
                               return_value=[1, 222, [101], ["C-剧"], ["1"]]), \
             mock.patch.object(bd.Network, "get_dir_list", return_value=[]):
            out = bd.run(notify=False)

        self.assertEqual(out["code"], 200)
        # 回退路径真的走了全量转存（parsed 整包传给 transfer_file）
        self.assertEqual(len(net.transfers), 1)
        self.assertEqual(net.transfers[0][0], [1, 222, [101], ["C-剧"], ["1"]])


class TestWebSessionDead(unittest.TestCase):
    """分享页被 302 到 passport（网页会话被风控）必须被识别并明确报错。"""

    def _make_session(self, status, location):
        net = bd.Network()

        class R:
            def __init__(self):
                self.status_code = status
                self.headers = {"Location": location} if location else {}

        net.s = mock.MagicMock()
        net.s.get.return_value = R()
        return net

    def test_passport_redirect_raises(self):
        net = self._make_session(302, "https://passport.baidu.com/v3/login/api/auth/?u=x")
        with self.assertRaises(bd._WebSessionDead):
            net.get_transfer_params("https://pan.baidu.com/s/1abc")

    def test_login_redirect_raises(self):
        net = self._make_session(302, "/login?redirecturl=x")
        with self.assertRaises(bd._WebSessionDead):
            net.get_transfer_params("https://pan.baidu.com/s/1abc")

    def test_normal_redirect_followed(self):
        net = bd.Network()

        class R:
            def __init__(self, status, text=""):
                self.status_code = status
                self.headers = {"Location": "/s/1abc"} if status == 302 else {}
                self._text = text

            def raise_for_status(self):
                pass

            def content(self):
                return b""

        page = R(200, "ok page")
        page.content = b"ok page"
        net.s = mock.MagicMock()
        net.s.get.side_effect = [R(302, ""), page]
        out = net.get_transfer_params("https://pan.baidu.com/s/1abc")
        self.assertEqual(out, "ok page")

    def test_run_breaks_on_session_dead(self):
        """会话失效时 run() 明确提示并终止本轮，而不是逐条资源反复报错。"""
        cfg = make_config()
        net = FakeNetwork(share_root=[])
        net.get_transfer_params = mock.MagicMock(
            side_effect=bd._WebSessionDead("分享页跳转登录授权"))

        with mock.patch.object(bd, "load_config", return_value=cfg), \
             mock.patch.object(bd.cache, "fetch_api_data",
                               return_value=[{"url": "https://pan.baidu.com/s/1abc",
                                              "password": "code", "note": "n1"},
                                             {"url": "https://pan.baidu.com/s/1abc2",
                                              "password": "code", "note": "n2"}]), \
             mock.patch.object(bd, "Network", return_value=net), \
             mock.patch.object(bd, "parse_url_and_code",
                               return_value=("u", "c")), \
             mock.patch.object(bd, "normalize_link", return_value="u c"), \
             mock.patch.object(bd, "parse_response",
                               return_value=[1, 222, [101], ["C-剧"], ["1"]]):
            out = bd.run(notify=False)

        # 会话一死就终止整轮，第二条资源不再尝试
        self.assertEqual(net.get_transfer_params.call_count, 1)
        self.assertEqual(out["code"], 200)


class TestInitPageChannel(unittest.TestCase):
    """提码页通道：/share/init 不要求登录态，绕开网页风控拿 shareid/uk。"""

    def _net_with_page(self, status, body):
        net = bd.Network()

        class R:
            def __init__(self):
                self.status_code = status
                self.text = body

        net.s = mock.MagicMock()
        net.s.get.return_value = R()
        return net

    def test_fetch_share_meta_ok(self):
        page = ('<script>window.SHAREPAGETYPE = "init";</script>'
                'shareid:"2317414047"} share_uk:"1406614745"')
        net = self._net_with_page(200, page)
        out = net.fetch_share_meta("B38d_d-cbrZX8xRPnBs6Zg")
        self.assertEqual(out, ("2317414047", "1406614745"))

    def test_fetch_share_meta_dead_link(self):
        net = self._net_with_page(404, "页面不存在")
        self.assertIsNone(net.fetch_share_meta("bbb222"))

    def test_fetch_share_meta_no_data(self):
        net = self._net_with_page(200, "<html>无数据</html>")
        self.assertIsNone(net.fetch_share_meta("whatever"))

    def test_run_prefers_init_channel(self):
        """run() 应走提码页通道，不再依赖 GET /s/ 分享页。"""
        cfg = make_config()
        net = FakeNetwork(share_root=[])
        init_calls = []

        def fake_meta(surl):
            init_calls.append(surl)
            return ("2317414047", "1406614745")

        net.fetch_share_meta = fake_meta
        net.share_list = mock.MagicMock(return_value=[
            {"server_filename": "C-剧", "fs_id": 200, "isdir": 1, "size": 0,
             "path": "/C-剧"}])
        # 转存目标目录为空 → 正常选择性转存
        net.get_dir_list = lambda p: []
        net.create_dir = lambda p: 0
        net.transfer_file_full = mock.MagicMock(return_value={
            "errno": 0,
            "info": [{"from_fs_id": 200, "to_fs_id": 1200, "errno": 0}]})

        with mock.patch.object(bd, "load_config", return_value=cfg), \
             mock.patch.object(bd.cache, "fetch_api_data",
                               return_value=[{"url": "https://pan.baidu.com/s/1abc",
                                              "password": "code", "note": "n1"}]), \
             mock.patch.object(bd, "Network", return_value=net), \
             mock.patch.object(bd, "parse_url_and_code",
                               return_value=("https://pan.baidu.com/s/1abc",
                                             "code")), \
             mock.patch.object(bd, "normalize_link",
                               return_value="https://pan.baidu.com/s/1abc code"):
            out = bd.run(notify=False)

        # 提码页被调用，且 surl 已去掉 "1" 版本前缀
        self.assertEqual(init_calls, ["abc"])
        # 选择性转存：目录干净（子层无引流）→ 整目录一次转存
        self.assertEqual(net.transfer_file_full.call_args[0][0][2], ['200'])
        self.assertEqual(net.transfer_file_full.call_args[0][1], "转存资源")


if __name__ == "__main__":
    unittest.main()
