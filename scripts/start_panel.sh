#!/bin/sh
# 启动采集端：把本机四路状态推给副屏
#
# 用法：
#   sh scripts/start_panel.sh            # 前台运行，Ctrl+C 停止
#   sh scripts/start_panel.sh --bg       # 后台运行，日志写入 logs/panel.out.log
#
# 采集端自带断线重连：板子复位、重新插拔、进入下载模式后都会自动恢复。

set -u

DIR=$(cd "$(dirname "$0")/.." && pwd)
DAEMON="$DIR/tools/agent_panel_daemon.py"

PY=""
for cand in \
    "$HOME/.espressif/python_env/cubepal_env/bin/python" \
    "$(command -v python3 2>/dev/null)" \
    "$(command -v python 2>/dev/null)"; do
    if [ -n "$cand" ] && [ -x "$cand" ] && "$cand" -c "import serial" >/dev/null 2>&1; then
        PY="$cand"; break
    fi
done

if [ -z "$PY" ]; then
    echo "找不到带 pyserial 的 Python。请先安装："
    echo "    python3 -m pip install pyserial psutil"
    exit 1
fi

if [ "${1:-}" = "--bg" ]; then
    LOG="$DIR/logs/panel.out.log"
    /bin/mkdir -p "$DIR/logs"
    if pgrep -f agent_panel_daemon.py >/dev/null 2>&1; then
        echo "采集端已在运行，无需重复启动"
        exit 0
    fi
    nohup "$PY" "$DAEMON" --interval 2 >> "$LOG" 2>&1 &
    echo "已在后台启动，PID $!"
    echo "日志：$LOG"
    echo "停止：pkill -f agent_panel_daemon.py"
    exit 0
fi

echo "按 Ctrl+C 停止"
exec "$PY" "$DAEMON" --interval 2
