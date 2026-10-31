"""引流文件夹插入（复制模式）。

思路：不在本地上传文件，而是把「网盘上已经存在的某个文件夹」整份复制到
本次转存出来的资源文件夹里。三平台各有一套复制接口：

    夸克   kuake copy <src> <dest>
    百度   POST /api/filemanager?opera=copy
    迅雷   POST /drive/v1/files:batchCopy

这样引流内容只需要在网盘里维护一份，任务跑多少次都不会重复上传；
换成自己的引流也只要改网盘里那个文件夹的内容。

本模块只负责「配置读取 + 路径规整 + 统一日志/异常收口」，
真正的复制动作由各适配器传入的 list_fn / copy_fn 完成，签名：

    list_fn(src: str) -> list[{"name": str, "path": str, "dir": bool}]
        列出引流文件夹的**直接子项**（只下一层，不递归）
    copy_fn(src: str, dest_dir: str) -> dict
        把单个子项复制到 dest_dir
        返回 {"ok": bool, "message": str, "skipped": bool}

dest_dir 语义：本次资源所在的**文件夹路径**（不是分享标题拼接）。
适配器负责把 fid 解析成路径，或在内部把路径换成 fid。

语义（2026-10-31 按用户口径修正）：**复制引流文件夹下面的子文件夹/文件**，
而不是把引流文件夹本身（连壳带名字）塞进资源目录。这样别人打开资源时
看到的就是「扫码图 + 群链接"这些内容本身，不会多出一层
叫「替换引流文件」的外壳文件夹。

2026-10-29：彻底移除「上传本地引流文件」这条兜底路。以前没配文件夹时会
退回去往每个人的分享里塞同一个 PDF，现在只认文件夹复制：没配就当作不做引流，
宁可没有，也不再往项目里塞本地素材。
"""
from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional

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
    # 目标就是源本身：把子项复制进引流文件夹毫无意义，直接跳过。
    if dest == normalize_remote_path(src):
        return None
    return {"platform": platform, "src": src, "dest": dest,
            "name": folder_name(src)}


def insert(
    config: Dict[str, Any],
    platform: str,
    dest_dir: str,
    list_fn: Callable[[str], List[Dict[str, Any]]],
    copy_fn: Callable[[str, str], Dict[str, Any]],
) -> Dict[str, Any]:
    """把引流文件夹**里面的子文件/子文件夹**逐个复制到 dest_dir。

    只复制一层（不递归展开子文件夹内部），保持用户建文件夹时的结构。
    永不抛异常：失败只打日志并返回 {"ok": False}，避免因为引流这一步
    把已经成功的转存/分享整体判死。
    """
    cn = PLATFORM_CN.get(platform, platform)
    p = plan(config, platform, dest_dir)
    if not p:
        return {"ok": False, "skipped": True, "message": "未配置引流文件夹"}

    try:
        children = list_fn(p["src"]) or []
    except Exception as exc:  # noqa: BLE001
        log_print(f"[{cn}] 读取引流文件夹失败: {exc}", "WARNING")
        return {"ok": False, "skipped": False, "message": str(exc),
                "src": p["src"], "dest": p["dest"]}

    if not children:
        log_print(f"[{cn}] 引流文件夹是空的，跳过: {p['src']}", "WARNING")
        return {"ok": False, "skipped": True, "message": "引流文件夹为空",
                "src": p["src"], "dest": p["dest"]}

    log_print(f"[{cn}] 复制引流内容 {p['src']} 下的 {len(children)} 项 → {p['dest']}",
              "INFO")
    ok_cnt, skip_cnt, errors = 0, 0, []
    for item in children:
        name = item.get("name") or ""
        src_path = item.get("path") or ""
        if not src_path:
            errors.append(f"{name}: 无路径")
            continue
        try:
            res = copy_fn(src_path, p["dest"]) or {}
        except Exception as exc:  # noqa: BLE001 - 单项失败不影响其余
            errors.append(f"{name}: {exc}")
            continue
        if res.get("skipped"):
            skip_cnt += 1
        elif res.get("ok"):
            ok_cnt += 1
        else:
            errors.append(f"{name}: {res.get('message') or '未知原因'}")

    total = len(children)
    if errors:
        log_print(f"[{cn}] 引流复制部分失败（成功 {ok_cnt}、已存在 {skip_cnt}、"
                  f"失败 {len(errors)}/{total}）: {'；'.join(errors[:3])}", "WARNING")
    else:
        log_print(f"[{cn}] 引流内容已复制（成功 {ok_cnt}、已存在 {skip_cnt}）→ "
                  f"{p['dest']}", "SUCCESS")

    return {"ok": ok_cnt > 0 or (skip_cnt == total), "skipped": skip_cnt == total,
            "message": f"成功 {ok_cnt}、已存在 {skip_cnt}、失败 {len(errors)}",
            "count": total, "ok_count": ok_cnt, "errors": errors,
            "src": p["src"], "dest": p["dest"]}

