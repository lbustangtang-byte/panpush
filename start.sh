#!/usr/bin/env bash
# 手动启动：PanSou 搜索服务(8888) + Web 管理界面(5003)
# 用法: bash start.sh [web端口]
set -e
cd "$(dirname "$0")"
PORT="${1:-5003}"

. .venv/bin/activate
mkdir -p data/logs data/share

echo "==> 启动 PanSou 搜索服务"
python main.py pansou start --wait 60 || echo "    [!] PanSou 未能就绪，转存前的链接预检会降级放行"

echo "==> 启动 Web 管理界面 (端口 $PORT)"
if [ -f data/web.pid ] && kill -0 "$(cat data/web.pid)" 2>/dev/null; then
  echo "    Web 已在运行 (PID $(cat data/web.pid))，跳过"
else
  setsid nohup python serve_web.py "$PORT" > data/logs/web.log 2>&1 < /dev/null &
  echo $! > data/web.pid
  echo "$PORT" > data/web.port
  sleep 3
  if kill -0 "$(cat data/web.pid)" 2>/dev/null; then
    echo "    Web 已启动 (PID $(cat data/web.pid))"
  else
    echo "    [!] Web 启动失败，查看 data/logs/web.log"
    tail -20 data/logs/web.log
    exit 1
  fi
fi

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo ""
echo "访问地址: http://${IP:-127.0.0.1}:${PORT}"
if grep -qE '^WEB_PASSWORD=.+' .env 2>/dev/null; then
  echo "外网访问: http://<公网IP>:${PORT}  （已启用密码登录）"
else
  echo "外网访问: 未开启 —— .env 里 WEB_PASSWORD 为空时只允许本机访问"
  echo "          要开放外网：设 WEB_PASSWORD 后重启，并在华为云安全组放行 ${PORT}"
fi
echo "日志:     data/logs/web.log  /  data/logs/pansou.log"
echo "停止:     bash stop.sh"
