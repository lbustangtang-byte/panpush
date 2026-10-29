#!/usr/bin/env bash
# netdisk2weixin 一键部署（Ubuntu / Debian，适配 1.7G 小内存服务器）
# 用法: bash deploy.sh
set -e
cd "$(dirname "$0")"

echo "==> [1/5] 检查 Python"
command -v python3 >/dev/null 2>&1 || { echo "缺少 python3，请先 apt install -y python3 python3-venv"; exit 1; }
python3 -V
if ! python3 -c "import venv" >/dev/null 2>&1; then
  echo "    安装 python3-venv ..."
  sudo apt-get update -qq && sudo apt-get install -y python3-venv
fi

echo "==> [2/5] 创建虚拟环境并安装依赖（清华源）"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
. .venv/bin/activate
pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple --upgrade pip
pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

echo "==> [3/5] 赋予二进制执行权限"
chmod +x assets/bin/kuake-*-linux-amd64 2>/dev/null || true
chmod +x pansou/dist/pansou-linux-amd64 2>/dev/null || true

echo "==> [4/5] 生成 .env（已存在则保留，不覆盖你的密钥）"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "    已生成 .env —— 请填写 QUARK_COOKIE / KUAKE_COOKIE / WECHAT_WEBHOOK_URL"
else
  echo "    .env 已存在，跳过"
fi
mkdir -p data/logs data/share

echo "==> [5/5] 环境诊断"
python main.py diagnose || true

echo ""
echo "部署完成。"
echo "  启动: bash start.sh    （PanSou 搜索服务 + Web 界面 :5003）"
echo "  停止: bash stop.sh"
