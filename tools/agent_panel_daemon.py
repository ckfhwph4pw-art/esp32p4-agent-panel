#!/usr/bin/env python3
"""
智能体副屏采集端

用途：轮询本机三个智能体的状态源，输出行分隔 JSON（NDJSON），供 ESP32-P4 副屏显示。
用法：
    python3 agent_panel_daemon.py                # 输出到屏幕，用于调试
    python3 agent_panel_daemon.py --port /dev/cu.usbmodemXXXX   # 串口推给板子
    python3 agent_panel_daemon.py --interval 2   # 轮询间隔秒数

状态定义：
    busy    工作中      20 秒内有活动
    idle    空闲        3 分钟内有活动但当前静默
    off     离线        超过 3 分钟无活动，或进程不存在
说明：wait（等你决定）需要业务侧主动上报，当前无法从文件推断，留作后续扩展。
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

HOME = os.path.expanduser("~")

# ---------- 事件名中文映射 ----------
# 依据本机审计日志的真实分布建立，未知事件按前缀归类
EVENT_PHRASE = {
    "command-safety.sandbox-executed": "执行命令",
    "file-safety.allow-listed":        "读写文件",
    "file-safety.deny-matched":        "文件操作被拦截",
    "file-safety.needs-approval":      "等你确认文件操作",
    "file-safety.ask-matched":         "询问是否允许文件操作",
    "file-safety.approved":            "文件操作已批准",
    "file-safety.auto-approved":       "文件操作自动批准",
    "network.ask-matched":             "询问是否允许联网",
    "network.approved":                "联网已批准",
    "audit.retention.pruned":          "审计日志维护",
}

# 这几类事件代表"正在等人做决定"，可以直接映射成 wait 状态
WAIT_EVENTS = {
    "file-safety.needs-approval",
    "file-safety.ask-matched",
    "network.ask-matched",
}

PREFIX_RULES = [
    ("command-", "执行命令"),
    ("file-",    "读写文件"),
    ("network-", "网络操作"),
    ("tool-",    "调用工具"),
    ("mcp",      "调用工具"),
    ("audit.",   "审计维护"),
]

def event_phrase(et):
    if not et:
        return "无事件"
    if et in EVENT_PHRASE:
        return EVENT_PHRASE[et]
    for pre, phrase in PREFIX_RULES:
        if et.startswith(pre):
            return phrase
    return et          # 实在认不出就原样显示

# ---------- 工具 ----------

def mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None

def newest_file(root, name):
    """在 root 下递归查找指定文件名，返回修改时间最新的那个。"""
    best, best_t = None, None
    for dirpath, _dirnames, filenames in os.walk(root):
        if name in filenames:
            p = os.path.join(dirpath, name)
            t = mtime(p)
            if t and (best_t is None or t > best_t):
                best, best_t = p, t
    return best, best_t

def pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False

def fmt_gap(seconds):
    s = int(seconds)
    if s < 60:
        return "%d 秒前" % s
    if s < 3600:
        return "%d 分钟前" % (s // 60)
    if s < 86400:
        return "%d 小时前" % (s // 3600)
    return "%d 天前" % (s // 86400)

def state_of(age, busy_s=20, idle_s=180):
    if age is None:
        return "off"
    if age <= busy_s:
        return "busy"
    if age <= idle_s:
        return "idle"
    return "off"

# ---------- 采集辅助 ----------

def cmd_summary(preview):
    """把完整命令压成一行摘要：取首个实际命令，去掉 cd 前缀，截断 40 字符"""
    if not preview:
        return ""
    line = preview.strip().split("\n")[0].strip()
    for sep in ("&&", "||"):
        if sep in line:
            parts = [x.strip() for x in line.split(sep)]
            rest = [x for x in parts if not x.startswith("cd ")]
            if rest:
                line = rest[0]
            break
    if line.startswith("cd "):
        line = ""
    for ch in ("|", ";"):
        if ch in line:
            line = line.split(ch)[0].strip()
    # 长路径只留最后一段，否则会把屏幕占满
    line = re.sub(r"(?:[\w~.$-]*/)+([\w.-]+)", r"\1", line)
    return line[:28]

def hour_heat(timestamps, now):
    """时间戳集合映射成 24 个小时格强度串（0-9），最近一小时在最右"""
    hours = [0] * 24
    for ts in timestamps:
        if ts is None:
            continue
        delta = int((now - ts) // 3600)
        if 0 <= delta < 24:
            hours[23 - delta] += 1
    mx = max(hours) if hours else 0
    if mx == 0:
        return "0" * 24
    return "".join(str(min(9, int(h * 9 / mx))) for h in hours)

def audit_scan(today_file):
    """扫描当日审计日志，返回 (总条数, 会话数, 时间戳列表, 最后一条记录)"""
    total, sessions, stamps, last = 0, set(), [], None
    try:
        with open(today_file, "rb") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                total += 1
                try:
                    r = json.loads(raw)
                except Exception:
                    continue
                last = r
                sid = r.get("sessionId")
                if sid:
                    sessions.add(sid)
                ts = r.get("timestamp")
                if isinstance(ts, (int, float)):
                    stamps.append(ts / 1000.0 if ts > 1e11 else float(ts))
    except OSError:
        pass
    return total, len(sessions), stamps, last

# ---------- 三路采集 ----------

def collect_workbuddy(now):
    today = datetime.now().strftime("%Y-%m-%d")
    audit = os.path.join(HOME, ".workbuddy", "audit-log", "%s.jsonl" % today)
    renderer = os.path.join(HOME, ".workbuddy", "logs", "renderer.log")

    t_audit = mtime(audit)
    t_render = mtime(renderer)
    times = [t for t in (t_audit, t_render) if t]
    age = (now - max(times)) if times else None

    total, n_sess, stamps, last = 0, 0, [], None
    if t_audit:
        total, n_sess, stamps, last = audit_scan(audit)

    last_event, last_cmd = "", ""
    if last:
        last_event = last.get("eventType") or last.get("category") or ""
        last_cmd = last.get("commandPreview") or ""

    st = state_of(age)
    if last_event in WAIT_EVENTS and age is not None and age < 180:
        st = "wait"

    return {
        "ag": "workbuddy",
        "st": st,
        "task": event_phrase(last_event),
        "cmd": cmd_summary(last_cmd),
        "cnt": total,
        "csub": "今日操作",
        "sub": ("会话 %d 个" % n_sess) if n_sess else "",
        "heat": hour_heat(stamps, now),
        "age": age,
    }

def collect_hermes(now):
    lock = os.path.join(HOME, ".local", "state", "hermes", "gateway-locks", "host-gateway.json")
    db = os.path.join(HOME, ".hermes", "state.db")

    pid = None
    try:
        with open(lock) as f:
            pid = json.load(f).get("pid")
    except Exception:
        pass
    alive = pid_alive(pid)

    last_ts, stamps, n_recent, last_tool = None, [], 0, ""
    try:
        c = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
        row = c.execute("select max(timestamp) from messages").fetchone()
        if row and row[0]:
            try:
                last_ts = float(row[0])
            except (TypeError, ValueError):
                last_ts = None
        day_ago = now - 86400
        for (ts,) in c.execute("select timestamp from messages where timestamp > ?", (day_ago,)):
            try:
                stamps.append(float(ts))
            except (TypeError, ValueError):
                pass
        row = c.execute("select count(*) from messages where timestamp > ?", (day_ago,)).fetchone()
        n_recent = row[0] if row else 0
        row = c.execute("select tool_name from messages where tool_name is not null and tool_name != '' order by rowid desc limit 1").fetchone()
        last_tool = row[0] if row else ""
        c.close()
    except Exception:
        pass

    # 用真实消息时间判断活跃；数据库 mtime 会被后台维护刷新，不可用
    age = (now - last_ts) if last_ts else None

    if not alive:
        st, note = "off", "网关进程不存在"
    elif age is not None and age <= 90:
        st, note = "busy", "处理中"
    else:
        st, note = "idle", "在线空闲"

    return {
        "ag": "hermes",
        "st": st,
        "task": note,
        "cmd": ("调用 " + last_tool) if last_tool else "",
        "cnt": n_recent,
        "csub": "24h 消息",
        "sub": "24 小时 %d 条消息" % n_recent,
        "heat": hour_heat(stamps, now),
        "age": age,
        "pid": pid,
        "pid_alive": alive,
    }

def collect_deepseek(now):
    root = os.path.join(HOME, ".dsh", "sessions")
    path, t = newest_file(root, "session.v4.jsonl.zstd")
    age = (now - t) if t else None

    stamps, n_sess = [], 0
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if fn == "session.v4.jsonl.zstd":
                n_sess += 1
                try:
                    stamps.append(os.path.getmtime(os.path.join(dirpath, fn)))
                except OSError:
                    pass

    return {
        "ag": "deepseek",
        "st": state_of(age, busy_s=120, idle_s=600),
        "task": "无进行中任务",
        "cmd": "",
        "cnt": n_sess,
        "csub": "会话总数",
        "sub": "共 %d 个会话" % n_sess,
        "heat": hour_heat(stamps, now),
        "age": age,
    }

def collect_pc(now):
    """本机硬件与使用状态：处理器、内存、磁盘、负载、核心数。"""
    cpu = mem = disk = load10 = 0
    cores = os.cpu_count() or 0
    model = ""

    try:
        import psutil
        # interval=0.1 让 psutil 采样一小段，避免首轮返回 0
        cpu = int(round(psutil.cpu_percent(interval=0.1)))
        mem = int(round(psutil.virtual_memory().percent))
        cores = psutil.cpu_count(logical=True) or cores
    except Exception:
        pass

    try:
        # macOS 上 psutil.disk_usage("/") 读的是只读系统卷，改用 statvfs
        import shutil
        du = shutil.disk_usage("/")
        disk = int(round(du.used * 100.0 / du.total))
    except Exception:
        pass

    try:
        load10 = int(round(os.getloadavg()[0] * 100))
    except Exception:
        pass

    try:
        import subprocess
        model = subprocess.check_output(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            text=True, timeout=2).strip()
    except Exception:
        model = ""

    if not model:
        try:
            import platform
            model = platform.machine()
        except Exception:
            model = "本机"

    st = "busy" if cpu >= 80 else "idle"

    return {
        "ag": "pc",
        "st": st,
        "task": model[:48],
        "cpu": cpu,
        "mem": mem,
        "disk": disk,
        "load": load10,
        "cnt": cores,
        "csub": "核心",
        "age": 0,
    }

# ---------- 主循环 ----------

def render_line(item, now):
    age = item.get("age")
    out = {
        "ag": item["ag"],
        "st": item["st"],
        "task": item["task"],
        "ago": fmt_gap(age) if age is not None else "未知",
        "el": item.get("el", -1),
    }
    if item.get("cmd"):
        out["cmd"] = item["cmd"]
    if item.get("cnt") is not None and item["cnt"] >= 0:
        out["cnt"] = item["cnt"]
    if item.get("csub"):
        out["csub"] = item["csub"]
    if item.get("sub"):
        out["sub"] = item["sub"]
    if item.get("heat"):
        out["heat"] = item["heat"]
    for k in ("cpu", "mem", "disk", "load"):
        if item.get(k) is not None:
            out[k] = item[k]
    return json.dumps(out, ensure_ascii=False, separators=(",", ":"))

def scan_ports(allow_any=False):
    """候选端口。

    默认只认 Agent Panel（端口名含序列号 AGP）。这块板在 ROM 下载模式下
    也会生成 usbmodem 端口（设备名 ESP32_P4），若回退到"任意 usbmodem"，
    采集端会连上引导装载器并往里写 JSON，干扰烧录。所以默认不做这种回退，
    确需时用 --any-port 显式打开。
    """
    try:
        names = sorted(os.listdir("/dev"))
    except OSError:
        return []
    agp = ["/dev/" + n for n in names if n.startswith("cu.usbmodem") and "AGP" in n]
    if agp or not allow_any:
        return agp
    return ["/dev/" + n for n in names if n.startswith("cu.usbmodem")]

def open_port(preferred, allow_any=False):
    """尝试打开端口，失败返回 (None, None)。"""
    candidates = []
    if preferred and os.path.exists(preferred):
        candidates.append(preferred)
    candidates += [p for p in scan_ports(allow_any) if p not in candidates]

    for port in candidates:
        try:
            import serial
            ser = serial.Serial(port, 115200, timeout=0.2)
            return ser, port
        except Exception as e:
            print("[等待] %s 打不开：%s" % (port, e), flush=True)
    return None, None

def push_round(ser, state):
    """采集并推送一轮。串口异常向上抛，由外层负责重连。"""
    now = time.time()
    items = [collect_pc(now), collect_workbuddy(now), collect_hermes(now), collect_deepseek(now)]
    state["rounds"] += 1
    busy_start = state["busy_start"]

    for it in items:
        ag = it["ag"]
        if it["st"] == "busy":
            busy_start.setdefault(ag, now)
            it["el"] = int(now - busy_start[ag])
        else:
            busy_start.pop(ag, None)
            it["el"] = -1

        line = render_line(it, now)
        ser.write((line + "\n").encode("utf-8"))
        time.sleep(0.02)     # USB CDC 自带流控，无需错峰

        # 读空板子积压的输出，避免其发送缓冲被撑满
        while ser.in_waiting:
            back = ser.readline()
            if not back:
                break
            txt = back.decode("utf-8", "replace").strip()
            if txt:
                print("[板子] " + txt, flush=True)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", help="串口设备，例如 /dev/cu.usbmodemXXXX")
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--once", action="store_true", help="只采集一轮后退出")
    ap.add_argument("--any-port", action="store_true",
                    help="允许回退到任意 usbmodem 端口（默认只认 Agent Panel）")
    args = ap.parse_args()

    # --once：只打印，不推串口
    if args.once:
        now = time.time()
        busy = {}
        for it in (collect_pc(now), collect_workbuddy(now), collect_hermes(now), collect_deepseek(now)):
            if it["st"] == "busy":
                busy.setdefault(it["ag"], now)
                it["el"] = int(now - busy[it["ag"]])
            else:
                it["el"] = -1
            print(render_line(it, now))
        return

    state = {"rounds": 0, "busy_start": {}}
    preferred = args.port
    ser = None
    last_ok = 0.0

    while True:
        # 无连接：尝试打开端口，失败则等待后重试（不退出）
        if ser is None:
            ser, port = open_port(preferred, getattr(args, "any_port", False))
            if ser is None:
                print("[等待] 暂无可用的板子端口，3 秒后重试", flush=True)
                time.sleep(3)
                continue
            preferred = port
            print("[连接] %s" % port, flush=True)

        try:
            push_round(ser, state)
            last_ok = time.time()
        except Exception as e:
            print("[断开] %s，准备重连" % e, flush=True)
            try:
                ser.close()
            except Exception:
                pass
            ser = None
            time.sleep(1.5)
            continue

        if state["rounds"] % 15 == 0:
            print("[心跳] 已推送 %d 轮，间隔 %.1f 秒" % (state["rounds"], args.interval), flush=True)
        time.sleep(args.interval)

if __name__ == "__main__":
    main()
