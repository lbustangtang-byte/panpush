"""统一日志工具。

提供 log_print()，供适配器统一输出带时间戳的日志。
"""
import os
from datetime import datetime
from .config import LOG_DIR

os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = str(LOG_DIR / "run.log")

# 可选监听器：工作台用它实时抓取任务日志（无监听器时零开销）。
# 回调签名 listener(line: str, level: str, message: str)。
_LISTENERS = []


def add_listener(fn):
    """注册日志监听器（重复注册会被忽略）。"""
    if fn not in _LISTENERS:
        _LISTENERS.append(fn)


def remove_listener(fn):
    """注销日志监听器，不存在时静默返回。"""
    try:
        _LISTENERS.remove(fn)
    except ValueError:
        pass


def log_print(message, level="INFO"):
    """打印并写入日志文件。"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [{level}] {message}"
    for fn in list(_LISTENERS):
        try:
            fn(line, level, str(message))
        except Exception:
            pass
    safe = str(message).encode("utf-8", errors="replace").decode("utf-8")
    try:
        print(safe)
    except Exception:
        try:
            import sys
            sys.stdout.buffer.write((safe + "\n").encode("utf-8", errors="replace"))
            sys.stdout.flush()
        except Exception:
            pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    return message