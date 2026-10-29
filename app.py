# -*- coding: utf-8 -*-
"""
AI 智能日程收纳 & 日历导入工具
=====================================

大学生 AI 创新比赛参赛作品（Streamlit 单文件应用）。

设计原则
--------
1. **内存计算**：日程只在 `st.session_state` 中存活，不写数据库、不落盘。
   图片仅在本机内存中做 OCR，不做任何持久化。
2. **能力可降级**：OCR 引擎（rapidocr / pytesseract）与大模型（任意 OpenAI 兼容
   接口）均为可选依赖。缺少任一能力时应用仍可运行，并给出明确提示。
3. **规则引擎兜底**：即使没有网络 / 没有 API Key，内置的中文时间规则解析器
   也能把文本整理成结构化的日程，保证比赛现场演示不会"空场"。

运行方式
--------
    pip install -r requirements.txt
    streamlit run app.py

作者声明：本项目为比赛演示用途，日程数据不构成任何正式记录。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import time
import traceback
import uuid
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

import pandas as pd
import streamlit as st

# ---------------------------------------------------------------------------
# 0. 全局配置
# ---------------------------------------------------------------------------

APP_TITLE = "AI 智能日程收纳"
APP_SUBTITLE = "碎片化日程 → 结构化日程表 → 一键导入手机 / Outlook 日历"
APP_VERSION = "1.0.0"

# 中国标准时间（UTC+8）。使用固定偏移而非 zoneinfo，避免 Windows 缺少 tzdata
# 时抛出 ZoneInfoNotFoundError；中国自 1991 年起不实行夏令时，固定偏移精确无误。
CN_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
CN_TZ_LABEL = "Asia/Shanghai"

# 输入与安全阈值
MAX_TEXT_CHARS = 20_000          # 单次粘贴文本上限，防止超长文本拖垮解析
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 单张图片上限 10MB
MAX_IMAGE_EDGE = 2_400           # OCR 前把最长边压到该值，兼顾速度与精度
ALLOWED_IMAGE_TYPES = {"png", "jpg", "jpeg", "webp", "bmp"}

# 时间推断常量
DEFAULT_EVENT_MINUTES = 60       # 未识别到结束时间时的默认时长
DEFAULT_DAY_START_HOUR = 9       # 只有日期没有时间时的默认开始
DEFAULT_RECUR_WEEKS = 16         # 循环日程默认展开/重复周数
VAGUE_DATE_WINDOW_DAYS = 180     # 超过该天数的"过去日期"判定为明年

# 大学作息：把"第X节"映射为具体时间（可在 README 中说明如何按校调整）
SECTION_MAP: dict[int, tuple[int, int]] = {
    1: (8, 0), 2: (8, 55), 3: (10, 0), 4: (10, 55),
    5: (14, 0), 6: (14, 55), 7: (16, 0), 8: (16, 55),
    9: (19, 0), 10: (19, 55), 11: (20, 50), 12: (21, 45),
}

PRIORITY_LEVELS = ["高", "中", "低"]
PRIORITY_ICS = {"高": 1, "中": 5, "低": 9}   # RFC 5545: 1 最高，9 最低
REMIND_OPTIONS = {
    "不提醒": 0,
    "提前 15 分钟": 15,
    "提前 1 小时": 60,
    "提前 1 天": 1440,
}

# 重复频率选项（界面文案）。对应 RRULE 的构造方式见 build_recur_rrule。
RECUR_FREQ_WEEKLY = "每周重复"
RECUR_FREQ_BIWEEKLY = "每两周重复"
RECUR_FREQ_DAILY = "每天重复"
RECUR_FREQ_OPTIONS = [RECUR_FREQ_WEEKLY, RECUR_FREQ_BIWEEKLY, RECUR_FREQ_DAILY]

# 重复次数的取值范围（含首条），用于「➖ 数字 ➕」步进器
RECUR_COUNT_MIN = 1
RECUR_COUNT_MAX = 60
RECUR_COUNT_DEFAULT = 16         # 与 DEFAULT_RECUR_WEEKS 对齐：一学期 16 周


# ---------------------------------------------------------------------------
# 1. 轻量日志（仅在页面上展示，绝不写入磁盘）
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    """调试日志，只在 Streamlit 控制台输出，不保存到任何文件。"""
    try:
        print(f"[schedule] {msg}", flush=True)
    except Exception:  # pragma: no cover - 日志本身不得影响主流程
        pass


# ---------------------------------------------------------------------------
# 2. 数据模型
# ---------------------------------------------------------------------------

@dataclass
class Event:
    """单条日程。所有 datetime 字段均为 naive 时间，语义为「中国标准时间」。"""

    title: str
    start: datetime
    end: datetime
    location: str = ""
    note: str = ""
    priority: str = "中"
    rrule: str = ""                 # 不含 "RRULE:" 前缀，如 "FREQ=WEEKLY;BYDAY=MO"
    vague: bool = False             # 时间是否由程序推测
    vague_reason: str = ""          # 推测原因，展示给用户核对
    source: str = "text"            # text / ocr / llm / manual / sample
    remind_minutes: int = -1        # -1 表示沿用全局设置；0 表示不提醒
    uid: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    raw: str = ""                   # 原始片段，便于用户核对
    conflict: bool = False          # 运行期计算，不参与导出

    # -- 派生属性 ---------------------------------------------------------
    @property
    def is_recurring(self) -> bool:
        return bool(self.rrule.strip())

    @property
    def duration_minutes(self) -> int:
        return max(0, int((self.end - self.start).total_seconds() // 60))

    @property
    def is_multiday(self) -> bool:
        return self.start.date() != self.end.date()

    @property
    def status(self) -> str:
        """人类可读的校验状态，用于表格展示。"""
        if self.conflict:
            return "🔴 时间冲突"
        if self.vague:
            return "⚠️ 待核对"
        return "✅ 正常"

    def to_row(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "标题": self.title,
            "开始": self.start,
            "结束": self.end,
            "地点": self.location,
            "优先级": self.priority,
            "备注": self.note,
            # 「循环」列只做人类可读展示（表格中禁止直接编辑），
            # 真正的 RRULE 存在隐藏列 rrule_raw 里，避免展示文案覆盖原始规则。
            "循环": _rrule_summary_short(self.rrule),
            "rrule_raw": self.rrule or "",
            "提醒": _remind_label(self.remind_minutes),
            "状态": self.status,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any], fallback: "Event | None" = None) -> "Event":
        """从 data_editor 的一行还原 Event，尽量保留不可见字段。"""
        base = fallback or cls(title="", start=datetime.now(), end=datetime.now())
        base.title = str(row.get("标题") or base.title or "未命名日程").strip()
        base.start = _coerce_datetime(row.get("开始"), base.start)
        base.end = _coerce_datetime(row.get("结束"), base.end)
        base.location = str(row.get("地点") or "")
        base.note = str(row.get("备注") or "")
        pri = str(row.get("优先级") or "中")
        base.priority = pri if pri in PRIORITY_LEVELS else "中"
        # 循环规则只认隐藏的原始列；兼容旧数据里「循环」列直接存 RRULE 的情况
        raw = _cell_text(row, "rrule_raw")
        if not raw:
            legacy = _cell_text(row, "循环")
            raw = legacy if "FREQ=" in legacy.upper() else ""
        base.rrule = raw
        base.remind_minutes = _parse_remind_label(str(row.get("提醒") or ""))
        return base


def _cell_text(row: dict[str, Any], key: str) -> str:
    """
    安全读取 data_editor 单元格的文本值。

    空单元格可能是 None / NaN / pd.NA，直接 `or ""` 在 pd.NA 上会抛
    「boolean value of NA is ambiguous」，因此统一走 pd.isna 判断。
    """
    value = row.get(key)
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "<na>"} else text


def _remind_label(minutes: int) -> str:
    for label, value in REMIND_OPTIONS.items():
        if value == minutes:
            return label
    if minutes is None or minutes < 0:
        return "跟随全局"
    return f"提前 {minutes} 分钟"


def _parse_remind_label(label: str) -> int:
    if label in REMIND_OPTIONS:
        return REMIND_OPTIONS[label]
    if label.startswith("跟随"):
        return -1
    m = re.search(r"(\d+)", label)
    return int(m.group(1)) if m else -1


def _coerce_datetime(value: Any, default: datetime) -> datetime:
    """把 data_editor 可能返回的 NaN / str / Timestamp 统一成 naive datetime。"""
    if value is None:
        return default
    try:
        if isinstance(value, float) and pd.isna(value):
            return default
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        pass
    try:
        ts = pd.to_datetime(value)
        if pd.isna(ts):
            return default
        return ts.to_pydatetime().replace(tzinfo=None)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# 3. 中文数字与时间解析工具
# ---------------------------------------------------------------------------

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

_WEEKDAY_CN = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
    "1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5, "7": 6,
}
_WEEKDAY_ICS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


def cn_number(text: str) -> int | None:
    """中文数字转整数，支持「十」「十一」「二十三」「两」等常见写法。"""
    text = text.strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if text == "十":
        return 10
    if "十" in text:
        left, _, right = text.partition("十")
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    total = 0
    for ch in text:
        if ch not in _CN_DIGITS:
            return None
        total = total * 10 + _CN_DIGITS[ch]
    return total


# 时间点：可选时段前缀 + 时 + 分隔符 + 分
_PERIOD_WORDS = {
    "凌晨": 0, "清晨": 0, "早晨": 0, "早上": 0, "上午": 0, "早": 0,
    "中午": 12, "正午": 12, "下午": 12, "午后": 12,
    "傍晚": 12, "晚上": 12, "晚": 12, "夜间": 12, "深夜": 12,
}

_TIME_TOKEN_RE = re.compile(
    r"(?P<period>凌晨|清晨|早晨|早上|上午|中午|正午|下午|午后|傍晚|晚上|夜间|深夜|早|晚)?"
    r"(?P<h>\d{1,2}|[一二两三四五六七八九十]{1,3})\s*"
    # 必须带明确的时刻标记（冒号 / 点 / 时），否则 "2026" "第3章" 会被误判为时间
    r"(?:[:：]\s*(?P<m>\d{1,2})|点|时)\s*"
    r"(?P<frac>半|一刻|三刻)?"
    r"(?:\s*(?P<m2>\d{1,2})\s*分)?"
)

# `(?<!第)` 用于排除「第1-2节」被误判成「1月2日」
_DATE_FULL_RE = re.compile(
    r"(?<!第)(?:(?P<y>20\d{2})\s*[-/.年]\s*)?(?P<mo>\d{1,2})\s*[-/.月]\s*(?P<d>\d{1,2})\s*[日号]?"
)
# 中文数字日期：「十一月五日」「十二月三十一号」
_DATE_CN_RE = re.compile(
    r"(?P<mo>[一二三四五六七八九十]{1,3})\s*月\s*(?P<d>[一二三四五六七八九十]{1,3})\s*[日号]"
)

_DATE_REL_RE = re.compile(
    r"(?P<rel>大后天|后天|明天|明日|明早|明晚|今天|今日|今早|今晚|昨天|昨日|前天)"
)
_DATE_WEEK_RE = re.compile(
    r"(?P<mod>下下|下个|下|这个|这|本|上个|上)?\s*(?:周|星期|礼拜)\s*(?P<wd>[一二三四五六日天1-7])"
)
_DATE_MONTHEND_RE = re.compile(r"(?P<which>本|这个|下个|下|这)?\s*月(?P<edge>底|末|初)")

# 时段区间分隔符
_RANGE_SEP_RE = re.compile(r"\s*(?:~|～|—|–|－|-|到|至|to)\s*", re.IGNORECASE)


@dataclass
class TimeSpan:
    """文本中识别出的一个时间区间。"""
    start: datetime
    end: datetime
    matched_text: str
    date_explicit: bool = False     # 日期是否明确出现
    time_explicit: bool = False     # 时刻是否明确出现
    vague: bool = False
    reason: str = ""


def _apply_period(hour: int, period: str | None, has_minute: bool) -> tuple[int, bool]:
    """
    把「下午3点」规范化为 15 点。

    返回 (24 小时制小时, 是否发生了推断)。
    规则：
      - 有明确时段词：按词偏移；下午/晚上 且 <=12 时 +12。
      - 无时段词：1~6 点按下午处理（+12）并标记推断；12 点按中午。
    """
    if period:
        base = _PERIOD_WORDS.get(period, 0)
        if base == 0 and period in ("凌晨", "清晨", "早晨", "早上", "上午", "早"):
            return (0 if hour == 12 else hour), False
        if base == 12:
            if hour == 12:
                return 12, False
            if 1 <= hour <= 11:
                return hour + 12, False
            # 已经是 24 小时制（如「晚上 23:59」），无需推断
            return hour, False
        return hour, True
    # 无时段词：按常见作息推断
    if 1 <= hour <= 6:
        return hour + 12, True
    return hour, False


def _parse_clock_tokens(text: str) -> list[tuple[int, int, int, int, bool, bool, int]]:
    """
    扫描文本中所有时间点。

    返回 [(hour24, minute, span_start, span_end, has_minute, inferred, raw_hour), ...]
    raw_hour 是未经「下午/晚上」推断的原始小时，用于判断跨天区间
    （例如「22点到2点」的 2 点应理解为次日凌晨，而不是下午 14 点）。
    """
    tokens: list[tuple[int, int, int, int, bool, bool, int]] = []
    for m in _TIME_TOKEN_RE.finditer(text):
        raw = m.group(0).strip()
        if not raw:
            continue
        h_raw = m.group("h")
        hour = cn_number(h_raw)
        if hour is None or hour > 24:
            continue
        minute = 0
        has_minute = False
        if m.group("m"):
            minute = int(m.group("m"))
            has_minute = True
        elif m.group("m2"):
            minute = int(m.group("m2"))
            has_minute = True
        elif m.group("frac"):
            frac = m.group("frac")
            minute = {"半": 30, "一刻": 15, "三刻": 45}.get(frac, 0)
            has_minute = True
        # 「3点」这类裸数字必须带单位，避免把「2026」当成时间
        if not (m.group("period") or m.group("m") or m.group("frac")
                or "点" in raw or "时" in raw or ":" in raw or "：" in raw):
            continue
        if minute > 59:
            minute = 0
        hour24, inferred = _apply_period(hour, m.group("period"), has_minute)
        if hour24 > 23:
            hour24 = hour24 - 12 if hour24 >= 24 else hour24
        tokens.append((hour24, minute, m.start(), m.end(), has_minute, inferred, hour))
    return tokens


def parse_date_in_text(text: str, anchor: date) -> tuple[date | None, int, int, bool]:
    """
    在文本中查找日期表达。

    返回 (日期, 起, 止, 是否模糊)。未找到返回 (None, -1, -1, False)。
    """
    # 1) 完整日期：2026-11-05 / 11月5日 / 11/5
    for m in _DATE_FULL_RE.finditer(text):
        mo, d = int(m.group("mo")), int(m.group("d"))
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            continue
        year = int(m.group("y")) if m.group("y") else anchor.year
        try:
            cand = date(year, mo, d)
        except ValueError:
            continue
        if not m.group("y"):
            # 未写年份：若比锚点早太多，判定为下一年（如 12 月说「1月5日」）
            if (anchor - cand).days > VAGUE_DATE_WINDOW_DAYS:
                try:
                    cand = date(year + 1, mo, d)
                except ValueError:
                    continue
        return cand, m.start(), m.end(), False

    # 1b) 中文数字日期：十一月五日
    m = _DATE_CN_RE.search(text)
    if m:
        mo, d = cn_number(m.group("mo")), cn_number(m.group("d"))
        if mo and d and 1 <= mo <= 12 and 1 <= d <= 31:
            try:
                cand = date(anchor.year, mo, d)
                if (anchor - cand).days > VAGUE_DATE_WINDOW_DAYS:
                    cand = date(anchor.year + 1, mo, d)
                return cand, m.start(), m.end(), False
            except ValueError:
                pass

    # 2) 相对日：今天/明天/后天...
    offsets = {"大后天": 3, "后天": 2, "明天": 1, "明日": 1, "明早": 1, "明晚": 1,
               "今天": 0, "今日": 0, "今早": 0, "今晚": 0, "昨天": -1, "昨日": -1, "前天": -2}
    m = _DATE_REL_RE.search(text)
    if m:
        return anchor + timedelta(days=offsets[m.group("rel")]), m.start(), m.end(), False

    # 3) 周几：下周三 / 本周五 / 周三
    m = _DATE_WEEK_RE.search(text)
    if m:
        target_wd = _WEEKDAY_CN.get(m.group("wd"))
        if target_wd is not None:
            mod = m.group("mod") or ""
            monday = anchor - timedelta(days=anchor.weekday())
            if mod.startswith("下下"):
                week_shift = 2
            elif mod.startswith("下"):
                week_shift = 1
            elif mod.startswith("上"):
                week_shift = -1
            else:
                week_shift = 0
            cand = monday + timedelta(days=week_shift * 7 + target_wd)
            vague = week_shift == 0 and not mod  # 只说「周三」可能是本周也可能是下周
            return cand, m.start(), m.end(), vague

    # 4) 月底 / 月初（模糊）
    m = _DATE_MONTHEND_RE.search(text)
    if m:
        which = m.group("which") or ""
        base = anchor.replace(day=1)
        if which.startswith("下"):
            base = (base + timedelta(days=32)).replace(day=1)
        if m.group("edge") in ("底", "末"):
            nxt = (base + timedelta(days=32)).replace(day=1)
            return nxt - timedelta(days=1), m.start(), m.end(), True
        return base, m.start(), m.end(), True

    return None, -1, -1, False


# 支持「每周二」「每周一三五」「每周一、三、五」。
# 刻意不允许字符之间出现空格，否则「每周三 4点开会」会把 4 误读成星期四。
_RECUR_WEEKLY_RE = re.compile(
    r"每(?:周|星期|礼拜)\s*(?P<wds>"
    r"[一二三四五六日天](?:[、,，和及与/]?[一二三四五六日天])*"   # 一三五 / 一、三、五
    r"|[1-7](?:[、,，][1-7])*"                                    # 135 / 1,3,5
    r")"
)
_RECUR_DAILY_RE = re.compile(r"每(?:天|日)")
_RECUR_WORKDAY_RE = re.compile(r"(?:每)?工作日|周一至周五|周一到周五")
_RECUR_MONTHLY_RE = re.compile(r"每(?:个)?月")


def parse_recurrence(text: str) -> tuple[str, str]:
    """
    识别循环规则。

    返回 (rrule 字符串, 已匹配的原文片段)。rrule 为空表示不循环。
    """
    m = _RECUR_WORKDAY_RE.search(text)
    if m:
        return "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", m.group(0)
    m = _RECUR_WEEKLY_RE.search(text)
    if m:
        days: list[str] = []
        for ch in re.findall(r"[一二三四五六日天1-7]", m.group("wds")):
            wd = _WEEKDAY_CN.get(ch)
            if wd is not None and _WEEKDAY_ICS[wd] not in days:
                days.append(_WEEKDAY_ICS[wd])
        if days:
            return f"FREQ=WEEKLY;BYDAY={','.join(days)}", m.group(0)
    m = _RECUR_DAILY_RE.search(text)
    if m:
        return "FREQ=DAILY", m.group(0)
    m = _RECUR_MONTHLY_RE.search(text)
    if m:
        return "FREQ=MONTHLY", m.group(0)
    return "", ""


# ---------------------------- 循环规则工具 --------------------------------
# RRULE 统一存成不含 "RRULE:" 前缀的字符串，例如：
#   FREQ=WEEKLY;BYDAY=MO,WE           每周一、三
#   FREQ=WEEKLY;INTERVAL=2;BYDAY=MO   每两周周一
#   FREQ=WEEKLY;BYDAY=MO;COUNT=10     每周一，共 10 次
# 次数用标准的 COUNT 属性表达，Calendar 客户端（iOS / Outlook / Google）都能识别。

_BYDAY_ICS_TO_CN = {"MO": "一", "TU": "二", "WE": "三", "TH": "四",
                    "FR": "五", "SA": "六", "SU": "日"}


def _rrule_field(rrule: str, key: str) -> str:
    """取出 RRULE 中某个属性的值（找不到返回空串）。"""
    m = re.search(rf"(?:^|;){key}=([^;]+)", (rrule or "").upper())
    return m.group(1).strip() if m else ""


def get_recur_count(rrule: str) -> int:
    """读取 RRULE 中的 COUNT，没有则返回 0（表示未指定次数）。"""
    raw = _rrule_field(rrule, "COUNT")
    return int(raw) if raw.isdigit() else 0


def set_recur_count(rrule: str, count: int) -> str:
    """
    写入 / 替换 RRULE 中的 COUNT。

    COUNT 与 UNTIL 在 RFC 5545 中互斥，因此写入 COUNT 时一并清除 UNTIL，
    否则部分日历客户端会直接丢弃整条 RRULE。
    """
    if not rrule:
        return ""
    cleaned = re.sub(r";?\s*(?:COUNT|UNTIL)=[^;]+", "", rrule, flags=re.IGNORECASE)
    cleaned = cleaned.strip("; ")
    if count and count > 0:
        return f"{cleaned};COUNT={int(count)}"
    return cleaned


def recur_freq_key(rrule: str) -> str:
    """把 RRULE 归类回界面上的三个频率选项，无法归类时返回空串。"""
    if not rrule:
        return ""
    freq = _rrule_field(rrule, "FREQ")
    interval = _rrule_field(rrule, "INTERVAL")
    if freq == "DAILY":
        return RECUR_FREQ_DAILY
    if freq == "WEEKLY":
        return RECUR_FREQ_BIWEEKLY if interval == "2" else RECUR_FREQ_WEEKLY
    return ""


def build_recur_rrule(freq_key: str, start: datetime, base_rrule: str = "") -> str:
    """
    按界面选择的频率构造 RRULE。

    星期几的取法：
      - 原规则里已写死 BYDAY（如「每周一三五」解析出的 MO,WE,FR）→ 尊重原文
      - 否则用日程开始日期对应的星期几
    这样「每周三的课」改成「每两周重复」仍是周三，不会漂移到别的日子。
    """
    existing_days = [d for d in _rrule_field(base_rrule, "BYDAY").split(",") if d]
    days = existing_days or [_WEEKDAY_ICS[start.weekday()]]
    byday = ",".join(days)

    if freq_key == RECUR_FREQ_DAILY:
        return "FREQ=DAILY"
    if freq_key == RECUR_FREQ_BIWEEKLY:
        return f"FREQ=WEEKLY;INTERVAL=2;BYDAY={byday}"
    return f"FREQ=WEEKLY;BYDAY={byday}"


def describe_recurrence(rrule: str) -> str:
    """
    把 RRULE 转成一句中文，用于表格展示与导出文件的可读列。

    例：FREQ=WEEKLY;BYDAY=MO;COUNT=10 → 「每周一 · 共 10 次」
    """
    if not rrule:
        return ""
    freq = _rrule_field(rrule, "FREQ")
    interval = _rrule_field(rrule, "INTERVAL")
    byday = [d for d in _rrule_field(rrule, "BYDAY").split(",") if d]
    days_list = [_BYDAY_ICS_TO_CN.get(d, d) for d in byday]
    days_cn = "、".join(days_list)                          # 一、三
    days_full = "、".join(f"周{d}" for d in days_list)      # 周一、周三
    count = get_recur_count(rrule)

    if freq == "DAILY":
        desc = "每天" if interval in ("", "1") else f"每 {interval} 天"
    elif freq == "WEEKLY":
        if interval in ("", "1"):
            desc = f"每周{days_cn}" if days_cn else "每周"
        elif interval == "2":
            desc = f"每两周的{days_full}" if days_full else "每两周"
        else:
            desc = f"每 {interval} 周的{days_full}" if days_full else f"每 {interval} 周"
    elif freq == "MONTHLY":
        desc = "每月" if interval in ("", "1") else f"每 {interval} 个月"
    elif freq == "YEARLY":
        desc = "每年"
    else:
        desc = rrule          # 无法归类时原样展示，避免信息丢失

    if count:
        desc += f" · 共 {count} 次"
    elif _rrule_field(rrule, "UNTIL"):
        desc += " · 到期自动结束"
    return desc


def _rrule_summary_short(rrule: str) -> str:
    """表格里用的短标签（不带次数说明以外的前后缀）。"""
    return describe_recurrence(rrule) or "单次"


_SECTION_RE = re.compile(
    r"第\s*(?P<a>\d{1,2}|[一二三四五六七八九十]{1,3})\s*(?:-|~|～|到|至)?\s*"
    r"(?P<b>\d{1,2}|[一二三四五六七八九十]{1,3})?\s*节"
)


def parse_section_range(text: str) -> tuple[datetime | None, datetime | None, str]:
    """解析「第1-2节」这类课表表达，返回 (开始时刻, 结束时刻, 匹配文本)。"""
    m = _SECTION_RE.search(text)
    if not m:
        return None, None, ""
    a = cn_number(m.group("a"))
    b = cn_number(m.group("b")) if m.group("b") else a
    if a is None or a not in SECTION_MAP:
        return None, None, ""
    start_h, start_m = SECTION_MAP[a]
    end_h, end_m = SECTION_MAP.get(b or a, (start_h + 1, start_m))
    # 一节课 45 分钟，两节连排默认 1 小时 40 分
    if b and b > a:
        last_h, last_m = SECTION_MAP.get(b, (end_h, end_m))
        end_h, end_m = last_h, last_m + 45
        if end_m >= 60:
            end_h, end_m = end_h + 1, end_m - 60
    return (
        datetime.combine(date.today(), datetime.min.time()).replace(hour=start_h, minute=start_m),
        datetime.combine(date.today(), datetime.min.time()).replace(hour=end_h, minute=end_m),
        m.group(0),
    )


# ---------------------------------------------------------------------------
# 4. 规则解析引擎（无网络也能工作）
# ---------------------------------------------------------------------------

_PRIORITY_HIGH_RE = re.compile(
    r"紧急|重要|务必|必须|一定|ddl|deadline|截止|到期|考试|答辩|面试|报名|提交|交表|验收|汇报",
    re.IGNORECASE,
)
_PRIORITY_LOW_RE = re.compile(r"可选|随意|有空|尽量|如果方便|非必须|旁听|自愿")

_LOCATION_LABEL_RE = re.compile(
    r"(?:地点|地址|会场|场地|上课地点|集合地点|location)\s*[:：]\s*(?P<loc>[^\n,，;；。|]{2,40})",
    re.IGNORECASE,
)
_LOCATION_AT_RE = re.compile(r"[@＠]\s*(?P<loc>[^\s,，;；。]{2,30})")
_LOCATION_KEYWORD_RE = re.compile(
    r"(?:腾讯会议|钉钉会议|飞书会议|Zoom|zoom|线上(?:会议)?|"
    # 「创新创业中心 2 号厅」「校医院」「大学生活动中心」
    r"[\u4e00-\u9fa5]{1,8}(?:中心|大厦|医院|宾馆|咖啡厅)\s*\d{0,2}\s*号?\s*(?:楼|厅|室|馆)?|"
    # 楼/馆/厅/堂/场/室 + 可选楼层 + 可选字母座号 + 可选编号，覆盖「计算机楼C401」「食堂二楼」「图书馆三楼」
    r"[\u4e00-\u9fa5]{2,6}[一二三四五六七八九十]?(?:楼|馆|厅|堂|场|室)\s*[A-Za-z]?[一二三四五六七八九十\d]{0,4}|"
    r"图书馆|体育馆|报告厅|会议室|实验室|教学楼|实验楼|食堂|操场|礼堂)"
)

# 形如「【课程表】」「[备忘]」「## 标题」「---」的分段标题行，不是日程
_HEADER_LINE_RE = re.compile(
    r"^(?:[【\[（(][^】\]）)]{0,40}[】\]）)]|#{1,6}\s*\S{0,40}|[-=*_]{3,})\s*$"
)

# 「全天」「整天」等整日占位表达
_ALL_DAY_RE = re.compile(r"全天|整天|整日|一天|全天候")

_NOTE_LABEL_RE = re.compile(r"(?:备注|说明|注意|note|备注信息)\s*[:：]\s*(?P<note>[^\n]{1,120})", re.IGNORECASE)

# 这些词出现在「XX：」位置时是字段标签而非聊天发言人，不能当作备注来源
_NON_SPEAKER_WORDS = {
    "地点", "地址", "时间", "日期", "备注", "说明", "注意", "内容",
    "主题", "标题", "事项", "event", "time", "location", "note",
}


def extract_location(text: str) -> tuple[str, str]:
    """从文本中提取地点，返回 (地点, 命中的原文片段)。"""
    m = _LOCATION_LABEL_RE.search(text)
    if m:
        return m.group("loc").strip(" ，,。;；"), m.group(0)
    m = _LOCATION_AT_RE.search(text)
    if m:
        return m.group("loc").strip(" ，,。;；"), m.group(0)
    m = _LOCATION_KEYWORD_RE.search(text)
    if m:
        loc = re.sub(r"^(?:在|于|从|到|去)", "", m.group(0).strip())
        return loc.strip(), m.group(0)
    return "", ""


def extract_priority(text: str) -> str:
    if _PRIORITY_HIGH_RE.search(text):
        return "高"
    if _PRIORITY_LOW_RE.search(text):
        return "低"
    return "中"


def _clean_title(text: str) -> str:
    """从一行文本里剥离时间/地点/标记，剩下的作为标题。"""
    cleaned = text
    # 顺序有意为之：先删循环词（「每周四」整体消失），
    # 再删日期词，否则「每周四」里的「周四」会先被吃掉，留下一个孤零零的「每」。
    for pattern in (
        _RECUR_WORKDAY_RE, _RECUR_WEEKLY_RE, _RECUR_DAILY_RE, _RECUR_MONTHLY_RE,
        _DATE_FULL_RE, _DATE_CN_RE, _DATE_REL_RE, _DATE_WEEK_RE, _DATE_MONTHEND_RE,
        _ALL_DAY_RE, _TIME_TOKEN_RE, _SECTION_RE,
        _LOCATION_LABEL_RE, _LOCATION_AT_RE, _NOTE_LABEL_RE,
    ):
        cleaned = pattern.sub(" ", cleaned)
    # 「22点到次日2点」中时间被删除后残留的「到次日」
    cleaned = re.sub(r"(?:到|至|~|～|—)?\s*(?:次日|翌日|第二天)", " ", cleaned)
    cleaned = re.sub(r"[（(【\[]?\s*(?:周|星期|礼拜)[一二三四五六日天]\s*[)）】\]]?", " ", cleaned)
    cleaned = re.sub(r"(?:上午|下午|晚上|早上|中午|凌晨|傍晚|晚)\s*(?=$|\s)", " ", cleaned)
    # 时间被删掉后残留的孤立连接词：「3点 到 5点」→ 中间的「到」
    cleaned = re.sub(r"(?<!\S)(?:到|至|在|从|每|前)(?!\S)", " ", cleaned)
    # 「23:59 前提交」中时间被删掉后残留的「前」，仅在其后接提交类动词时清理
    cleaned = re.sub(r"(?<=\s)前(?=(?:提交|完成|截止|上交|上传|填写|报名|回复|确认|发))", " ", cleaned)
    cleaned = re.sub(r"[，,、;；|·•\-–—~～:/：\s]+", " ", cleaned)
    cleaned = re.sub(r"^[\s\d.、)\]]+", "", cleaned)
    cleaned = cleaned.strip(" 。.，,、;；:：")
    return cleaned


def _split_candidates(text: str) -> list[str]:
    """
    把整段文本切成「候选日程行」。

    策略：按换行优先；对明显的分号/序号列表也做切分；过滤空行与过短行。
    """
    lines: list[str] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        # 形如「1. 」「2、」「3) 」的列表项各自成行。
        # 注意：点号后必须跟空白，否则 "11.5 交报告" 会被误拆成 "11." + "5 交报告"。
        parts = re.split(r"(?:^|\s)(?=\d{1,2}[、)）]\s+|\d{1,2}\.\s+)", line)
        for part in parts:
            part = part.strip()
            if part:
                lines.append(part)
    # 剔除纯符号行（如 "!@#$"）与空行：它们不可能包含日程信息
    return [ln for ln in lines if len(ln) >= 2 and re.search(r"[\u4e00-\u9fa5A-Za-z0-9]", ln)]


def _looks_like_date_only(line: str) -> bool:
    """判断该行是否只是「11月5日」这样的日期小标题。"""
    stripped = _DATE_FULL_RE.sub("", line)
    stripped = _DATE_REL_RE.sub("", stripped)
    stripped = _DATE_WEEK_RE.sub("", stripped)
    stripped = re.sub(r"[\s、:：.。\-—()（）]", "", stripped)
    return len(stripped) == 0


# --- 行程（车票 / 登机牌）识别 ----------------------------------------------
# 购票截图经 OCR 后，时间、车次、站名常被拆成十几行短碎片，
# 逐行解析会产出一堆「未命名日程」。这里先用整体视角识别一段行程，
# 合成一条事件，并把用掉的行标记出来，避免重复入库。
_TRIP_TRAIN_RE = re.compile(r"(?<![A-Za-z0-9])([GDCKZT]\d{1,4})(?!\d)")
_BARE_CLOCK_RE = re.compile(r"^\s*(\d{1,2})\s*[:：]\s*(\d{2})\s*$")
_CITY_LINE_RE = re.compile(r"^[\u4e00-\u9fa5]{2,6}$")
# 车票界面上大量非日程文案，识别站名时要排除
_TRIP_NOISE_WORDS = {
    "改签", "退票", "复制", "成人票", "学生票", "儿童票", "订酒店", "租车",
    "约车", "积分支付", "线上购买", "二维码检票", "变更到站", "改签票",
    "当日有效", "餐饮", "特产", "车票", "订单号", "乘车日期", "检票", "支付",
    "周一", "周二", "周三", "周四", "周五", "周六", "周日", "周六日",
    "当天", "当日", "次日", "出发", "到达", "电子票", "纸质票", "报销凭证",
}


def _extract_trip_events(text: str, anchor_day: date) -> tuple[list[Event], set[str]]:
    """
    识别「车次 + 出发时间 + 到达时间 + 出发站 + 到达站」构成的一段行程。

    只在文本里出现车次（G/D/C/K/Z/T + 数字）时才介入，普通日程完全不受影响。
    返回 (行程事件列表, 已被消费的行文本集合)。
    """
    lines = [ln.strip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    flat = "\n".join(lines)

    train_m = _TRIP_TRAIN_RE.search(flat)
    if not train_m:
        return [], set()

    # 车次前后的时刻。不锚定行首，因此「09:56 镇江 G7045 10:27 常州」这类
    # 单行布局与「每行一个碎片」的分行布局都能命中。
    clocks = [
        (m.start(), m.end(), int(m.group(1)), int(m.group(2)))
        for m in re.finditer(r"(?<!\d)(\d{1,2})\s*[:：]\s*(\d{2})(?!\d)", flat)
    ]
    before = [c for c in clocks if c[1] <= train_m.start()]
    after = [c for c in clocks if c[0] >= train_m.end()]
    if not before or not after:
        return [], set()

    s1, e1, h1, m1 = before[-1]
    s2, e2, h2, m2 = after[0]

    # --- 出发站 / 到达站 ------------------------------------------------
    from_city = ""
    to_city = ""
    mid = flat[e1:train_m.start()].strip()
    if re.fullmatch(r"[\u4e00-\u9fa5]{2,6}", mid):
        # 单行布局：出发站夹在出发时间与车次之间，到达站紧跟到达时间
        from_city = mid
        same_line_tail = flat[e2:].split("\n")[0]
        hit = re.match(r"\s*([\u4e00-\u9fa5]{2,6})", same_line_tail)
        to_city = hit.group(1) if hit else ""
    else:
        # 分行布局（12306）：站名跟在到达时间之后，先出发站后到达站
        t2_line_idx = flat.count("\n", 0, s2)
        tail_lines = lines[t2_line_idx + 1:]
        cities = [
            ln for ln in tail_lines
            if _CITY_LINE_RE.match(ln) and ln not in _TRIP_NOISE_WORDS
        ][:2]
        from_city = cities[0] if cities else ""
        to_city = cities[1] if len(cities) > 1 else ""

    # 日期：优先用全文里带年份的完整日期
    day, _, _, _ = parse_date_in_text(flat, anchor_day)
    day = day or anchor_day

    start_dt = datetime.combine(day, datetime.min.time()).replace(hour=h1 % 24, minute=m1)
    end_dt = datetime.combine(day, datetime.min.time()).replace(hour=h2 % 24, minute=m2)
    if end_dt <= start_dt:                     # 跨天车次，如 23:30 → 06:00
        end_dt += timedelta(days=1)

    # 从原文补齐座位 / 订单号 / 票价，放进备注
    extras: list[str] = []
    seat = re.search(r"([一二三]等座\s*\d{1,3}\s*车\s*\d{1,3}\s*[A-F]?\s*号?)", flat)
    if seat:
        extras.append(seat.group(1).replace(" ", ""))
    order = re.search(r"订单号\s*[:：]\s*([A-Za-z0-9]{6,})", flat)
    if order:
        extras.append(f"订单号 {order.group(1)}")
    price = re.search(r"[¥￥]\s*(\d+(?:\.\d+)?)", flat)
    if price:
        extras.append(f"¥{price.group(1)}")

    title = " → ".join(p for p in (from_city, to_city) if p)
    title = f"{title} {train_m.group(1)}".strip() if title else f"乘坐 {train_m.group(1)}"

    ev = Event(
        title=title,
        start=start_dt,
        end=end_dt,
        location=" → ".join(p for p in (from_city, to_city) if p),
        note="｜".join(extras)[:120],
        priority="高",
        vague=False,
        source="text",
        raw=f"{train_m.group(1)} {h1:02d}:{m1:02d}-{h2:02d}:{m2:02d}",
    )

    # 已消费的行：出发时刻、到达时刻、车次所在行，以及被用作站名的行
    consumed: set[str] = set()
    for offset in (s1, s2, train_m.start()):
        consumed.add(lines[flat.count("\n", 0, offset)])
    consumed.update(c for c in (from_city, to_city) if c)
    return [ev], consumed


def parse_with_rules(text: str, anchor: datetime | None = None,
                     default_remind: int = -1) -> list[Event]:
    """
    规则引擎：把一段自由文本解析成 Event 列表。

    这是没有大模型时的主力解析路径，也是大模型结果的补全/校验来源。
    """
    anchor_dt = anchor or datetime.now()
    anchor_day = anchor_dt.date()
    events: list[Event] = []
    inherit_date: date | None = None   # 上一行日期小标题，供后续行继承

    # 整体级日期：整段文字里若出现「2026年10月31日」这类带年份的完整日期，
    # 就作为无日期行的兜底，避免「09:56」这类碎片被错挂到今天。
    doc_day: date | None = None
    if re.search(r"20\d{2}\s*[年./-]", text) or re.search(r"日期\s*[:：]", text):
        cand, _, _, _ = parse_date_in_text(text, anchor_day)
        doc_day = cand

    # 行程（车票/登机牌）优先整体识别，避免碎片逐行入库
    trip_events, trip_consumed = _extract_trip_events(text, doc_day or anchor_day)
    events.extend(trip_events)

    for line in _split_candidates(text):
        line = line[:400]
        if line.strip() in trip_consumed:
            continue

        # 过滤「【课程表 截图里的内容】」「## 备忘」这类分段标题行：
        # 它们本身不含任何时间信息，不应被当成日程
        if (_HEADER_LINE_RE.match(line)
                and not _parse_clock_tokens(line)
                and parse_date_in_text(line, anchor_day)[0] is None):
            continue

        # --- 日期 -----------------------------------------------------
        if _looks_like_date_only(line):
            d, _, _, vague = parse_date_in_text(line, anchor_day)
            if d:
                inherit_date = d
            continue

        # 「乘车日期：2026年10月31日」这类元信息行只提供日期上下文，本身不是日程
        if re.match(r"^(?:乘车|出发|到达|返程|出行|会议|活动)?日期\s*[:：]", line):
            d, _, _, _v = parse_date_in_text(line, anchor_day)
            if d:
                inherit_date = d
            continue

        day, d_start, d_end, date_vague = parse_date_in_text(line, anchor_day)
        date_explicit = day is not None
        vague_reasons: list[str] = []

        if day is None:
            if inherit_date is not None:
                day = inherit_date
                vague_reasons.append("日期继承自上文小标题")
            elif doc_day is not None:
                day = doc_day
                vague_reasons.append("日期取自全文中的完整日期")
            else:
                day = anchor_day
                vague_reasons.append("未识别到日期，按今天处理")

        # --- 循环 -----------------------------------------------------
        rrule, _ = parse_recurrence(line)

        # --- 时间点 ---------------------------------------------------
        clock_text = line
        if d_start >= 0:
            clock_text = line[:d_start] + " " * (d_end - d_start) + line[d_end:]
        tokens = _parse_clock_tokens(clock_text)

        # --- 噪声过滤 -------------------------------------------------
        # 车票 / 账单 / 通知类截图会 OCR 出一大堆纯 UI 文案（「退票」「¥36」
        # 「9折」「复制」），它们既无时间也无日期。若不拦掉，逐行入库会产出
        # 十几条「未命名日程」，把真正的那一条淹没。
        if not (
            tokens or rrule or date_explicit
            or _ALL_DAY_RE.search(line)
            or "节" in line                                          # 课表「第3-4节」
            or re.search(r"ddl|deadline|截止|到期|最后期限", line, re.IGNORECASE)
        ):
            continue

        # 整行只有一个时刻（如「09:56」）的碎片，单独成条没有意义
        if _BARE_CLOCK_RE.match(line) and not date_explicit:
            continue

        start_dt: datetime | None = None
        end_dt: datetime | None = None
        time_explicit = False

        # 课表「第X-Y节」优先
        sec_start, sec_end, sec_text = parse_section_range(line)
        if sec_start and sec_start.date() == date.today():
            start_dt = datetime.combine(day, sec_start.time())
            end_dt = datetime.combine(day, sec_end.time())
            if end_dt <= start_dt:
                end_dt = start_dt + timedelta(minutes=95)
            time_explicit = True
            if not rrule:
                rrule = f"FREQ=WEEKLY;BYDAY={_WEEKDAY_ICS[day.weekday()]}"

        if start_dt is None and tokens:
            h1, m1, s1, e1, mm1, inf1, raw1 = tokens[0]
            start_dt = datetime.combine(day, datetime.min.time()).replace(hour=h1, minute=m1)
            time_explicit = True
            if inf1:
                vague_reasons.append("未写上午/下午，已按常见时段推断")

            # 找区间终点：两个时间点之间必须有分隔符（- ~ 到 至）
            if len(tokens) >= 2:
                h2, m2, s2, e2, mm2, inf2, raw2 = tokens[1]
                between = clock_text[e1:s2]
                if _RANGE_SEP_RE.fullmatch(between.strip()) or (
                    between.strip() and _RANGE_SEP_RE.search(between)
                ):
                    # 第二个时间点没写时段词时，需依据第一个时间点推断：
                    #   22点到2点  → 2 点是次日凌晨，保持 2
                    #   下午3点到5点 → 5 点是下午，+12 得 17
                    #   上午9点到11点 → 保持 11
                    if inf2:
                        if h1 >= 18 and raw2 <= 11:
                            h2 = raw2
                        elif 12 <= h1 < 18 and raw2 < 12:
                            h2 = raw2 + 12
                        elif h1 < 12 and raw2 < 12:
                            h2 = raw2
                    end_dt = datetime.combine(day, datetime.min.time()).replace(hour=h2 % 24, minute=m2)

            # 显式时长：「2小时」「90分钟」
            if end_dt is None:
                dur = re.search(r"(\d+(?:\.\d+)?)\s*(?:个)?\s*(小时|h|H)", line)
                dur_min = re.search(r"(\d+)\s*分钟", line)
                if dur:
                    end_dt = start_dt + timedelta(minutes=float(dur.group(1)) * 60)
                elif dur_min:
                    end_dt = start_dt + timedelta(minutes=int(dur_min.group(1)))

        # 「全天 / 整天」：识别为整日事件（00:00–23:59），
        # 必须放在时间点解析之后，否则会覆盖显式时刻
        if start_dt is None and _ALL_DAY_RE.search(line):
            start_dt = datetime.combine(day, datetime.min.time())
            end_dt = datetime.combine(day, datetime.max.time()).replace(microsecond=0)
            time_explicit = True

        if start_dt is None:
            # 完全没有时间信息
            start_dt = datetime.combine(day, datetime.min.time()).replace(hour=DEFAULT_DAY_START_HOUR)
            vague_reasons.append(f"未识别到具体时间，默认 {DEFAULT_DAY_START_HOUR}:00")

        # DDL / 截止时间是一个「时间点」而非时间段，时长应为 0
        is_deadline = bool(re.search(r"ddl|deadline|截止|到期|最后期限", line, re.IGNORECASE)) \
            and len(tokens) <= 1 and start_dt is not None

        if end_dt is None:
            if is_deadline:
                end_dt = start_dt          # 零时长：表示「截止于此刻」
            else:
                end_dt = start_dt + timedelta(minutes=DEFAULT_EVENT_MINUTES)
                if time_explicit:
                    vague_reasons.append("未写结束时间，默认持续 1 小时")

        # 跨天修正：结束早于开始 → 视为次日（如 22:00-02:00）
        if end_dt < start_dt:
            end_dt = end_dt + timedelta(days=1)
            vague_reasons.append("结束时间早于开始时间，已按跨天处理")
        elif end_dt == start_dt and not is_deadline:
            end_dt = start_dt + timedelta(minutes=DEFAULT_EVENT_MINUTES)

        # --- 标题 / 地点 / 优先级 -------------------------------------
        location, loc_raw = extract_location(line)
        note_m = _NOTE_LABEL_RE.search(line)
        note = note_m.group("note").strip() if note_m else ""
        # 聊天记录形如「班长：下周三下午2点开会」——把说话人记为备注，便于溯源
        if not note:
            speaker = re.match(r"^([\u4e00-\u9fa5A-Za-z0-9_]{1,8})\s*[:：]\s*(?=\S)", line)
            if speaker and speaker.group(1) not in _NON_SPEAKER_WORDS:
                note = f"来源：{speaker.group(1)}"
        note = note[:120]

        title = _clean_title(line)
        if loc_raw:
            title = title.replace(loc_raw, " ")
        title = re.sub(r"\s{2,}", " ", title).strip()
        if not title:
            title = "未命名日程"
        if len(title) > 40:
            title = title[:40] + "…"

        # 循环日程里的「周四」是循环规则的一部分，不算模糊日期
        vague = bool(vague_reasons) or (date_vague and not rrule)
        if date_vague and not vague_reasons and not rrule:
            vague_reasons.append("仅写了星期，已按最近的该星期几处理")

        events.append(Event(
            title=title,
            start=start_dt,
            end=end_dt,
            location=location,
            note=note,
            priority=extract_priority(line),
            rrule=rrule,
            vague=vague,
            vague_reason="；".join(vague_reasons),
            source="text",
            remind_minutes=default_remind,
            raw=line,
        ))

    return events


# ---------------------------------------------------------------------------
# 5. OCR 层（多引擎可选，全部本地执行，图片不出本机）
# ---------------------------------------------------------------------------

def get_ocr_engine() -> tuple[str, Any]:
    """
    探测可用的 OCR 引擎，按优先级返回 ("rapidocr"|"pytesseract"|"none", 引擎对象)。

    使用 st.cache_resource 缓存，避免每次交互都重新加载模型（约 10MB）。
    """
    return _load_ocr_engine()


@st.cache_resource(show_spinner=False)
def _load_ocr_engine() -> tuple[str, Any]:
    # 优先 RapidOCR：纯 pip 安装、内置中文模型、无需系统级依赖
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore
        return "rapidocr", RapidOCR()
    except Exception as exc:  # noqa: BLE001
        log(f"rapidocr_onnxruntime 不可用: {exc}")
    try:
        from rapidocr import RapidOCR  # type: ignore  # 新版包名
        return "rapidocr", RapidOCR()
    except Exception as exc:  # noqa: BLE001
        log(f"rapidocr 不可用: {exc}")
    # 退而求其次：本机已安装 Tesseract 时可用 pytesseract
    try:
        import pytesseract  # type: ignore
        pytesseract.get_tesseract_version()
        return "pytesseract", pytesseract
    except Exception as exc:  # noqa: BLE001
        log(f"pytesseract 不可用: {exc}")
    return "none", None


def _prepare_image(data: bytes) -> tuple[Any, list[str]]:
    """
    校验并规范化上传图片。

    返回 (PIL.Image, 警告列表)；校验失败时抛 ValueError（由调用方转成用户提示）。
    """
    from PIL import Image, ImageOps, UnidentifiedImageError

    if not data:
        raise ValueError("图片内容为空，请重新上传。")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(
            f"图片过大（{len(data) / 1024 / 1024:.1f}MB），请压缩到 "
            f"{MAX_IMAGE_BYTES // 1024 // 1024}MB 以内后重试。"
        )
    try:
        img = Image.open(io.BytesIO(data))
        img.load()  # 强制解码，损坏文件会在这一步暴露
    except UnidentifiedImageError:
        raise ValueError("无法识别该图片格式，文件可能已损坏或不是图片。请上传 PNG/JPG 截图。")
    except (OSError, ValueError) as exc:
        raise ValueError(f"图片解码失败（{type(exc).__name__}），文件可能已损坏。") from exc

    warnings: list[str] = []
    # 手机截图的 EXIF 方向修正
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:  # noqa: BLE001
        pass

    if img.width < 40 or img.height < 40:
        raise ValueError("图片尺寸过小（小于 40px），无法进行文字识别。")

    # 大图缩放：只缩小不放大，保持清晰度同时显著提升 OCR 速度
    if max(img.size) > MAX_IMAGE_EDGE:
        ratio = MAX_IMAGE_EDGE / max(img.size)
        new_size = (max(1, int(img.width * ratio)), max(1, int(img.height * ratio)))
        img = img.resize(new_size)
        warnings.append(f"图片较大，已等比缩放至 {new_size[0]}×{new_size[1]} 以提升识别速度。")

    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    return img, warnings


def ocr_image(data: bytes) -> dict[str, Any]:
    """
    对单张图片执行 OCR。

    返回 {"ok", "text", "engine", "warnings", "error", "confidence"}，
    任何异常都被收敛为 ok=False + 友好中文提示，绝不向上抛出。
    """
    result: dict[str, Any] = {
        "ok": False, "text": "", "engine": "none",
        "warnings": [], "error": "", "confidence": None,
    }
    try:
        img, warns = _prepare_image(data)
        result["warnings"].extend(warns)
    except ValueError as exc:
        result["error"] = str(exc)
        return result
    except Exception as exc:  # noqa: BLE001
        log(f"图片预处理异常: {traceback.format_exc()}")
        result["error"] = f"图片处理失败（{type(exc).__name__}），请更换图片重试。"
        return result

    engine_name, engine = get_ocr_engine()
    result["engine"] = engine_name
    if engine_name == "none":
        result["error"] = (
            "未安装 OCR 引擎。请在终端执行 `pip install -r requirements.txt`"
            "（或 `pip install rapidocr onnxruntime`）后重启应用；"
            "也可以直接在左侧粘贴文字，文本解析功能不受影响。"
        )
        return result

    try:
        if engine_name == "rapidocr":
            import numpy as np
            arr = np.array(img)
            output = engine(arr)
            # rapidocr 不同版本返回 (result, elapse) 或 带 .txts 的对象
            lines: list[str] = []
            scores: list[float] = []
            if isinstance(output, tuple):
                payload = output[0]
                if payload:
                    for item in payload:
                        if isinstance(item, (list, tuple)) and len(item) >= 3:
                            lines.append(str(item[1]))
                            try:
                                scores.append(float(item[2]))
                            except (TypeError, ValueError):
                                pass
            else:
                txts = getattr(output, "txts", None) or []
                scs = getattr(output, "scores", None) or []
                lines = [str(t) for t in txts]
                scores = [float(s) for s in scs if s is not None]
            text = "\n".join(ln.strip() for ln in lines if ln and ln.strip())
            result["confidence"] = round(sum(scores) / len(scores), 3) if scores else None
        else:  # pytesseract
            import pytesseract
            text = pytesseract.image_to_string(img, lang="chi_sim+eng")
            text = "\n".join(ln.strip() for ln in text.splitlines() if ln.strip())
    except Exception as exc:  # noqa: BLE001
        log(f"OCR 执行异常: {traceback.format_exc()}")
        result["error"] = (
            f"OCR 识别失败（{type(exc).__name__}）。建议：裁剪截图只保留文字区域、"
            "提高图片清晰度，或改用「粘贴文字」方式录入。"
        )
        return result

    result["text"] = text
    if len(text.strip()) < 4:
        result["error"] = (
            "未从图片中识别到有效文字。可能是图片过于模糊、文字过小或背景干扰过强，"
            "建议重新截图（放大文字区域）或直接粘贴文字。"
        )
        return result
    if result["confidence"] is not None and result["confidence"] < 0.55:
        result["warnings"].append(
            f"识别置信度偏低（{result['confidence']:.0%}），解析结果可能需要人工核对。"
        )
    result["ok"] = True
    return result


# ---------------------------------------------------------------------------
# 6. 大模型解析层（任意 OpenAI 兼容接口，可选）
# ---------------------------------------------------------------------------

AI_SYSTEM_PROMPT = """你是一个严谨的日程结构化引擎，负责把用户提供的杂乱文本整理成日程 JSON。

【输出要求】
只输出一个 JSON 对象，形如 {"events": [...]}，不要任何解释文字、不要 Markdown 代码块。
每个 event 的字段：
- title: 字符串，简洁的日程标题（不超过 30 字），不要包含时间和地点
- start: 字符串，"YYYY-MM-DD HH:MM"（24 小时制）
- end: 字符串，"YYYY-MM-DD HH:MM"，必须晚于 start
- location: 字符串，没有则空串
- note: 字符串，补充说明，没有则空串
- priority: "高" | "中" | "低"（DDL、考试、面试、答辩、必须参加 → 高）
- rrule: 字符串，循环规则。每周固定 → "FREQ=WEEKLY;BYDAY=MO,WE"；
         每天 → "FREQ=DAILY"；工作日 → "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"；不循环 → ""
- vague: 布尔值，时间是否为推测得出
- vague_reason: 字符串，若 vague 为 true，用中文简短说明推测依据

【硬性规则】
1. 基准日期是 {today}（星期{weekday}），所有相对时间（明天、下周三）都据此换算。
2. 用户没写年份时，取未来最近的合理年份。
3. 没写结束时间：活动默认 1 小时；DDL/截止时间只取该时刻，end 与 start 相同。
4. 没写具体时间但能确定日期：默认 09:00 开始，并置 vague=true。
5. 完全无法判断时间的条目：跳过，不要编造。
6. 同一件事在多处重复出现，只保留一条。
7. 「第1-2节」这类课表表达，第1节=08:00，第2节=08:55，第3节=10:00，第4节=10:55，
   第5节=14:00，第6节=14:55，第7节=16:00，第8节=16:55，第9节=19:00，第10节=19:55。
8. 跨天活动（如 22:00 到次日 02:00）的 end 必须写次日日期。
9. 已过期的日程不要输出。
10. 时间有歧义（如只写「3点」）必须置 vague=true。
"""


class AIError(Exception):
    """大模型调用失败，携带可直接展示给用户的中文提示。"""


def _read_secret(key: str) -> str:
    """
    读取 Streamlit secrets（部署到 Streamlit Cloud 时使用）。

    优先级低于环境变量；本地开发没有 secrets.toml 时静默返回空串。
    绝不会把密钥回显到页面或日志。
    """
    try:
        value = st.secrets.get(key)  # type: ignore[union-attr]
        return str(value).strip() if value else ""
    except Exception:  # noqa: BLE001 - 无 secrets.toml 时会抛异常，属预期情况
        return ""


def get_ai_config() -> dict[str, Any]:
    """
    从环境变量（或 Streamlit secrets）读取大模型配置。

    严禁在代码中硬编码密钥：这里只读取运行时注入的配置，读不到就返回 available=False。
    """
    api_key = (
        (os.environ.get("AI_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
        or _read_secret("AI_API_KEY")
        or _read_secret("OPENAI_API_KEY")
    )
    base_url = (
        (os.environ.get("AI_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or "").strip()
        or _read_secret("AI_BASE_URL")
        or "https://api.deepseek.com/v1"
    ).rstrip("/")
    model = (
        (os.environ.get("AI_MODEL") or "").strip()
        or _read_secret("AI_MODEL")
        or "deepseek-chat"
    )
    try:
        timeout = float(os.environ.get("AI_TIMEOUT", "60"))
    except ValueError:
        timeout = 60.0
    return {
        "available": bool(api_key),
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "timeout": max(10.0, min(timeout, 180.0)),
    }


def _extract_json_block(content: str) -> dict[str, Any]:
    """从模型返回中稳健地抠出 JSON（兼容 ```json 包裹、前后废话）。"""
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    # 退一步：截取第一个 { 到最后一个 }
    start, end = content.find("{"), content.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(content[start:end + 1])
        except json.JSONDecodeError:
            pass
    # 再退一步：可能是裸数组
    start, end = content.find("["), content.rfind("]")
    if start != -1 and end > start:
        try:
            return {"events": json.loads(content[start:end + 1])}
        except json.JSONDecodeError:
            pass
    raise AIError("大模型返回的内容不是合法 JSON，本次改用本地规则引擎解析。")


def _post_chat(config: dict[str, Any], messages: list[dict[str, str]],
               force_json: bool = True) -> str:
    """调用 OpenAI 兼容的 /chat/completions，含超时与 HTTP 错误处理。"""
    import requests

    url = f"{config['base_url']}/chat/completions"
    payload: dict[str, Any] = {
        "model": config["model"],
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": 2000,
    }
    if force_json:
        payload["response_format"] = {"type": "json_object"}

    headers = {
        "Authorization": f"Bearer {config['api_key']}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(url, headers=headers, json=payload,
                             timeout=(5, config["timeout"]))
    except requests.exceptions.Timeout:
        raise AIError(f"大模型请求超时（超过 {config['timeout']:.0f} 秒）。"
                      "已自动改用本地规则引擎解析，可在左侧调大 AI_TIMEOUT 后重试。")
    except requests.exceptions.SSLError:
        raise AIError("与大模型服务建立安全连接失败（SSL 错误），请检查网络或代理设置。")
    except requests.exceptions.ConnectionError:
        raise AIError("无法连接大模型服务，请检查网络、代理或 AI_BASE_URL 配置是否正确。")
    except requests.exceptions.RequestException as exc:
        raise AIError(f"大模型请求失败（{type(exc).__name__}），请稍后重试。")

    if resp.status_code == 401:
        raise AIError("大模型鉴权失败（401）：API Key 无效或已过期，请检查 AI_API_KEY。")
    if resp.status_code == 404:
        raise AIError("接口不存在（404）：请检查 AI_BASE_URL 与 AI_MODEL 是否匹配。")
    if resp.status_code == 429:
        raise AIError("大模型调用频率或额度超限（429），请稍后再试。")
    if resp.status_code >= 500:
        raise AIError(f"大模型服务端异常（HTTP {resp.status_code}），请稍后重试。")
    if resp.status_code == 400 and force_json:
        # 部分服务不支持 response_format，去掉后重试一次
        return _post_chat(config, messages, force_json=False)
    if not resp.ok:
        raise AIError(f"大模型返回异常状态（HTTP {resp.status_code}）。")

    try:
        data = resp.json()
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError, ValueError):
        raise AIError("大模型返回结构异常，无法提取内容，本次改用本地规则引擎。")


def parse_with_ai(text: str, config: dict[str, Any],
                  default_remind: int = -1) -> list[Event]:
    """调用大模型解析文本，返回 Event 列表。失败抛 AIError。"""
    now = datetime.now(CN_TZ).replace(tzinfo=None)
    weekday_cn = "一二三四五六日"[now.weekday()]
    system = AI_SYSTEM_PROMPT.replace("{today}", now.strftime("%Y-%m-%d")).replace(
        "{weekday}", weekday_cn
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"请整理以下日程文本：\n\n{text}"},
    ]
    content = _post_chat(config, messages)
    data = _extract_json_block(content)

    raw_events = data.get("events") if isinstance(data, dict) else data
    if not isinstance(raw_events, list):
        raise AIError("大模型返回的 JSON 结构不符合预期（缺少 events 数组）。")

    events: list[Event] = []
    for item in raw_events[:60]:      # 单次最多 60 条，防止异常输出撑爆页面
        if not isinstance(item, dict):
            continue
        start = _safe_parse_dt(item.get("start"))
        end = _safe_parse_dt(item.get("end"))
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        if start is None:
            continue
        if end is None or end <= start:
            end = start + timedelta(minutes=DEFAULT_EVENT_MINUTES)
        pri = str(item.get("priority") or "中").strip()
        if pri not in PRIORITY_LEVELS:
            pri = "中"
        rrule = str(item.get("rrule") or "").strip().upper()
        rrule = re.sub(r"^RRULE:", "", rrule)
        # 只允许 FREQ=...;BYDAY=...;UNTIL=... 这类安全字符，防止注入非法内容
        if rrule and not re.fullmatch(r"[A-Z0-9=;,:\-]{0,120}", rrule):
            rrule = ""
        events.append(Event(
            title=title[:40],
            start=start,
            end=end,
            location=str(item.get("location") or "")[:60],
            note=str(item.get("note") or "")[:300],
            priority=pri,
            rrule=rrule,
            vague=bool(item.get("vague")),
            vague_reason=str(item.get("vague_reason") or "")[:120],
            source="llm",
            remind_minutes=default_remind,
            raw=text[:200],
        ))
    return events


def _safe_parse_dt(value: Any) -> datetime | None:
    """把模型给的多种时间写法统一成 naive datetime，失败返回 None。"""
    if not value:
        return None
    s = str(value).strip().replace("T", " ").replace("/", "-")
    s = re.sub(r"(Z|[+-]\d{2}:?\d{2})$", "", s).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%m-%d %H:%M", "%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            if fmt.startswith("%m"):
                dt = dt.replace(year=datetime.now().year)
            return dt
        except ValueError:
            continue
    try:
        ts = pd.to_datetime(s)
        if pd.isna(ts):
            return None
        return ts.to_pydatetime().replace(tzinfo=None)
    except Exception:
        return None


def merge_ai_and_rules(ai_events: list[Event], rule_events: list[Event]) -> list[Event]:
    """
    融合两路解析结果。

    以 AI 结果为主，用规则结果补全空缺字段（地点、优先级、循环），
    并为 AI 遗漏的条目补位。同一时间段内标题相近者视为同一条。
    """
    merged: list[Event] = []
    used_rule: set[int] = set()

    for ai_ev in ai_events:
        best_i, best_score = -1, 0.0
        for i, r_ev in enumerate(rule_events):
            if i in used_rule:
                continue
            delta = abs((ai_ev.start - r_ev.start).total_seconds()) / 60
            if delta > 180:
                continue
            sim = _title_similarity(ai_ev.title, r_ev.title)
            score = sim + (1.0 if delta <= 15 else 0.4)
            if score > best_score:
                best_i, best_score = i, score
        if best_i >= 0 and best_score >= 0.9:
            r_ev = rule_events[best_i]
            used_rule.add(best_i)
            if not ai_ev.location and r_ev.location:
                ai_ev.location = r_ev.location
            if not ai_ev.note and r_ev.note:
                ai_ev.note = r_ev.note
            if not ai_ev.rrule and r_ev.rrule:
                ai_ev.rrule = r_ev.rrule
            if ai_ev.priority == "中" and r_ev.priority != "中":
                ai_ev.priority = r_ev.priority
            if not ai_ev.vague and r_ev.vague:
                ai_ev.vague = True
                ai_ev.vague_reason = r_ev.vague_reason
        merged.append(ai_ev)

    for i, r_ev in enumerate(rule_events):
        if i in used_rule:
            continue
        # AI 漏掉的条目：规则引擎结果里若与其他 AI 条目高度相近则跳过
        if any(_title_similarity(r_ev.title, m.title) > 0.85
               and abs((r_ev.start - m.start).total_seconds()) < 24 * 3600
               for m in merged):
            continue
        r_ev.vague = True
        r_ev.vague_reason = (r_ev.vague_reason + "；" if r_ev.vague_reason else "") + "由本地规则引擎补充"
        merged.append(r_ev)

    return merged


def _title_similarity(a: str, b: str) -> float:
    """标题相似度 0~1，用于去重与结果融合。"""
    from difflib import SequenceMatcher
    na = re.sub(r"[\s\W_]+", "", a.lower())
    nb = re.sub(r"[\s\W_]+", "", b.lower())
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


# ---------------------------------------------------------------------------
# 7. 去重 / 冲突检测 / 过期过滤
# ---------------------------------------------------------------------------

def normalize_events(events: list[Event], now: datetime | None = None) -> tuple[list[Event], dict[str, Any]]:
    """
    对日程集合做：过期过滤 → 去重 → 冲突检测。

    返回 (处理后的列表, 统计报告)。
    """
    now = now or datetime.now()
    report = {"expired": 0, "duplicated": 0, "conflicts": 0}

    # --- 7.1 过滤过期（循环日程保留，因为未来仍会发生） ---------------
    fresh: list[Event] = []
    for ev in events:
        if ev.is_recurring:
            fresh.append(ev)
            continue
        if ev.end < now - timedelta(minutes=1):
            report["expired"] += 1
            continue
        fresh.append(ev)

    # --- 7.2 去重（标题相似 + 开始时间相近） --------------------------
    deduped: list[Event] = []
    for ev in sorted(fresh, key=lambda e: e.start):
        dup = False
        for kept in deduped:
            if abs((ev.start - kept.start).total_seconds()) > 20 * 60:
                continue
            if ev.end.date() != kept.end.date():
                continue
            if _title_similarity(ev.title, kept.title) >= 0.82:
                # 保留信息更全的一条
                if len(ev.location) + len(ev.note) > len(kept.location) + len(kept.note):
                    kept.location = ev.location or kept.location
                    kept.note = ev.note or kept.note
                    kept.raw = ev.raw or kept.raw
                dup = True
                break
        if dup:
            report["duplicated"] += 1
            continue
        deduped.append(ev)

    # --- 7.3 冲突检测（同一自然日内时间区间重叠） --------------------
    for ev in deduped:
        ev.conflict = False
    ordered = sorted(deduped, key=lambda e: e.start)
    for i, a in enumerate(ordered):
        for b in ordered[i + 1:]:
            if b.start >= a.end:
                break
            # 只比较真正重叠的实例（考虑循环日程的首次实例）
            if a.start.date() != b.start.date() and not (a.is_multiday or b.is_multiday):
                continue
            if a.start < b.end and b.start < a.end:
                a.conflict = True
                b.conflict = True
    report["conflicts"] = sum(1 for e in ordered if e.conflict)

    deduped.sort(key=lambda e: e.start)
    return deduped, report


def find_conflict_pairs(events: list[Event]) -> list[tuple[Event, Event]]:
    """返回所有冲突的日程对，用于在页面上逐条展示。"""
    ordered = sorted(events, key=lambda e: e.start)
    pairs: list[tuple[Event, Event]] = []
    for i, a in enumerate(ordered):
        for b in ordered[i + 1:]:
            # 已按开始时间排序，一旦后一条的开始不早于前一条的结束即可提前终止
            if b.start >= a.end and not a.is_multiday and not b.is_multiday:
                break
            if a.start < b.end and b.start < a.end and a.uid != b.uid:
                pairs.append((a, b))
    return pairs


# ---------------------------------------------------------------------------
# 8. 导出层：ICS / CSV / Markdown
# ---------------------------------------------------------------------------

def _ics_escape(text: str) -> str:
    """
    按 RFC 5545 §3.3.11 转义文本。

    顺序很重要：必须先转义反斜杠，否则会把后续插入的转义符二次转义。
    """
    if text is None:
        return ""
    s = str(text)
    s = s.replace("\\", "\\\\")
    s = s.replace(";", "\\;")
    s = s.replace(",", "\\,")
    s = s.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")
    # 控制字符会破坏 ICS 结构，直接剔除
    s = "".join(ch for ch in s if ch == "\t" or ord(ch) >= 0x20)
    return s


def _ics_fold(line: str, limit: int = 75) -> str:
    """
    按 RFC 5545 §3.1 折行：每行不超过 75 个八位组，续行以单个空格开头。

    必须按 UTF-8 字节而非字符计数，否则中文标题会让 Outlook 解析失败。
    """
    if not line:
        return ""
    out: list[str] = []
    cur = ""
    cur_len = 0
    for ch in line:
        blen = len(ch.encode("utf-8"))
        if cur_len + blen > limit:
            out.append(cur)
            cur = " " + ch        # 续行前导空格计入长度
            cur_len = 1 + blen
        else:
            cur += ch
            cur_len += blen
    out.append(cur)
    return "\r\n".join(out)


def _ics_dt(dt: datetime) -> str:
    """把 naive 本地时间（UTC+8 语义）转为 ICS 的 UTC 时间戳。"""
    aware = dt.replace(tzinfo=CN_TZ) if dt.tzinfo is None else dt.astimezone(CN_TZ)
    return aware.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _ics_duration(minutes: int) -> str:
    if minutes <= 0:
        return "PT0S"
    if minutes % 1440 == 0:
        return f"P{minutes // 1440}D"
    if minutes % 60 == 0:
        return f"PT{minutes // 60}H"
    return f"PT{minutes}M"


def _rrule_with_until(rrule: str, start: datetime, weeks: int) -> str:
    """
    给循环规则补上结束条件，避免无限重复污染用户日历。

    优先级：COUNT（用户显式指定次数） > UNTIL（按学期周数兜底）。
    RFC 5545 规定 COUNT 与 UNTIL 互斥，同时出现时部分客户端会丢弃整条规则，
    所以这里一旦检测到 COUNT 就原样返回，绝不追加 UNTIL。
    """
    if not rrule:
        return ""
    if get_recur_count(rrule):
        return rrule
    if "UNTIL" in rrule.upper():
        return rrule
    until = start + timedelta(weeks=weeks)
    until_dt = until.replace(hour=23, minute=59, second=0, microsecond=0)
    return f"{rrule};UNTIL={_ics_dt(until_dt)}"


def build_ics(events: list[Event], calendar_name: str = "AI 智能日程",
              default_remind: int = 15, recur_weeks: int = DEFAULT_RECUR_WEEKS) -> str:
    """生成标准 .ics 文本（CRLF 换行、含 VALARM 提醒）。"""
    stamp = _ics_dt(datetime.now())
    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//AI Schedule Hub//AI Schedule Importer 1.0//CN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_ics_escape(calendar_name)}",
        f"X-WR-TIMEZONE:{CN_TZ_LABEL}",
        "X-WR-CALDESC:由 AI 智能日程收纳工具生成",
    ]

    for ev in events:
        # 事件 UID 必须全局唯一且稳定，用已有 uid + 域名后缀
        uid = f"{ev.uid}@ai-schedule.local"
        remind = default_remind if ev.remind_minutes < 0 else ev.remind_minutes

        description_parts: list[str] = []
        if ev.note:
            description_parts.append(ev.note)
        if ev.vague and ev.vague_reason:
            description_parts.append(f"[待核对] {ev.vague_reason}")
        if ev.conflict:
            description_parts.append("[注意] 该日程与其他日程存在时间冲突")
        description_parts.append("由 AI 智能日程收纳工具生成")

        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{_ics_dt(ev.start)}",
            # 零时长事件（如 DDL 截止点）在多数日历中不可见，补 1 分钟使其可见
            f"DTEND:{_ics_dt(ev.end if ev.end > ev.start else ev.start + timedelta(minutes=1))}",
            f"SUMMARY:{_ics_escape(ev.title)}",
        ]
        if ev.location:
            lines.append(f"LOCATION:{_ics_escape(ev.location)}")
        lines.append(f"DESCRIPTION:{_ics_escape(chr(10).join(description_parts))}")
        lines.append(f"PRIORITY:{PRIORITY_ICS.get(ev.priority, 5)}")
        if ev.rrule:
            lines.append(f"RRULE:{_rrule_with_until(ev.rrule, ev.start, recur_weeks)}")
        # 分类便于在日历中按来源筛选
        lines.append(f"CATEGORIES:{_ics_escape('AI日程')}")
        lines.append("STATUS:CONFIRMED")
        lines.append("TRANSP:OPAQUE")

        if remind and remind > 0:
            trigger = f"-P{remind // 1440}D" if remind % 1440 == 0 else (
                f"-PT{remind // 60}H" if remind % 60 == 0 else f"-PT{remind}M"
            )
            lines += [
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                f"DESCRIPTION:{_ics_escape(ev.title)}",
                f"TRIGGER:{trigger}",
                "END:VALARM",
            ]
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(_ics_fold(ln) for ln in lines) + "\r\n"


def build_csv(events: list[Event], default_remind: int = 15) -> bytes:
    """生成 CSV（UTF-8 BOM，保证 Excel 直接双击不乱码）。"""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(["标题", "开始时间", "结束时间", "时长(分钟)", "地点",
                     "优先级", "备注", "重复", "重复规则(RRULE)", "提醒", "状态", "来源"])
    for ev in events:
        remind = default_remind if ev.remind_minutes < 0 else ev.remind_minutes
        writer.writerow([
            ev.title,
            ev.start.strftime("%Y-%m-%d %H:%M"),
            ev.end.strftime("%Y-%m-%d %H:%M"),
            ev.duration_minutes,
            ev.location,
            ev.priority,
            ev.note,
            describe_recurrence(ev.rrule) or "不重复",
            ev.rrule or "",
            _remind_label(remind),
            ev.status.replace("🔴 ", "").replace("⚠️ ", "").replace("✅ ", ""),
            ev.source,
        ])
    return buf.getvalue().encode("utf-8-sig")


def build_markdown(events: list[Event], default_remind: int = 15) -> str:
    """生成 Markdown 日程表，按日期分组，便于粘贴到文档或 README。"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    out: list[str] = [
        "# 📅 我的日程表",
        "",
        f"> 生成时间：{now_str}　|　共 **{len(events)}** 项日程　|　由 AI 智能日程收纳工具生成",
        "",
    ]
    if not events:
        out.append("_暂无日程。_")
        return "\n".join(out)

    grouped: dict[date, list[Event]] = {}
    for ev in events:
        grouped.setdefault(ev.start.date(), []).append(ev)

    weekday_cn = "一二三四五六日"
    for day in sorted(grouped):
        items = sorted(grouped[day], key=lambda e: e.start)
        out.append(f"## {day.strftime('%Y-%m-%d')} 星期{weekday_cn[day.weekday()]}")
        out.append("")
        out.append("| 时间 | 标题 | 地点 | 优先级 | 备注 | 状态 |")
        out.append("| --- | --- | --- | --- | --- | --- |")
        for ev in items:
            if ev.is_multiday:
                span = f"{ev.start:%m-%d %H:%M} → {ev.end:%m-%d %H:%M}"
            else:
                span = f"{ev.start:%H:%M} - {ev.end:%H:%M}"
            if ev.rrule:
                span += f" 🔁 {describe_recurrence(ev.rrule)}"
            loc = ev.location.replace("|", "\\|") or "—"
            note = ev.note.replace("|", "\\|").replace("\n", " ") or "—"
            out.append(f"| {span} | {ev.title} | {loc} | {ev.priority} | {note} | {ev.status} |")
        out.append("")
    out.append("---")
    out.append("_本文件由 AI 智能日程收纳工具自动生成，数据仅存在于浏览器会话内存中。_")
    return "\n".join(out)


def events_to_dataframe(events: list[Event]) -> pd.DataFrame:
    """把 Event 列表转成 data_editor 可用的 DataFrame。"""
    columns = ["uid", "标题", "开始", "结束", "地点", "优先级", "备注",
               "循环", "rrule_raw", "提醒", "状态"]
    if not events:
        # 空表也要指定 dtype，否则 DatetimeColumn 渲染会异常
        empty = pd.DataFrame({c: pd.Series(dtype="object") for c in columns})
        empty["开始"] = pd.Series(dtype="datetime64[ns]")
        empty["结束"] = pd.Series(dtype="datetime64[ns]")
        return empty[columns]
    return pd.DataFrame([ev.to_row() for ev in events], columns=columns)


def _df_signature(df: pd.DataFrame) -> str:
    """
    计算 DataFrame 的内容指纹，用于判断用户是否真的编辑过表格。

    刻意不使用 pandas 的哈希工具：datetime 列在 fillna 后 dtype 会退化，
    可能导致指纹不稳定，从而触发无限 rerun。
    """
    cols = ["uid", "标题", "开始", "结束", "地点", "优先级", "备注",
            "循环", "rrule_raw", "提醒"]
    parts: list[str] = []
    try:
        for _, row in df.iterrows():
            parts.append("|".join(str(row.get(c, "")) for c in cols))
    except Exception:  # noqa: BLE001
        return "fallback-" + str(len(df))
    return hashlib.md5("\x1f".join(parts).encode("utf-8")).hexdigest()


def dataframe_to_events(df: pd.DataFrame, previous: list[Event]) -> tuple[list[Event], int, int]:
    """
    把 data_editor 编辑结果同步回 Event 列表。

    返回 (新列表, 新增数, 删除数)。依靠隐藏的 uid 列识别行身份。
    """
    by_uid = {ev.uid: ev for ev in previous}
    result: list[Event] = []
    added = 0
    seen: set[str] = set()

    for _, row in df.iterrows():
        uid = str(row.get("uid") or "").strip()
        if uid and uid in by_uid:
            ev = Event.from_row(row.to_dict(), by_uid[uid])
            ev.uid = uid
            seen.add(uid)
            result.append(ev)
        else:
            ev = Event.from_row(row.to_dict())
            ev.source = "manual"
            if not ev.title.strip():
                continue
            added += 1
            result.append(ev)

    deleted = sum(1 for uid in by_uid if uid not in seen)
    return result, added, deleted


# ---------------------------------------------------------------------------
# 9. 演示数据（相对今天生成，保证现场演示永不过期）
# ---------------------------------------------------------------------------

def build_sample_text() -> str:
    """构造一段刻意"脏乱"的示例文本：课表 + DDL + 会议 + 聊天记录混排。"""
    today = datetime.now()
    monday = today - timedelta(days=today.weekday())
    d = lambda n: (monday + timedelta(days=n)).strftime("%m月%d日")  # noqa: E731
    return f"""【课程表 截图里的内容】
{d(0)} 周一 第1-2节 高等数学 教学楼A302 张老师
{d(0)} 第3-4节 大学英语 外语楼B105
{d(1)} 周二 第5-6节 数据结构与算法 计算机楼C401
{d(2)} 周三 第7-8节 人工智能导论 实验楼D203
每周四 第9-10节 创新创业实践 报告厅
每周五下午3点到5点 项目组例会 腾讯会议

【微信群聊记录】
班长：下周三下午2点 在图书馆三楼研讨间开班会，务必到场，别迟到
学委：提醒一下，{d(3)} 晚上23:59 前提交《机器学习》课程设计报告，DDL 不能拖
室友：周六上午10点 体育馆 羽毛球，来不来都行

【备忘录】
{d(5)} 09:00-17:30 大学生 AI 创新大赛决赛答辩 创新创业中心 2 号厅 要带电脑和 U 盘
11月30日 晚上8点半 和导师讨论论文选题 线上
明天中午12点 食堂二楼 和组员碰一下分工
本周五 下午4点 提交竞赛报名表给辅导员
下周一 上午9点 体检 校医院 空腹
12月5日 全天 参加志愿者活动（跨校区，早出晚归）
"""


# ---------------------------------------------------------------------------
# 10. 页面样式（静态 CSS，绝不拼接任何用户输入，避免 XSS）
# ---------------------------------------------------------------------------

_CSS = """
<style>
/* ---------- 全局 ---------- */
.block-container { padding-top: 1.4rem; padding-bottom: 1rem; max-width: 1180px; }
/* 只隐藏主菜单与页脚；保留 header 以免侧边栏折叠按钮失效 */
#MainMenu { visibility: hidden; }
footer { visibility: hidden; }
header[data-testid="stHeader"] { background: transparent; }
html, body, [class*="css"] { font-family: -apple-system, "Segoe UI", "PingFang SC",
    "Microsoft YaHei", sans-serif; }

/* ---------- 顶部 hero ---------- */
.hero {
    background: linear-gradient(120deg, #4F46E5 0%, #7C3AED 48%, #DB2777 100%);
    border-radius: 18px; padding: 26px 30px 24px 30px; margin-bottom: 18px;
    box-shadow: 0 10px 30px rgba(79, 70, 229, .22); position: relative; overflow: hidden;
}
.hero::after {
    content: ""; position: absolute; right: -60px; top: -60px; width: 220px; height: 220px;
    background: rgba(255,255,255,.12); border-radius: 50%;
}
.hero-badge {
    display: inline-block; font-size: 11px; letter-spacing: 2px; font-weight: 700;
    color: #EDE9FE; background: rgba(255,255,255,.16); border: 1px solid rgba(255,255,255,.3);
    border-radius: 999px; padding: 4px 12px; margin-bottom: 12px;
}
.hero h1 { color: #FFFFFF; font-size: 30px; font-weight: 800; margin: 0 0 8px 0;
    letter-spacing: -.4px; }
.hero p { color: rgba(255,255,255,.9); font-size: 14px; margin: 0; line-height: 1.6; }

/* ---------- 指标卡 ---------- */
.metric-row { display: flex; gap: 12px; margin: 6px 0 16px 0; flex-wrap: wrap; }
.metric-card {
    flex: 1 1 150px; background: #FFFFFF; border: 1px solid #E9E7F5; border-radius: 14px;
    padding: 14px 16px; box-shadow: 0 2px 8px rgba(31, 24, 71, .05);
}
.metric-card .label { font-size: 12px; color: #6B7280; font-weight: 600; margin-bottom: 4px; }
.metric-card .value { font-size: 24px; font-weight: 800; color: #1F2937; line-height: 1.2; }
.metric-card .value.warn { color: #D97706; }
.metric-card .value.danger { color: #DC2626; }
.metric-card .value.ok { color: #059669; }

/* ---------- 小标签 ---------- */
.chip {
    display: inline-block; font-size: 12px; font-weight: 600; border-radius: 999px;
    padding: 3px 10px; margin-right: 6px; margin-bottom: 4px;
}
.chip-ok { background: #ECFDF5; color: #047857; border: 1px solid #A7F3D0; }
.chip-warn { background: #FFFBEB; color: #B45309; border: 1px solid #FDE68A; }
.chip-bad { background: #FEF2F2; color: #B91C1C; border: 1px solid #FECACA; }
.chip-info { background: #EEF2FF; color: #4338CA; border: 1px solid #C7D2FE; }

/* ---------- 按钮 ---------- */
.stButton > button, .stDownloadButton > button {
    border-radius: 10px; font-weight: 600; border: 1px solid #DDD6FE; transition: all .18s ease;
}
.stButton > button[kind="primary"] {
    background: linear-gradient(120deg, #4F46E5, #7C3AED); border: none;
    box-shadow: 0 4px 14px rgba(79, 70, 229, .25);
}
.stButton > button[kind="primary"]:hover { transform: translateY(-1px);
    box-shadow: 0 6px 18px rgba(79, 70, 229, .32); }
.stDownloadButton > button:hover { border-color: #A78BFA; color: #6D28D9; }

/* ---------- Tab ---------- */
.stTabs [data-baseweb="tab-list"] { gap: 6px; border-bottom: 1px solid #EEF0F4; }
.stTabs [data-baseweb="tab"] {
    height: 42px; border-radius: 10px 10px 0 0; padding: 0 18px;
    font-weight: 600; font-size: 14px; color: #6B7280;
}
.stTabs [aria-selected="true"] { color: #4F46E5; background: #F5F3FF; }

/* ---------- 侧边栏 ---------- */
[data-testid="stSidebar"] { background: #FAFAFE; border-right: 1px solid #EFECFB; }
[data-testid="stSidebar"] .stMarkdown h3 { font-size: 15px; color: #4B5563; }

/* ---------- 区块标题 ---------- */
.section-title {
    font-size: 16px; font-weight: 700; color: #1F2937; margin: 14px 0 8px 0;
    padding-left: 10px; border-left: 4px solid #7C3AED; line-height: 1.3;
}

/* ---------- 提示条 ---------- */
.notice {
    border-radius: 12px; padding: 12px 16px; font-size: 13px; line-height: 1.65;
    margin: 8px 0 14px 0;
}
.notice-info { background: #F5F3FF; border-left: 4px solid #7C3AED; color: #4C1D95; }
.notice-warn { background: #FFFBEB; border-left: 4px solid #F59E0B; color: #92400E; }
.notice-ok { background: #ECFDF5; border-left: 4px solid #10B981; color: #065F46; }
.notice-bad { background: #FEF2F2; border-left: 4px solid #EF4444; color: #991B1B; }

/* ---------- 页脚免责声明 ---------- */
.disclaimer {
    margin-top: 26px; padding: 18px 20px; border-radius: 14px;
    background: #F9FAFB; border: 1px dashed #D1D5DB; color: #6B7280; font-size: 12px;
    line-height: 1.8;
}
.disclaimer strong { color: #374151; }
.disclaimer .title { font-size: 13px; font-weight: 700; color: #374151; margin-bottom: 6px; }
</style>
"""


def _inject_css() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)


def _html(html: str) -> None:
    """
    渲染**纯静态** HTML 片段。

    安全约定：本函数只接受开发者在代码中写死的字符串，
    任何来自用户的内容都必须经过 html.escape 后才能传入。
    """
    st.markdown(html, unsafe_allow_html=True)


def _chip(text: str, kind: str = "info") -> str:
    return f'<span class="chip chip-{kind}">{text}</span>'


# ---------------------------------------------------------------------------
# 11. 会话状态
# ---------------------------------------------------------------------------

def init_state() -> None:
    """初始化 session_state。所有数据只存在于内存，刷新页面即清空。"""
    defaults: dict[str, Any] = {
        "events": [],                 # list[Event]
        "last_report": None,          # 最近一次解析的报告
        "last_error": "",             # 最近一次的错误提示
        "last_ocr": [],               # 最近一次 OCR 的过程信息
        "editor_key": 0,              # 用于强制刷新 data_editor
        "editor_hash": "",            # 编辑内容指纹，避免重复同步
        "confirm_clear": False,       # 「清空全部日程」的二次确认状态
        "use_ai": True,               # 是否优先调用大模型
        "default_remind": 15,         # 全局提醒（分钟）
        "recur_weeks": DEFAULT_RECUR_WEEKS,
        "calendar_name": "我的 AI 日程",
        "raw_text_input": "",
        "total_parsed": 0,
        "pending_toast": "",          # 跨 rerun 传递的操作提示
        # --- 重复设置面板的状态 ---
        "recur_target_uid": "",       # 当前正在设置的日程
        "recur_enabled": False,       # 「是否重复」开关
        "recur_freq": RECUR_FREQ_WEEKLY,
        "recur_count": RECUR_COUNT_DEFAULT,
        "recur_advanced_open": False,  # 高级：手写 RRULE 展开状态
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _push_events(new_events: list[Event], source_label: str) -> dict[str, Any]:
    """
    把新解析出的日程追加进会话（不覆盖已有内容），并做归一化处理。

    返回本次操作报告。
    """
    before_count = len(st.session_state.events)
    combined = list(st.session_state.events) + new_events
    normalized, report = normalize_events(combined)

    # 计算真正新增的条数（按 uid 差集）
    old_uids = {ev.uid for ev in st.session_state.events}
    added = sum(1 for ev in normalized if ev.uid not in old_uids)

    st.session_state.events = normalized
    st.session_state.total_parsed += len(new_events)
    report.update({
        "source": source_label,
        "raw_count": len(new_events),
        "added": added,
        "before_count": before_count,
        "after_count": len(normalized),
    })
    # 数据变了，强制刷新编辑器
    st.session_state.editor_key += 1
    st.session_state.editor_hash = ""
    return report


def _recompute_conflicts() -> None:
    """手动编辑后重新计算冲突标记（不做过期过滤，避免误删用户刚加的行）。"""
    events = st.session_state.events
    for ev in events:
        ev.conflict = False
    ordered = sorted(events, key=lambda e: e.start)
    for i, a in enumerate(ordered):
        for b in ordered[i + 1:]:
            if b.start >= a.end:
                break
            if a.start < b.end and b.start < a.end:
                a.conflict = True
                b.conflict = True


# ---------------------------------------------------------------------------
# 12. 解析编排
# ---------------------------------------------------------------------------

def _read_config_hint() -> str:
    return (
        "配置大模型（任选其一，均在**环境变量**中设置，切勿写进代码）：\n\n"
        "```bash\n"
        "# DeepSeek\n"
        "set AI_API_KEY=sk-xxxx\n"
        "set AI_BASE_URL=https://api.deepseek.com/v1\n"
        "set AI_MODEL=deepseek-chat\n"
        "```\n"
        "也兼容通义千问、Kimi、智谱、OpenAI 等任意 OpenAI 兼容接口。"
    )


def run_parse(text: str, image_files: list[Any], use_ai: bool) -> None:
    """
    统一的解析入口：文本清洗 → OCR → 规则解析 → AI 解析 → 融合 → 入库。

    所有异常在此收敛，保证页面不会因任何输入而崩溃。
    """
    st.session_state.last_error = ""
    st.session_state.last_ocr = []
    ocr_texts: list[str] = []
    warnings: list[str] = []

    # --- 1. 文本输入校验 ------------------------------------------------
    text = (text or "").strip()
    if len(text) > MAX_TEXT_CHARS:
        st.session_state.last_error = (
            f"文本过长（{len(text)} 字），单次最多处理 {MAX_TEXT_CHARS} 字。"
            "建议分批粘贴，已录入的日程不会被覆盖。"
        )
        return

    # --- 2. 图片 OCR ----------------------------------------------------
    if image_files:
        if len(image_files) > 3:
            warnings.append("单次最多识别 3 张图片，超出的已忽略。")
        for idx, f in enumerate(image_files[:3], start=1):
            try:
                data = f.getvalue()
            except Exception:  # noqa: BLE001
                warnings.append(f"第 {idx} 张图片读取失败，已跳过。")
                continue
            res = ocr_image(data)
            st.session_state.last_ocr.append({
                "name": getattr(f, "name", f"图片{idx}"),
                "ok": res["ok"],
                "engine": res["engine"],
                "text": res["text"],
                "error": res["error"],
                "warnings": res["warnings"],
                "confidence": res["confidence"],
            })
            if res["ok"]:
                ocr_texts.append(res["text"])
                warnings.extend(res["warnings"])
            else:
                warnings.append(f"第 {idx} 张图片：{res['error']}")

    combined = "\n".join([t for t in [text] + ocr_texts if t.strip()]).strip()

    # --- 3. 空输入兜底 --------------------------------------------------
    if not combined:
        st.session_state.last_error = (
            "没有可解析的内容。请粘贴日程文字，或上传清晰的日程截图（PNG/JPG）。"
            if not image_files else
            "图片中未识别到有效文字，且没有粘贴文本。请更换更清晰的截图，或直接粘贴文字。"
        )
        st.session_state.last_report = None
        return

    # --- 4. 解析 --------------------------------------------------------
    rule_events: list[Event] = []
    ai_events: list[Event] = []
    ai_error = ""
    t0 = time.time()

    try:
        rule_events = parse_with_rules(combined)
    except Exception:  # noqa: BLE001
        log(f"规则解析异常: {traceback.format_exc()}")
        rule_events = []

    ai_config = get_ai_config()
    if use_ai and ai_config["available"]:
        try:
            with st.spinner("AI 正在理解你的日程…"):
                ai_events = parse_with_ai(combined, ai_config)
        except AIError as exc:
            ai_error = str(exc)
        except Exception as exc:  # noqa: BLE001
            log(f"AI 解析异常: {traceback.format_exc()}")
            ai_error = f"AI 解析出现意外错误（{type(exc).__name__}），已改用本地规则引擎。"

    elapsed = time.time() - t0

    # --- 5. 融合 --------------------------------------------------------
    if ai_events and rule_events:
        final_events = merge_ai_and_rules(ai_events, rule_events)
        engine_label = "AI + 规则双引擎"
    elif ai_events:
        final_events = ai_events
        engine_label = "AI 解析"
    elif rule_events:
        final_events = rule_events
        engine_label = "本地规则引擎" + ("（AI 失败后降级）" if ai_error else "")
    else:
        st.session_state.last_error = (
            "未能从内容中识别出任何日程。请检查文本中是否包含时间信息，"
            "例如「明天下午3点 项目讨论」。也可以点击「加载示例日程」查看可识别的格式。"
        )
        st.session_state.last_report = None
        return

    # --- 6. 入库 --------------------------------------------------------
    report = _push_events(final_events, engine_label)
    report["engine"] = engine_label
    report["elapsed"] = round(elapsed, 2)
    report["ai_error"] = ai_error
    report["warnings"] = warnings
    report["ocr_count"] = len(ocr_texts)
    report["vague"] = sum(1 for ev in st.session_state.events if ev.vague)
    st.session_state.last_report = report

    if report["added"] == 0 and report["raw_count"] > 0:
        st.session_state.last_report["all_duplicate"] = True


# ---------------------------------------------------------------------------
# 13. 侧边栏
# ---------------------------------------------------------------------------

def render_sidebar() -> None:
    with st.sidebar:
        _html(
            '<div style="padding:2px 0 10px 0;">'
            '<div style="font-size:19px;font-weight:800;color:#4C1D95;letter-spacing:-.3px;">'
            '🗂️ 日程收纳台</div>'
            '<div style="font-size:12px;color:#9CA3AF;margin-top:2px;">'
            f'v{APP_VERSION} · 内存计算 · 不存数据</div></div>'
        )

        # --- AI 状态 ---
        st.markdown("### 🤖 AI 解析")
        ai_config = get_ai_config()
        if ai_config["available"]:
            _html(
                _chip("已连接", "ok")
                + f'<span style="font-size:12px;color:#6B7280;">'
                  f'{_escape(ai_config["model"])}</span>'
            )
            st.checkbox("优先使用 AI 解析", key="use_ai",
                        help="关闭后仅使用本地规则引擎，完全离线。")
        else:
            _html(_chip("未配置 API Key", "warn"))
            st.caption("未检测到大模型密钥，将使用**本地规则引擎**解析（离线可用）。")
            with st.expander("如何配置"):
                st.markdown(_read_config_hint())

        # --- OCR 状态 ---
        st.markdown("### 🔍 OCR 识别")
        engine_name, _ = get_ocr_engine()
        if engine_name == "rapidocr":
            _html(_chip("本地 OCR 已就绪", "ok"))
            st.caption("图片在本机内存中识别，不会上传到任何服务器。")
        elif engine_name == "pytesseract":
            _html(_chip("Tesseract 已就绪", "ok"))
        else:
            _html(_chip("OCR 未安装", "warn"))
            st.caption("执行 `pip install -r requirements.txt` 即可启用图片识别"
                       "（Python 3.13+ 需 `pip install rapidocr onnxruntime`）。")

        st.markdown("---")

        # --- 全局设置 ---
        st.markdown("### ⚙️ 全局设置")
        remind_labels = list(REMIND_OPTIONS.keys())
        current_label = _remind_label(st.session_state.default_remind)
        idx = remind_labels.index(current_label) if current_label in remind_labels else 1
        chosen = st.selectbox("默认提醒", remind_labels, index=idx,
                              help="导出日历时写入 VALARM；单条日程可在表格中单独覆盖。")
        st.session_state.default_remind = REMIND_OPTIONS[chosen]

        st.session_state.recur_weeks = st.slider(
            "循环日程兜底周数", 4, 52, st.session_state.recur_weeks, 1,
            help="仅对「没有单独设置次数」的循环日程生效：从开始日往后推这么多周。"
                 "在日程表页用重复设置面板指定了次数的日程，以该次数为准。"
        )

        st.markdown("---")
        # --- 数据 ---
        st.markdown("### 📊 当前数据")
        total = len(st.session_state.events)
        conflict = sum(1 for ev in st.session_state.events if ev.conflict)
        vague = sum(1 for ev in st.session_state.events if ev.vague)
        st.markdown(
            f'<div style="font-size:13px;line-height:2;color:#4B5563;">'
            f'日程总数：<b>{total}</b><br>'
            f'时间冲突：<b style="color:#DC2626;">{conflict}</b><br>'
            f'待人工核对：<b style="color:#D97706;">{vague}</b>'
            f'</div>',
            unsafe_allow_html=True,
        )
        if total and st.button("🗑️ 清空全部日程", width="stretch"):
            st.session_state.events = []
            st.session_state.last_report = None
            st.session_state.last_error = ""
            st.session_state.editor_key += 1
            st.session_state.editor_hash = ""
            st.rerun()

        st.markdown("---")
        st.caption("🔒 所有日程与图片仅在本机内存中处理，关闭页面即销毁，不写入任何数据库或文件。")


# ---------------------------------------------------------------------------
# 14. Tab 1：录入与解析
# ---------------------------------------------------------------------------

def _cb_load_sample() -> None:
    """「加载示例」按钮回调：在 widget 实例化前写入文本框内容。"""
    st.session_state.raw_text_input = build_sample_text()
    st.session_state.last_error = ""
    st.session_state.last_report = None


def _cb_clear_input() -> None:
    """「清空输入」按钮回调：只清输入区，不动已录入的日程。"""
    st.session_state.raw_text_input = ""
    st.session_state.last_report = None
    st.session_state.last_error = ""
    st.session_state.last_ocr = []


def render_ingest_tab() -> None:
    _html('<div class="section-title">把碎片日程丢进来</div>')
    _html(
        '<div class="notice notice-info">'
        '支持<b>纯文本粘贴</b>与<b>截图上传</b>，程序会自动识别标题、时间、地点、优先级与循环规则。'
        '已录入的日程<b>不会被覆盖</b>，可反复追加。'
        '</div>'
    )

    col_left, col_right = st.columns([3, 2], gap="large")

    with col_left:
        # 不使用返回值，统一以 session_state.raw_text_input 为准，
        # 这样「加载示例」回调写入的内容才能被正确解析
        st.text_area(
            "① 粘贴日程文字",
            key="raw_text_input",
            height=240,
            placeholder=(
                "例如：\n"
                "明天下午3点 项目组例会 腾讯会议\n"
                "每周二第3-4节 数据结构 计算机楼C401\n"
                "11月20日 23:59 前提交课程论文 DDL"
            ),
            help=f"单次最多 {MAX_TEXT_CHARS} 字。支持聊天记录、备忘录、课表等自由格式。",
        )

    with col_right:
        st.markdown("**② 上传日程截图**")
        images = st.file_uploader(
            "支持 PNG / JPG / WEBP / BMP",
            type=list(ALLOWED_IMAGE_TYPES),
            accept_multiple_files=True,
            label_visibility="collapsed",
            help="单张不超过 10MB，最多 3 张。图片仅在本机内存中做 OCR。",
        )
        if images:
            st.caption(f"已选择 {len(images)} 张图片（最多识别前 3 张）")
            for f in images[:3]:
                # 用固定像素宽度而非 use_column_width：后者在新版 Streamlit 已弃用
                try:
                    st.image(f, width=280)
                except Exception:  # noqa: BLE001
                    st.caption("（该图片无法预览，但仍会尝试识别）")

    st.markdown("")
    btn_cols = st.columns([1, 1, 1, 2])

    # 「加载示例」「清空输入」必须通过 on_click 回调修改 session_state：
    # 回调在 rerun 开始时、text_area 实例化之前执行，否则会触发
    # StreamlitAPIException: cannot be modified after the widget is instantiated。
    with btn_cols[0]:
        do_parse = st.button("✨ 开始解析", type="primary", width="stretch")
    with btn_cols[1]:
        st.button("📚 加载示例", width="stretch",
                  on_click=_cb_load_sample,
                  help="一键填入示例日程，适合比赛现场演示。")
    with btn_cols[2]:
        st.button("🧹 清空输入", width="stretch", on_click=_cb_clear_input)

    # --- 解析 ---
    if do_parse:
        # 兜底：run_parse 内部虽已逐层捕获，但任何遗漏的异常都不应让页面白屏
        try:
            run_parse(st.session_state.raw_text_input, images or [], st.session_state.use_ai)
        except Exception:  # noqa: BLE001
            log(f"解析流程未捕获异常: {traceback.format_exc()}")
            st.session_state.last_error = (
                "解析过程中出现意外错误，已保留此前录入的日程。"
                "请检查输入内容后重试，或改用「粘贴文字」方式。"
            )
            st.session_state.last_report = None

    # --- 错误提示 ---
    if st.session_state.last_error:
        _html(f'<div class="notice notice-bad">⚠️ {_escape(st.session_state.last_error)}</div>')

    # --- 解析报告 ---
    report = st.session_state.last_report
    if report:
        _render_parse_report(report)
        _render_ocr_detail()

    # --- 最近结果速览 ---
    if st.session_state.events:
        _html('<div class="section-title">最近录入的日程</div>')
        recent = sorted(st.session_state.events, key=lambda e: e.start)[-6:]
        rows = []
        for ev in recent:
            rows.append({
                "时间": f"{ev.start:%m-%d %H:%M} - {ev.end:%m-%d %H:%M}",
                "标题": ev.title,
                "地点": ev.location or "—",
                "状态": ev.status,
            })
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


def _escape(text: str) -> str:
    """HTML 转义，用于把用户可控文本安全地放进 HTML 片段。"""
    import html as _html_mod
    return _html_mod.escape(str(text))


def _render_parse_report(report: dict[str, Any]) -> None:
    """渲染一次解析的统计报告。"""
    engine = report.get("engine", "")
    elapsed = report.get("elapsed", 0)
    _html(
        f'<div class="notice notice-ok">'
        f'✅ 解析完成（引擎：{_escape(engine)}，耗时 {elapsed}s）'
        f'　新增 <b>{report.get("added", 0)}</b> 条，'
        f'当前共 <b>{report.get("after_count", 0)}</b> 条。'
        f'</div>'
    )

    chips = []
    if report.get("duplicated"):
        chips.append(_chip(f"去重 {report['duplicated']} 条", "info"))
    if report.get("expired"):
        chips.append(_chip(f"过滤过期 {report['expired']} 条", "info"))
    if report.get("conflicts"):
        chips.append(_chip(f"时间冲突 {report['conflicts']} 条", "bad"))
    if report.get("vague"):
        chips.append(_chip(f"待核对 {report['vague']} 条", "warn"))
    if report.get("ocr_count"):
        chips.append(_chip(f"OCR 识别 {report['ocr_count']} 张", "ok"))
    if report.get("all_duplicate"):
        chips.append(_chip("全部为重复日程，未新增", "warn"))
    if chips:
        _html('<div style="margin-bottom:10px;">' + "".join(chips) + "</div>")

    if report.get("ai_error"):
        _html(
            f'<div class="notice notice-warn">🤖 {_escape(report["ai_error"])}</div>'
        )
    for warn in report.get("warnings", [])[:6]:
        st.caption(f"· {warn}")


def _render_ocr_detail() -> None:
    ocr_items = st.session_state.last_ocr
    if not ocr_items:
        return
    with st.expander("🔍 查看 OCR 识别过程", expanded=False):
        for item in ocr_items:
            status = "成功" if item["ok"] else "失败"
            conf = f"，置信度 {item['confidence']:.0%}" if item.get("confidence") else ""
            st.markdown(f"**{item['name']}** — {status}（引擎：{item['engine']}{conf}）")
            if item["ok"]:
                preview = item["text"][:600] + ("…" if len(item["text"]) > 600 else "")
                st.code(preview, language="text")
            else:
                st.caption(f"失败原因：{item['error']}")


# ---------------------------------------------------------------------------
# 15. Tab 2：可视化日程表
# ---------------------------------------------------------------------------

def _style_events(df: pd.DataFrame, events: list[Event]) -> Any:
    """
    用 pandas Styler 给冲突行标红、模糊行标黄。

    data_editor 支持 Styler 的背景色；若当前 Streamlit 版本不支持，
    调用方会捕获异常并降级为无样式表格。
    """
    status_map = {ev.uid: ev.status for ev in events}

    def _row_css(row: pd.Series) -> list[str]:
        status = status_map.get(str(row.get("uid")), "")
        if status.startswith("🔴"):
            return ["background-color: #FEE2E2; color: #7F1D1D"] * len(row)
        if status.startswith("⚠️"):
            return ["background-color: #FEF9E7; color: #78350F"] * len(row)
        return [""] * len(row)

    return df.style.apply(_row_css, axis=1)


# ---------------------------------------------------------------------------
# 15. 重复设置面板（开关 + 频率 + 次数步进器）
# ---------------------------------------------------------------------------

def _recur_options() -> dict[str, str]:
    """重复设置下拉的选项：可读标签 → 日程 uid。"""
    return {
        f"{ev.start:%m-%d %H:%M} | {ev.title}": ev.uid
        for ev in sorted(st.session_state.events, key=lambda e: e.start)
    }


def _fill_recur_controls(ev: "Event | None") -> None:
    """把某条日程的重复状态回填到面板控件（仅在控件渲染前调用才合法）。"""
    if ev is None:
        return
    st.session_state.recur_enabled = ev.is_recurring
    st.session_state.recur_freq = recur_freq_key(ev.rrule) or RECUR_FREQ_WEEKLY
    st.session_state.recur_count = get_recur_count(ev.rrule) or RECUR_COUNT_DEFAULT


def _on_recur_pick_change() -> None:
    """下拉切换日程时，同步目标 uid 并回填该日程的重复设置。"""
    mapping = _recur_options()
    uid = mapping.get(str(st.session_state.get("recur_pick_label") or ""), "")
    if not uid:
        return
    st.session_state.recur_target_uid = uid
    _fill_recur_controls(
        next((e for e in st.session_state.events if e.uid == uid), None)
    )


def _bump_recur_count(delta: int) -> None:
    """步进器回调：在当前值基础上加/减，并夹在合法区间内。"""
    cur = int(st.session_state.get("recur_count") or RECUR_COUNT_DEFAULT)
    st.session_state.recur_count = max(RECUR_COUNT_MIN, min(RECUR_COUNT_MAX, cur + delta))


def _nth_occurrence(rrule: str, start: datetime, n: int) -> datetime | None:
    """估算第 n 次发生的日期，仅用于面板上的「预计最后一次」提示。"""
    if not rrule or n <= 0:
        return None
    freq = _rrule_field(rrule, "FREQ")
    interval = _rrule_field(rrule, "INTERVAL")
    step = int(interval) if interval.isdigit() else 1
    if freq == "DAILY":
        return start + timedelta(days=step * (n - 1))
    if freq == "WEEKLY":
        per_week = max(1, len([d for d in _rrule_field(rrule, "BYDAY").split(",") if d]))
        return start + timedelta(weeks=(n - 1) // per_week * step)
    if freq == "MONTHLY":
        return start + timedelta(days=30 * step * (n - 1))
    return None


def _apply_recur_setting(uid: str, enabled: bool, freq_key: str, count: int) -> None:
    """
    把面板上的设置写回目标日程。

    注意：本函数只能作为按钮的 on_click 回调执行。Streamlit 不允许在 widget
    渲染之后直接改它自己绑定的 session_state key，而回调在 widget 实例化之前运行，
    因此在这里同步开关/频率/次数的状态是安全的。
    """
    target = next((e for e in st.session_state.events if e.uid == uid), None)
    if target is None:
        st.session_state.pending_toast = "目标日程已不存在，请重新选择"
        return

    if enabled:
        rrule = build_recur_rrule(freq_key, target.start, target.rrule)
        target.rrule = set_recur_count(
            rrule, max(RECUR_COUNT_MIN, min(RECUR_COUNT_MAX, count))
        )
        st.session_state.pending_toast = f"已设为「{describe_recurrence(target.rrule)}」"
        st.session_state.recur_freq = freq_key
        st.session_state.recur_count = max(RECUR_COUNT_MIN, min(RECUR_COUNT_MAX, count))
    else:
        target.rrule = ""
        st.session_state.pending_toast = "已取消重复，改为单次日程"

    st.session_state.recur_enabled = bool(enabled)
    st.session_state.recur_target_uid = uid

    _recompute_conflicts()
    # 表格里的「重复」列内容变了，必须重建编辑器，否则旧缓存会盖掉本次修改
    st.session_state.editor_key += 1
    st.session_state.editor_hash = ""


def _render_recur_panel() -> None:
    """
    重复设置面板：是否重复 → 重复频率 → 重复次数（➖ 数字 ➕）→ 应用。

    次数写入标准 RRULE 的 COUNT 属性，导出的 .ics 会在第 N 次之后自动停止，
    不会被日历客户端当作「永久重复」。
    """
    if not st.session_state.events:
        return

    options = _recur_options()
    labels, uids = list(options.keys()), list(options.values())

    # 目标失效（被删除 / 时间改动）时回退到第一条
    if st.session_state.recur_target_uid not in uids:
        st.session_state.recur_target_uid = uids[0]
        _fill_recur_controls(
            next((e for e in st.session_state.events
                  if e.uid == st.session_state.recur_target_uid), None)
        )

    # 旧标签已不在选项里时清掉，否则 selectbox 会抛「值不在选项中」
    if st.session_state.get("recur_pick_label") not in options:
        st.session_state.pop("recur_pick_label", None)

    picked_label = st.selectbox(
        "选择要设置重复的日程",
        labels,
        index=uids.index(st.session_state.recur_target_uid),
        key="recur_pick_label",
        on_change=_on_recur_pick_change,
        help="先选一条日程，再决定它是否重复、多久重复一次、总共重复几次。",
    )
    target_ev = next((e for e in st.session_state.events
                      if e.uid == st.session_state.recur_target_uid), None)

    enabled = st.toggle(
        "是否重复",
        key="recur_enabled",
        help="关闭表示只发生一次；开启后可设置重复频率与次数。",
    )

    if enabled:
        freq = st.radio(
            "重复频率",
            RECUR_FREQ_OPTIONS,
            key="recur_freq",
            horizontal=True,
        )
        count = int(st.session_state.recur_count)

        # ---- 次数步进器：➖ ｜ 中间数字 ｜ ➕ ----
        st.markdown(
            '<div style="font-size:13px;font-weight:600;color:#374151;'
            'margin:8px 0 2px;">重复次数</div>',
            unsafe_allow_html=True,
        )
        with st.container(key="recur_stepper"):
            c_minus, c_num, c_plus = st.columns([1, 1.4, 1], gap="small")
            with c_minus:
                # 用全角单色字符而不是 ➖/➕：emoji 会被系统渲染成彩色（左右颜色不一致），
                # 半角 −/+ 在默认字号下又偏细小，全角字符视觉最饱满。
                st.button(
                    "－", key="recur_minus", on_click=_bump_recur_count, args=(-1,),
                    width="stretch", disabled=count <= RECUR_COUNT_MIN,
                    help=f"减少一次（最少 {RECUR_COUNT_MIN} 次）",
                )
            with c_num:
                _html(
                    # 与左右按钮等高（40px）并用 flex 居中，避免数字上下错位
                    '<div style="height:40px;display:flex;align-items:center;'
                    'justify-content:center;background:#F5F3FF;border:1px solid #DDD6FE;'
                    'border-radius:10px;">'
                    f'<span style="font-size:24px;font-weight:800;color:#4F46E5;">{count}</span>'
                    '<span style="font-size:13px;font-weight:600;color:#6B7280;'
                    'margin-left:4px;">次</span></div>'
                )
            with c_plus:
                st.button(
                    "＋", key="recur_plus", on_click=_bump_recur_count, args=(1,),
                    width="stretch", disabled=count >= RECUR_COUNT_MAX,
                    help=f"增加一次（最多 {RECUR_COUNT_MAX} 次）",
                )

        # 实时预览：点「应用」前就能看到最终规则与结束日期
        start_for_preview = target_ev.start if target_ev else datetime.now()
        preview = set_recur_count(
            build_recur_rrule(freq, start_for_preview,
                              target_ev.rrule if target_ev else ""),
            count,
        )
        last_dt = _nth_occurrence(preview, start_for_preview, count)
        st.caption(
            f"将生成：**{describe_recurrence(preview)}**"
            + (f"　·　预计最后一次 {last_dt:%Y-%m-%d}" if last_dt else "")
        )
    else:
        freq = st.session_state.recur_freq
        count = int(st.session_state.recur_count)
        st.caption("当前为单次日程，不会在日历中重复。")

    # 高级入口：保留手写 RRULE 的能力
    with st.expander("⚙️ 高级：直接编辑重复规则（RRULE）"):
        st.caption("例如 `FREQ=WEEKLY;BYDAY=MO,WE;COUNT=10`；留空表示不重复。"
                   "修改后需点下方「应用重复设置」才会生效。")
        st.code(target_ev.rrule if target_ev and target_ev.rrule else "（当前不重复）",
                language="text")

    st.markdown("")
    b1, b2 = st.columns(2)
    picked_uid = options.get(picked_label, st.session_state.recur_target_uid)
    with b1:
        st.button(
            "✅ 应用重复设置", key="apply_recur", type="primary", width="stretch",
            on_click=_apply_recur_setting, args=(picked_uid, enabled, freq, count),
        )
    with b2:
        st.button(
            "🚫 取消这条的重复", key="remove_recur", width="stretch",
            help="改回单次日程，不会删除这条日程本身。",
            on_click=_apply_recur_setting, args=(picked_uid, False, freq, count),
        )


def render_schedule_tab() -> None:
    events: list[Event] = st.session_state.events

    # 上一步操作留下的提示（st.toast 无法跨 rerun 存活，需中转）
    if st.session_state.get("pending_toast"):
        st.toast(st.session_state.pending_toast)
        st.session_state.pending_toast = ""

    if not events:
        _html(
            '<div class="notice notice-info" style="margin-top:16px;">'
            '📭 还没有日程。<br>'
            '请到「录入解析」页粘贴文字或上传截图，或点击「加载示例」快速体验。'
            '</div>'
        )
        if st.button("📚 一键加载示例日程", type="primary"):
            sample = parse_with_rules(build_sample_text())
            _push_events(sample, "示例数据")
            st.rerun()
        return

    # --- 统计卡 ---
    total = len(events)
    conflict = sum(1 for e in events if e.conflict)
    vague = sum(1 for e in events if e.vague)
    recurring = sum(1 for e in events if e.is_recurring)
    _html(
        '<div class="metric-row">'
        f'<div class="metric-card"><div class="label">日程总数</div>'
        f'<div class="value">{total}</div></div>'
        f'<div class="metric-card"><div class="label">时间冲突</div>'
        f'<div class="value {"danger" if conflict else "ok"}">{conflict}</div></div>'
        f'<div class="metric-card"><div class="label">待人工核对</div>'
        f'<div class="value {"warn" if vague else "ok"}">{vague}</div></div>'
        f'<div class="metric-card"><div class="label">循环日程</div>'
        f'<div class="value">{recurring}</div></div>'
        '</div>'
    )

    # --- 冲突明细 ---
    if conflict:
        pairs = find_conflict_pairs(events)
        with st.expander(f"🔴 检测到 {len(pairs)} 组时间冲突，点击查看", expanded=True):
            for a, b in pairs[:10]:
                st.markdown(
                    f"- **{a.start:%m-%d %H:%M}–{a.end:%H:%M}** {a.title} "
                    f"　⚡ 与　**{b.start:%m-%d %H:%M}–{b.end:%H:%M}** {b.title} 重叠"
                )
            st.caption("提示：冲突只做提醒，不会自动删除任何日程，请按需手动调整。")

    # --- 可编辑表格 ---
    _html('<div class="section-title">日程表（可直接编辑 / 增删行）</div>')
    st.caption("改单元格后自动保存。删整行：选中行后按 Delete 键，或点表格右上角工具栏里的删除图标；"
               "也可以展开下方「删除日程」逐条删或一键清空。"
               "「重复」列为只读展示，要调整请用下方「重复设置」面板。"
               "🔴 红色=时间冲突　🟡 黄色=时间待核对")

    df = events_to_dataframe(events)

    editor_cols = {
        "uid": None,
        "标题": st.column_config.TextColumn("标题", width="medium", required=True),
        "开始": st.column_config.DatetimeColumn("开始", format="YYYY-MM-DD HH:mm", step=300),
        "结束": st.column_config.DatetimeColumn("结束", format="YYYY-MM-DD HH:mm", step=300),
        "地点": st.column_config.TextColumn("地点", width="small"),
        "优先级": st.column_config.SelectboxColumn("优先级", options=PRIORITY_LEVELS, width="small"),
        "备注": st.column_config.TextColumn("备注", width="medium"),
        "循环": st.column_config.TextColumn(
            "重复", width="small", disabled=True,
            help="只读展示。要改动请用下方「重复设置」面板——在那里用开关 + 次数步进器调整。",
        ),
        "rrule_raw": None,          # 隐藏列：真正的 RRULE，表格不展示
        "提醒": st.column_config.SelectboxColumn(
            "提醒", options=["跟随全局"] + list(REMIND_OPTIONS.keys()), width="small"
        ),
        "状态": st.column_config.TextColumn("状态", width="small", disabled=True),
    }

    styled = None
    try:
        styled = _style_events(df, events)
    except Exception:  # noqa: BLE001
        styled = df

    try:
        edited = st.data_editor(
            styled,
            key=f"editor_{st.session_state.editor_key}",
            num_rows="dynamic",
            width="stretch",
            hide_index=True,
            column_config=editor_cols,
            height=min(520, 60 + 36 * len(df)),
        )
    except Exception:  # noqa: BLE001
        log(f"data_editor 渲染失败，降级为 DataFrame: {traceback.format_exc()}")
        st.warning("表格样式在当前环境下不可用，已切换为只读视图。")
        st.dataframe(df.drop(columns=["uid"]), width="stretch", hide_index=True)
        return

    # --- 同步编辑结果 ---
    # 用内容指纹判断是否发生了真实编辑，避免「同步 → rerun → 再同步」的无限循环
    new_sig = _df_signature(edited)
    if not st.session_state.editor_hash:
        # 表格刚被重建（首次渲染或数据变更后），此时它就是 session_state 的镜像
        st.session_state.editor_hash = new_sig
    elif new_sig != st.session_state.editor_hash:
        new_events, added, deleted = dataframe_to_events(edited, st.session_state.events)
        st.session_state.events = new_events
        _recompute_conflicts()
        # 数据行数可能变化，重建编辑器以保证内部状态与数据一致
        st.session_state.editor_key += 1
        st.session_state.editor_hash = ""
        if deleted:
            st.session_state.pending_toast = f"已删除 {deleted} 条日程"
        elif added:
            st.session_state.pending_toast = f"已新增 {added} 条日程"
        else:
            st.session_state.pending_toast = "日程已更新"
        st.rerun()

    # --- 重复设置 ---
    st.markdown("")
    _html('<div class="section-title">重复设置</div>')
    st.caption("给选中的日程设置重复频率与次数。重复次数会写入日历文件，到达次数后自动停止，"
               "不会变成永久重复。")
    with st.container(border=True):
        _render_recur_panel()

    # --- 删除日程（单条 / 全部）---
    st.markdown("")
    with st.expander("🗑️ 删除日程（单条 / 全部清空）", expanded=True):
        options = {
            f"{ev.start:%m-%d %H:%M} | {ev.title}": ev.uid
            for ev in sorted(st.session_state.events, key=lambda e: e.start)
        }
        if options:
            picked = st.selectbox("选择要删除的日程", list(options.keys()), key="del_pick")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("删除选中这一条", key="del_btn"):
                    target = options[picked]
                    st.session_state.events = [
                        ev for ev in st.session_state.events if ev.uid != target
                    ]
                    _recompute_conflicts()
                    st.session_state.editor_key += 1
                    st.session_state.editor_hash = ""
                    st.session_state.pending_toast = "已删除 1 条日程"
                    st.rerun()
            with c2:
                if st.button("清空全部日程", key="clear_all_btn"):
                    st.session_state.confirm_clear = True

            # 二次确认，避免误点把现场演示数据全清空
            if st.session_state.get("confirm_clear"):
                _html(
                    f'<div class="notice notice-warn" style="margin-top:8px;">'
                    f'⚠️ 将清空全部 <b>{len(st.session_state.events)}</b> 条日程，'
                    f'此操作不可撤销。确认继续？</div>'
                )
                c3, c4 = st.columns(2)
                with c3:
                    if st.button("确定清空", key="clear_yes", type="primary"):
                        st.session_state.events = []
                        st.session_state.confirm_clear = False
                        st.session_state.editor_key += 1
                        st.session_state.editor_hash = ""
                        st.session_state.pending_toast = "已清空全部日程"
                        st.rerun()
                with c4:
                    if st.button("取消", key="clear_no"):
                        st.session_state.confirm_clear = False
                        st.rerun()
        else:
            st.caption("暂无可删除的日程。")

    # --- 课表视图 ---
    with st.expander("📆 按周查看（未来 7 天时间轴）"):
        _render_week_view(st.session_state.events)


def _render_week_view(events: list[Event]) -> None:
    """渲染未来 7 天的简易时间轴，帮助直观发现冲突。"""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    days = [today + timedelta(days=i) for i in range(7)]
    weekday_cn = "一二三四五六日"
    cols = st.columns(7)
    for i, (col, day) in enumerate(zip(cols, days)):
        with col:
            is_today = day.date() == datetime.now().date()
            color = "#7C3AED" if is_today else "#6B7280"
            st.markdown(
                f'<div style="text-align:center;font-size:11px;font-weight:700;color:{color};">'
                f'{day:%m/%d}<br>周{weekday_cn[day.weekday()]}</div>',
                unsafe_allow_html=True,
            )
            day_events = [e for e in events if e.start.date() == day.date()]
            if not day_events:
                st.markdown(
                    '<div style="text-align:center;color:#D1D5DB;font-size:11px;'
                    'padding:12px 0;">—</div>',
                    unsafe_allow_html=True,
                )
                continue
            for ev in sorted(day_events, key=lambda e: e.start)[:6]:
                bg = "#FEE2E2" if ev.conflict else ("#FEF9E7" if ev.vague else "#EEF2FF")
                fg = "#7F1D1D" if ev.conflict else ("#78350F" if ev.vague else "#3730A3")
                st.markdown(
                    f'<div style="background:{bg};color:{fg};border-radius:8px;'
                    f'padding:6px 7px;margin-bottom:5px;font-size:11px;line-height:1.35;">'
                    f'<b>{ev.start:%H:%M}</b><br>{_escape(ev.title[:12])}</div>',
                    unsafe_allow_html=True,
                )


# ---------------------------------------------------------------------------
# 16. Tab 3：导出
# ---------------------------------------------------------------------------

def render_export_tab() -> None:
    events: list[Event] = st.session_state.events
    _html('<div class="section-title">导出为日历文件</div>')

    if not events:
        _html(
            '<div class="notice notice-warn">'
            '📭 暂无可导出的日程。请先在「录入解析」页添加日程。</div>'
        )
        return

    col1, col2 = st.columns([2, 1])
    with col1:
        name = st.text_input(
            "日历名称",
            value=st.session_state.calendar_name,
            key="cal_name_input",
            help="导入到手机 / Outlook 后显示的日历分组名称。留空则使用默认名称。",
        )
        st.session_state.calendar_name = (name or "").strip() or "我的 AI 日程"
    with col2:
        st.metric("导出条目", len(events))

    default_remind = st.session_state.default_remind
    fixed_count = sum(1 for e in events if get_recur_count(e.rrule))
    recurring_total = sum(1 for e in events if e.is_recurring)
    _html(
        '<div class="notice notice-info">'
        f'当前全局提醒：<b>{_remind_label(default_remind)}</b>；'
        f'循环日程 <b>{recurring_total}</b> 条，其中 <b>{fixed_count}</b> 条已指定重复次数。'
        '未指定次数的按侧边栏「兜底周数」推算结束日期。<br>'
        '.ics 为通用日历标准格式，可直接导入 iOS 日历、Android、Outlook、Google 日历。'
        '</div>'
    )

    # --- 生成三种格式 ---
    ics_error = ""
    ics_text = ""
    try:
        ics_text = build_ics(
            events,
            calendar_name=st.session_state.calendar_name or "我的 AI 日程",
            default_remind=default_remind,
            recur_weeks=st.session_state.recur_weeks,
        )
    except Exception:  # noqa: BLE001
        log(f"ICS 生成失败: {traceback.format_exc()}")
        ics_error = "日历文件生成失败，请尝试减少日程数量后重试。"

    try:
        csv_bytes = build_csv(events, default_remind)
    except Exception:  # noqa: BLE001
        csv_bytes = b""

    try:
        md_text = build_markdown(events, default_remind)
    except Exception:  # noqa: BLE001
        md_text = ""

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    dl1, dl2, dl3 = st.columns(3)

    with dl1:
        if ics_text:
            st.download_button(
                "📅 下载 .ics 日历",
                data=ics_text.encode("utf-8"),
                file_name=f"ai_schedule_{stamp}.ics",
                mime="text/calendar",
                type="primary",
                width="stretch",
                help="标准 iCalendar 格式，双击即可导入系统日历",
            )
        else:
            st.error(ics_error or "ICS 生成失败")
    with dl2:
        if csv_bytes:
            st.download_button(
                "📊 下载 CSV 表格",
                data=csv_bytes,
                file_name=f"ai_schedule_{stamp}.csv",
                mime="text/csv",
                width="stretch",
                help="UTF-8 BOM 编码，Excel / WPS 直接打开不乱码",
            )
    with dl3:
        if md_text:
            st.download_button(
                "📝 下载 Markdown",
                data=md_text.encode("utf-8"),
                file_name=f"ai_schedule_{stamp}.md",
                mime="text/markdown",
                width="stretch",
                help="按日期分组的表格，适合粘贴到笔记或文档",
            )

    # --- 预览 ---
    if ics_text:
        with st.expander("👀 预览 .ics 文件内容"):
            st.code(ics_text[:3000] + ("\n…" if len(ics_text) > 3000 else ""), language="text")
        with st.expander("📝 预览 Markdown 日程表"):
            st.markdown(md_text[:4000])

    # --- 导入指引 ---
    _html('<div class="section-title">如何导入到手机 / 电脑</div>')
    st.markdown(
        """
**iPhone / iPad**：把 `.ics` 文件用「邮件」或「文件」App 打开 → 自动跳转「日历」→ 点击「全部添加」。

**Android**：用「文件管理」打开 `.ics` → 选择「日历」导入；或先发到 Gmail，在网页版 Google 日历中选择「导入」。

**Outlook（Windows / Mac）**：`文件` → `打开和导出` → `导入/导出` → 选择「iCalendar (.ics)」→ 选中文件。

**Google 日历**：网页版 `设置` → `导入和导出` → 选择 `.ics` 文件 → 选择目标日历 → 导入。
        """
    )


# ---------------------------------------------------------------------------
# 17. Tab 4：使用说明
# ---------------------------------------------------------------------------

def render_help_tab() -> None:
    _html('<div class="section-title">3 步上手</div>')
    st.markdown(
        """
1. **录入**：在「录入解析」页粘贴文字，或上传课表 / 聊天截图 → 点击「✨ 开始解析」。
2. **核对**：切到「日程表」页，标红的是时间冲突、标黄的是时间待核对，直接在表格里改。
3. **导出**：在「导出」页下载 `.ics`，发给手机或导入 Outlook 即可。
        """
    )

    _html('<div class="section-title">它能识别什么</div>')
    c1, c2 = st.columns(2)
    with c1:
        st.markdown(
            """
**时间表达**
- `明天下午3点`、`下周三 14:00`
- `11月20日 23:59`、`2026-12-01`
- `3点到5点`、`9:00-11:30`、`持续2小时`
- `第1-2节`（自动按大学作息换算）
- `全天`、跨天（`22:00 到次日2:00`）

**循环规则**
- `每周二`、`每周一三五`
- `工作日`、`每天`、`每月`
            """
        )
    with c2:
        st.markdown(
            """
**结构化字段**
- 标题（自动剥离时间与地点）
- 地点（`地点：`、`@`、`腾讯会议` 等）
- 优先级（DDL / 答辩 / 面试 → 高）
- 备注、提醒时间

**自动处理**
- 重复日程去重（标题 + 时间双维度）
- 过滤已过期日程
- 检测时间冲突并标红
- 模糊时间自动推测并标注 ⚠️
            """
        )

    _html('<div class="section-title">常见问题</div>')
    with st.expander("为什么有些时间标注了 ⚠️ 待核对？"):
        st.markdown(
            "当文本里没有写清楚具体时间（例如只写了日期、或只写「3点」没说上下午），"
            "程序会按常见作息推断一个时间，并标记为待核对。"
            "把鼠标移到表格的「状态」列即可看到具体推测原因。"
        )
    with st.expander("没有配置 API Key 能用吗？"):
        st.markdown(
            "可以。内置的本地规则引擎完全离线，能处理绝大多数格式规整的日程文本。"
            "配置大模型后，对聊天记录这类高度口语化、上下文依赖强的文本理解会更好。"
        )
    with st.expander("我的日程会被上传吗？"):
        st.markdown(
            "日程只存在当前浏览器会话的内存里，不写数据库、不写文件。"
            "图片 OCR 在本机完成，原图不会离开你的电脑。"
            "如果你配置了大模型，只有**识别出的文字**会发送给该模型服务用于解析。"
        )


# ---------------------------------------------------------------------------
# 18. 页脚
# ---------------------------------------------------------------------------

def render_footer() -> None:
    _html(
        '<div class="disclaimer">'
        '<div class="title">⚖️ 免责声明与隐私说明</div>'
        '1. <strong>数据处理</strong>：本应用的所有日程数据仅保存在当前浏览器会话的内存中，'
        '不写入数据库、不写入文件、不上传服务器；关闭或刷新页面即全部销毁。<br>'
        '2. <strong>图片处理</strong>：截图通过本地 OCR 引擎识别，原始图片不会离开本机。<br>'
        '3. <strong>大模型调用</strong>：若管理员配置了 AI 接口，'
        '仅<strong>识别出的文本内容</strong>会被发送至该模型服务用于结构化解析，'
        '不包含图片、账号或设备信息。<br>'
        '4. <strong>解析结果仅供参考</strong>：AI 与规则引擎均可能出错，'
        '特别是日期、时间与循环规则。请在导出前务必人工核对，'
        '标有 ⚠️ 的条目为程序推测结果。<br>'
        '5. <strong>使用风险自负</strong>：因日程解析错误导致的错过会议、DDL 等后果，'
        '开发者不承担责任。本应用为比赛演示作品，不提供任何形式的服务保证。<br>'
        '6. <strong>第三方日历</strong>：导出的 .ics 文件由你自行导入第三方日历软件，'
        '其数据处理遵循对应软件的隐私政策。'
        '</div>'
    )
    _html(
        '<div style="text-align:center;color:#9CA3AF;font-size:11px;padding:14px 0 6px 0;">'
        f'{APP_TITLE} · v{APP_VERSION}　|　Streamlit + OCR + LLM　|　'
        '为大学生 AI 创新比赛而作</div>'
    )


# ---------------------------------------------------------------------------
# 19. 主入口
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(
        page_title=f"{APP_TITLE} · 日历导入工具",
        page_icon="🗂️",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    _inject_css()
    init_state()

    _html(
        '<div class="hero">'
        '<div class="hero-badge">AI · OCR · ICS</div>'
        f'<h1>{APP_TITLE}</h1>'
        f'<p>{APP_SUBTITLE}</p>'
        '</div>'
    )

    render_sidebar()

    tab1, tab2, tab3, tab4 = st.tabs(
        ["📥 录入解析", "📅 日程表", "📤 导出日历", "❓ 使用说明"]
    )
    with tab1:
        try:
            render_ingest_tab()
        except Exception:  # noqa: BLE001
            log(f"录入页异常: {traceback.format_exc()}")
            st.error("录入页渲染出现异常，请刷新页面重试。已录入的日程不会丢失。")
    with tab2:
        try:
            render_schedule_tab()
        except Exception:  # noqa: BLE001
            log(f"日程表页异常: {traceback.format_exc()}")
            st.error("日程表渲染出现异常，请刷新页面重试。")
    with tab3:
        try:
            render_export_tab()
        except Exception:  # noqa: BLE001
            log(f"导出页异常: {traceback.format_exc()}")
            st.error("导出页渲染出现异常，请刷新页面重试。")
    with tab4:
        try:
            render_help_tab()
        except Exception:  # noqa: BLE001
            log(f"说明页异常: {traceback.format_exc()}")
            st.error("说明页渲染出现异常。")

    render_footer()


if __name__ == "__main__":
    main()
