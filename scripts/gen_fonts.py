#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 LVGL 点阵字库。

字体源文件体积很大（中文字库源码约 15 兆），不适合纳入版本库，
所以由本脚本按需生成。生成结果写入 main/ 目录。

依赖：
    lv_font_conv  ——  npm install -g lv_font_conv
                     或 npm install lv_font_conv 后指定 --lv-font-conv 路径

用法：
    python3 scripts/gen_fonts.py
    python3 scripts/gen_fonts.py --lv-font-conv /path/to/lv_font_conv
    python3 scripts/gen_fonts.py --only noto30
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "main")

# 字体下载地址。jsDelivr 有两个域名，cdn 不通时用 fastly。
FONT_URLS = {
    "NotoSansSC.ttf": [
        "https://fastly.jsdelivr.net/gh/google/fonts@main/ofl/notosanssc/NotoSansSC%5Bwght%5D.ttf",
        "https://cdn.jsdelivr.net/gh/google/fonts@main/ofl/notosanssc/NotoSansSC%5Bwght%5D.ttf",
    ],
    "Audiowide-Regular.ttf": [
        "https://fastly.jsdelivr.net/gh/google/fonts@main/ofl/audiowide/Audiowide-Regular.ttf",
        "https://cdn.jsdelivr.net/gh/google/fonts@main/ofl/audiowide/Audiowide-Regular.ttf",
    ],
    "Orbitron.ttf": [
        "https://fastly.jsdelivr.net/gh/google/fonts@main/ofl/orbitron/Orbitron%5Bwght%5D.ttf",
        "https://cdn.jsdelivr.net/gh/google/fonts@main/ofl/orbitron/Orbitron%5Bwght%5D.ttf",
    ],
}

# 需要生成的字体：名称 -> (字体文件, 字号, 是否含中文)
TARGETS = {
    "noto30":     ("NotoSansSC.ttf", 30, True),
    "noto16":     ("NotoSansSC.ttf", 16, True),
    "orbitron48": ("Orbitron.ttf",   48, False),
    "orbitron32": ("Orbitron.ttf",   32, False),
    "orbitron20": ("Orbitron.ttf",   20, False),
    # Audiowide：宽体圆角科技字型，用于英文状态词、顶栏与底栏时钟
    "tech40":     ("Audiowide-Regular.ttf", 40, False),
    "tech22":     ("Audiowide-Regular.ttf", 22, False),
    "tech20":     ("Audiowide-Regular.ttf", 20, False),
}


def build_charset():
    """ASCII 全段 + 中文标点 + GB2312 全部汉字（一级 3755 + 二级 3008）"""
    chars = [chr(c) for c in range(0x20, 0x7F)]
    chars += list("，。、；：？！“”‘’（）《》【】…—·％＋－×÷　℃°")
    for lo, hi in ((0xB0, 0xD8), (0xD8, 0xF8)):
        for b1 in range(lo, hi):
            for b2 in range(0xA1, 0xFF):
                try:
                    chars.append(bytes([b1, b2]).decode("gb2312"))
                except UnicodeDecodeError:
                    pass
    # 去重但保序
    return "".join(dict.fromkeys(chars))


def download(name, cache_dir):
    dst = os.path.join(cache_dir, name)
    if os.path.exists(dst) and os.path.getsize(dst) > 100000:
        print("  已缓存 %s" % name)
        return dst
    last_err = None
    for url in FONT_URLS[name]:
        try:
            print("  下载 %s ..." % url.split("/")[2])
            with urllib.request.urlopen(url, timeout=120) as r, open(dst, "wb") as f:
                shutil.copyfileobj(r, f)
            if os.path.getsize(dst) > 100000:
                print("  完成 %.1f MB" % (os.path.getsize(dst) / 1048576))
                return dst
        except Exception as e:          # noqa: BLE001
            last_err = e
            print("  失败：%s" % e)
    raise RuntimeError("字体下载失败：%s（%s）" % (name, last_err))


def find_conv(explicit):
    if explicit:
        return explicit
    for cand in (
        shutil.which("lv_font_conv"),
        os.path.expanduser("~/.workbuddy/binaries/node/workspace/node_modules/.bin/lv_font_conv"),
    ):
        if cand and os.path.exists(cand):
            return cand
    raise RuntimeError(
        "找不到 lv_font_conv。请先安装：\n"
        "    npm install -g lv_font_conv\n"
        "或用 --lv-font-conv 指定绝对路径。")


def fix_include(path):
    """生成文件里引用的是 lvgl/lvgl.h，ESP-IDF 下该路径不存在"""
    with open(path, "r", errors="replace") as f:
        s = f.read()
    s = s.replace('#include "lvgl/lvgl.h"', '#include "lvgl.h"')
    with open(path, "w") as f:
        f.write(s)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lv-font-conv", help="lv_font_conv 可执行文件路径")
    ap.add_argument("--only", help="只生成指定字体，逗号分隔，如 noto30,noto16")
    ap.add_argument("--cache", default=os.path.join(tempfile.gettempdir(), "agent-panel-fonts"),
                    help="字体文件缓存目录")
    args = ap.parse_args()

    only = set(args.only.split(",")) if args.only else set(TARGETS)

    os.makedirs(args.cache, exist_ok=True)
    os.makedirs(OUT_DIR, exist_ok=True)

    conv = find_conv(args.lv_font_conv)
    print("转换工具：%s" % conv)

    print("准备字符集 ...")
    charset = build_charset()
    print("  共 %d 个字符" % len(charset))

    needed = {TARGETS[k][0] for k in only if k in TARGETS}
    fonts = {}
    for name in sorted(needed):
        fonts[name] = download(name, args.cache)

    for key in TARGETS:
        if key not in only:
            continue
        font_file, size, with_cjk = TARGETS[key]
        out = os.path.join(OUT_DIR, "lv_font_%s.c" % key)
        cmd = [conv, "--font", fonts[font_file], "--range", "0x20-0x7E"]
        if with_cjk:
            cmd += ["--symbols", charset]
        cmd += ["--size", str(size), "--bpp", "4", "--format", "lvgl",
                "--no-compress", "--lv-font-name", "lv_font_%s" % key,
                "-o", out]
        print("生成 %s（%d 号）..." % (key, size))
        rc = subprocess.call(cmd)
        if rc != 0:
            raise SystemExit("生成 %s 失败（退出码 %d）" % (key, rc))
        fix_include(out)
        print("  → main/lv_font_%s.c  %.1f MB" % (key, os.path.getsize(out) / 1048576))

    print("\n全部完成。")


if __name__ == "__main__":
    main()
