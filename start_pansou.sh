#!/usr/bin/env bash
# 启动 PanSou 搜索服务（必须注入 ENABLED_PLUGINS，否则插件数为 0、搜索永远为空）
# 插件/频道列表由 core/pansou_launcher.py 从 pansou/docker-compose.yml 解析后注入。
set -e
cd "$(dirname "$0")"
[ -f .venv/bin/activate ] && . .venv/bin/activate

python main.py pansou start --wait 60

echo "---- 健康检查 ----"
curl -s -m 5 http://127.0.0.1:8888/api/health | python -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(f\"status={d.get('status')}  插件={d.get('plugin_count')}  频道={d.get('channels_count')}\")
    if d.get('plugin_count') == 0:
        print('!! 插件数为 0：搜索会永远返回 0 条，检查 pansou/docker-compose.yml 的 ENABLED_PLUGINS')
except Exception as e:
    print('健康检查失败:', e)
"
