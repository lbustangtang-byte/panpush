"""链接识别测试（extract_links）。

重点保护：**漏识别**比转存失败更危险 ——
没识别到的链接会原样留在文案里，等于替原作者导流，用户还看不出来。
2026-10-29 实测：三平台混排的文案里，迅雷那条被整条漏掉。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.text_replacer import extract_links  # noqa: E402


MIXED = """📺C-穿成短命炮灰-我靠贴贴反派续命（更45）
三皇子更新咯🔥🔥🔥 穿成短命炮灰靠反派贴贴续命更41+13寺庙(硬盘灰吃饭)(含海外版39 22 29

讯雷🔗：https://pan.xunlei.com/s/VP2_xrvTtavlFHnfLMJlXlQOA1?pwd=ju4q#  

跨克🔗：https://pan.quark.cn/s/bfac87e49251

佰度🔗:https://pan.baidu.com/s/1B38d_d-cbrZX8xRPnBs6Zg?pwd=8888"""


class TestExtractLinks(unittest.TestCase):
    def test_three_platforms_all_found(self):
        """三平台混排必须全部识别，一条都不能漏。"""
        got = {l["platform"]: l for l in extract_links(MIXED)}
        self.assertEqual(set(got), {"xunlei", "quark", "baidu"})
        self.assertEqual(got["xunlei"]["url"],
                         "https://pan.xunlei.com/s/VP2_xrvTtavlFHnfLMJlXlQOA1?pwd=ju4q")
        self.assertEqual(got["quark"]["url"], "https://pan.quark.cn/s/bfac87e49251")
        self.assertEqual(got["baidu"]["url"],
                         "https://pan.baidu.com/s/1B38d_d-cbrZX8xRPnBs6Zg?pwd=8888")

    def test_passwords_from_query(self):
        got = {l["platform"]: l for l in extract_links(MIXED)}
        self.assertEqual(got["xunlei"]["password"], "ju4q")
        self.assertEqual(got["baidu"]["password"], "8888")

    def test_xunlei_hash_anchor_excluded(self):
        """URL 末尾的 # 是前端锚点，不能算进链接，否则转存接口会报错。"""
        links = extract_links("https://pan.xunlei.com/s/abc123?pwd=zzzz#section")
        self.assertEqual(len(links), 1)
        self.assertFalse(links[0]["url"].endswith("#section"))
        self.assertEqual(links[0]["url"], "https://pan.xunlei.com/s/abc123?pwd=zzzz")

    def test_orders_by_position(self):
        links = extract_links(MIXED)
        positions = [l["span"][0] for l in links]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual([l["platform"] for l in links], ["xunlei", "quark", "baidu"])

    def test_duplicate_url_deduped(self):
        t = "https://pan.quark.cn/s/abc https://pan.quark.cn/s/abc"
        self.assertEqual(len(extract_links(t)), 1)

    def test_no_link_returns_empty(self):
        self.assertEqual(extract_links("这段文案里一个链接都没有"), [])


if __name__ == "__main__":
    unittest.main()
