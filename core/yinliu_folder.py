"""引流文件夹插入（复制模式）。

思路：不在本地上传文件，而是把「网盘上已经存在的某个文件夹」整份复制到
本次转存出来的资源文件夹里。三平台各有一套复制接口：

    夸克   kuake copy <src> <dest>
    百度   POST /api/filemanager?opera=copy
    迅雷   POST /drive/v1/files:batchCopy

这样引流内容只需要在网盘里维护一份，任务跑多少次都不会重复上传；
换成自己的引流也只要改网盘里那个文件夹的内容。

本模块只负责「配置读取 + 路径规整 + 统一日志/异常收口」，
真正的复制动作由各适配器传入的 copy_fn 完成，签名：

    copy_fn(src: str, dest_dir: str) -> dict
    返回 {"ok": bool, "message": str, "skipped": bool}

dest_dir 语义：本次资源所在的**文件夹路径**（不是分享标题拼接）。
适配器负责把 fid 解析成路径，或在内部把路径换成 fid。

2026-10-29：彻底移除「上传本地引流文件」这条兜底路。以前没配文件夹时会
退回去往每个人的分享里塞同一个 PDF，现在只认文件夹复制：没配就当作不做引流，
宁可没有，也不再往项目里塞本地素材。
"""
from __future__ import annotations

import os
from typing import Any, Callable, Dict, Optional

from core.logger import log_print

PLATFORM_CN = {"quark": "夸克", "baidu": "百度", "xunlei": "迅雷"}


def normalize_remote_path(path: str) -> str:
    """把网盘路径规整成 /a/b 形式，去掉多余斜杠与首尾空白。"""
    p = str(path or "").strip().replace("\\", "/")
    if not p:
        return ""
    p = "/" + "/".join(seg for seg in p.split("/") if seg)
    return "/" if p == "/" else p


def get_folder(config: Dict[str, Any], platform: str) -> str:
    """取某平台的引流文件夹路径（绝对路径），未配置返回空串。"""
    cfg = (config or {}).get(platform) or {}
    return normalize_remote_path(cfg.get("yinliu_folder") or "")


def is_enabled(config: Dict[str, Any], platform: str) -> bool:
    """是否启用复制式引流：总开关打开 + 该平台配置了文件夹。"""
    meta = (config or {}).get("yinliu_folder") or {}
    if not meta.get("enabled", True):
        return False
    return bool(get_folder(config, platform))


def folder_name(folder: str) -> str:
    """取引流文件夹自身的名字，用于同名检测。"""
    return os.path.basename(normalize_remote_path(folder).rstrip("/"))


def plan(config: Dict[str, Any], platform: str, dest_dir: str) -> Optional[Dict[str, str]]:
    """生成一次插入计划；不需要插入时返回 None。"""
    if not is_enabled(config, platform):
        return None
    src = get_folder(config, platform)
    dest = normalize_remote_path(dest_dir)
    if not dest:
        return None
    # 目标就是源的父目录：再复制一份会变成 /父目录/引流 与源本身同名，
    # 既无意义又可能触发同名冲突，直接跳过。
    if dest == normalize_remote_path(os.path.dirname(src)) and folder_name(src):
        return None
    return {"platform": platform, "src": src, "dest": dest,
            "name": folder_name(src)}


def insert(
    config: Dict[str, Any],
    platform: str,
    dest_dir: str,
    copy_fn: Callable[[str, str], Dict[str, Any]],
) -> Dict[str, Any]:
    """把引流文件夹复制到 dest_dir。

    永不抛异常：失败只打日志并返回 {"ok": False}，避免因为引流这一步
    把已经成功的转存/分享整体判死。
    """
    cn = PLATFORM_CN.get(platform, platform)
    p = plan(config, platform, dest_dir)
    if not p:
        return {"ok": False, "skipped": True, "message": "未配置引流文件夹"}

    log_print(f"[{cn}] 复制引流文件夹 {p['src']} → {p['dest']}", "INFO")
    try:
        res = copy_fn(p["src"], p["dest"]) or {}
    except Exception as exc:  # noqa: BLE001 - 引流失败不影响主流程
        log_print(f"[{cn}] 引流文件夹复制异常: {exc}", "WARNING")
        return {"ok": False, "skipped": False, "message": str(exc),
                "src": p["src"], "dest": p["dest"]}

    if res.get("skipped"):
        log_print(f"[{cn}] 引流文件夹已存在，跳过: {p['dest']}/{p['name']}", "INFO")
    elif res.get("ok"):
        log_print(f"[{cn}] 引流文件夹已复制到: {p['dest']}", "SUCCESS")
    else:
        log_print(f"[{cn}] 引流文件夹复制失败: {res.get('message') or '未知原因'}",
                  "WARNING")
    res.setdefault("src", p["src"])
    res.setdefault("dest", p["dest"])
    return res
