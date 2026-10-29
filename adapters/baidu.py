"""百度网盘适配器。

迁移自 fresh_baidu2weixin.py，实现统一适配器接口 run()。
纯 requests 实现，无浏览器依赖。
"""
import os
from pathlib import Path
import re
import json
import time
import random
import string

import requests

from core import cache
from core.config import load_config, resolve_path
from core.logger import log_print

BASE_URL = "https://pan.baidu.com"
HEADERS = {
    "Host": "pan.baidu.com",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/webp,image/apng,*/*;q=0.8,"
               "application/signed-exchange;v=b3;q=0.9"),
    "Sec-Fetch-Site": "same-site",
    "Sec-Fetch-Mode": "navigate",
    "Referer": "https://pan.baidu.com",
    "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-US;q=0.7,en-GB;q=0.6,ru;q=0.5",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/114.0.0.0 Safari/537.36"),
}
EXP_MAP = {"1天": 1, "7天": 7, "30天": 30, "永久": 0}
ERROR_CODES = {
    -1: "链接错误，链接失效或缺少提取码",
    -4: "转存失败，无效登录。请退出账号在其他地方的登录",
    -6: "转存失败，请用浏览器无痕模式获取 Cookie 后再试",
    -7: "转存失败，转存文件夹名有非法字符",
    -8: "转存失败，目录中已有同名文件或文件夹存在",
    -9: "链接错误，提取码错误",
    -10: "转存失败，容量不足",
    -12: "链接错误，提取码错误",
    -62: "转存失败，链接访问次数过多",
    0: "转存成功",
    2: "转存失败，目标目录不存在",
    4: "转存失败，目录中存在同名文件",
    12: "转存失败，转存文件数超过限制",
    20: "转存失败，容量不足",
    105: "链接错误，所访问的页面不存在",
    404: "转存失败，秒传无效",
}

SHARE_ID_REGEX = re.compile(r'"shareid":(\d+?),"')
USER_ID_REGEX = re.compile(r'"share_uk":"(\d+?)","')
FS_ID_REGEX = re.compile(r'"fs_id":(\d+?),"')
SERVER_FILENAME_REGEX = re.compile(r'"server_filename":"(.+?)","')
ISDIR_REGEX = re.compile(r'"isdir":(\d+?),"')


class _WebSessionDead(Exception):
    """百度网页会话失效（分享页被 302 到 passport 免密授权页）。"""


def _saved_bdstoken():
    """读 .env 里的 BAIDU_BDSTOKEN（直接读文件，不依赖进程环境变量）。"""
    path = Path(resolve_path(".env"))
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for line in lines:
        if line.strip().startswith("BAIDU_BDSTOKEN="):
            return line.split("=", 1)[1].strip()
    return (os.getenv("BAIDU_BDSTOKEN") or "").strip()


class Network:
    def __init__(self):
        self.s = requests.Session()
        self.headers = HEADERS.copy()
        self.bdstoken = ""
        requests.packages.urllib3.disable_warnings()

    def get_bdstoken(self):
        # 服务器端 gettemplatevariable 已对非浏览器请求恒返 -6（接口事实废弃）。
        # 扫码时趁会话活着在页面里取过一份，存在 .env 的 BAIDU_BDSTOKEN，
        # 优先用它；没有再走接口试一次（万一以后接口又能用了）。
        saved = _saved_bdstoken()
        if saved:
            self.bdstoken = saved
            return saved
        url = f"{BASE_URL}/api/gettemplatevariable"
        params = {
            "clienttype": "0",
            "app_id": "38824127",
            "web": "1",
            "fields": '["bdstoken","token","uk","isdocuser","servertime"]',
        }
        r = self.s.get(url=url, params=params, headers=self.headers, timeout=10,
                       allow_redirects=False, verify=False)
        if r.json()["errno"] != 0:
            return r.json()["errno"]
        return r.json()["result"]["bdstoken"]

    def get_dir_list(self, folder_name):
        url = f"{BASE_URL}/api/list"
        params = {
            "order": "time", "desc": "1", "showempty": "0", "web": "1",
            "page": "1", "num": "1000", "dir": folder_name, "bdstoken": self.bdstoken,
        }
        r = self.s.get(url=url, params=params, headers=self.headers, timeout=15,
                       allow_redirects=False, verify=False)
        if r.json()["errno"] != 0:
            return r.json()["errno"]
        return r.json()["list"]

    def create_dir(self, folder_name):
        url = f"{BASE_URL}/api/create"
        params = {"a": "commit", "bdstoken": self.bdstoken}
        data = {"path": folder_name, "isdir": "1", "block_list": "[]"}
        r = self.s.post(url=url, params=params, headers=self.headers, data=data,
                        timeout=15, allow_redirects=False, verify=False)
        return r.json()["errno"]

    def verify_pass_code(self, link_url, pass_code):
        url = f"{BASE_URL}/share/verify"
        # 旧代码写死 link_url[25:48]，只适用于 /s/1 开头、恰好 23 位的老式短链。
        # 带 "-" 的新短链（如 /s/13smUAr-NQ4gmyxubqbQSdQ）会被截断，导致 surl 无效。
        # 改为从 /s/ 之后取到结尾或查询参数之前。
        m = re.search(r"/s/([^?&\s]+)", link_url)
        surl = m.group(1) if m else link_url[25:48]
        # 百度短链 /s/1XXXXXXXX 里的 "1" 是版本前缀，surl 参数要去掉它。
        # 老代码 link_url[25:48] 恰好从 "1" 之后开始截，等价于去掉前缀，
        # 但它按固定长度截，遇到带 "-" 的短链就会截错。
        if surl.startswith("1"):
            surl = surl[1:]
        params = {
            "surl": surl, "bdstoken": self.bdstoken,
            "t": str(int(round(time.time() * 1000))),
            "channel": "chunlei", "web": "1", "clienttype": "0",
        }
        data = {"pwd": pass_code, "vcode": "", "vcode_str": ""}
        r = self.s.post(url=url, params=params, headers=self.headers, data=data,
                        timeout=10, allow_redirects=False, verify=False)
        if r.json()["errno"] != 0:
            return r.json()["errno"]
        return r.json()["randsk"]

    def share_list(self, shareid, uk, dir_path=None):
        """列出别人分享内部的条目（转存前做引流过滤用）。

        走 /share/list，与 verify/transfer 同一个网页会话——转存一直可用，
        说明这个会话层面没被掐死；返回 list 或 errno(int)。
        dir_path=None 时列分享根目录（root=1）。
        """
        params = {
            "shareid": shareid, "uk": uk,
            "page": "1", "num": "1000", "web": "1",
            "clienttype": "0", "channel": "chunlei",
        }
        if dir_path:
            params["dir"] = dir_path
        else:
            params["root"] = "1"
        try:
            r = self.s.post(url=f"{BASE_URL}/share/list", params=params,
                            headers=self.headers, timeout=15,
                            allow_redirects=False, verify=False)
            j = r.json()
        except Exception as e:  # noqa: BLE001
            log_print(f"分享内列目录请求异常: {e}", "WARNING")
            return -1
        if j.get("errno") != 0:
            return j.get("errno", -1)
        out = j.get("list") or []
        # isdir/fs_id 有时是字符串（"1"/"7658260864710"），统一转 int，
        # 下游 `e.get("isdir") == 1` 才能判准。dir 查询要用条目的真实
        # path（分享方网盘绝对路径，如 /最新热门资源/.../C-xxx）。
        for e in out:
            try:
                e["isdir"] = int(e.get("isdir") or 0)
            except (TypeError, ValueError):
                e["isdir"] = 0
        return out

    def fetch_share_meta(self, surl):
        """从 /share/init 提码页解析 shareid/uk。

        关键通道：该页面不要求有效登录态（普通 Cookie 即可 200 返回），
        页面 JS 变量里直接带 shareid / share_uk。而 /s/ 分享页会被机房
        IP 风控 302 到 passport 授权页。走这条通道，转存解析完全绕开
        网页风控 —— 账号长期在线，不需要反复扫码。

        surl 为去掉 "1" 版本前缀的短码（与 verify 的 surl 一致）。
        Returns: (shareid, uk) 或 None（死链/页面变化）。
        """
        h = dict(self.headers)
        h["Accept-Encoding"] = "identity"
        # 提码页不需要 BDCLND；带着它反而会被 302 去 /s/ 分享页（而分享页
        # 正被 IP 风控 302 到 passport，形成死循环）。剥掉再请求。
        parts = [p for p in (h.get("Cookie") or "").split(";")
                 if p.strip() and not p.strip().lower().startswith("bdclnd=")]
        h["Cookie"] = ";".join(parts)
        try:
            r = self.s.get(url=f"{BASE_URL}/share/init?surl={surl}",
                           headers=h, timeout=15, verify=False,
                           allow_redirects=False)
        except Exception as e:  # noqa: BLE001
            log_print(f"提码页请求异常: {e}", "WARNING")
            return None
        if r.status_code != 200:
            log_print(f"提码页返回 {r.status_code}（链接可能已失效）", "WARNING")
            return None
        t = r.text
        m_sid = re.search(r'shareid[:"]+(\d+)', t)
        m_uk = re.search(r'share_uk[:"]+"?(\d+)', t)
        if not (m_sid and m_uk):
            return None
        return m_sid.group(1), m_uk.group(1)

    def get_transfer_params(self, url):
        # 机房 IP 风控下，GET 分享页可能被 302 到 passport 免密授权页，
        # 该页响应头标 gzip 实际是 brotli，直接交给 requests 解码必炸
        # （ContentDecodingError，且重定向链上循环）。改为手动逐跳跟随，
        # 认出 passport 直接抛 _WebSessionDead，让上层给出"重新扫码"的明确提示。
        h = dict(self.headers)
        h["Accept-Encoding"] = "identity"  # 规避 passport 页压缩头错乱
        for attempt in range(3):
            try:
                u = url
                for _ in range(6):
                    r = self.s.get(url=u, headers=h, timeout=15, verify=False,
                                   allow_redirects=False)
                    if r.status_code in (301, 302, 303, 307, 308):
                        loc = r.headers.get("Location", "")
                        if "passport.baidu.com" in loc or loc.startswith("/login"):
                            raise _WebSessionDead("分享页跳转登录授权")
                        u = loc if loc.startswith("http") else f"{BASE_URL}{loc}"
                        continue
                    r.raise_for_status()
                    return r.content.decode("utf-8")
                raise _WebSessionDead("分享页重定向循环")
            except _WebSessionDead:
                raise
            except Exception:  # noqa: BLE001
                if attempt == 2:
                    raise
                time.sleep(1)

    def transfer_file_full(self, params_list, folder_name):
        """转存并返回完整响应（info: from→to fs_id；duplicated: 已有条目）。

        选择性转存需要 to_fs_id 直接定位转存后的网盘条目，
        不依赖「按名字回查」（百度批量转存时可能自动改名）。
        """
        url = f"{BASE_URL}/share/transfer"
        params = {
            "shareid": params_list[0], "from": params_list[1],
            "bdstoken": self.bdstoken, "channel": "chunlei", "web": "1",
            "clienttype": "0",
        }
        data = {"fsidlist": f"[{','.join(str(f) for f in params_list[2])}]",
                "path": f"/{folder_name}"}
        r = self.s.post(url=url, params=params, headers=self.headers, data=data,
                        timeout=30, allow_redirects=False, verify=False)
        try:
            return r.json()
        except Exception:  # noqa: BLE001
            return {"errno": -1}

    def transfer_file(self, params_list, folder_name):
        url = f"{BASE_URL}/share/transfer"
        params = {
            "shareid": params_list[0], "from": params_list[1],
            "bdstoken": self.bdstoken, "channel": "chunlei", "web": "1",
            "clienttype": "0",
        }
        data = {"fsidlist": f"[{','.join(str(f) for f in params_list[2])}]",
                "path": f"/{folder_name}"}
        r = self.s.post(url=url, params=params, headers=self.headers, data=data,
                        timeout=30, allow_redirects=False, verify=False)
        return r.json()["errno"]

    def copy_folder(self, src_path, dest_path, skip_exists=True):
        """把网盘上一个文件夹复制到 dest_path 目录下（同名则跳过）。

        src_path / dest_path 都是网盘绝对路径，例如 /引流 → /转存资源/某某课程。
        走 /api/filemanager?opera=copy，秒传式复制，不占额外上传流量。
        """
        src_path = "/" + str(src_path or "").strip("/")
        dest_path = "/" + str(dest_path or "").strip("/") if str(dest_path or "").strip("/") else "/"
        name = src_path.rsplit("/", 1)[-1]
        parent = src_path.rsplit("/", 1)[0] or "/"
        if not name:
            return {"ok": False, "message": "引流路径为空"}

        entries = self.get_dir_list(parent)
        if isinstance(entries, int):
            return {"ok": False, "message": f"读取父目录失败 errno={entries}"}
        src_item = next((e for e in entries
                         if e.get("server_filename") == name and e.get("isdir") == 1), None)
        if not src_item:
            return {"ok": False, "message": f"引流文件夹不存在或不是文件夹: {src_path}"}

        if skip_exists:
            dest_entries = self.get_dir_list(dest_path)
            if isinstance(dest_entries, list) and any(
                    e.get("server_filename") == name for e in dest_entries):
                return {"ok": True, "skipped": True, "message": "目标已存在同名文件夹"}

        filelist = [{"id": src_item["fs_id"], "path": src_path,
                     "dest": dest_path, "newname": name, "ondup": "newcopy"}]
        params = {
            "opera": "copy", "async": "1", "onnest": "fail",
            "bdstoken": self.bdstoken, "channel": "chunlei",
            "web": "1", "clienttype": "0", "app_id": "250528",
        }
        try:
            r = self.s.post(url=f"{BASE_URL}/api/filemanager", params=params,
                            data={"filelist": json.dumps(filelist, ensure_ascii=False)},
                            headers=self.headers, timeout=30,
                            allow_redirects=False, verify=False)
            j = r.json()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"复制请求异常: {e}"}

        errno = j.get("errno", -1)
        if errno != 0:
            return {"ok": False,
                    "message": f"复制失败 errno={errno} {ERROR_CODES.get(errno, '')}".strip()}
        for item in (j.get("info") or []):
            sub = item.get("errno", 0)
            if sub != 0:
                return {"ok": False,
                        "message": f"复制失败 errno={sub} {ERROR_CODES.get(sub, '')}".strip()}
        return {"ok": True, "skipped": False, "message": "已复制"}

    def _filemanager(self, opera, filelist):
        """调用 /api/filemanager 的通用封装（删除、重命名等共用）。"""
        params = {
            "opera": opera, "async": "1", "onnest": "fail",
            "bdstoken": self.bdstoken, "channel": "chunlei",
            "web": "1", "clienttype": "0", "app_id": "250528",
        }
        r = self.s.post(url=f"{BASE_URL}/api/filemanager", params=params,
                        data={"filelist": json.dumps(filelist, ensure_ascii=False)},
                        headers=self.headers, timeout=30,
                        allow_redirects=False, verify=False)
        return r.json()

    def delete_file(self, path):
        """删除单个文件/文件夹，返回 True 表示成功。"""
        try:
            j = self._filemanager("delete", [path])
        except Exception as e:  # noqa: BLE001
            log_print(f"百度删除请求异常: {path} - {e}", "WARNING")
            return False
        if j.get("errno", -1) != 0:
            log_print(f"百度删除失败: {path} errno={j.get('errno')}", "WARNING")
            return False
        for item in (j.get("info") or []):
            if item.get("errno", 0) != 0:
                log_print(f"百度删除失败: {path} errno={item.get('errno')}", "WARNING")
                return False
        return True

    def rename_file(self, path, fs_id, new_name):
        """重命名单个文件/文件夹，返回 True 表示成功。"""
        item = {"path": path, "newname": new_name}
        if fs_id:
            item["id"] = fs_id
        try:
            j = self._filemanager("rename", [item])
        except Exception as e:  # noqa: BLE001
            log_print(f"百度重命名请求异常: {path} - {e}", "WARNING")
            return False
        if j.get("errno", -1) != 0:
            log_print(f"百度重命名失败: {path} errno={j.get('errno')}", "WARNING")
            return False
        for info in (j.get("info") or []):
            if info.get("errno", 0) != 0:
                log_print(f"百度重命名失败: {path} errno={info.get('errno')}", "WARNING")
                return False
        return True

    def create_share_multi(self, fs_ids, expiry, password):
        """创建分享链接；fs_ids 支持多个（多文件/多目录混合分享）。

        单条目时与 create_share 等价。多文件分享是风控期的关键能力：
        平铺转存出来的散文件靠它聚成一个链接，不依赖建目录。
        """
        url = f"{BASE_URL}/share/set"
        params = {
            "channel": "chunlei", "bdstoken": self.bdstoken,
            "clienttype": "0", "app_id": "250528", "web": "1",
        }
        fid_list = "[" + ",".join(str(i) for i in fs_ids) + "]"
        data = {
            "period": expiry, "pwd": password, "eflag_disable": "true",
            "channel_list": "[]", "schannel": "4", "fid_list": fid_list,
        }
        r = self.s.post(url=url, params=params, headers=self.headers, data=data,
                        timeout=15, allow_redirects=False, verify=False)
        if r.json()["errno"] != 0:
            return r.json()["errno"]
        return r.json()["link"]

    def create_share(self, fs_id, expiry, password):
        return self.create_share_multi([fs_id], expiry, password)


def normalize_link(url_code):
    normalized = url_code.replace("share/init?surl=", "s/1")
    normalized = re.sub(r"[?&]pwd=", " ", normalized)
    normalized = re.sub(r"提取码*[：:]", " ", normalized)
    normalized = re.sub(r"^.*?(https?://)", "https://", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def parse_url_and_code(url_code):
    parts = url_code.strip().split(" ")
    url = parts[0]
    code = parts[1] if len(parts) > 1 else ""
    return url[:47], code[-4:] if code else ""


def parse_response(response):
    shareid_list = SHARE_ID_REGEX.findall(response)
    user_id_list = USER_ID_REGEX.findall(response)
    fs_id_list = FS_ID_REGEX.findall(response)
    server_filename_list = SERVER_FILENAME_REGEX.findall(response)
    isdir_list = ISDIR_REGEX.findall(response)
    if not all([shareid_list, user_id_list, fs_id_list, server_filename_list, isdir_list]):
        return -1
    return [shareid_list[0], user_id_list[0], fs_id_list,
            list(dict.fromkeys(server_filename_list)), isdir_list]


def update_cookie(bdclnd, cookie):
    cookies_dict = dict(map(lambda it: it.split("=", 1),
                            filter(None, cookie.split(";"))))
    cookies_dict["BDCLND"] = bdclnd
    return ";".join([f"{k}={v}" for k, v in cookies_dict.items()])


def generate_code():
    return "".join(random.choice(string.ascii_letters + string.digits)
                   for _ in range(4))


# 选择性转存的最大下钻深度（引流文件几乎都在分享的前一两层）
_SEL_MAX_DEPTH = 3

# 非会员多文件分享上限（实测第 34 个起返回 errno 109）
_SEL_SHARE_LIMIT = 33


class _ShareListUnavailable(Exception):
    """share/list 不可用（被风控/errno 非 0），调用方需回退全量转存。"""


def _dir_is_clean_baidu(network, path, filter_cfg, excludes, my_contact,
                        max_depth=4):
    """只读递归检查网盘目录里是否残留引流内容（不删不改）。

    用于「同名目录已存在」时决定直接复用还是换新目录名重转。

    Returns:
        (clean, dirty_name)  clean=False 时 dirty_name 是查到的引流条目名
    """
    from core.yinliu_filter import match_yinliu, rewrite_contact

    keywords = filter_cfg.get("keywords") or None
    extensions = filter_cfg.get("extensions") or None
    max_bytes = filter_cfg.get("max_small_file_bytes")
    aggressive = bool(filter_cfg.get("aggressive_ext_check"))

    def _excluded(name):
        return any(x and x in name for x in (excludes or []))

    def scan(dir_path, depth):
        entries = network.get_dir_list(dir_path)
        if isinstance(entries, int):
            return True, ""  # 列不动就不拦，宁可复用也不卡流程
        for e in entries:
            name = e.get("server_filename", "")
            if _excluded(name):
                continue
            if rewrite_contact(name, my_contact):
                return False, name  # 别人联系方式还在，而 rename 也被风控
            is_yl, _ = match_yinliu(name, int(e.get("size") or 0),
                                    keywords=keywords, extensions=extensions,
                                    max_small_file_bytes=max_bytes,
                                    extra_excludes=excludes or None,
                                    aggressive_ext_check=aggressive)
            if is_yl:
                return False, name
            if e.get("isdir") == 1 and depth < max_depth:
                ok, dirty = scan(f"{dir_path.rstrip('/')}/{name}", depth + 1)
                if not ok:
                    return False, dirty
        return True, ""

    return scan(path, 0)


def _selective_transfer_baidu(network, config, shareid, uk, target_folder,
                              file_name, is_dir, root_entries=None):
    """选择性转存：转存前先列分享内容，把引流文件挡在自己网盘外。

    背景：服务器 IP 对百度的网页会话风控很严，转存后调 filemanager 删除
    经常 -6 失败，引流文件残留在分享里。而转存/列目录/分享一直可用，
    所以把清理从「转存后删除」前移成「转存前过滤」——引流文件根本不进
    网盘，自然不需要删。

    Args:
        root_entries: 调用方已经通过 share/list 拿到的根条目（提码页通道
                      附带产出），传入可省一次列目录请求。

    Returns:
        {"status": "transferred"|"reused"|"failed"|"unavailable",
         "mode": "single"|"flat",
         "names": 分享时应使用的网盘条目名列表（单条目=整目录分享）,
         "skipped": [(path, reason)], "conflicts": [(name, reason)]}
    """
    from core import yinliu_folder as yf
    from core.yinliu_filter import match_empty_dir_bait, match_yinliu, rewrite_contact

    cfg = config.get("clean_yinliu") or {}
    own = yf.folder_name(config.get("baidu", {}).get("yinliu_folder") or "")
    excludes = [own] if own else []
    keywords = cfg.get("keywords") or None
    extensions = cfg.get("extensions") or None
    max_bytes = cfg.get("max_small_file_bytes")
    my_contact = cfg.get("my_contact")
    aggressive = bool(cfg.get("aggressive_ext_check"))

    def _excluded(name):
        return any(x and x in name for x in excludes)

    def _verdict(name, size):
        """返回 "skip"(引流) / "keep" / "contact"(联系方式尾巴，原样转存)。"""
        if _excluded(name):
            return "keep"
        if rewrite_contact(name, my_contact):
            return "contact"
        is_yl, _ = match_yinliu(name, size, keywords=keywords,
                                extensions=extensions,
                                max_small_file_bytes=max_bytes,
                                extra_excludes=excludes or None,
                                aggressive_ext_check=aggressive)
        return "skip" if is_yl else "keep"

    # 顶层现状：同名旧目录（重跑遗留）判断复用；同名条目用于免冲突平铺。
    dir_list = network.get_dir_list(f"/{target_folder}")
    existing_top = {}
    if isinstance(dir_list, list):
        existing_top = {e.get("server_filename"): e for e in dir_list}

    # 同名旧目录已存在：干净 → 直接复用分享，零成本
    if is_dir and file_name in existing_top:
        clean, dirty = _dir_is_clean_baidu(
            network, f"/{target_folder}/{file_name}", cfg, excludes, my_contact)
        if clean:
            return {"status": "reused", "mode": "single",
                    "names": [file_name], "skipped": [], "conflicts": []}
        log_print(f"[百度] 已有同名目录残留引流（{dirty}），"
                  f"改用平铺转存+多文件分享（不依赖建目录/删除）", "INFO")

    plan = []     # (name, 分享内 fs_id, size, is_dir) 待转存条目
    skipped = []  # (path, reason)
    conflicts = []  # (name, reason) 顶层同名但内容不一致，跳过

    try:
        def _dir_dirty(path_in, name, depth):
            """递归判断分享内一个目录是否含有引流内容（决定整转还是下钻）。"""
            if depth > _SEL_MAX_DEPTH:
                return False
            children = network.share_list(shareid, uk, path_in)
            if isinstance(children, int):
                return False  # 列不动 → 视为干净整转，事后清理兜底
            if not children:
                if _excluded(name):
                    return False
                bait, _ = match_empty_dir_bait(name, keywords)
                return bait
            for c in children:
                cn = c.get("server_filename", "")
                if _verdict(cn, int(c.get("size") or 0)) == "skip":
                    return True
                if c.get("isdir") == 1:
                    cpath = c.get("path") or f"{path_in.rstrip('/')}/{cn}"
                    if _dir_dirty(cpath, cn, depth + 1):
                        return True
            return False

        def collect(entries, depth):
            """递归收集要转存的干净叶子；脏目录下钻、叶子上浮平铺。

            关键设计：转存目标统一是 target_folder 顶层（已存在，无需建
            目录——/api/create 在风控期 -6）。目录条目转存到顶层时百度会
            自动创建同名子目录；目录内部有引流时继续下钻，把干净叶子提
            上来平铺。
            """
            for e in entries:
                name = e.get("server_filename", "")
                size = int(e.get("size") or 0)
                e_dir = e.get("isdir") == 1
                path_in = e.get("path") or f"/{name}"
                act = _verdict(name, size)
                if act == "skip":
                    skipped.append((path_in, "转存前过滤（命中引流规则）"))
                    continue
                if not e_dir or depth >= _SEL_MAX_DEPTH:
                    plan.append((name, e["fs_id"], size, e_dir))
                    continue
                children = network.share_list(shareid, uk, path_in)
                if isinstance(children, int):
                    # 子目录列不出来 → 整目录转存，事后清理兜底
                    plan.append((name, e["fs_id"], size, e_dir))
                    continue
                if not children:
                    if act == "contact":
                        plan.append((name, e["fs_id"], size, e_dir))
                        continue
                    bait, why = match_empty_dir_bait(name, keywords)
                    if bait and not _excluded(name):
                        skipped.append((path_in, why))
                    else:
                        plan.append((name, e["fs_id"], size, e_dir))
                    continue
                if _dir_dirty(path_in, name, depth):
                    # 内部有引流 → 下钻，干净叶子上浮平铺
                    collect(children, depth + 1)
                else:
                    plan.append((name, e["fs_id"], size, e_dir))

        if root_entries is None:
            root_entries = network.share_list(shareid, uk, None)
        if isinstance(root_entries, int):
            raise _ShareListUnavailable(root_entries)
        collect(root_entries, 0)
    except _ShareListUnavailable as e:
        return {"status": "unavailable", "errno": e.args[0],
                "names": [], "skipped": skipped, "conflicts": conflicts}

    # ── 平铺执行：先按顶层同名预检，避免 -8；一次 transfer 搞定 ──
    reused_pairs = []        # (name, 网盘 fs_id) 顶层已有同名同大小，直接复用
    to_transfer = []         # 分享内 fs_id（str）
    transfer_src = []        # (name, 分享内 fs_id)
    for name, fs_id, size, e_dir in plan:
        ex = existing_top.get(name)
        if ex is not None:
            if int(ex.get("size") or 0) == size:
                # 顶层已有同名同大小 → 就是上次转存的同一资源，直接复用
                reused_pairs.append((name, ex["fs_id"]))
            else:
                conflicts.append((name, "顶层已有同名但大小不同的条目"))
            continue
        to_transfer.append(str(fs_id))
        transfer_src.append((name, fs_id))

    share_names = [n for n, _ in reused_pairs]
    share_fs_ids = [f for _, f in reused_pairs]

    if to_transfer:
        j = network.transfer_file_full([shareid, uk, to_transfer], target_folder)
        errno = j.get("errno")
        if errno != 0:
            return {"status": "failed", "errno": errno,
                    "names": [], "fs_ids": [], "skipped": skipped,
                    "conflicts": conflicts}
        # info: from_fs_id → to_fs_id；duplicated: 目标已存在同一文件，直接给 fs_id
        got_by_src = {int(it["from_fs_id"]): int(it["to_fs_id"])
                      for it in j.get("info") or []
                      if it.get("errno", 0) == 0 and it.get("from_fs_id")
                      and it.get("to_fs_id")}
        got_by_name = {(it.get("path") or "").rsplit("/", 1)[-1]:
                       int(it.get("fs_id") or 0)
                       for it in (j.get("duplicated") or {}).get("list") or []}
        for name, src in transfer_src:
            to_id = got_by_src.get(src) or got_by_name.get(name)
            if to_id:
                share_names.append(name)
                share_fs_ids.append(to_id)
            else:
                log_print(f"[百度] 条目转存未确认，跳出分享: {name}", "WARNING")

    if not share_fs_ids:
        log_print("[百度] 分享内容全部命中引流规则，没有可转存的条目", "INFO")
        return {"status": "transferred", "mode": "flat",
                "names": [], "fs_ids": [],
                "skipped": skipped, "conflicts": conflicts}

    # 条目数超非会员多文件分享上限（实测 33）：放弃平铺，整根目录转存。
    # 引流文件会随之进来（可接受，换取集数完整），全部走 transfer 不碰
    # 建目录/删除接口。目标同名旧目录冲突 -8 → 直接复用旧目录分享。
    if len(share_fs_ids) > _SEL_SHARE_LIMIT:
        root_fid = next((e["fs_id"] for e in (root_entries or [])
                         if e.get("server_filename") == file_name), None) \
            if is_dir else None
        if root_fid:
            log_print(f"[百度] 干净条目 {len(share_fs_ids)} 个超过多文件分享上限 "
                      f"{_SEL_SHARE_LIMIT}，改用整目录转存（引流文件将保留）",
                      "WARNING")
            j = network.transfer_file_full([shareid, uk, [str(root_fid)]],
                                           target_folder)
            errno = j.get("errno")
            if errno in (-8, 4):
                return {"status": "reused", "mode": "single",
                        "names": [file_name], "fs_ids": None,
                        "skipped": skipped, "conflicts": conflicts}
            if errno == 0:
                return {"status": "transferred", "mode": "single",
                        "names": [file_name], "fs_ids": None,
                        "skipped": skipped, "conflicts": conflicts}
            return {"status": "failed", "errno": errno, "names": [],
                    "fs_ids": [], "skipped": skipped, "conflicts": conflicts}
        # 根层是文件集合（非目录）：超限只能截断
        log_print(f"[百度] 条目数超过多文件分享上限，截断到 {_SEL_SHARE_LIMIT} 个",
                  "WARNING")
        share_fs_ids = share_fs_ids[:_SEL_SHARE_LIMIT]
        share_names = share_names[:_SEL_SHARE_LIMIT]

    mode = "single" if len(share_fs_ids) == 1 else "flat"
    return {"status": "transferred", "mode": mode,
            "names": share_names, "fs_ids": share_fs_ids,
            "skipped": skipped, "conflicts": conflicts}


def _clean_yinliu_baidu(network, config, top_path, is_dir, size=0):
    """清理本次转存内容里的他人引流文件（判定规则与夸克一致）。"""
    from core import clean_engine
    from core import yinliu_folder as yf

    filter_cfg = config.get("clean_yinliu") or {}
    if not filter_cfg.get("enabled") or not top_path:
        return None

    own = yf.folder_name(config.get("baidu", {}).get("yinliu_folder") or "")
    excludes = [own] if own else []

    def ls_fn(node):
        entries = network.get_dir_list(node["path"])
        if isinstance(entries, int):
            return []
        out = []
        for e in entries:
            name = e.get("server_filename", "")
            p = e.get("path") or f"{node['path'].rstrip('/')}/{name}"
            out.append({"path": p, "id": e.get("fs_id"), "name": name,
                        "size": int(e.get("size") or 0),
                        "dir": bool(e.get("isdir"))})
        return out

    def delete_fn(node):
        return network.delete_file(node["path"])

    def rename_fn(node, new_name):
        return network.rename_file(node["path"], node.get("id"), new_name)

    root = {"path": top_path, "id": None,
            "name": os.path.basename(top_path.rstrip("/")),
            "size": size, "dir": is_dir}
    return clean_engine.clean_tree("百度", [root], filter_cfg,
                                   ls_fn, delete_fn, rename_fn, excludes)


def _insert_yinliu_baidu(network, config, dest_dir):
    """把配置的引流文件夹复制到资源目录（失败只告警，不影响分享）。"""
    from core import yinliu_folder as yf

    if not yf.is_enabled(config, "baidu"):
        return None
    skip = (config.get("yinliu_folder") or {}).get("skip_exists", True)
    return yf.insert(config, "baidu", dest_dir,
                     lambda s, d: network.copy_folder(s, d, skip))


def run(count=None, kw=None, include=None, exclude=None, preview_only=False, force=False,
        selected_items=None, platform_cfg=None, notify=True):
    """百度网盘转存分享主流程（统一接口）。

    Args:
        notify: 是否发送企业微信汇总通知。文本替换路径（core.text_replacer）
                自己会推送最终文案，这里必须传 False，否则一次转存会发两条消息。
    """
    config = load_config()
    baidu = config["baidu"]
    cookie = baidu.get("cookie", "")
    if count is None:
        count = baidu.get("count") or config["count"]
    target_folder = baidu.get("target_folder") or config["target_folder"]
    expire_hours = config.get("cache_expire_hours", 24)
    webhook_url = config.get("wechat", {}).get("webhook_url", "") if notify else ""

    if not cookie:
        log_print("错误：请配置百度网盘 COOKIE", "ERROR")
        return {"code": 400, "message": "未配置百度网盘 Cookie"}

    if selected_items is not None:
        resources = selected_items
    else:
        resources = cache.fetch_api_data("baidu", kw or "", expire_hours=expire_hours,
                                          force_refresh=force)

    if not resources:
        log_print("没有找到百度网盘资源", "WARNING")
        return {"code": 200, "message": "没有获取到资源", "items": [],
                "preview": preview_only, "share_results": []}

    random.shuffle(resources)
    resources = resources[:count]
    log_print(f"获取到 {len(resources)} 个百度网盘资源")

    # 预览模式：仅返回资源列表，不执行转存
    if preview_only:
        return {"code": 200, "message": "预览成功", "preview": True,
                "items": resources, "share_results": []}

    network = Network()
    network.headers["Cookie"] = cookie

    bdstoken = network.get_bdstoken()
    if isinstance(bdstoken, int):
        log_print(f"获取 bdstoken 失败，错误代码：{bdstoken}", "ERROR")
        return {"code": 500, "message": f"获取 bdstoken 失败: {bdstoken}"}
    network.bdstoken = bdstoken

    result = network.get_dir_list(f"/{target_folder}")
    if isinstance(result, int):
        return_code = network.create_dir(target_folder)
        if return_code != 0:
            log_print(f"创建目录失败，错误代码：{return_code}", "ERROR")
            return {"code": 500, "message": f"创建目录失败: {return_code}"}
    log_print(f"目标目录：{target_folder}")

    results = []
    for idx, resource in enumerate(resources, 1):
        url = resource.get("url", "")
        password = resource.get("password", "")
        note = resource.get("note", "")

        if not url or "pan.baidu.com" not in url:
            continue

        log_print(f"正在处理第 {idx} 个资源: {note}")
        try:
            normalized_link = normalize_link(f"{url} {password}")
            parsed_url, code = parse_url_and_code(normalized_link)

            if code:
                bdclnd = network.verify_pass_code(parsed_url, code)
                if isinstance(bdclnd, int):
                    log_print(f"验证提取码失败：{ERROR_CODES.get(bdclnd, bdclnd)}", "WARNING")
                    continue
                network.headers["Cookie"] = update_cookie(bdclnd, network.headers["Cookie"])

            # ── 解析分享：首选提码页通道（不受 IP 网页风控影响）──────────
            # /share/init 不要求登录态，页面自带 shareid/uk；再用 share/list
            # (POST) 拿条目列表。只有新通道失灵才回退旧的 GET /s/ 分享页。
            parsed = None
            root_entries = None
            m_surl = re.search(r"/s/([^?&\s]+)", parsed_url)
            surl = m_surl.group(1) if m_surl else ""
            if surl.startswith("1"):
                surl = surl[1:]
            meta = network.fetch_share_meta(surl) if surl else None
            if meta:
                shareid, uk = meta
                entries = network.share_list(shareid, uk, None)
                if isinstance(entries, list) and entries:
                    parsed = [shareid, uk,
                              [e["fs_id"] for e in entries],
                              [e.get("server_filename", "") for e in entries],
                              ["1" if e.get("isdir") == 1 else "0"
                               for e in entries]]
                    root_entries = entries
                elif isinstance(entries, int):
                    log_print(f"列分享内容失败 errno={entries}，回退分享页解析",
                              "WARNING")
            else:
                log_print(f"提码页未取到分享信息（链接可能已失效）: {url}",
                          "WARNING")

            if parsed is None:
                # 回退：GET /s/ 分享页（被风控时会 302 passport）
                response = network.get_transfer_params(parsed_url)
                # 死链 / 被取消的分享：verify_pass_code 可能仍返回 bdclnd，
                # 但分享页标题会是"百度网盘-链接不存在"，此时页面里没有 fs_id。
                # 不单独识别的话，会误报成"解析链接失败"，让人以为是代码问题。
                if "链接不存在" in response or "你访问的页面不存在" in response:
                    log_print(f"链接已失效（分享不存在或已取消）：{url}", "WARNING")
                    continue
                parsed = parse_response(response)
                if not isinstance(parsed, list):
                    log_print(f"解析链接失败：{ERROR_CODES.get(parsed, parsed)}", "WARNING")
                    continue
                root_entries = None

            shareid, uk = parsed[0], parsed[1]
            file_name = parsed[3][0] if parsed[3] else "未知文件"
            is_dir = parsed[4] == ["1"]

            # ── 选择性转存（默认路径）：转存前列分享内容，把引流文件挡在网盘外 ──
            # 引流条目不转存 → 网盘里没有它 → 不再依赖被风控的删除接口。
            reused_existing = False
            share_names = None   # 新链路：应分享的网盘条目名列表
            single_dir = True    # 单目录模式才做清理/引流钩子
            if (config.get("clean_yinliu") or {}).get("enabled"):
                sel = _selective_transfer_baidu(network, config, shareid, uk,
                                                target_folder, file_name,
                                                is_dir, root_entries)
                status = sel.get("status")
                if status == "reused":
                    reused_existing = True
                    share_names = sel.get("names") or [file_name]
                elif status == "transferred":
                    for p, why in sel.get("skipped") or []:
                        log_print(f"[百度] 转存前已拦截引流: {p} | {why}", "SUCCESS")
                    for n, why in sel.get("conflicts") or []:
                        log_print(f"[百度] 跳过冲突条目: {n} | {why}", "WARNING")
                    share_names = sel.get("names") or []
                    single_dir = sel.get("mode") == "single"
                else:
                    reason = {"failed": "执行失败",
                              "unavailable": "分享内容不可列"}.get(status, status)
                    log_print(f"[百度] 选择性转存未走通（{reason}），"
                              f"回退全量转存+事后清理: {file_name}", "WARNING")
                    transfer_result = network.transfer_file(parsed, target_folder)
                    if transfer_result in (-8, 4):
                        reused_existing = True
                    elif transfer_result != 0:
                        log_print(f"转存失败：{ERROR_CODES.get(transfer_result, transfer_result)}",
                                  "WARNING")
                        continue
            else:
                transfer_result = network.transfer_file(parsed, target_folder)
                # 同名冲突有两个错误码：-8 和 4（见 ERROR_CODES）。
                # 处理方式：不再折腾新目录，直接拿网盘里已有的同名条目去创建分享。
                # 理由：同名说明这个资源之前已经转存过，内容就在自己网盘里，
                # 重新转存既占空间又可能被风控，直接分享已有的最稳。
                reused_existing = False
                if transfer_result in (-8, 4):
                    log_print(f"目录中已有同名文件，直接分享已有条目：{note}", "INFO")
                    reused_existing = True
                    transfer_result = 0

                if transfer_result != 0:
                    log_print(f"转存失败：{ERROR_CODES.get(transfer_result, transfer_result)}", "WARNING")
                    continue

            if reused_existing:
                log_print(f"目录中已有同名文件，直接分享已有条目：{note}", "INFO")

            dir_list = network.get_dir_list(f"/{target_folder}")
            if not isinstance(dir_list, list):
                log_print(f"无法列出目录 /{target_folder}（errno={dir_list}），"
                          f"跳过分享：{note}", "WARNING")
                continue

            name_map = {e["server_filename"]: e for e in dir_list}
            root = "/" + str(target_folder).strip("/")
            share_url = None
            if share_names is not None:
                # ── 新链路：用转存响应里的网盘 fs_id 直接分享（多文件）──
                sel_fs_ids = sel.get("fs_ids") if status == "transferred" else None
                if sel_fs_ids is None:
                    # reused 路径：按条目名回查网盘 fs_id
                    sel_fs_ids = [name_map[n]["fs_id"]
                                  for n in share_names if n in name_map]
                if single_dir and len(sel_fs_ids) == 1:
                    # 单目录：做清理/引流钩子（多文件平铺不做，结构已打平）
                    n = share_names[0]
                    item = name_map.get(n)
                    if item is not None:
                        is_item_dir = item.get("isdir") == 1
                        target_path = f"{root}/{n}"
                        _clean_yinliu_baidu(network, config, target_path,
                                            is_item_dir,
                                            int(item.get("size") or 0))
                        _insert_yinliu_baidu(network, config,
                                             target_path if is_item_dir else root)
                if sel_fs_ids:
                    share_password = generate_code()
                    share_result = network.create_share_multi(
                        sel_fs_ids, str(EXP_MAP["永久"]), share_password)
                    if share_result == 109:
                        # 条目数超非会员分享上限 → 整目录转存（或复用旧目录）
                        log_print("[百度] 多文件分享超上限，改用整目录分享",
                                  "WARNING")
                        transfer_result = network.transfer_file(
                            parsed, target_folder)
                        if transfer_result in (-8, 4):
                            reused_existing = True
                            transfer_result = 0
                        if transfer_result == 0:
                            item = name_map.get(file_name)
                            if item is not None:
                                share_password = generate_code()
                                share_result = network.create_share(
                                    item["fs_id"], str(EXP_MAP["永久"]),
                                    share_password)
                                if isinstance(share_result, str):
                                    share_url = (
                                        f"{share_result}?pwd={share_password}")
                    elif isinstance(share_result, str):
                        share_url = f"{share_result}?pwd={share_password}"
            else:
                # ── 旧全量路径：找 file_name 单条目分享 ──
                for item in dir_list:
                    if item["server_filename"] == file_name and \
                            item["isdir"] == (1 if is_dir else 0):
                        target_path = f"{root}/{file_name}"
                        # 先清理别人的引流，再复制自己的（顺序不能反，
                        # 否则自己的文件夹会被同一条规则扫到）
                        _clean_yinliu_baidu(network, config, target_path, is_dir,
                                            int(item.get("size") or 0))
                        _insert_yinliu_baidu(network, config,
                                             target_path if is_dir else root)
                        share_password = generate_code()
                        share_result = network.create_share(item["fs_id"],
                                                            str(EXP_MAP["永久"]),
                                                            share_password)
                        if isinstance(share_result, str):
                            share_url = f"{share_result}?pwd={share_password}"
                        break

            if share_url:
                results.append({"note": note, "share_url": share_url,
                                "file_name": file_name,
                                "reused": reused_existing})
                tag = "（复用已有文件）" if reused_existing else ""
                log_print(f"分享成功{tag}：{note} -> {share_url}", "SUCCESS")
            else:
                log_print(f"创建分享链接失败：{note}", "WARNING")

            time.sleep(random.uniform(1, 3))
        except _WebSessionDead:
            log_print("百度网页会话已被风控失效（分享页跳登录授权），"
                      "请到工作台重新扫码登录后重试本轮", "ERROR")
            break
        except Exception as e:
            log_print(f"处理资源失败 {note}：{e}", "ERROR")

    wechat_result = None
    feishu_bot_result = None
    feishu_result = None
    # 统一收口：企微 + 飞书 bot + 飞书 bitable（core.post_share）
    # notify=False 时（文本替换路径由外层统一推送）本段整体跳过
    if notify:
        success_shares = [r for r in results if r.get("share_url")]
        if success_shares or results:
            m = send_messages(config, success_shares, kw=kw, failed=not success_shares)
            wechat_result = m["wechat"]
            feishu_bot_result = m["feishu"]
        if success_shares:
            feishu_result = write_feishu_bitable(config, success_shares, platform="baidu")

    log_print(f"完成！成功处理 {len(results)} 个资源")
    return {
        "code": 200,
        "message": "批量转存分享操作完成",
        "total_selected": count,
        "save_success_count": len(results),
        "share_success_count": len(results),
        "share_results": results,
        "wechat_result": wechat_result,
        "feishu_bot_result": feishu_bot_result,
        "feishu_result": feishu_result,
        "preview": preview_only,
        "items": None,
    }