#!/usr/bin/env python3
"""
智能体副屏采集端

用途：轮询本机三个智能体的状态源，输出行分隔 JSON（NDJSON），供 ESP32-P4 副屏显示。
用法：
    python3 agent_panel_daemon.py                # 输出到屏幕，用于调试
    python3 agent_panel_daemon.py --port /dev/cu.usbmodemXXXX   # 串口推给板子
    python3 agent_panel_daemon.py --interval 1   # 轮询间隔秒数，默认 1 秒

状态定义（活跃驱动，不再是简单的时间阈值）：
    busy    工作中      最近 45 秒内出现过真实活跃
    idle    空闲        进程在，但 45 秒内没有活跃
    off     离线        进程不存在
    wait    等你决定    业务侧给出需要人工确认的事件时

「真实活跃」的判据：
    1. 日志字节增长率抬升（实测工作 1183 至 4227 字节/秒，等待约 110，空闲约 90）
    2. 审计事件刚落盘（8 秒内）
    3. 命令已开工但还没等到结果（sandbox-executed 之后没有 tool_result）
为什么不用 CPU 和进程树：AI 工作时主要在等模型返回，本地 CPU 仅 0.006 至 0.16 秒/秒；
工具执行进程也已脱离 WorkBuddy 主进程树，两者的区分力都不够。
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
    "security-review.tool_result.allowed":  "工具运行完毕",
    "security-review.sandbox.final-allow":  "沙箱已放行",
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

# ---------- 实时活跃判定 ----------
# 旧做法用「最后一次日志写入距今多久」判忙，但渲染日志空闲时也在零星写入，
# 结果状态长期粘在忙碌、不跟随实际工作。实测（2026-10-27）：
#   执行工具时 1479 字节/秒，等待工具返回 113 字节/秒，真空闲约 90 字节/秒。
# 因此改成看「增长速率」是否抬升，抬升即记一次活跃，活跃后再保持一小段时间。

ACTIVE_RATE  = 400.0   # 字节/秒，高于此值判为正在工作（实测忙 1479 / 闲 113）
ACTIVE_HOLD  = 45.0    # 秒，最后一次活跃后继续保持忙碌的时长
EVENT_FRESH  = 8.0     # 秒，审计事件在这个时间内到达也算活跃

last_active = {}       # 智能体标识 -> 最近一次真实活跃的时间戳


def mark_active(ag, t):
    last_active[ag] = t


def held_state(ag, alive, now, hold=ACTIVE_HOLD):
    """活跃保持：只要最近 hold 秒内出现过真实活动就算忙碌。"""
    if not alive:
        return "off"
    t = last_active.get(ag)
    if t is not None and now - t <= hold:
        return "busy"
    return "idle"


class ActivityMeter:
    """按文件字节增长速率判断此刻是否在活动。

    只看「最后一次写入时刻」会被空闲时的零星日志误导，必须看速率相对
    自身基线的抬升，所以同时维护近期采样，供调用方对照基线。
    """

    def __init__(self, paths, window=180.0):
        self.paths = list(paths)
        self.window = window
        self.samples = []          # [(时间戳, 字节总数)]

    def _total(self):
        tot = 0
        for p in self.paths:
            try:
                tot += os.path.getsize(p)
            except OSError:
                pass
        return tot

    def sample(self, now):
        self.samples.append((now, self._total()))
        while self.samples and now - self.samples[0][0] > self.window:
            self.samples.pop(0)
        return self.rate(now)

    def rate(self, now, span=4.0):
        """最近 span 秒的平均增长速度，字节/秒；样本不足返回 None。"""
        if len(self.samples) < 2:
            return None
        ref = self.samples[0]
        for t, b in reversed(self.samples):
            ref = (t, b)
            if now - t >= span:
                break
        dt = now - ref[0]
        if dt < 0.8:
            return None
        return max(0.0, (self._total() - ref[1]) / dt)

    def baseline(self, now):
        """窗口内的平均速度，用于自适应对照。"""
        if len(self.samples) < 2:
            return 0.0
        t0, b0 = self.samples[0]
        dt = now - t0
        if dt < 10:
            return 0.0
        return max(0.0, (self._total() - b0) / dt)

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

EXEC_EVENTS   = ("command-safety.sandbox-executed",
                 "file-safety.needs-approval",
                 "file-safety.ask-matched")
RESULT_EVENTS = ("security-review.tool_result.allowed",
                 "file-safety.auto-approved",
                 "security-review.sandbox.final-allow")


def audit_scan(today_file):
    """扫描当日审计日志。

    返回 (总条数, 会话数, 时间戳列表, 最后一条记录, 最后开工时间,
          最后收工时间, 最后开工记录)。
    开工与收工时间是成对事件的时刻：命令开始执行会写一条 command-safety，
    结果返回会写一条 tool_result，两者配对即可判断「此刻是否还在执行」。
    最后一条记录往往是「已结束」，所以要另外留下开工那条，才能显示正在做什么。
    """
    total, sessions, stamps, last = 0, set(), [], None
    t_exec = t_result = None
    last_exec = None
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
                if not isinstance(ts, (int, float)):
                    continue
                sec = ts / 1000.0 if ts > 1e11 else float(ts)
                stamps.append(sec)
                et = r.get("eventType") or ""
                if et in EXEC_EVENTS:
                    t_exec = sec
                    last_exec = r
                elif et in RESULT_EVENTS:
                    t_result = sec
    except OSError:
        pass
    return total, len(sessions), stamps, last, t_exec, t_result, last_exec

# ---------- 三路采集 ----------

_scan_cache = {}


def audit_scan_cached(path):
    """当日审计日志按「大小 + 修改时间」缓存，避免每秒重扫整文件。"""
    try:
        st = os.stat(path)
    except OSError:
        return 0, 0, [], None, None, None, None
    key = (st.st_size, st.st_mtime)
    hit = _scan_cache.get(path)
    if hit and hit[0] == key:
        return hit[1]
    res = audit_scan(path)
    _scan_cache[path] = (key, res)
    return res


_meters = {}


def meter_for(ag, paths):
    """同一智能体复用计数器；路径变化（例如跨天换文件）时重建。"""
    paths = tuple(paths)
    m = _meters.get(ag)
    if m is None or tuple(m.paths) != paths:
        m = ActivityMeter(paths)
        _meters[ag] = m
    return m


def collect_workbuddy(now):
    today = datetime.now().strftime("%Y-%m-%d")
    audit = os.path.join(HOME, ".workbuddy", "audit-log", "%s.jsonl" % today)
    renderer = os.path.join(HOME, ".workbuddy", "logs", "renderer.log")

    total, n_sess, stamps, last, t_exec, t_result, last_exec = audit_scan_cached(audit)
    if not stamps:
        t = mtime(audit)
        stamps = [t] if t else []

    ts_last = max(stamps) if stamps else None

    last_event, last_cmd = "", ""
    if last:
        last_event = last.get("eventType") or last.get("category") or ""
        last_cmd = last.get("commandPreview") or ""

    # 实时活跃：日志增长速率抬升，或审计事件刚落地
    rate = meter_for("workbuddy", [renderer, audit]).sample(now)
    if rate is not None and rate >= ACTIVE_RATE:
        mark_active("workbuddy", now)
    if ts_last is not None and now - ts_last <= EVENT_FRESH:
        mark_active("workbuddy", now)

    # 命令已开工但还没收到结果 → 仍在执行。长命令期间日志几乎不增长，
    # 只靠速率会误判为空闲，这一条把「正在跑」兜住。
    inflight = (t_exec is not None
                and (t_result is None or t_exec > t_result)
                and now - t_exec < 600)
    if inflight:
        mark_active("workbuddy", now)

    # 「最后活动」用实时检测到的活跃时刻。审计日志可能长时间不落盘
    # （实测出现过 48 分钟空档），若用它会把正在工作显示成几十分钟前。
    la = last_active.get("workbuddy")
    age = (now - la) if la is not None else ((now - ts_last) if ts_last else None)

    st = held_state("workbuddy", True, now)
    if last_event in WAIT_EVENTS and age is not None and age < 180:
        st = "wait"

    # 命中「正在执行」时，用开工那条事件描述当前动作，
    # 否则文案会停在「工具运行完毕」，看不出此刻在做什么。
    if inflight and last_exec:
        task_now = "正在执行命令"
        cmd_now = cmd_summary(last_exec.get("commandPreview") or "")
    elif st == "busy":
        # 靠速率判为忙碌（模型输出或渲染活跃），但没有明确的开工事件
        task_now = "正在处理"
        cmd_now = cmd_summary(last_cmd)
    else:
        task_now = event_phrase(last_event)
        cmd_now = cmd_summary(last_cmd)

    return {
        "ag": "workbuddy",
        "st": st,
        "task": task_now,
        "cmd": cmd_now,
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

    last_ts, stamps, n_recent, n_minute, last_tool = None, [], 0, 0, ""
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
        # 最近一分钟的新消息条数，用来判断此刻是否真在处理
        row = c.execute("select count(*) from messages where timestamp > ?", (now - 60,)).fetchone()
        n_minute = row[0] if row else 0
        row = c.execute("select tool_name from messages where tool_name is not null and tool_name != '' order by rowid desc limit 1").fetchone()
        last_tool = row[0] if row else ""
        c.close()
    except Exception:
        pass

    # 用真实消息时间判断活跃；数据库 mtime 会被后台维护刷新，不可用
    age = (now - last_ts) if last_ts else None

    # 实时活跃：消息刚落库，或最近一分钟仍在持续产生消息
    if age is not None and age <= EVENT_FRESH:
        mark_active("hermes", now)
    if n_minute > 0:
        mark_active("hermes", now)

    st = held_state("hermes", alive, now)
    if not alive:
        note = "网关进程不存在"
    elif st == "busy":
        note = "处理中"
    else:
        note = "在线空闲"

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

_proc_cache = {}


def proc_alive_cmd(substr, now, ttl=5.0):
    """按命令行片段判断进程是否在跑；5 秒缓存，避免每秒遍历进程表。"""
    hit = _proc_cache.get(substr)
    if hit and now - hit[0] < ttl:
        return hit[1]
    alive = False
    try:
        import psutil
        for p in psutil.process_iter(["cmdline"]):
            try:
                cl = " ".join(p.info["cmdline"] or [])
            except Exception:
                continue
            if substr in cl:
                alive = True
                break
    except Exception:
        alive = True      # 拿不到进程表时不误判为离线
    _proc_cache[substr] = (now, alive)
    return alive


_walk_cache = {"t": 0.0, "stamps": [], "n": 0}


def collect_deepseek(now):
    root = os.path.join(HOME, ".dsh", "sessions")
    path, t = newest_file(root, "session.v4.jsonl.zstd")
    age = (now - t) if t else None

    # 全量遍历目录较重，15 秒刷新一次即可
    if now - _walk_cache["t"] > 15:
        stamps, n_sess = [], 0
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if fn == "session.v4.jsonl.zstd":
                    n_sess += 1
                    try:
                        stamps.append(os.path.getmtime(os.path.join(dirpath, fn)))
                    except OSError:
                        pass
        _walk_cache.update({"t": now, "stamps": stamps, "n": n_sess})
    stamps, n_sess = _walk_cache["stamps"], _walk_cache["n"]

    alive = proc_alive_cmd("/Applications/DeepSeek Harness.app/", now)

    # 会话文件刚落盘即算活跃
    if age is not None and age <= EVENT_FRESH + 2:
        mark_active("deepseek", now)

    return {
        "ag": "deepseek",
        "st": held_state("deepseek", alive, now),
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

def tz_offset():
    """本地时区相对 UTC 的偏移秒数。四舍五入到 15 分钟，避开夏令时边界抖动。"""
    try:
        off = int(time.mktime(time.localtime()) - time.mktime(time.gmtime()))
        return int(round(off / 900.0) * 900)
    except Exception:
        return 0

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
    # 时间基准：板子据此在本地走秒，不必依赖网络对时
    out["ts"] = int(now)
    out["tz"] = tz_offset()
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
    ap.add_argument("--interval", type=float, default=1.0,
                    help="轮询间隔秒数，越小状态越跟手（默认 1 秒）")
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
