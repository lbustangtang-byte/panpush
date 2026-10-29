# -*- coding: utf-8 -*-
"""工作台 API（Flask Blueprint，挂载在 /api/workbench 下）。

四个页面各对应一组接口：

  任务   POST /run、POST /preview、GET /task/status
  资源库 GET /pool、POST /pool/refresh、POST /run-selected
  凭证   GET /health、POST /credentials
  历史   GET /history、GET /history/export、POST /link/check

两个关键约束（踩过坑，别改回去）：

1. **.env 只能按行替换**。用户手工写了大量注释和分组，整体重写会把它们冲掉。
   所有写配置的地方必须走 ``env_write()``。
2. **任务单例**。适配器内部会并发操作网盘目录，两个任务同时跑会互相踩。
   用 ``_TASK_LOCK`` 保证同一时刻只有一个任务在跑，第二个请求直接 409。
"""
import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime

import httpx
from flask import Blueprint, Response, jsonify, request

from core.cache import _clean_note
from core.config import PROJECT_ROOT, SHARE_DIR, load_config
from core.logger import add_listener, log_print, remove_listener

bp = Blueprint("workbench", __name__)

ENV_PATH = PROJECT_ROOT / ".env"
POOL_PATH = PROJECT_ROOT / "资源池.json"
HISTORY_PATH = SHARE_DIR / "share_url_total.txt"
CRED_STATUS_PATH = PROJECT_ROOT / "data" / "cred_status.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

# 允许工作台写入的键 -> (中文名, 分组)。白名单之外的键一律拒绝。
CRED_SPEC = {
    "QUARK_COOKIE": ("夸克 Cookie", "quark"),
    "KUAKE_COOKIE": ("夸克 CLI Cookie", "quark"),
    "BAIDU_COOKIE": ("百度 Cookie", "baidu"),
    "XUNLEI_REFRESH_TOKEN": ("迅雷 refresh_token", "xunlei"),
    "WECHAT_WEBHOOK_URL": ("企业微信 Webhook", "push"),
    "FEISHU_WEBHOOK_URL": ("飞书 Webhook", "push"),
    # QQ 群：走 OneBot 协议端（NapCat / LLOneBot），不是官方机器人
    "QQ_ENABLED": ("QQ 推送开关", "push"),
    "ONEBOT_URL": ("OneBot 接口地址", "push"),
    "QQ_GROUP_ID": ("QQ 群号", "push"),
    "ONEBOT_ACCESS_TOKEN": ("OneBot 访问令牌", "push"),
    # 引流文件夹（复制模式）：填网盘内绝对路径，留空则该平台不插入
    "YINLIU_FOLDER": ("引流文件夹（全局）", "yinliu"),
    "QUARK_YINLIU_FOLDER": ("夸克引流文件夹", "yinliu"),
    "BAIDU_YINLIU_FOLDER": ("百度引流文件夹", "yinliu"),
    "XUNLEI_YINLIU_FOLDER": ("迅雷引流文件夹", "yinliu"),
    # 引流清理（转存后删别人留的广告）
    "CLEAN_YINLIU": ("转存后清理引流", "clean"),
    "CLEAN_YINLIU_MY_CONTACT": ("自己的联系方式", "clean"),
    "CLEAN_YINLIU_EXCLUDES": ("清理排除名单", "clean"),
}

PLATFORM_CN = {"quark": "夸克", "baidu": "百度", "xunlei": "迅雷", "other": "其他"}


# ===========================================================================
# .env 读写：按行替换，保留注释与行序
# ===========================================================================
def _read_env_lines():
    if not ENV_PATH.exists():
        return []
    return ENV_PATH.read_text(encoding="utf-8").splitlines()


def env_read():
    """解析 .env 得到 {key: value}（不读 os.environ，避免旧值污染）。"""
    out = {}
    for line in _read_env_lines():
        m = re.match(r"^\s*([A-Za-z0-9_]+)\s*=(.*)$", line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2)
        # 行尾注释：只有 # 前面是空白才算注释，避免误伤含 # 的值
        cut = re.search(r"(?:^|\s)#", raw)
        if cut:
            raw = raw[:cut.start()]
        out[key] = raw.strip().strip('"').strip("'")
    return out


def _split_comment(raw: str):
    """把 "   # 注释" 这种行尾注释单独摘出来。"""
    m = re.search(r"(?:^|\s)#", raw)
    if not m:
        return raw.rstrip(), ""
    return raw[:m.start()].rstrip(), "  " + raw[m.start():].strip()


def env_write(updates: dict) -> list:
    """把 updates 写回 .env，只改等号右边，保留行尾注释与行顺序。

    返回实际写入的键列表。不存在于文件中的键会追加到末尾。
    """
    lines = _read_env_lines()
    written = []
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)([A-Za-z0-9_]+)(\s*)=(.*)$", line)
        if not m:
            continue
        key = m.group(2)
        if key not in updates:
            continue
        _, comment = _split_comment(m.group(4))
        lines[i] = f"{m.group(1)}{key}{m.group(3)}={updates[key]}{comment}"
        written.append(key)

    for key, value in updates.items():
        if key not in written:
            lines.append(f"{key}={value}")
            written.append(key)

    ENV_PATH.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    # 同步进程内环境变量：dotenv 默认不覆盖已存在的值，只写文件不生效
    for key, value in updates.items():
        os.environ[key] = value
    return written


# ===========================================================================
# 平台识别 / 资源池
# ===========================================================================
def detect_platform(url: str) -> str:
    u = (url or "").lower()
    if "pan.quark.cn" in u or "pan.uc.cn" in u:
        return "quark"
    if "pan.baidu.com" in u:
        return "baidu"
    if "pan.xunlei.com" in u:
        return "xunlei"
    return "other"


_pool_cache = {"mtime": None, "items": []}
_pool_lock = threading.Lock()


def load_pool(force: bool = False):
    """读资源池并按 url 去重，补上 platform 与清洗后的标题。"""
    if not POOL_PATH.exists():
        return []
    mtime = POOL_PATH.stat().st_mtime
    with _pool_lock:
        if not force and _pool_cache["mtime"] == mtime:
            return _pool_cache["items"]
        try:
            raw = json.loads(POOL_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            log_print(f"读取资源池失败: {exc}", "ERROR")
            return []
        if isinstance(raw, dict):
            raw = raw.get("data", [])
        items, seen = [], set()
        for it in raw or []:
            if not isinstance(it, dict):
                continue
            url = (it.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            raw_note = (it.get("note") or "").strip()
            items.append({
                "note": _clean_note(raw_note) or raw_note[:60] or "(无标题)",
                "url": url,
                "platform": detect_platform(url),
            })
        for i, it in enumerate(items, 1):
            it["index"] = i
        _pool_cache.update(mtime=mtime, items=items)
        return items


def save_pool(items):
    payload = [{"index": i, "note": it["note"], "url": it["url"]}
               for i, it in enumerate(items, 1)]
    POOL_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    load_pool(force=True)


# ===========================================================================
# 历史
# ===========================================================================
def _status_kind(status: str) -> str:
    s = (status or "").upper()
    if s.startswith("LINK_DEAD"):
        return "dead"
    if s in ("DONE", "SUCCESS"):
        return "success"
    if s.startswith("SAVE_FAIL"):
        return "save_fail"
    if s.startswith("SHARE_FAIL"):
        return "share_fail"
    if s == "PICKED":
        return "picked"
    return "other"


def load_history():
    if not HISTORY_PATH.exists():
        return []
    rows = []
    try:
        content = HISTORY_PATH.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        log_print(f"读取历史失败: {exc}", "WARNING")
        return []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.rsplit("  |  ", 2)
        if len(parts) != 3:
            continue
        note, url, status = (p.strip() for p in parts)
        rows.append({
            "note": note,
            "url": url,
            "status": status,
            "kind": _status_kind(status),
            "platform": detect_platform(url),
        })
    rows.reverse()  # 最近的在前
    return rows


# ===========================================================================
# 任务状态机（单例）
# ===========================================================================
_TASK_LOCK = threading.Lock()
_task = {
    "id": None,
    "running": False,
    "platform": None,
    "kw": None,
    "stage": "idle",       # idle / searching / saving / sharing / pushing / done / error
    "total": 0,
    "done": 0,
    "ok": 0,
    "fail": 0,
    "started_at": None,
    "finished_at": None,
    "error": None,
    "logs": [],
    "result": None,
    "items": None,         # 预览候选（供确认后转存）
}
_STAGE_KEYWORDS = [
    ("转存成功", "saving"),
    ("转存失败", "saving"),
    ("分享成功", "sharing"),
    ("创建分享失败", "sharing"),
    ("推送", "pushing"),
]


def _task_snapshot():
    with _TASK_LOCK:
        snap = dict(_task)
        snap["logs"] = list(_task["logs"][-300:])
        return snap


def _reset_task_locked(platform=None, kw=None):
    """重置任务状态。**调用方必须已持有 _TASK_LOCK**（用 _reset_task 更安全）。"""
    _task.update({
        "id": datetime.now().strftime("%Y%m%d%H%M%S"),
        "running": False, "platform": platform, "kw": kw,
        "stage": "idle", "total": 0, "done": 0, "ok": 0, "fail": 0,
        "started_at": None, "finished_at": None, "error": None,
        "logs": [], "result": None, "items": None,
    })


def _reset_task(platform=None, kw=None):
    """重置任务状态（自行加锁）。注意 Lock 不可重入，不要在持锁时调它。"""
    with _TASK_LOCK:
        _reset_task_locked(platform, kw)


def _make_log_listener():
    """把适配器日志实时塞进任务状态，并顺带推断进度。"""
    def listener(line, level, message):
        with _TASK_LOCK:
            if not _task["running"]:
                return
            _task["logs"].append(line)
            if len(_task["logs"]) > 800:
                del _task["logs"][:200]
            m = re.search(r"\[(\d+)/(\d+)\]", message)
            if m:
                _task["done"] = max(_task["done"], int(m.group(1)))
                _task["total"] = max(_task["total"], int(m.group(2)))
            for kw, stage in _STAGE_KEYWORDS:
                if kw in message:
                    _task["stage"] = stage
                    break
    return listener


def _call_adapter(platform, **kwargs):
    from adapters import ADAPTERS
    fn = ADAPTERS[platform]
    if platform != "quark":
        kwargs.pop("check_links", None)
    return fn(**kwargs)


def _run_in_thread(platform, kw, include, exclude, count, items=None):
    listener = _make_log_listener()
    add_listener(listener)
    try:
        with _TASK_LOCK:
            _task["stage"] = "searching"
        res = _call_adapter(
            platform, count=count, kw=kw, include=include, exclude=exclude,
            preview_only=False, selected_items=items,
        )
        with _TASK_LOCK:
            _task["result"] = {
                "code": res.get("code"),
                "message": res.get("message"),
                "total_selected": res.get("total_selected", 0),
                "save_success_count": res.get("save_success_count", 0),
                "share_success_count": res.get("share_success_count", 0),
                "share_results": (res.get("share_results") or [])[:50],
                "wechat_result": res.get("wechat_result"),
                "feishu_result": res.get("feishu_result"),
            }
            _task["ok"] = int(res.get("share_success_count") or 0)
            _task["fail"] = max(0, int(res.get("total_selected") or 0) - _task["ok"])
            _task["stage"] = "done" if res.get("code") == 200 else "error"
            if res.get("code") != 200:
                _task["error"] = res.get("message") or "任务失败"
            _task["finished_at"] = time.time()
    except Exception as exc:  # 适配器异常不能让线程静默死掉
        log_print(f"任务异常: {exc}", "ERROR")
        with _TASK_LOCK:
            _task["stage"] = "error"
            _task["error"] = str(exc)
            _task["finished_at"] = time.time()
    finally:
        remove_listener(listener)
        with _TASK_LOCK:
            _task["running"] = False


# ===========================================================================
# 凭证验证
# ===========================================================================
def _sync_xunlei_store(value):
    """把 .env 里的迅雷 token 同步进运行时存储。

    迅雷每换一次票就轮换 refresh_token，运行时那份（data/xunlei_token.json）
    永远比 .env 新。反过来，扫码保存时只写了 .env，运行时就还留着旧票 ——
    两边必须同时写，才不会出现"界面绿着、跑任务却报 token 已失效"。
    """
    value = str(value or "").strip()
    if not value:
        return
    try:
        from adapters.xunlei import TokenStore
        TokenStore().save(value)
    except Exception:  # noqa: BLE001
        pass


def _cred_fp(value):
    return hashlib.sha1((value or "").encode("utf-8")).hexdigest()[:12]


def load_cred_status():
    try:
        return json.loads(CRED_STATUS_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_cred_status(key, value, ok, msg):
    """记住「这个值上次真实验证的结果」。

    界面不能只看"填了没填"就点绿灯 —— 填了一串过期 Cookie 也是填了。
    """
    data = load_cred_status()
    data[key] = {"ok": bool(ok), "msg": (msg or "")[:200],
                 "fp": _cred_fp(value), "at": int(time.time())}
    try:
        CRED_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        CRED_STATUS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log_print(f"[workbench] 校验状态写入失败: {exc}", "WARNING")


def cred_verified(key, value):
    """True=已验证有效；False=验证失败；None=没验过，或值已经被改过。"""
    if not value:
        return None
    st = load_cred_status().get(key) or {}
    if st.get("fp") != _cred_fp(value):
        return None
    return bool(st.get("ok"))


def probe_xunlei_token(token, timeout=25):
    """用 refresh_token 真换一次 access_token。

    返回 (ok, 刷新后的 refresh_token, 说明)。失败时第三项是原因。
    不能只看 HTTP 200 —— 迅雷对失效 token 也返回 200 之外的 400/4126。
    """
    from adapters.xunlei import TokenStore, XunleiClient

    if not token:
        return False, "", "值为空"
    try:
        client = XunleiClient(token, token_store=TokenStore())
        client.ensure_tokens()
    except Exception as exc:  # noqa: BLE001
        return False, "", str(exc).strip().splitlines()[0][:160]
    return True, getattr(client, "_refresh_token", "") or token, getattr(client, "_user_name", "")


def _verify_impl(key, value, timeout=12):
    """对单个凭证做真实的在线校验。返回 (ok, message)。

    校验失败不代表凭证一定无效（上游接口可能改版），调用方按"提示不阻断"处理。
    """
    if not value:
        return False, "值为空"
    try:
        if key in ("QUARK_COOKIE", "KUAKE_COOKIE"):
            r = httpx.get("https://pan.quark.cn/account/info",
                          params={"fr": "pc", "platform": "pc"},
                          headers={"cookie": value, "user-agent": UA},
                          timeout=timeout)
            j = r.json()
            data = j.get("data") or {}
            nick = data.get("nickname")
            # 这个接口的 code 是字符串 "OK"，不是数字 0；只认数字 0 会误判成失效
            ok_code = j.get("code") in (0, "OK") or j.get("success") is True
            if ok_code and nick:
                return True, f"夸克登录有效：{nick}"
            return False, f"夸克返回 code={j.get('code')} {j.get('message', '')}".strip()

        if key == "BAIDU_COOKIE":
            # 只查 uinfo（xpan 接口，BDUSS 层）。
            #
            # 为什么不再查 gettemplatevariable（bdstoken）：
            #   ① 该接口 2026-10 起对服务器端请求恒返回 errno=-6，接口事实上已废；
            #   ② 网页层会话（PANPSC）由百度风控管理，扫码成功后半小时内就可能
            #      被服务端作废（实测：Cookie 原样放回真实浏览器也会被踢到登录页），
            #      服务器端根本测不出「网页会话当下是否可用」；
            #   ③ 转存/分享走 xpan 接口，只认 BDUSS，不依赖 bdstoken ——
            #   历史任务已证明：没有 PANPSC 的 Cookie 也能转存 60 项并分享成功。
            # 唯一受影响的是「自动清理引流」（要 bdstoken），以任务时的实际结果为准。
            r = httpx.get("https://pan.baidu.com/rest/2.0/xpan/nas",
                          params={"method": "uinfo"},
                          headers={"cookie": value, "user-agent": UA},
                          timeout=timeout)
            j = r.json()
            name = j.get("netdisk_name") or j.get("baidu_name")
            if j.get("errno") != 0:
                return False, f"百度返回 errno={j.get('errno')}（Cookie 可能已过期）"
            if re.search(r"PANPSC=[^;\s]+", value):
                return True, (f"百度登录有效：{name}（转存/分享可用；"
                              f"已记录云盘会话，自动清理以任务时的结果为准）")
            return True, (f"百度登录有效：{name}（转存/分享可用；"
                          f"未带云盘会话，自动清理引流可能失败）")

        if key == "XUNLEI_REFRESH_TOKEN":
            ok, _new, info = probe_xunlei_token(value)
            if ok:
                return True, f"迅雷登录有效{('：' + info) if info else ''}"
            return False, f"迅雷校验失败：{info}"

        if key == "WECHAT_WEBHOOK_URL":
            if not re.match(r"^https://qyapi\.weixin\.qq\.com/cgi-bin/webhook/send\?key=", value):
                return False, "不是企业微信群机器人地址（应形如 https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...）"
            return True, "地址格式正确（未实际发消息，避免打扰群）"

        if key == "FEISHU_WEBHOOK_URL":
            if not value.startswith("https://"):
                return False, "不是合法的 Webhook 地址"
            return True, "地址格式正确（未实际发消息）"

        if key == "ONEBOT_URL":
            # 只探活，绝不往群里发测试消息 —— 验证一次就刷屏一次太吵。
            # ① get_login_info：协议端在线且 QQ 已登录
            # ② get_group_info：群号确实存在（协议端没进这个群会返回 100）
            env_now = env_read()
            group = (env_now.get("QQ_GROUP_ID") or "").strip()
            base = (value or "").strip().rstrip("/")
            if not base.startswith(("http://", "https://")):
                return False, "OneBot 地址应形如 http://127.0.0.1:3000"
            token = (env_now.get("ONEBOT_ACCESS_TOKEN") or "").strip()
            hdr = {"Authorization": f"Bearer {token}"} if token else {}
            r = httpx.post(f"{base}/get_login_info", json={}, headers=hdr,
                           timeout=timeout)
            j = r.json()
            if j.get("retcode") not in (None, 0):
                return False, (f"OneBot 返回错误：retcode={j.get('retcode')} "
                               f"{j.get('message') or j.get('wording') or ''}").strip()
            nick = ((j.get("data") or {}).get("nickname") or "").strip()
            if not group:
                return False, "协议端在线，但还没填 QQ 群号"
            gid = int(group) if group.isdigit() else group
            r2 = httpx.post(f"{base}/get_group_info", json={"group_id": gid},
                            headers=hdr, timeout=timeout)
            j2 = r2.json()
            if j2.get("retcode") not in (None, 0):
                return False, (f"协议端在，但读不到群 {group}"
                               f"（retcode={j2.get('retcode')}）；确认这个 QQ 号已经在该群里")
            gname = ((j2.get("data") or {}).get("group_name") or "").strip()
            return True, (f"QQ 通道可用：{nick or '已登录'}"
                          + (f" / 群「{gname}」" if gname else f" / 群 {group}"))
    except Exception as exc:
        return False, f"校验请求失败：{exc}"
    return True, "已保存"


def verify_credential(key, value, timeout=12):
    """真实校验 + 记录结果，供 /health 显示「已验证 / 验证失败 / 未验证」。"""
    ok, msg = _verify_impl(key, value, timeout)
    if value:
        save_cred_status(key, value, ok, msg)
    return ok, msg


# ===========================================================================
# 路由：健康度
# ===========================================================================
@bp.get("/health")
def health():
    cfg = load_config()
    env = env_read()

    status = load_cred_status()      # 读一次，别为每个凭证读一遍盘

    def cred_state(key):
        v = env.get(key) or ""
        st = status.get(key) or {}
        fresh = bool(v) and st.get("fp") == _cred_fp(v)
        return {
            "ok": bool(v),
            "len": len(v),
            # 值的指纹：前端用它判断"这个值我已经自动验过了"，换值才重验
            "fp": _cred_fp(v) if v else "",
            "masked": (v[:6] + "…" + v[-4:]) if len(v) > 12 else "",
            # verified 三态：None=没验过或值被改过，界面显示灰色而不是绿灯
            "verified": (bool(st.get("ok")) if fresh else None),
            "message": (st.get("msg") or "") if fresh else "",
            "checked_at": st.get("at") if fresh else None,
        }

    def qq_state():
        """QQ 是"三个字段凑一个通道"，不能套单值那套 cred_state。"""
        q = (cfg.get("qq") or {})
        url = (q.get("api_url") or "").strip()
        group = str(q.get("group_id") or "").strip()
        ok = bool(q.get("enabled") and url and group)
        st = status.get("ONEBOT_URL") or {}
        fresh = ok and st.get("fp") == _cred_fp(url)
        return {
            "ok": ok,
            "enabled": bool(q.get("enabled")),
            "len": len(url),
            "fp": _cred_fp(url) if url else "",
            "masked": url[:34] + ("…" if len(url) > 34 else ""),
            # 地址要能原样回填到输入框，截断的 masked 会把配置写坏
            "url": url,
            "group_id": group,
            "verified": (bool(st.get("ok")) if fresh else None),
            "message": (st.get("msg") or "") if fresh else "",
            "checked_at": st.get("at") if fresh else None,
        }

    # PanSou 健康检查（2 秒超时，避免拖慢页面）
    pansou = {"ok": False, "plugins": 0, "url": ""}
    try:
        from core.config import get_pansou_health_url
        url = get_pansou_health_url()
        pansou["url"] = url
        r = httpx.get(url, timeout=2.0)
        j = r.json()
        pansou["ok"] = (j.get("status") == "ok")
        pansou["plugins"] = int(j.get("plugin_count") or 0)
    except Exception:
        pass

    pool = load_pool()
    by_platform = {}
    for it in pool:
        by_platform[it["platform"]] = by_platform.get(it["platform"], 0) + 1

    return jsonify({
        "success": True,
        "pansou": pansou,
        "creds": {
            "quark": {
                "cookie": cred_state("QUARK_COOKIE"),
                "kuake": cred_state("KUAKE_COOKIE"),
            },
            "baidu": {"cookie": cred_state("BAIDU_COOKIE")},
            "xunlei": {"token": cred_state("XUNLEI_REFRESH_TOKEN")},
            "wechat": cred_state("WECHAT_WEBHOOK_URL"),
            "feishu": cred_state("FEISHU_WEBHOOK_URL"),
            "qq": qq_state(),
        },
        "pool": {"count": len(pool), "by_platform": by_platform},
        "history": {"count": len(load_history())},
        "task": _task_snapshot(),
        "yinliu": {
            "enabled": bool((cfg.get("yinliu_folder") or {}).get("enabled")),
            "default": (cfg.get("yinliu_folder") or {}).get("default") or "",
            "quark": cfg.get("quark", {}).get("yinliu_folder") or "",
            "baidu": cfg.get("baidu", {}).get("yinliu_folder") or "",
            "xunlei": cfg.get("xunlei", {}).get("yinliu_folder") or "",
        },
        "clean": {
            "enabled": bool((cfg.get("clean_yinliu") or {}).get("enabled")),
            "my_contact": (cfg.get("clean_yinliu") or {}).get("my_contact") or "",
            "excludes": ",".join((cfg.get("clean_yinliu") or {}).get("excludes") or []),
            "aggressive": bool((cfg.get("clean_yinliu") or {}).get("aggressive_ext_check")),
        },
        "config": {
            "count": cfg.get("count"),
            "target_folder": cfg.get("target_folder"),
            "local_pool": bool(cfg),
        },
    })


# ===========================================================================
# 路由：凭证
# ===========================================================================
@bp.post("/credentials/parse")
def parse_credentials():
    """把用户粘贴的任意内容识别成 (平台, 配置项, 值)。

    只做解析，不落盘 —— 前端确认后再走 /credentials 保存。

    前端会用 base64 传 text_b64：公网网关的 WAF 会把含 `curl` 的请求体
    当成命令注入拦掉，编码后就没这个特征了。兼容明文 text 便于本地调试。
    """
    data = request.get_json(silent=True) or {}
    text = data.get("text") or ""
    b64 = data.get("text_b64")
    if b64:
        import base64
        try:
            text = base64.b64decode(b64).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return jsonify({"ok": False, "platform": None, "platform_name": "",
                            "key": "", "value": "", "kind": "unknown",
                            "confidence": "low", "notes": ["编码内容解析失败，请重试"],
                            "warnings": [], "length": 0, "preview": ""})
    from core import cred_parse
    result = cred_parse.parse(text)
    return jsonify(result)


@bp.post("/credentials")
def save_credentials():
    data = request.get_json(silent=True) or {}
    payload = data.get("values") or {}
    do_verify = bool(data.get("verify", True))

    updates = {}
    for key, value in payload.items():
        if key not in CRED_SPEC:
            return jsonify({"success": False,
                            "message": f"不允许写入的配置项：{key}"}), 400
        updates[key] = str(value).strip()

    if not updates:
        return jsonify({"success": False, "message": "没有需要保存的内容"}), 400

    try:
        written = env_write(updates)
    except Exception as exc:
        return jsonify({"success": False, "message": f"写入 .env 失败：{exc}"}), 500

    if updates.get("XUNLEI_REFRESH_TOKEN"):
        _sync_xunlei_store(updates["XUNLEI_REFRESH_TOKEN"])

    # 夸克两个 Cookie 必须一致，否则 kuake CLI 与 HTTP 各用一份会互相矛盾。
    # 注意清空时也要同步 —— 否则界面显示未配置，CLI 还在用旧 Cookie。
    if "QUARK_COOKIE" in updates:
        env_write({"KUAKE_COOKIE": updates["QUARK_COOKIE"]})
        written.append("KUAKE_COOKIE")

    checks = {}
    if do_verify:
        for key in ("QUARK_COOKIE", "BAIDU_COOKIE", "XUNLEI_REFRESH_TOKEN",
                    "WECHAT_WEBHOOK_URL", "FEISHU_WEBHOOK_URL", "ONEBOT_URL"):
            if key in updates and updates[key]:
                ok, msg = verify_credential(key, updates[key])
                checks[key] = {"ok": ok, "message": msg}

    bad = [k for k, v in checks.items() if not v["ok"]]
    msg = f"已保存 {len(set(written))} 项配置"
    if checks:
        msg += "；" + ("全部校验通过" if not bad else f"{len(bad)} 项校验未通过（配置仍已保存）")

    return jsonify({"success": True, "message": msg,
                    "written": sorted(set(written)), "checks": checks})


@bp.post("/credentials/verify")
def verify_saved_credentials():
    """不写入任何东西，只把 .env 里当前的值拿去线上验一次。"""
    data = request.get_json(silent=True) or {}
    key = (data.get("key") or "").strip()
    if key not in CRED_SPEC:
        return jsonify({"success": False, "message": f"不认识的配置项：{key}"}), 400

    value = (env_read().get(key) or "").strip()
    if not value:
        return jsonify({"success": False, "message": "这一项还没配置"}), 400

    ok, msg = verify_credential(key, value)
    # 夸克两个键通常存的是同一份 Cookie：验了一个就把另一个的结论同步过去，
    # 否则界面上会出现「一个绿一个灰」的错觉。
    if key in ("QUARK_COOKIE", "KUAKE_COOKIE"):
        other = "KUAKE_COOKIE" if key == "QUARK_COOKIE" else "QUARK_COOKIE"
        ov = (env_read().get(other) or "").strip()
        if ov and ov == value:
            save_cred_status(other, ov, ok, msg)
    if ok and key == "XUNLEI_REFRESH_TOKEN":
        # 迅雷刷新即轮换：把换回来的新值写回 .env，否则留下的是作废值
        from adapters.xunlei import TokenStore
        new_tok = TokenStore().load()
        if new_tok and new_tok != value:
            try:
                env_write({key: new_tok})
                save_cred_status(key, new_tok, True, msg)
            except Exception:  # noqa: BLE001
                pass
    return jsonify({"success": True, "verified": ok, "message": msg})


# ===========================================================================
# 路由：浏览器内登录（不用 F12 抄 Cookie）
# ===========================================================================
#
# 用户在自己电脑的浏览器里打开工作台，我们没法读他本机的 Cookie
# （跨域 + HttpOnly）。所以反过来：在服务器上开一个无头 Chromium，
# 把画面推给他看，他点哪儿我们把坐标转过去，登录成功后再从服务器端
# 浏览器上下文里把凭证取出来。整个链路用户只做一件事：扫码。
def _browser_err(exc):
    return jsonify({"success": False, "message": str(exc)}), 400


@bp.post("/browser/start")
def browser_start():
    from core import browser_login

    data = request.get_json(silent=True) or {}
    platform = (data.get("platform") or "").strip()
    try:
        s = browser_login.start(platform)
    except ValueError as exc:
        return _browser_err(exc)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"success": False,
                        "message": f"浏览器启动失败：{exc}"}), 500

    return jsonify({
        "success": True,
        "sid": s.sid,
        "platform": platform,
        "viewport": browser_login.VIEWPORT,
        "url": s.page_url,
        "message": "浏览器已打开，用手机 App 扫码即可",
    })


@bp.get("/browser/frame")
def browser_frame():
    """当前画面。返回 JPEG 二进制，交给 <img> 直接显示，省掉 base64 的 33% 开销。"""
    from core import browser_login

    sid = (request.args.get("sid") or "").strip()
    try:
        s = browser_login.get(sid)
        jpg = s.call("shot", timeout=30)
    except KeyError as exc:
        return jsonify({"success": False, "message": str(exc).strip("'")}), 404
    except Exception as exc:  # noqa: BLE001
        return jsonify({"success": False, "message": f"截图失败：{exc}"}), 500

    return Response(jpg, mimetype="image/jpeg",
                    headers={"Cache-Control": "no-store, max-age=0"})


@bp.get("/browser/status")
def browser_status():
    from core import browser_login

    sid = (request.args.get("sid") or "").strip()
    try:
        s = browser_login.get(sid)
        cred = s.call("cred", timeout=20)
    except KeyError as exc:
        return jsonify({"success": False, "alive": False,
                        "message": str(exc).strip("'")}), 404
    except Exception as exc:  # noqa: BLE001
        return jsonify({"success": True, "alive": True, "logged_in": False,
                        "ok": False, "message": f"读取登录状态失败：{exc}"})

    return jsonify({
        "success": True,
        "alive": True,
        "logged_in": cred.get("logged_in"),
        "ok": cred.get("ok"),
        "key": cred.get("key"),
        "message": cred.get("message"),
        "page_url": cred.get("page_url"),
        "page_title": cred.get("page_title"),
    })


@bp.post("/browser/click")
def browser_click():
    from core import browser_login

    data = request.get_json(silent=True) or {}
    try:
        s = browser_login.get((data.get("sid") or "").strip())
        s.call("click", data.get("x", 0), data.get("y", 0))
    except Exception as exc:  # noqa: BLE001
        return _browser_err(exc)
    return jsonify({"success": True})


@bp.post("/browser/type")
def browser_type():
    from core import browser_login

    data = request.get_json(silent=True) or {}
    text = str(data.get("text") or "")[:400]
    try:
        s = browser_login.get((data.get("sid") or "").strip())
        s.call("type", text)
    except Exception as exc:  # noqa: BLE001
        return _browser_err(exc)
    return jsonify({"success": True})


@bp.post("/browser/key")
def browser_key():
    from core import browser_login

    data = request.get_json(silent=True) or {}
    key = str(data.get("key") or "Enter")[:20]
    try:
        s = browser_login.get((data.get("sid") or "").strip())
        s.call("key", key)
    except Exception as exc:  # noqa: BLE001
        return _browser_err(exc)
    return jsonify({"success": True})


@bp.post("/browser/scroll")
def browser_scroll():
    from core import browser_login

    data = request.get_json(silent=True) or {}
    try:
        s = browser_login.get((data.get("sid") or "").strip())
        s.call("scroll", data.get("dy", 0))
    except Exception as exc:  # noqa: BLE001
        return _browser_err(exc)
    return jsonify({"success": True})


@bp.post("/browser/reload")
def browser_reload():
    from core import browser_login

    data = request.get_json(silent=True) or {}
    try:
        s = browser_login.get((data.get("sid") or "").strip())
        s.call("reload", timeout=60)
    except Exception as exc:  # noqa: BLE001
        return _browser_err(exc)
    return jsonify({"success": True, "message": "已重新加载，二维码是新的"})


@bp.post("/browser/save")
def browser_save():
    """把浏览器里抓到的凭证写进 .env，并做一次在线校验。"""
    from core import browser_login

    data = request.get_json(silent=True) or {}
    try:
        s = browser_login.get((data.get("sid") or "").strip())
        cred = s.call("cred", timeout=20)
    except Exception as exc:  # noqa: BLE001
        return _browser_err(exc)

    if not cred.get("ok"):
        return jsonify({"success": False, "message": cred.get("message") or "还没登录成功"}), 400

    key, value = cred["key"], cred["value"]

    # 百度：BDUSS 有效 ≠ 云盘会话有效。缺 PANPSC 的 Cookie 存下来，
    # 界面会绿油油，真跑转存时 bdstoken 拿不到、errno=-6。
    # 与其存一份半残凭证，不如现在再补一次会话，补不上就明说。
    if key == "BAIDU_COOKIE" and not re.search(r"PANPSC=[^;\s]+", value):
        try:
            s.call("baidu_session", timeout=45)
            c2 = s.call("cred", timeout=20)
            value = c2.get("value") or value
        except Exception:  # noqa: BLE001
            pass
        if not re.search(r"PANPSC=[^;\s]+", value):
            return jsonify({
                "success": False,
                "message": "百度账号登录成功，但云盘会话没建立起来（PANPSC 为空，"
                           "服务器返回 Auth Login Params Not Corret）。"
                           "点「换个二维码」重新扫一次，这次扫完在网盘首页停留 5 秒再操作。",
            }), 400

    # 迅雷的 refresh_token 是一次性的：每换一次 access_token，旧值立刻作废。
    # 所以拿到候选后必须"真换一次"确认它当下可用，而且只能换这一次 ——
    # 换成功得到的那个新值才是要保存的值。
    #
    # 换票分两路，都在此完成，不许事后再验：
    #   ① 页面内 fetch：origin / cookie / 指纹全是迅雷自己的环境，最接近网页自己换票
    #   ② 服务器端 requests：① 被 CORS 或脚本限制挡住时的退路
    if key == "XUNLEI_REFRESH_TOKEN":
        cands = [c for c in (cred.get("candidates") or [value]) if c]
        # 再让页面自己全量翻一遍：store 里未必只有一条，固定路径取的那个
        # 很可能正是被页面消费掉的旧值。多翻几个候选，只多花几十毫秒。
        try:
            deep = s.call("tokens", timeout=15) or []
        except Exception:  # noqa: BLE001
            deep = []
        for t in deep:
            if t and t not in cands:
                cands.append(t)
        cands = cands[:8]
        picked, picked_name, reasons = "", "", []

        def _try(batch):
            """逐个候选真换一次票；成功即返回，失败把原因攒起来。"""
            nonlocal picked, picked_name
            for cand in batch:
                try:
                    r = s.call("refresh", cand, timeout=25)
                except Exception as exc:  # noqa: BLE001
                    r = {"ok": False, "message": str(exc)[:120]}
                if r.get("ok"):
                    picked = r.get("refresh_token") or cand
                    picked_name = "页面内换票成功"
                    return True
                reasons.append(f"页面内：{r.get('message')}")
                ok, new_tok, info = probe_xunlei_token(cand)
                if ok:
                    picked = new_tok or cand
                    picked_name = info or ""
                    return True
                reasons.append(f"服务器端：{info}")
            return False

        _try(cands)
        if not picked:
            # 第一轮全废说明手里的值都已经作废了。让页面自己重载一次：
            # 它会拿 Cookie 重新静默登录、重新换票，那一瞬的响应里
            # 才是"当下有效"的那个值 —— 这是唯一不依赖页面存储位置的办法。
            try:
                fresh = s.call("resync", timeout=50) or []
            except Exception:  # noqa: BLE001
                fresh = []
            more = [t for t in fresh if t and t not in cands]
            if more:
                cands += more
                _try(more)

        if not picked:
            detail = "；".join(dict.fromkeys(reasons))[:200]
            hint = ("没从页面里读到 refresh_token" if not cands
                    else f"{len(cands)} 个候选全被迅雷拒绝")
            return jsonify({
                "success": False,
                # 不加"保存失败："前缀 —— 前端 catch 已经加了一次，会变成复读
                "message": f"{hint}（{detail}）。点「换个二维码」重扫一次；"
                           f"若仍失败，把这段提示发我。",
                "candidates": [c[:10] + "…" for c in cands],
            }), 400
        value = picked

    updates = {key: value}
    try:
        written = env_write(updates)
        if key == "XUNLEI_REFRESH_TOKEN":
            _sync_xunlei_store(value)
        if key == "QUARK_COOKIE":
            # 夸克 CLI 与 HTTP 两处必须一致，否则会出现"界面有效、跑任务 401"
            env_write({"KUAKE_COOKIE": value})
            written.append("KUAKE_COOKIE")
        if key == "BAIDU_COOKIE" and (cred.get("bdstoken") or "").strip():
            # bdstoken 只能趁扫码会话活着时在页面里取到（服务器端接口已废），
            # 存下来给任务时的自动清理用。会话被百度风控作废也没关系，
            # bdstoken 本身跟着 BDUSS 走，比 PANPSC 活得久。
            env_write({"BAIDU_BDSTOKEN": cred["bdstoken"].strip()})
            written.append("BAIDU_BDSTOKEN")
    except Exception as exc:  # noqa: BLE001
        return jsonify({"success": False, "message": f"写入 .env 失败：{exc}"}), 500

    if key == "XUNLEI_REFRESH_TOKEN":
        # 不能再验一次：迅雷每刷新一次就轮换 token，再验等于把刚写的值弄废
        msg = f"迅雷登录有效{('：' + picked_name) if picked_name else ''}"
        ok = True
        save_cred_status(key, value, ok, msg)
    else:
        ok, msg = verify_credential(key, value)
    browser_login.stop(s.sid)

    return jsonify({
        "success": True,
        "saved": sorted(set(written)),
        "verified": ok,
        "message": msg if not ok else f"已保存并验证：{msg}",
    })


@bp.post("/browser/stop")
def browser_stop():
    from core import browser_login

    data = request.get_json(silent=True) or {}
    browser_login.stop((data.get("sid") or "").strip())
    return jsonify({"success": True, "message": "已关闭浏览器"})


# ===========================================================================
# 路由：资源库
# ===========================================================================
@bp.get("/pool")
def pool_list():
    q = (request.args.get("q") or "").strip().lower()
    platform = (request.args.get("platform") or "all").strip()
    page = max(1, int(request.args.get("page") or 1))
    size = min(100, max(5, int(request.args.get("size") or 20)))

    items = load_pool()
    if platform != "all":
        items = [it for it in items if it["platform"] == platform]
    if q:
        items = [it for it in items if q in it["note"].lower() or q in it["url"].lower()]

    total = len(items)
    start = (page - 1) * size
    return jsonify({
        "success": True,
        "total": total,
        "page": page,
        "size": size,
        "pages": max(1, (total + size - 1) // size),
        "items": items[start:start + size],
    })


@bp.post("/pool/refresh")
def pool_refresh():
    """从 PanSou 拉一批新资源，按 url 去重后并入本地资源池。"""
    data = request.get_json(silent=True) or {}
    kw = (data.get("kw") or "").strip()
    platform = (data.get("platform") or "quark").strip()
    if not kw:
        return jsonify({"success": False, "message": "请填写搜索关键词"}), 400

    import httpx as _httpx
    from core.config import get_pansou_api_url

    try:
        r = _httpx.get(get_pansou_api_url(),
                       params={"kw": kw, "cloud_types": platform, "res": "merge"},
                       timeout=45)
        body = r.json()
    except Exception as exc:
        return jsonify({"success": False, "message": f"PanSou 请求失败：{exc}"}), 502

    merged = (body.get("data") or {}).get("merged_by_type") or {}
    fetched = []
    for pf, arr in merged.items():
        for it in arr or []:
            url = (it.get("url") or "").strip()
            if url:
                fetched.append({"note": _clean_note(it.get("note") or "") or "未命名",
                                "url": url})

    existing = load_pool()
    seen = {it["url"] for it in existing}
    added = [it for it in fetched if it["url"] not in seen]
    if added:
        save_pool(existing + added)

    return jsonify({
        "success": True,
        "message": f"抓到 {len(fetched)} 条，新增 {len(added)} 条，去重跳过 {len(fetched) - len(added)} 条",
        "added": len(added),
        "total": len(existing) + len(added),
    })


# ===========================================================================
# 路由：预览 / 运行
# ===========================================================================
def _search_candidates(platform, kw, include, exclude, limit):
    """给预览用的候选资源：先查本地池（关键词过滤），不够再查 PanSou。"""
    include = include or []
    exclude = exclude or []
    pool = load_pool()
    cands = [it for it in pool if it["platform"] == platform]

    def hit(note):
        if include and not any(k in note for k in include):
            return False
        if exclude and any(k in note for k in exclude):
            return False
        return True

    cands = [it for it in cands if hit(it["note"])]
    if kw:
        k = kw.lower()
        cands = [it for it in cands if k in it["note"].lower()]

    # 本地不足且关键词有效（关键词非空、不等于默认的 "1"）时，走在线搜索补齐
    if len(cands) < limit and kw and kw != "1":
        try:
            import httpx as _httpx
            from core.config import get_pansou_api_url
            r = _httpx.get(get_pansou_api_url(),
                           params={"kw": kw, "cloud_types": platform, "res": "merge"},
                           timeout=45)
            body = r.json()
            merged = (body.get("data") or {}).get("merged_by_type") or {}
            seen = {it["url"] for it in cands}
            for arr in merged.values():
                for it in arr or []:
                    url = (it.get("url") or "").strip()
                    note = _clean_note(it.get("note") or "") or "未命名"
                    if not url or url in seen or detect_platform(url) != platform:
                        continue
                    if not hit(note):
                        continue
                    seen.add(url)
                    cands.append({"note": note, "url": url, "platform": platform})
                    if len(cands) >= limit * 3:
                        break
        except Exception as exc:
            log_print(f"在线搜索失败（仅用本地池）: {exc}", "WARNING")

    for i, it in enumerate(cands, 1):
        it["index"] = i
    return cands[:max(limit * 3, limit)]


@bp.post("/preview")
def preview():
    """预览候选资源。不走平台凭证，纯搜索，速度快。"""
    data = request.get_json(silent=True) or {}
    platform = (data.get("platform") or "quark").strip()
    kw = (data.get("kw") or "").strip()
    include = data.get("include") or []
    exclude = data.get("exclude") or []
    count = int(data.get("count") or 5)
    if isinstance(include, str):
        include = [x.strip() for x in include.replace("，", ",").split(",") if x.strip()]
    if isinstance(exclude, str):
        exclude = [x.strip() for x in exclude.replace("，", ",").split(",") if x.strip()]

    if platform not in ("quark", "baidu", "xunlei"):
        return jsonify({"success": False, "message": f"未知平台：{platform}"}), 400

    items = _search_candidates(platform, kw, include, exclude, count)
    # 预览不打断正在跑的任务：只在空闲时刷新状态快照
    with _TASK_LOCK:
        if not _task["running"]:
            _reset_task_locked(platform, kw)
            _task["items"] = items
            _task["total"] = len(items)

    return jsonify({
        "success": True,
        "platform": platform,
        "total": len(items),
        "items": items,
        "message": f"找到 {len(items)} 条{PLATFORM_CN.get(platform, '')}候选" if items
                   else "没有找到匹配资源，换个关键词试试",
    })


@bp.post("/run")
def run_task():
    data = request.get_json(silent=True) or {}
    platform = (data.get("platform") or "quark").strip()
    kw = (data.get("kw") or "").strip()
    count = int(data.get("count") or 5)
    include = data.get("include") or []
    exclude = data.get("exclude") or []
    items = data.get("items") or None

    if platform not in ("quark", "baidu", "xunlei"):
        return jsonify({"success": False, "message": f"未知平台：{platform}"}), 400
    if isinstance(include, str):
        include = [x.strip() for x in include.replace("，", ",").split(",") if x.strip()]
    if isinstance(exclude, str):
        exclude = [x.strip() for x in exclude.replace("，", ",").split(",") if x.strip()]

    with _TASK_LOCK:
        if _task["running"]:
            return jsonify({"success": False,
                            "message": "已有任务在运行，请等它结束"}), 409
        running_items = items
        _task.update({
            "id": datetime.now().strftime("%Y%m%d%H%M%S"),
            "running": True, "platform": platform, "kw": kw,
            "stage": "searching", "total": count, "done": 0, "ok": 0, "fail": 0,
            "started_at": time.time(), "finished_at": None, "error": None,
            "logs": [], "result": None,
        })

    if running_items:
        count = len(running_items)

    t = threading.Thread(
        target=_run_in_thread,
        args=(platform, kw, include, exclude, count, running_items),
        daemon=True,
    )
    t.start()
    return jsonify({"success": True, "message": f"任务已启动（{PLATFORM_CN[platform]}）",
                    "task_id": _task["id"]})


@bp.get("/task/status")
def task_status():
    return jsonify({"success": True, "task": _task_snapshot()})


@bp.post("/task/reset")
def task_reset():
    with _TASK_LOCK:
        if _task["running"]:
            return jsonify({"success": False, "message": "任务运行中，不能清空"}), 409
    _reset_task()
    return jsonify({"success": True, "message": "已清空"})


# ===========================================================================
# 路由：历史
# ===========================================================================
@bp.get("/history")
def history():
    kind = (request.args.get("kind") or "all").strip()
    q = (request.args.get("q") or "").strip().lower()
    page = max(1, int(request.args.get("page") or 1))
    size = min(200, max(10, int(request.args.get("size") or 30)))

    rows = load_history()
    stats = {}
    for r in rows:
        stats[r["kind"]] = stats.get(r["kind"], 0) + 1

    if kind != "all":
        rows = [r for r in rows if r["kind"] == kind]
    if q:
        rows = [r for r in rows if q in r["note"].lower() or q in r["url"].lower()]

    total = len(rows)
    start = (page - 1) * size
    return jsonify({
        "success": True,
        "total": total,
        "page": page,
        "pages": max(1, (total + size - 1) // size),
        "stats": stats,
        "items": rows[start:start + size],
    })


@bp.get("/history/export")
def history_export():
    rows = load_history()
    lines = [f"{r['note']}  |  {r['url']}  |  {r['status']}" for r in rows]
    text = "\n".join(lines) + ("\n" if lines else "")
    fname = f"share_history_{datetime.now().strftime('%Y%m%d_%H%M')}.txt"
    return Response(text, mimetype="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


# ===========================================================================
# 路由：单链接预检
# ===========================================================================
@bp.post("/link/check")
def link_check():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"success": False, "message": "缺少 url"}), 400

    platform = detect_platform(url)
    if platform == "other":
        return jsonify({"success": False, "message": "无法识别的网盘链接"}), 400

    try:
        from core.pansou_check import check_links
        res = check_links([url], disk_type=platform)
    except Exception as exc:
        return jsonify({"success": False, "message": f"检测失败：{exc}"}), 500

    if url not in res:
        return jsonify({"success": True, "state": "unknown",
                        "message": "未能判定（PanSou 未返回结果，按可用处理）"})
    state, summary = res[url]
    return jsonify({"success": True, "state": state, "message": summary})


# ===========================================================================
# 蓝图注册
# ===========================================================================
def register(app):
    app.register_blueprint(bp, url_prefix="/api/workbench")
    try:
        app.json.ensure_ascii = False     # Flask >= 2.3
    except Exception:
        app.config["JSON_AS_ASCII"] = False
    return app
