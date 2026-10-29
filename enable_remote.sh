#!/usr/bin/env bash
# 一键开启工作台外网访问：生成随机密码 → 写入 .env → 重启 → 提示安全组
# 用法: bash enable_remote.sh [端口]
set -e
cd "$(dirname "$0")"
PORT="${1:-5003}"

if [ ! -f .env ]; then
  echo "[!] 找不到 .env，先跑 bash deploy.sh"
  exit 1
fi

# 只取字母数字，避免密码里的 /+= 被 URL 或 shell 吞掉
PWD_GEN=$(openssl rand -base64 32 | tr -dc 'A-Za-z0-9' | cut -c1-16)

sed -i "s|^WEB_PASSWORD=.*|WEB_PASSWORD=${PWD_GEN}|" .env
if ! grep -qE '^WEB_USER=' .env; then
  echo 'WEB_USER=admin' >> .env
fi

echo "==> 已生成访问密码，正在重启工作台"
bash stop.sh >/dev/null 2>&1 || true
bash start.sh "$PORT" 2>&1 | tail -8

PUBIP=$(curl -s -m 5 ifconfig.me 2>/dev/null || echo "")
echo ""
echo "============================================================"
echo " 访问地址: http://${PUBIP:-<你的公网IP>}:${PORT}"
echo " 用户名  : $(grep -E '^WEB_USER=' .env | cut -d= -f2)"
echo " 密  码  : ${PWD_GEN}"
echo "============================================================"
echo " 上面密码只显示这一次，请立刻保存到密码管理器。"
echo ""
echo " 还没打开的话，检查这两处："
echo "   1. 华为云控制台 → 安全组 → 入方向 → 放行 TCP ${PORT}"
echo "   2. 服务器上执行: sudo ufw allow ${PORT}/tcp"
echo "============================================================"
