"""引流清理通用引擎（三平台共用）。

夸克原先在 adapters/quark.py 里有一套基于路径的清理实现；本模块把同样的
规则抽象成「节点 + 三个原语」，让百度、迅雷复用同一套判定，行为保持一致：

    ls_fn(node)              -> List[node]
    delete_fn(node)          -> bool
    rename_fn(node, new_name)-> bool

node = {"path": 展示路径, "id": 平台内部 id, "name": 文件名,
        "size": 字节数, "dir": 是否目录}

判定顺序（与夸克一致，不能调换）：
  ① 命中排除名单（自己的引流文件夹/文件）→ 保留
  ② 文件名里带别人的微信/QQ → 改名成自己的号，**不删**（删了等于删课）
  ③ 命中引流关键词 → 删除
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from core.logger import log_print


def _fmt_size(size: Any) -> str:
    try:
        n = int(size or 0)
    except (TypeError, ValueError):
        n = 0
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n / 1024 / 1024:.1f}MB"


def write_record(records: List[Dict[str, Any]], stats: Dict[str, int],
                 platform: str = "") -> Optional[str]:
    """把本次清理明细追加写入 data/logs/yinliu_clean_YYYY-MM-DD.txt。"""
    from core.config import LOG_DIR

    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        now = datetime.now()
        out_path = os.path.join(str(LOG_DIR), f"yinliu_clean_{now:%Y-%m-%d}.txt")
        with open(out_path, "a", encoding="utf-8") as f:
            f.write("=" * 70 + "\n")
            f.write(f"引流清理记录  {now:%Y-%m-%d %H:%M:%S}"
                    f"{'  [' + platform + ']' if platform else ''}\n")
            f.write(f"扫描 {stats.get('checked', 0)} 个文件，"
                    f"删除 {stats.get('deleted', 0)} 个引流文件，"
                    f"改写联系方式 {stats.get('renamed', 0)} 个\n")
            f.write("-" * 70 + "\n")
            for r in records:
                if r.get("deleted"):
                    flag = "[已删除]"
                elif r.get("renamed"):
                    flag = "[已改名]"
                elif r.get("is_yinliu"):
                    flag = "[引流未删]"
                elif r.get("is_dir"):
                    flag = "[目录]"
                else:
                    flag = "[保留]"
                reason = f"  <{r['reason']}>" if r.get("reason") else ""
                f.write(f"{flag} {r.get('path', '')}  ({_fmt_size(r.get('size'))}){reason}\n")
            f.write("\n")
        log_print(f"引流清理记录已写入: {out_path}", "INFO")
        return out_path
    except Exception as e:  # noqa: BLE001
        log_print(f"写入引流清理记录失败: {e}", "WARNING")
        return None


def clean_tree(
    platform: str,
    roots: List[Dict[str, Any]],
    filter_cfg: Dict[str, Any],
    ls_fn: Callable[[Dict[str, Any]], List[Dict[str, Any]]],
    delete_fn: Callable[[Dict[str, Any]], bool],
    rename_fn: Callable[[Dict[str, Any], str], bool],
    excludes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """递归清理一棵目录树里的引流文件。

    参数都是可注入的原语，平台差异（CLI / REST / fid vs path）全在调用方消化。
    任何单个条目的失败只记日志，不中断整轮清理。
    """
    from core.yinliu_filter import match_yinliu, rewrite_contact

    keywords = filter_cfg.get("keywords") or None
    extensions = filter_cfg.get("extensions") or None
    max_bytes = filter_cfg.get("max_small_file_bytes")
    my_contact = filter_cfg.get("my_contact")
    aggressive = bool(filter_cfg.get("aggressive_ext_check"))

    all_excludes = list(filter_cfg.get("excludes") or [])
    if excludes:
        all_excludes.extend([e for e in excludes if e])

    deleted: List[str] = []
    renamed: List[str] = []
    records: List[Dict[str, Any]] = []
    checked = 0

    def _excluded(name: str) -> bool:
        base = os.path.basename(name or "")
        return any(x and x in base for x in all_excludes)

    def _handle(node: Dict[str, Any]) -> bool:
        """处理单个节点，返回 True 表示已被删除（删除的目录不再下钻）。"""
        nonlocal checked
        checked += 1

        name = node.get("name") or os.path.basename(node.get("path") or "")
        path = node.get("path") or name
        size = node.get("size") or 0
        is_dir = bool(node.get("dir"))

        if _excluded(name):
            records.append({"path": path, "size": size, "is_dir": is_dir,
                            "is_yinliu": False, "deleted": False, "renamed": False,
                            "reason": "排除名单"})
            return False

        new_name = rewrite_contact(name, my_contact)
        if new_name:
            log_print(f"[{platform}] 发现他人联系方式: {name}", "INFO")
            try:
                ok = rename_fn(node, new_name)
            except Exception as e:  # noqa: BLE001
                ok = False
                log_print(f"[{platform}] 改名异常: {name} - {e}", "WARNING")
            if ok:
                renamed.append(path)
                # 路径同步更新，否则后续按路径下钻会找不到
                parent = os.path.dirname(path)
                node["name"] = new_name
                node["path"] = f"{parent.rstrip('/')}/{new_name}" if parent else new_name
                log_print(f"[{platform}] 已改名: {name} → {new_name}", "SUCCESS")
                records.append({"path": path, "size": size, "is_dir": is_dir,
                                "is_yinliu": False, "deleted": False, "renamed": True,
                                "reason": f"联系方式改写为 {new_name}"})
                return False
            log_print(f"[{platform}] 改名失败，继续按引流判定: {path}", "WARNING")

        is_yl, reason = match_yinliu(
            path, size,
            keywords=keywords,
            extensions=extensions,
            max_small_file_bytes=max_bytes,
            extra_excludes=all_excludes or None,
            aggressive_ext_check=aggressive,
        )

        if not is_yl:
            records.append({"path": path, "size": size, "is_dir": is_dir,
                            "is_yinliu": False, "deleted": False, "renamed": False,
                            "reason": ""})
            return False

        log_print(f"[{platform}] 发现引流文件: {path} | {reason}", "INFO")
        try:
            ok = delete_fn(node)
        except Exception as e:  # noqa: BLE001
            ok = False
            log_print(f"[{platform}] 删除异常: {path} - {e}", "WARNING")
        if ok:
            deleted.append(path)
            log_print(f"[{platform}] 已删除: {path}", "SUCCESS")
        else:
            log_print(f"[{platform}] 删除失败: {path}", "WARNING")
        records.append({"path": path, "size": size, "is_dir": is_dir,
                        "is_yinliu": True, "deleted": bool(ok), "renamed": False,
                        "reason": reason})
        return bool(ok)

    # 顶层是文件就直接判；是目录就广度优先下钻
    queue: List[Dict[str, Any]] = []
    for root in roots or []:
        if root.get("dir"):
            queue.append(root)
        else:
            _handle(root)

    while queue:
        node = queue.pop(0)
        try:
            children = ls_fn(node) or []
        except Exception as e:  # noqa: BLE001
            log_print(f"[{platform}] 列目录失败: {node.get('path')} - {e}", "WARNING")
            continue

        # 空目录 + 引流话术 → 整条删掉。
        # 这里有 children 已在手，判断「是否为空」是零成本的；
        # 必须是空目录才删，非空目录哪怕名字再像也不动，防止误删正课。
        if not children:
            from core.yinliu_filter import match_empty_dir_bait

            _name = node.get("name") or os.path.basename(node.get("path") or "")
            checked += 1
            if not _excluded(_name):
                _bait, _why = match_empty_dir_bait(_name, keywords)
                if _bait:
                    _path = node.get("path") or _name
                    log_print(f"[{platform}] 发现空引流目录: {_path} | {_why}", "INFO")
                    try:
                        _ok = delete_fn(node)
                    except Exception as e:  # noqa: BLE001
                        _ok = False
                        log_print(f"[{platform}] 删除异常: {_path} - {e}", "WARNING")
                    if _ok:
                        deleted.append(_path)
                        log_print(f"[{platform}] 已删除空引流目录: {_path}", "SUCCESS")
                    records.append({"path": _path, "size": 0, "is_dir": True,
                                    "is_yinliu": True, "deleted": bool(_ok),
                                    "renamed": False, "reason": _why})
                    continue
            records.append({"path": node.get("path") or _name, "size": 0,
                            "is_dir": True, "is_yinliu": False, "deleted": False,
                            "renamed": False, "reason": "空目录"})

        for child in children:
            removed = _handle(child)
            if child.get("dir") and not removed:
                queue.append(child)

    log_print(f"[{platform}] 引流清理完成：检查 {checked} 个，删除 {len(deleted)} 个，"
              f"改写联系方式 {len(renamed)} 个", "INFO")
    write_record(records, {"checked": checked, "deleted": len(deleted),
                           "renamed": len(renamed)}, platform)
    return {"deleted": deleted, "renamed": renamed, "total": checked}
