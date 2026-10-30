"""
把系统里的思源黑体（Noto Sans SC VF，SIL OFL）子集化成可分片加载的 woff2。

分层策略（浏览器按 unicode-range 懒加载，用到哪片才下哪片）：
  hot  : 应用界面 + 江大语料里出现的 1268 高频字 —— 几乎必用，常驻下载
  cjk-N: GB2312 一级字表里剩下的字，按码位顺序切成 N 片
  ext  : 二级生僻字，单独一片，页面几乎不用，不下载

每片存 400（Regular）与 700（Bold）两个静态字重。
输出到 static/fonts/，配合 server.enableStaticServing 托管。

用法：python tools/build_font.py（需要 fonttools + brotli）
"""
from __future__ import annotations

import collections
import json
import os
import re
import time

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

SRC = r"C:/Windows/Fonts/NotoSansSC-VF.ttf"
OUT_DIR = "static/fonts"
MANIFEST = "static/fonts/manifest.json"
FAMILY = "Source Han Sans SC"

LATIN = set(chr(c) for c in range(0x20, 0x7F))
PUNCT = set(
    "　、。〃〈〉《》「」『』【】〔〕〖〗〝〞…‥・！？，．：；？＠｀｜～"
    "‘’“”–—°±×÷≈≠≤≥∞√∑∏∫∂"
    "←↑→↓↔↕⇄⇅⇒⇔⇕⌈⌉⌊⌋"
    "ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ"
    "０１２３４５６７８９"
    "₁₂₃₄₅₆₇₈₉₀₊₋₌₍₎"
    "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
    "⓪ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ"
    "㎡℃‰№™©®℉€£¥₩"
)


def gb2312(lo: int, hi: int) -> set[str]:
    out: set[str] = set()
    for b in range(lo, hi):
        for c in range(0xA1, 0xFF):
            try:
                out.add(bytes([b, c]).decode("gb2312"))
            except Exception:
                pass
    return out


def hot_chars() -> set[str]:
    """应用源码 + 江大 100 条语料里出现的字，按频次取前 1268。"""
    cnt: collections.Counter = collections.Counter()
    for path in ("app.py", ".workbuddy/train/ju100.txt"):
        if not os.path.exists(path):
            continue
        text = open(path, encoding="utf-8").read()
        cnt.update(re.findall(r"[\u4e00-\u9fff]", text))
    return {ch for ch, _ in cnt.most_common(1268)}


def build_one(text: str, out_path: str, wght: int) -> int:
    """实例化成静态字重后子集化。

    保留可变轴会让每片都背上完整 gvar 差分数据（体积翻倍），
    这里直接实例化 —— gvar 烘焙进 glyf 后再丢弃。
    """
    opts = subset.Options()
    opts.flavor = "woff2"
    opts.desubroutinize = True
    opts.drop_tables += ["BASE"]
    opts.layout_features = ["kern", "liga", "locl", "ccmp", "vert", "vrt2", "palt"]
    opts.name_IDs = ["*"]
    opts.name_legacy = True
    opts.notdef_outline = True
    opts.recalc_bounds = True
    opts.drop_phantom = True
    opts.retain_gids = False

    font = TTFont(SRC)
    instantiateVariableFont(font, {"wght": wght}, inplace=True, updateFontNames=True)
    sub = subset.Subsetter(options=opts)
    sub.populate(text=text)
    sub.subset(font)
    font.flavor = "woff2"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    font.save(out_path)
    return os.path.getsize(out_path)


def unicode_range(chars: set[str]) -> str:
    codes = sorted({ord(c) for c in chars})
    ranges: list[str] = []
    start = prev = codes[0]
    for c in codes[1:]:
        if c == prev + 1:
            prev = c
            continue
        ranges.append(_rng(start, prev))
        start = prev = c
    ranges.append(_rng(start, prev))
    return ", ".join(ranges)


def _rng(a: int, b: int) -> str:
    return f"U+{a:04X}" if a == b else f"U+{a:04X}-{b:04X}"


def split_contiguous(chars: set[str], target: int) -> list[set[str]]:
    """按码位顺序每 target 字切一片，片内码位尽量连续。"""
    ordered = sorted(chars, key=ord)
    parts, cur = [], []
    for ch in ordered:
        cur.append(ch)
        if len(cur) >= target:
            parts.append(set(cur))
            cur = []
    if cur:
        parts.append(set(cur))
    return parts


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)

    hot = hot_chars()
    level1 = gb2312(0xB0, 0xD8)
    level2 = gb2312(0xD8, 0xF8)

    rest1 = level1 - hot
    cjk_parts = split_contiguous(rest1, 600)
    ext_parts = split_contiguous(level2, 1500)

    plan = [("hot", hot | LATIN | PUNCT)]
    plan += [(f"cjk-{i+1}", p) for i, p in enumerate(cjk_parts)]
    plan += [(f"ext-{i+1}", p) for i, p in enumerate(ext_parts)]

    faces: list[dict] = []
    total = 0
    for name, chars in plan:
        if not chars:
            continue
        text = "".join(sorted(chars))
        rng = unicode_range(chars)
        for wght, tag in ((400, "Regular"), (700, "Bold")):
            out = os.path.join(OUT_DIR, f"{FAMILY.replace(' ', '')}-{name}-{tag}.woff2")
            t0 = time.time()
            size = build_one(text, out, wght)
            total += size
            faces.append({
                "name": f"{name}-{tag}",
                "file": f"app/static/fonts/{os.path.basename(out)}",
                "family": FAMILY,
                "weight": str(wght),
                "style": "normal",
                "unicodeRange": rng,
                "size_kb": round(size / 1024),
            })
            print(f"{name:8s} {tag:7s} {len(chars):5d}字 {size/1024:6.0f}KB "
                  f"({time.time()-t0:.1f}s)")

    with open(MANIFEST, "w", encoding="utf-8") as fh:
        json.dump(faces, fh, ensure_ascii=False, indent=1)
    hot_kb = sum(f["size_kb"] for f in faces if f["name"].startswith("hot"))
    print(f"\n总 {total/1024:.0f}KB / {len(faces)} 片；首屏必下 hot 片 = {hot_kb}KB")


if __name__ == "__main__":
    main()
