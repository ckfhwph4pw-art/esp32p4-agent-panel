#!/bin/sh
# 通过 USB OTG 口烧录（无需换到 UART 口）
#
# 用法：
#   sh scripts/flash_usb.sh              # 烧 build/ 目录下的产物
#   sh scripts/flash_usb.sh <构建目录>    # 指定其他构建目录
#
# 前置：板子插在 USB OTG 口（丝印 USB OTG，不是 USB TO UART），
#       并已进入 ROM 下载模式：按住 BOOT → 点 RESET → 松开 BOOT。
#
# 关键：USB 方式烧录必须加 --no-compress，否则报
#       C900 Too much data（压缩块过大）。该参数必须写在 write_flash 之后。

set -u

DIR=$(cd "$(dirname "$0")/.." && pwd)
BUILD="${1:-$DIR/build}"

if [ ! -f "$BUILD/flash_args" ]; then
    echo "找不到 $BUILD/flash_args"
    echo "请先编译："
    echo "    idf.py -B \"$BUILD\" build"
    exit 1
fi

# 下载模式：设备名 ESP32_P4，端口不含序列号 AGP
# 应用模式：设备名 Agent Panel，端口含 AGP001
PORT=""
for p in /dev/cu.usbmodem*; do
    [ -e "$p" ] || continue
    case "$p" in
        *AGP*) continue ;;
    esac
    PORT="$p"
    break
done

if [ -z "$PORT" ]; then
    echo "未检测到 ROM 下载模式设备。"
    echo
    echo "请按以下步骤进入下载模式："
    echo "  1. 按住 BOOT 键不放"
    echo "  2. 点一下 RESET 键"
    echo "  3. 松开 BOOT"
    echo "然后再运行本脚本。"
    echo
    echo "当前串口："
    ls /dev/cu.usbmodem* 2>/dev/null || echo "  （无）"
    exit 1
fi

PY=""
for cand in \
    "$HOME/.espressif/python_env/cubepal_env/bin/python" \
    "$(command -v python3 2>/dev/null)" \
    "$(command -v python 2>/dev/null)"; do
    if [ -n "$cand" ] && [ -x "$cand" ] && "$cand" -c "import esptool" >/dev/null 2>&1; then
        PY="$cand"; break
    fi
done

if [ -z "$PY" ]; then
    echo "找不到带 esptool 的 Python。请先执行： . \$IDF_PATH/export.sh"
    exit 1
fi

echo "下载模式端口：$PORT"
echo "构建目录：$BUILD"
echo "开始烧录（不压缩模式）..."

cd "$BUILD" || exit 1
"$PY" -m esptool --chip esp32p4 -p "$PORT" write_flash --no-compress @flash_args
RC=$?

if [ $RC -eq 0 ]; then
    echo
    echo "烧录完成，板子会自动重启。约 2 秒后出现 /dev/cu.usbmodem*AGP* 端口。"
else
    echo
    echo "烧录失败（退出码 $RC）。常见原因："
    echo "  - 端口在烧录中重新枚举：等几秒重试"
    echo "  - 未进入下载模式：重按 BOOT + RESET"
    echo "  - 串口被占用：pkill -f agent_panel_daemon.py"
    exit $RC
fi
