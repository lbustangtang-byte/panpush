#!/usr/bin/env bash
# 停止 PanSou 与 Web 界面
set +e
cd "$(dirname "$0")"

if [ -f .venv/bin/activate ]; then
  . .venv/bin/activate
fi

PORT=$(cat data/web.port 2>/dev/null || echo 5003)

echo "==> 停止 Web 管理界面"
STOPPED=0
if [ -f data/web.pid ]; then
  PID=$(cat data/web.pid)
  if kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null
    sleep 1
    kill -9 "$PID" 2>/dev/null
    echo "    已停止 PID $PID"
    STOPPED=1
  fi
  rm -f data/web.pid
fi
if [ "$STOPPED" = "0" ]; then
  # 按端口兜底（不按进程名匹配，避免误伤当前 shell）
  if command -v fuser >/dev/null 2>&1; then
    if fuser -k "${PORT}/tcp" 2>/dev/null; then
      echo "    已停止占用 ${PORT} 端口的进程"
    else
      echo "    端口 ${PORT} 无监听进程"
    fi
  else
    echo "    未找到 pid 文件，且系统无 fuser，请手动 kill"
  fi
fi

echo "==> 停止 PanSou"
python main.py pansou stop 2>/dev/null || echo "    PanSou 未运行"
