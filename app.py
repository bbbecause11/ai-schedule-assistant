# -*- coding: utf-8 -*-
"""
AI 智能日程收纳 & 日历导入工具
=====================================

由 DeepSeek-V4.1-Flash 模型在 WorkBuddy 工作台构建的 Streamlit 单文件应用。

设计原则
--------
1. **内存计算**：日程只在 `st.session_state` 中存活，不写数据库、不落盘。
   图片仅在本机内存中做 OCR，不做任何持久化。
2. **能力可降级**：OCR 引擎（rapidocr / pytesseract）与大模型（任意 OpenAI 兼容
   接口）均为可选依赖。缺少任一能力时应用仍可运行，并给出明确提示。
3. **规则引擎兜底**：即使没有网络 / 没有 API Key，内置的中文时间规则解析器
   也能把文本整理成结构化的日程，保证离线环境下演示不会"空场"。

运行方式
--------
    pip install -r requirements.txt
    streamlit run app.py

作者声明：本项目为个人学习与演示用途，日程数据不构成任何正式记录。
"""

from __future__ import annotations

import calendar
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
from datetime import time as dt_time
from typing import Any, Iterable

import pandas as pd
import streamlit as st

# ---------------------------------------------------------------------------
# 0. 全局配置
# ---------------------------------------------------------------------------

APP_TITLE = "AI 智能日程收纳"
APP_SUBTITLE = "碎片化日程 → 结构化日程表 → 一键导入手机 / Outlook 日历"

# 版本号：1.0.0 为首版；此后每完成一轮用户可见的功能迭代升一个小版本。
#   1.1.0  重复日程面板（每天 / 每周 / 每两周 + 次数步进器）
#   1.2.0  月历视图、循环开始日期、移除全局兜底周数、ins 风毛玻璃 UI、江大示例
#   1.3.0  设置与外观面板、全站悬停说明、明暗双主题、四档界面字号
#   1.4.0  课表版面重建（识别课表截图并还原成逐节日程）、图片自动放大与增强
#   1.5.0  配色体系重构：设计令牌全覆盖、原生组件深色适配、主题持久化与系统偏好
#   1.6.0  时间块归并：字段列表型截图按时间节点合并，属性词不再当作日程标题
APP_VERSION = "1.6.0"

# 页脚署名
APP_MODEL = "DeepSeek-V4.1-Flash"
APP_BUILDER = "WorkBuddy 工作台"

# 中国标准时间（UTC+8）。使用固定偏移而非 zoneinfo，避免 Windows 缺少 tzdata
# 时抛出 ZoneInfoNotFoundError；中国自 1991 年起不实行夏令时，固定偏移精确无误。
CN_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
CN_TZ_LABEL = "Asia/Shanghai"

# 输入与安全阈值
MAX_TEXT_CHARS = 20_000          # 单次粘贴文本上限，防止超长文本拖垮解析
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 单张图片上限 10MB
MAX_IMAGE_EDGE = 2_400           # OCR 前把最长边压到该值，兼顾速度与精度
OCR_UPSCALE_BELOW = 1_100        # 短边小于该值的图会先放大再识别
OCR_UPSCALE_MAX = 2.5            # 放大倍率上限，避免糊成一片反而更差
ALLOWED_IMAGE_TYPES = {"png", "jpg", "jpeg", "webp", "bmp"}

# 时间推断常量
DEFAULT_EVENT_MINUTES = 60       # 未识别到结束时间时的默认时长
DEFAULT_DAY_START_HOUR = 9       # 只有日期没有时间时的默认开始
# 循环日程默认重复次数（一学期约 16 周）。界面上不再提供全局「兜底周数」，
# 次数统一由「日程表 → 重复设置」面板逐条控制；解析出的循环日程会自动带上
# 该默认次数，保证导出的 .ics 一定有结束条件，不会变成永久重复。
DEFAULT_RECUR_WEEKS = 16         # 仅在 build_ics 内部兜底，已不再暴露到界面
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
    # 每个星期几前面都允许再带一次「周 / 星期 / 礼拜」：
    # 「每周二、周四」「每周一和周三」「每周五」都能整段吃掉，
    # 否则「每周二、周四」只会识别出周二。
    r"(?:周|星期|礼拜)?\s*[一二三四五六日天]"
    r"(?:\s*[、,，和及与/]?\s*(?:周|星期|礼拜)?\s*[一二三四五六日天])*"
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
            # 单天写「每周三」，多天写「周二、周四」（再叠「每周」会显得啰嗦）
            desc = f"每周{days_cn}" if len(days_list) <= 1 else days_full
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
    # 「京江2号楼2205」这类「X+数字+号楼」的写法在高校很常见，必须单独列一条，
    # 否则「京江」与「楼」之间夹了数字，通用分支匹配不上。
    r"[\u4e00-\u9fa5]{1,4}\d{1,2}号楼\s*[A-Za-z]?[一二三四五六七八九十\d]{0,4}|"
    r"[\u4e00-\u9fa5]{2,6}[一二三四五六七八九十]?(?:楼|馆|厅|堂|场|室)\s*[A-Za-z]?[一二三四五六七八九十\d]{0,4}|"
    r"讲堂群\s*\d{0,4}|"
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


# ---------------------------------------------------------------------------
# 「时间块 + 字段列表」结构识别
#
# 教务系统、排班表、会议详情页都存在同一种结构：
#
#     10:45
#     职业生涯发展与规划(6-7节)
#     场地：回龙2311(多)
#     校区：回龙
#     教师：张伟
#     课程性质简称：必修
#     选课备注：
#     重修标：
#     每周四 · 共 16 次
#
# 如果按「一行一条」处理，一门课会被拆成十几个日程：场地成了标题、
# 教师成了标题、连「必修」都能变成标题。正确做法是先按时间节点切块，
# 把块内所有行当**同一件事的属性**，再从中挑出真正的主标题。
# ---------------------------------------------------------------------------

# 字段前缀 → 语义归属。长的优先匹配（「课程性质简称」要先于「课程性质」）。
_FIELD_ALIASES: dict[str, str] = {
    "上课地点": "location", "场地": "location", "地点": "location", "教室": "location",
    "上课校区": "campus", "校区": "campus",
    "授课教师": "teacher", "任课教师": "teacher", "主讲教师": "teacher",
    "主讲": "teacher", "教师": "teacher", "老师": "teacher",
    "课程性质简称": "nature", "课程性质": "nature", "课程类型": "nature", "性质": "nature",
    "选课备注": "note", "重修标": "note", "重修": "note", "备注": "note",
    "上课周次": "weeks", "周次": "weeks", "节次": "period", "上课时间": "time",
    "学分": "credit", "学时": "hours", "课程号": "code", "课程代码": "code",
    "教学班": "cls", "班级": "cls", "考核方式": "assess",
}
_FIELD_KEYS = "|".join(sorted(map(re.escape, _FIELD_ALIASES), key=len, reverse=True))
# 三种写法都要认：
#   「场地：回龙2311(多)」（冒号） / 「场地  回龙2311(多)」（空格对齐）
#   「场地三江楼0802」（OCR 把分隔符整个吞掉）
_FIELD_KEY_RE = re.compile(
    rf"^\s*(?P<key>{_FIELD_KEYS})\s*(?:[:：]\s*)?(?P<val>.*\S)?\s*$")
# 整行只有字段名（可能还留着冒号），值落在下一行
_FIELD_BARE_RE = re.compile(rf"^\s*(?P<key>{_FIELD_KEYS})\s*[:：]?\s*$")

# 时间节点的三种写法：10:45 / 10：45 / 第6-7节
_BLOCK_TIME_RE = re.compile(r"^\s*(?P<h>\d{1,2})\s*[:：]\s*(?P<m>\d{2})(?:\s|$)")
_BLOCK_PERIOD_RE = re.compile(r"^\s*第\s*(\d{1,2})(?:\s*[-–—~]\s*(\d{1,2}))?\s*节\s*[:：]?\s*$")
# 「每周四 · 共 16 次」「每周 1 次」这类循环说明
_BLOCK_RECUR_RE = re.compile(r"(每\s*[周天日]|每周|\d+\s*次\s*/?\s*周)")

# 课程性质、考核方式这类"值"，单独成行时绝不是标题
_NATURE_VALUES = {
    "必修", "选修", "限选", "任选", "公选", "必选", "通识", "通识课",
    "专业必修", "专业选修", "公共必修", "公共选修", "学科基础", "专业基础",
    "考试", "考查", "闭卷", "开卷",
}

# 地点特征：楼/室/馆/场 + 紧邻的门牌号，或整串就是一个 3~4 位房号。
# 不能只凭「含『室』字」就判定成地点——「实验室安全培训」是日程名，不是地点。
_PLACE_RE = re.compile(
    r"(楼|室|馆|场|中心|校区|机房|阶梯)\s*[A-Za-z]?\d"
    r"|\d{3,4}\s*[（(]?\S*[)）]?$"
    r"|^[\u4e00-\u9fa5]{2,6}(楼|室|馆|场|堂|厅)$"
)
# 名字里带这些词的一定是事件，不是地点
_EVENT_HINT_RE = re.compile(r"(课|培训|会议|讲座|活动|研讨|答辩|考试|实验课|班会|例会)")


def _field_of(line: str) -> tuple[str, str] | None:
    """命中学段前缀就返回 (语义, 值)；纯字段名返回 (语义, "")。"""
    m = _FIELD_KEY_RE.match(line)
    if m:
        return _FIELD_ALIASES[m.group("key")], (m.group("val") or "").strip()
    m = _FIELD_BARE_RE.match(line)
    if m:
        return _FIELD_ALIASES[m.group("key")], ""
    return None


def _looks_like_place(text: str) -> bool:
    """像不像一个纯地点（没有事件含义）。"""
    t = text.strip()
    if not t or len(t) > 20:
        return False
    if _EVENT_HINT_RE.search(t):
        return False
    return bool(_PLACE_RE.search(t))


def _looks_like_person(text: str) -> bool:
    """2~4 个汉字、且首字是常见姓氏 → 当人名。"""
    t = text.strip()
    if not re.fullmatch(r"[\u4e00-\u9fa5]{2,4}", t):
        return False
    return t[0] in _CN_SURNAMES


def _is_meta_only(text: str) -> bool:
    """
    这一行只能是属性、绝不适合当标题吗？

    覆盖：字段前缀行、纯地点、纯人名、纯课程性质、纯循环说明、纯数字。
    """
    t = text.strip()
    if not t:
        return True
    if _field_of(t) is not None:
        return True
    if t in _NATURE_VALUES:
        return True
    if _looks_like_place(t) or _looks_like_person(t):
        return True
    if _BLOCK_RECUR_RE.search(t) and len(t) <= 16:
        return True
    if re.fullmatch(r"[\d\s:：\-–—~/次周天日晚上午下午:：]+", t):
        return True
    return False


def _time_block_start(line: str) -> tuple[int, int] | None:
    """这一行是不是时间节点（块的开头）？返回 (时, 分)。"""
    m = _BLOCK_TIME_RE.match(line)
    if not m:
        return None
    h, mi = int(m.group("h")), int(m.group("m"))
    if 0 <= h <= 23 and 0 <= mi <= 59:
        return h, mi
    return None


def _period_block_start(line: str) -> tuple[int, int] | None:
    """「第6-7节」形式的块开头，返回 (起始节, 结束节)。"""
    m = _BLOCK_PERIOD_RE.match(line)
    if not m:
        return None
    p0 = int(m.group(1))
    p1 = int(m.group(2)) if m.group(2) else p0
    if 1 <= p0 <= 20 and p0 <= p1 <= 20:
        return p0, p1
    return None


def _extract_time_blocks(text: str, anchor_day: date) -> tuple[list[Event], set[str]]:
    """
    把「时间节点 + 若干属性行」的列表结构归并成日程。

    返回 (事件列表, 已被消费的行文本集合)；不是这种结构时返回 ([], set())，
    普通聊天记录、车票、通知类文本完全不受影响。

    判定门槛：全文至少有 2 行命中字段前缀（场地/教师/课程性质…），
    否则说明只是普通的多行备忘，交给原规则引擎逐行处理。
    """
    raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines = [ln.strip() for ln in raw_lines]
    if sum(1 for ln in lines if _field_of(ln) is not None) < 2:
        return [], set()

    # --- 1. 按时间节点切块 ------------------------------------------------
    blocks: list[dict[str, Any]] = []
    preamble: list[str] = []            # 时间节点之前的日期/周次表头
    current: dict[str, Any] | None = None
    for ln in lines:
        if not ln:
            continue
        clock = _time_block_start(ln)
        period = None if clock else _period_block_start(ln)
        if clock or period:
            current = {"clock": clock, "period": period, "lines": [],
                       "head": ln}
            blocks.append(current)
            # 「10:45 场地：回龙2311」这种时间与首行黏在一起的情况，拆出剩余部分
            if clock:
                rest = _BLOCK_TIME_RE.sub("", ln, count=1).strip(" -—·|")
                if rest:
                    current["lines"].append(rest)
            continue
        if current is None:
            preamble.append(ln)           # 时间节点之前的内容（标题栏）不属于任何块
            continue
        current["lines"].append(ln)

    if not blocks:
        return [], set()

    flat = "\n".join(lines)
    day, _, _, _ = parse_date_in_text(flat, anchor_day)
    day = day or anchor_day

    events: list[Event] = []
    consumed: set[str] = set()
    # 表头（「2026 年 10 月 29 日 · 周四 第 44 周」）不能再被逐行解析成一条日程
    for ln in preamble:
        if re.search(r"20\d{2}\s*年|星期[一二三四五六日天]|周[一二三四五六日天]|第\s*\d+\s*周", ln):
            consumed.add(ln)
    for blk in blocks:
        ev = _block_to_event(blk, day)
        if ev is None:
            continue
        events.append(ev)
        consumed.update(blk["lines"])
        consumed.add(blk.get("head", ""))          # 「10:45」「第1-2节」这行本身
        if blk["clock"]:
            consumed.add(f"{blk['clock'][0]:02d}:{blk['clock'][1]:02d}")
    return events, consumed


def _block_to_event(blk: dict[str, Any], day: date) -> Event | None:
    """把一个时间块里的若干行，拆成 标题 / 地点 / 备注 / 循环，合成一条日程。"""
    attrs: dict[str, list[str]] = {"location": [], "campus": [], "teacher": [],
                                   "nature": [], "note": [], "weeks": [],
                                   "credit": [], "hours": [], "cls": []}
    titles: list[str] = []
    recur_note = ""
    pending_key: str | None = None          # 「教师」单独成行时，下一行是它的值

    for ln in blk["lines"]:
        hit = _field_of(ln)
        if hit:
            key, val = hit
            if val:
                attrs.setdefault(key, []).append(val)
                pending_key = None
            else:
                pending_key = key        # 值为空，等下一行补
            continue
        # 循环说明要先于"待补值字段"判断：
        # 「选课备注：」下一行往往是「每周四 · 共 16 次」，不能被当成备注值。
        if _BLOCK_RECUR_RE.search(ln) and len(ln) <= 24:
            recur_note = recur_note or ln
            pending_key = None
            continue
        if pending_key:
            attrs.setdefault(pending_key, []).append(ln)
            pending_key = None
            continue
        if _is_meta_only(ln):
            # 纯地点/人名/性质：按特征归位，绝不进标题
            if _looks_like_place(ln):
                attrs["location"].append(ln)
            elif _looks_like_person(ln):
                attrs["teacher"].append(ln)
            elif ln in _NATURE_VALUES:
                attrs["nature"].append(ln)
            continue
        titles.append(ln)

    # --- 时间 -------------------------------------------------------------
    start_dt: datetime | None = None
    end_dt: datetime | None = None
    if blk["clock"]:
        h, m = blk["clock"]
        start_dt = datetime.combine(day, dt_time(h, m))
        end_dt = start_dt + timedelta(minutes=45)
    elif blk["period"]:
        p0, p1 = blk["period"]
        h0, m0 = _period_clock(p0, 0)
        h1, m1 = _period_clock(p1, 1)
        start_dt = datetime.combine(day, dt_time(h0, m0))
        end_dt = datetime.combine(day, dt_time(h1, m1))
    if start_dt is None:
        return None

    # --- 标题：宁可回退，也不拿场地/教师/必修当标题 ----------------------
    title = ""
    for cand in titles:
        cand = re.sub(r"\s{2,}", " ", cand).strip(" -—·|")
        if cand and not _is_meta_only(cand):
            title = cand
            break
    fallback = f"{start_dt:%H:%M} 日程"
    if not title:
        title = fallback

    # --- 地点：具体场地优先，校区只作补充 --------------------------------
    location = ""
    sites = [v for v in attrs["location"] if v]
    campus = [v for v in attrs["campus"] if v]
    if sites:
        location = sites[0]
        if campus and campus[0] and campus[0] not in location:
            location = f"{location}（{campus[0]}）"
    elif campus:
        location = campus[0]

    # --- 备注：教师 / 性质 / 备注 / 学分 全部进说明 ----------------------
    bits: list[str] = []
    for key, label in (("teacher", "教师"), ("nature", "课程性质"),
                       ("credit", "学分"), ("hours", "学时"), ("cls", "教学班")):
        vals = [v for v in attrs.get(key, []) if v]
        if vals:
            bits.append(f"{label} {'、'.join(dict.fromkeys(vals))}")
    for v in attrs.get("note", []):
        if v:
            bits.append(f"备注 {v}")
    if recur_note:
        bits.append(recur_note)
    note = " · ".join(bits)

    # --- 循环 -------------------------------------------------------------
    rrule, _label = parse_recurrence(recur_note or "")
    m_count = re.search(r"共\s*(\d{1,2})\s*次", recur_note or "")
    if rrule and m_count:
        rrule = f"{rrule};COUNT={int(m_count.group(1))}"
    vague_reason = "从截图的字段列表按时间归并得出，请核对标题与时间"
    if title == fallback:
        vague_reason = "该时间块没有找到明确的日程名，标题按时间自动生成，请补充"

    return Event(
        title=title,
        start=start_dt,
        end=end_dt,
        location=location,
        note=note,
        priority="中",
        rrule=rrule,
        vague=True,
        vague_reason=vague_reason,
        source="ocr",
        raw="\n".join(blk["lines"]),
    )


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

    # 「时间节点 + 字段列表」结构优先整体归并。
    # 这类截图一行一个字段（场地/教师/课程性质…），逐行解析会把一门课
    # 拆成十几条日程，还会拿「场地：回龙2311」当标题。
    block_events, block_consumed = _extract_time_blocks(text, doc_day or anchor_day)
    events.extend(block_events)
    has_blocks = bool(block_events)

    for line in _split_candidates(text):
        line = line[:400]
        if line.strip() in trip_consumed or line.strip() in block_consumed:
            continue
        # 已按时间块归并过的文档，字段行一律不再单独成条
        if has_blocks and _field_of(line) is not None:
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


def _ocr_quality(blocks: list[OcrBlock]) -> float:
    """
    给一次识别结果打分，用于在「原图」和「增强图」之间择优。

    只累加长度大于 1 的文字块：单字块多半是表格线、页码、水印噪声，
    计进去反而会让「识别出一堆碎片」的差结果得分更高。
    """
    score = 0.0
    for b in blocks:
        n = len(b.text.strip())
        if n >= 2:
            score += n * max(0.3, b.score)
    return score


def _ocr_looks_poor(blocks: list[OcrBlock]) -> bool:
    """判断是否需要再试一次增强图：文字块太少，或整体置信度偏低。"""
    solid = [b for b in blocks if len(b.text.strip()) >= 2]
    if len(solid) < 8:
        return True
    avg = sum(b.score for b in solid) / len(solid)
    return avg < 0.80


def _enhance_for_ocr(img: Any) -> Any:
    """
    为低对比度 / 浅色水印背景的截图准备一张增强图。

    只做三件不改变字形的事：灰度 → 自动拉伸色阶 → 轻微锐化。
    不做二值化：截图里的浅灰表格线一旦被二值化会变成黑线，反而干扰检测。
    """
    try:
        from PIL import ImageFilter, ImageOps

        gray = ImageOps.autocontrast(img.convert("L"), cutoff=1)
        sharp = gray.filter(ImageFilter.UnsharpMask(radius=1.2, percent=140, threshold=3))
        return sharp.convert("RGB")
    except Exception:  # noqa: BLE001
        log(f"图像增强失败（跳过）: {traceback.format_exc()}")
        return None


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
    elif min(img.size) < OCR_UPSCALE_BELOW:
        # 小图放大：课表 / 聊天记录截图经常只有几百像素宽，
        # 文字笔画不到 8px，检测模型会直接漏掉整行。放大到能认出笔画为止。
        ratio = min(OCR_UPSCALE_MAX, OCR_UPSCALE_BELOW / max(1, min(img.size)))
        if ratio > 1.05:
            new_size = (int(img.width * ratio), int(img.height * ratio))
            if max(new_size) <= MAX_IMAGE_EDGE:
                img = img.resize(new_size, Image.LANCZOS)
                warnings.append(f"图片偏小，已放大至 {new_size[0]}×{new_size[1]} 以提升识别率。")

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
        # 版面重建结果（仅课表截图会填充）
        "cells": [], "layout": None, "raw_text": "",
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

    blocks: list[OcrBlock] = []
    last_output: Any = None
    try:
        if engine_name == "rapidocr":
            import numpy as np
            last_output = engine(np.array(img))
            blocks = _blocks_from_rapidocr(last_output)
            # 识别效果差时（文字块少或均分低），用增强图再跑一遍，取更好的那次
            if _ocr_looks_poor(blocks):
                alt = _enhance_for_ocr(img)
                if alt is not None:
                    alt_output = engine(np.array(alt))
                    alt_blocks = _blocks_from_rapidocr(alt_output)
                    if _ocr_quality(alt_blocks) > _ocr_quality(blocks):
                        blocks, last_output = alt_blocks, alt_output
                        result["warnings"].append("原图对比度偏低，已自动增强后重新识别。")
        else:  # pytesseract
            blocks = _blocks_from_pytesseract(img)
        # 引擎只给文字、不给坐标框时（个别版本 / 自封装），退化成纯文本拼接，
        # 版面重建自然做不了，但文字解析照旧可用。
        text = ("\n".join(b.text for b in blocks if b.text.strip())
                or _plain_text_from_ocr(last_output))
        scores = [b.score for b in blocks if b.score > 0]
        result["confidence"] = round(sum(scores) / len(scores), 3) if scores else None
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

    # --- 版面重建：能认出课表网格就顺带把单元格拆出来 ------------------
    try:
        grid = _detect_timetable_grid(blocks)
        if grid:
            cells = _grid_cells(blocks, grid)
            if cells:
                result["layout"] = {
                    "kind": "timetable",
                    "weekdays": grid.weekday_count,
                    "periods": grid.period_count,
                    "cells": len(cells),
                }
                result["cells"] = cells
                result["text"] = _timetable_lines(cells)
                result["raw_text"] = text
    except Exception:  # noqa: BLE001
        log(f"课表版面重建失败（不影响普通识别）: {traceback.format_exc()}")

    result["ok"] = True
    return result


# ---------------------------------------------------------------------------
# 5b. 课表版面重建
#
# 把 OCR 的 121 个文字块直接 join 成文本，等于把表格压成一维：
# 「高等数学」「星期一」「第1-2节」变成三行互不相干的字，解析器只能瞎猜，
# 于是出现「（ 节）周校区 回龙校」这种垃圾标题。
#
# 这一层保留每个文字块的坐标，把表头「星期几」还原成列、
# 左侧「节次编号」还原成行，再把每个单元格里的文字拼回去，
# 最后按「星期 + 节次」直接生成日程。识别准不准，取决于结构而不是运气。
# ---------------------------------------------------------------------------

@dataclass
class OcrBlock:
    """一条 OCR 文字及其外接框（像素坐标，原点在左上角）。"""

    text: str
    score: float
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def w(self) -> float:
        return max(1.0, self.x1 - self.x0)

    @property
    def h(self) -> float:
        return max(1.0, self.y1 - self.y0)


def _blocks_from_rapidocr(output: Any) -> list[OcrBlock]:
    """兼容 rapidocr 3.x（RapidOCROutput 对象）与 1.x（(payload, elapse) 元组）。"""
    blocks: list[OcrBlock] = []
    if isinstance(output, tuple):
        for item in (output[0] if output else None) or []:
            if not (isinstance(item, (list, tuple)) and len(item) >= 3):
                continue
            try:
                xs = [float(p[0]) for p in item[0]]
                ys = [float(p[1]) for p in item[0]]
                blocks.append(OcrBlock(str(item[1]).strip(), float(item[2]),
                                       min(xs), min(ys), max(xs), max(ys)))
            except (TypeError, ValueError, IndexError):
                continue
        return blocks

    txts = list(getattr(output, "txts", None) or [])
    scores = list(getattr(output, "scores", None) or [])
    boxes = getattr(output, "boxes", None)
    if boxes is None:
        return blocks
    for idx, text in enumerate(txts):
        try:
            xs = [float(p[0]) for p in boxes[idx]]
            ys = [float(p[1]) for p in boxes[idx]]
        except (TypeError, ValueError, IndexError):
            continue
        score = float(scores[idx]) if idx < len(scores) else 0.0
        blocks.append(OcrBlock(str(text).strip(), score,
                               min(xs), min(ys), max(xs), max(ys)))
    return blocks


def _plain_text_from_ocr(output: Any) -> str:
    """引擎没给坐标框时的兜底：把识别到的文字按行拼起来。"""
    if output is None:
        return ""
    if isinstance(output, tuple):
        items = (output[0] if output else None) or []
        return "\n".join(str(it[1]).strip() for it in items
                         if isinstance(it, (list, tuple)) and len(it) >= 2)
    return "\n".join(str(t).strip() for t in (getattr(output, "txts", None) or []))


def _blocks_from_pytesseract(img: Any) -> list[OcrBlock]:
    """pytesseract 走 image_to_data，按行聚合出带坐标的文字块。"""
    import pytesseract
    from pytesseract import Output

    data = pytesseract.image_to_data(img, lang="chi_sim+eng", output_type=Output.DICT)
    lines: dict[tuple[int, int, int], dict[str, Any]] = {}
    for i, word in enumerate(data.get("text", [])):
        word = (word or "").strip()
        if not word:
            continue
        try:
            conf = float(data["conf"][i])
        except (KeyError, TypeError, ValueError):
            conf = -1.0
        if conf < 0:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        x, y = data["left"][i], data["top"][i]
        w, h = data["width"][i], data["height"][i]
        slot = lines.setdefault(key, {
            "words": [], "conf": [],
            "x0": x, "y0": y, "x1": x + w, "y1": y + h,
        })
        slot["words"].append(word)
        slot["conf"].append(conf / 100.0)
        slot["x0"] = min(slot["x0"], x)
        slot["y0"] = min(slot["y0"], y)
        slot["x1"] = max(slot["x1"], x + w)
        slot["y1"] = max(slot["y1"], y + h)
    return [
        OcrBlock(" ".join(s["words"]),
                 sum(s["conf"]) / max(1, len(s["conf"])),
                 s["x0"], s["y0"], s["x1"], s["y1"])
        for s in lines.values()
    ]


# 表头「星期一」～「星期日」。容忍 OCR 把「日」认成「天」，以及全/半角混排。
_WEEKDAY_HEAD_RE = re.compile(r"^(?:星期|周|礼拜)\s*([一二三四五六日天])$")
# 左侧节次编号。OCR 常把它和「上午」粘成「1上」「1止」，所以只看开头数字。
_PERIOD_HEAD_RE = re.compile(r"^(?:第)?\s*(\d{1,2})\s*节?$")

# 课表单元格里「地点」的写法五花八门：W9阶 / W文约209 / W2机房 / 科教城体育场。
_LOC_TOKEN_RE = re.compile(r"[Ww]\s*([^\s,，。;；/]{1,14})")
_LOC_HINT_RE = re.compile(r"(楼|场|馆|室|机房|中心|校区|阶|实验室|实验楼|教\d)")

# 周次的写法：2-6,9-18 / 17~18 / 1-16单周。分隔符只认逗号——认空格的话
# 「W文约101 2-6」里 101 的尾巴会被拆成一个假周次，房间号就丢了。
_WEEK_RANGE_SEP = r"[-–—~～至]"
_WEEK_UNIT = rf"\d{{1,2}}(?:{_WEEK_RANGE_SEP}\d{{1,2}})?"
_WEEK_TOKEN = rf"{_WEEK_UNIT}(?:\s*[,，、]\s*{_WEEK_UNIT})*"
_WEEK_TAIL_RE = re.compile(rf"({_WEEK_TOKEN})\s*(单周|双周|单|双)?\s*周?")

# 单元格里单独出现的「上午/下午」标记（OCR 有时读成「下年」），不是课程。
_SECTION_WORDS = {"上", "下", "中", "晚", "早", "上午", "下午", "晚上",
                  "中午", "下年", "上年", "上中", "中下"}

# 房间号被 OCR 挪了一位：「W经济楼10 81-12」其实是「W经济楼108 1-12」。
# 触发条件很窄（前半段拼出来的数字 >30，不可能是教学周），不会误伤正常课表。
_ROOM_SHIFT_RE = re.compile(
    rf"(?P<loc>[楼室馆场阶]\s*\d{{1,2}})\s+(?P<lead>\d)(?P<rest>{_WEEK_TOKEN})\s*$"
)


@dataclass
class TimetableGrid:
    """课表的行列结构：cols 按星期先后排，rows 按节次从小到大排。"""

    header_bottom: float
    left_edge: float
    cols: list[tuple[float, float, int]]    # (x0, x1, weekday, 0=周一)
    rows: list[tuple[float, float, int]]    # (y0, y1, period)

    @property
    def weekday_count(self) -> int:
        return len(self.cols)

    @property
    def period_count(self) -> int:
        return len(self.rows)


def _detect_timetable_grid(blocks: list[OcrBlock]) -> TimetableGrid | None:
    """
    从文字块里认出「星期 × 节次」的网格。

    判断依据：同一水平行上出现 >= 5 个「星期X」，且它们左侧还有 >= 3 个
    纯数字（节次编号）。两个条件都满足才当成课表，否则返回 None 走原有文本解析，
    保证普通日程截图（会议通知、车票）行为完全不变。
    """
    if len(blocks) < 12:
        return None

    # --- 1. 找星期表头 --------------------------------------------------
    heads: dict[int, OcrBlock] = {}
    for b in blocks:
        text = b.text.replace(" ", "")
        # 无表格线时「节次」和「星期一」会被识别成同一块（"节次 星期一"），
        # 所以用 finditer 在文本内部找「星期X」，并按字符位置比例折算它的横坐标。
        for m in re.finditer(r"(?:星期|周|礼拜)([一二三四五六日天])", text):
            wd = _WEEKDAY_CN.get(m.group(1))
            if wd is None:
                continue
            left = b.x0 + b.w * (m.start() / max(1, len(text)))
            right = b.x0 + b.w * (m.end() / max(1, len(text)))
            cand = OcrBlock(m.group(0), b.score, left, b.y0, right, b.y1)
            old = heads.get(wd)
            if old is None or cand.score > old.score:
                heads[wd] = cand
    if len(heads) < 5:
        return None

    ys = [b.cy for b in heads.values()]
    line_h = sorted(b.h for b in heads.values())[len(heads) // 2]
    # 同一行才算正常课表；竖排（星期在左、节次在上）暂不支持
    if max(ys) - min(ys) > line_h * 1.6:
        return None
    header_bottom = max(b.y1 for b in heads.values())

    # --- 2. 由表头中心推出列边界（相邻中心取中点） ----------------------
    ordered = sorted(heads.items(), key=lambda kv: kv[1].cx)
    centers = [b.cx for _, b in ordered]
    if len(centers) >= 2:
        gaps = sorted(centers[i + 1] - centers[i] for i in range(len(centers) - 1))
        gap = gaps[len(gaps) // 2]
    else:
        gap = ordered[0][1].w * 2
    if gap <= 0:
        return None

    cols: list[tuple[float, float, int]] = []
    for i, (wd, b) in enumerate(ordered):
        left = centers[i] - gap / 2 if i > 0 else b.x0 - gap * 0.10
        right = centers[i] + gap / 2 if i < len(centers) - 1 else b.x1 + gap * 0.10
        cols.append((left, right, wd))
    left_edge = cols[0][0]

    # --- 3. 找左侧节次编号 ----------------------------------------------
    # 用 cx 判断「在不在最左列」：OCR 常把「1」和「上午」粘成「1上」，
    # 这个块的右边界会越过列线，用 x1 判断就会把第 1 节整行丢掉。
    labels: list[tuple[float, float, int]] = []
    for b in blocks:
        if b.y0 <= header_bottom - 2 or b.cx > left_edge:
            continue
        compact = b.text.replace(" ", "")
        m = _PERIOD_HEAD_RE.match(compact) or re.match(r"^(\d{1,2})[^\d]", compact)
        if not m:
            continue
        num = int(m.group(1))
        if 1 <= num <= 20:
            labels.append((b.y0, b.y1, num))
    if len(labels) < 3:
        return None

    labels.sort()
    seen: set[int] = set()
    uniq: list[tuple[float, float, int]] = []
    for y0, y1, num in labels:
        if num in seen:            # 同一节次被重复识别，只留第一次
            continue
        seen.add(num)
        uniq.append((y0, y1, num))
    if len(uniq) < 3:
        return None
    # 正常课表节次自上而下递增；若 OCR 顺序错乱，按编号重排
    if [n for _, _, n in uniq] != sorted(n for _, _, n in uniq):
        uniq.sort(key=lambda t: t[2])

    # --- 4. 用相邻节次编号的中点切行 ------------------------------------
    # 不能拿编号的顶边当行边界：编号有的贴顶、有的居中，一旦编号位置比内容低，
    # 整列内容就会整体串到上一节。取相邻编号「中心点的中值」当边界才稳定，
    # 也不受行高随内容变化的影响（一格塞两门课时那行会明显变高）。
    centers = [(y0 + y1) / 2 for y0, y1, _ in uniq]
    if len(centers) >= 2:
        steps = sorted(centers[i + 1] - centers[i] for i in range(len(centers) - 1))
        step = max(steps[len(steps) // 2], line_h * 1.5)
    else:
        step = max(line_h * 3, 1.0)
    bounds = [(centers[i] + centers[i + 1]) / 2 for i in range(len(centers) - 1)]

    rows: list[tuple[float, float, int]] = []
    for i, (_, _, num) in enumerate(uniq):
        top = min(centers[i] - step / 2, header_bottom) if i == 0 else bounds[i - 1]
        bottom = bounds[i] if i < len(bounds) else centers[i] + step / 2
        rows.append((top, bottom, num))

    return TimetableGrid(header_bottom=header_bottom, left_edge=left_edge,
                         cols=cols, rows=rows)


def _split_cell_courses(text: str) -> list[dict[str, Any]]:
    """
    拆一个单元格里的全部课程。

    教务系统的导出一格可能塞两门课，每条以「/」收尾：
    「形势与政策(1) 冯慧 W1111 17~18, / 俄罗斯历史 Okhotnikov Egor W9 阶 9-12, /」
    """
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    seen_titles: list[str] = []
    for piece in re.split(r"[/／]", text or ""):
        if not piece.strip(" ,，、;；"):
            continue
        info = _split_course_cell(piece)
        title = info["title"].strip()
        if len(title) < 2:
            continue
        if re.fullmatch(r"[\d\s()（）.．·、/\\-]+", title):
            continue
        # 跨节的格子会被合并成「课程 / 课程 /」，同一门课只保留一份，
        # 否则一门课会变成好几条重复日程。
        key = _course_key(info)
        if key in seen:
            continue
        # 第二份常被 OCR 读残（「高等数学(一)(上) 卜庆刚 9阶1W9阶」），
        # 指纹对不上，但它和已收录的那条同名前缀，按重复处理。
        if any(len(t) >= 4 and (title.startswith(t) or t.startswith(title))
               for t in seen_titles):
            continue
        seen.add(key)
        seen_titles.append(title)
        entries.append(info)
    return entries


def _course_key(info: dict[str, Any]) -> str:
    """
    课程指纹：课程名 + 周次 + 教师，用来判断相邻两节的格子是不是同一门课。

    课程名和教师连起来去空格再比对——OCR 对同一门课有时输出「体育(1) 体育部」，
    有时输出「体育(1)体育部」，不归一化就合并不了，会多出几条重复日程。
    """
    name = re.sub(r"\s+", "", f"{info['title']}{info['teacher']}")
    return "|".join([name, _weeks_label(info["weeks"]), info["odd_even"]])


def _grid_cells(blocks: list[OcrBlock], grid: TimetableGrid) -> list[dict[str, Any]]:
    """
    把文字块落到「星期 × 节次」的格子上。

    按列（星期）分组后做纵向聚类，而不是「每块按中心点归一行」——教务系统里
    一格课跨 1-3 节时文字是垂直居中的，逐块归行会把同一门课的几行字劈到相邻
    两行去，于是「俄罗斯历史」被拆成半句飘到下一节。

    相邻两块之间的空隙明显小于行高时，就算同一格；同一格覆盖的节次范围由它
    最上面和最下面那块落在哪一行决定。
    """
    cols: dict[int, list[OcrBlock]] = {}
    for b in blocks:
        if b.y0 < grid.header_bottom - 2 or b.cx < grid.left_edge:
            continue                    # 表头行、左侧节次 / 上午下午列
        wd = None
        for x0, x1, w in grid.cols:
            if x0 - 8 <= b.cx <= x1 + 8:
                wd = w
                break
        if wd is None:
            continue
        cols.setdefault(wd, []).append(b)

    def band(y: float) -> int | None:
        for r0, r1, p in grid.rows:
            if r0 - 2 <= y < r1:
                return p
        return None

    cells: list[dict[str, Any]] = []
    for wd, items in cols.items():
        items.sort(key=lambda b: (b.cy, b.x0))
        heights = sorted(b.h for b in items)
        line_h = heights[len(heights) // 2] if heights else 20.0
        row_h = min((r1 - r0) for r0, r1, _ in grid.rows) if grid.rows else 60.0
        # 同一格内相邻两行的文字框通常是紧贴甚至重叠的（间距 <= 0），
        # 而隔开两门课的空隙至少也有小半个行高。阈值取 30% 行高：
        # 实测同一格内的行间距全为负，最近的两门课相隔 10px，用 7px 能切开。
        gap_thr = max(3.0, min(line_h * 0.30, row_h * 0.30))

        runs: list[list[OcrBlock]] = []
        for b in items:
            if runs and b.y0 - max(x.y1 for x in runs[-1]) <= gap_thr:
                runs[-1].append(b)
            else:
                runs.append([b])

        for run in runs:
            run.sort(key=lambda b: (b.y0, b.x0))
            # 节次取这一格「中心」落在哪一行。用「上沿到上沿」的完整跨度会多算
            # 一节：教务系统的课表把一格文字垂直居中，跨 2 节的一格，文字常常
            # 顶进上一行、底探进下一行，实测每格都溢出 1 行。真正的跨度交给
            # 相邻重复副本去合并（第 1、2 节各写一遍 → 合成 1-2 节）。
            center = sum(b.cy for b in run) / len(run)
            period = band(center) or band(min(b.y0 for b in run)) or band(
                max(b.y1 for b in run))
            if period is None:
                continue
            cells.append({
                "weekday": wd,
                "period": period,
                "text": " ".join(x.text for x in run),
            })

    cells.sort(key=lambda c: (c["weekday"], c["period"]))
    return _merge_same_course(cells)


def _merge_same_course(cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    把连续几节重复出现的同一门课合并成一条跨节日程。

    这类课表每一节都是一行、内容重复一遍（第 1、2 节各写一次同一门课），
    所以「节次相连 + 课程指纹相同」就说明它其实是一堂课。
    """
    merged: list[dict[str, Any]] = []
    for cell in cells:
        courses = _split_cell_courses(cell["text"])
        key = "&".join(sorted(_course_key(c) for c in courses))
        cell = dict(cell, courses=courses, key=key)
        span = (cell["period"], cell.get("period_end", cell["period"]))
        if merged and key:
            prev = merged[-1]
            # 允许重叠：跨节格子的上沿偶尔会探进下一行一点点
            if (prev["weekday"] == cell["weekday"]
                    and cell["period"] <= prev["periods"][1] + 1
                    and key == prev["key"]):
                prev["periods"] = (prev["periods"][0], max(prev["periods"][1], span[1]))
                prev["text"] = f"{prev['text']} {cell['text']}"
                continue
        if not key:
            continue                    # 空格子（只有噪声）
        merged.append(dict(cell, periods=span))
    return merged




def _parse_week_ranges(spec: str) -> list[tuple[int, int]]:
    """
    把「2-6,9-18」解析成 [(2,6),(9,18)]。

    顺手清掉一个高频 OCR 噪声：单元格末尾的「/」有时被读成「1」，
    于是在周次后面多出一个「, 1」。真实课表的周次是按升序写的，
    所以「9-18 之后又冒出一个 1」必然是分隔符误读，直接丢掉。
    """
    out: list[tuple[int, int]] = []
    for part in re.split(r"[,，、]", spec):
        nums = re.findall(r"\d{1,2}", part)
        if not nums:
            continue
        a = int(nums[0])
        b = int(nums[1]) if len(nums) > 1 else a
        if a > b:
            a, b = b, a
        if 1 <= a <= 30 and a <= b <= 30:
            out.append((a, b))
    if len(out) >= 2 and out[-1] == (1, 1) and out[-2][0] > 1:
        out.pop()
    # 被上一段完全包含的重复项（「2-6,9-18,11」里的 11）一并去掉
    if len(out) >= 2:
        kept: list[tuple[int, int]] = []
        for a, b in out:
            if any(not (b < x or a > y) for x, y in kept):
                continue
            kept.append((a, b))
        out = kept
    return out


# 常见中文姓氏。OCR 常把教师姓和课程名粘成一个词（「高等数学(一)(上)卜」+「庆刚」），
# 靠这张表把粘在课程名末尾的那个字认回来。只收百名以内的高频姓，宁缺毋滥。
_CN_SURNAMES = set(
    "王李张刘陈杨黄赵周吴徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘"
    "于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江"
    "尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤卜"
)
# 单独成词的「地点后缀」，OCR 常把它们与前缀数字拆开（W9 + 阶 → 9 + 阶）
_LOC_SUFFIX = {"阶", "楼", "室", "机房", "场", "馆", "中心", "校区", "阶梯", "教学楼"}


def _week_boundary_ok(raw: str, i: int) -> bool:
    """
    周次片段前面是不是一个「干净」的边界。

    「W文约101 2-6」里 2-6 前面是空格，干净；「W9阶2-6」前面是汉字，也干净。
    而「W文约1012-6」里 2-6 前面是数字 1，说明房间号和周次被 OCR 粘在一起了。
    """
    if i <= 0:
        return True
    ch = raw[i - 1]
    if ch.isspace():
        return True
    return "\u4e00" <= ch <= "\u9fff"


def _week_is_continuation(raw: str, i: int) -> bool:
    """逗号后面的片段只是前面周次的续写（「2-6,9-18」里的 9-18），不能单独当周次。"""
    return i > 0 and raw[i - 1] in ",，、;；"


def _take_week_suffix(raw: str) -> tuple[list[tuple[int, int]], str, str]:
    """
    从右端摘出周次，返回 (weeks, 单双周, 去掉周次后的前缀)。

    做法是把「能单独解释成周次」的后缀全部列出来再挑，而不是让正则从左往右
    贪心匹配——「W文约101 2-6,9-15」里 101 的末位也能拼出一个（错的）周次
    「1 2-6,9-15」，贪心匹配会先撞上它，房间号的 101 就被吃掉了。
    """
    cands: list[tuple[int, list[tuple[int, int]], str, bool, bool, bool]] = []
    for i in range(max(0, len(raw) - 36), len(raw)):
        if _week_is_continuation(raw, i):
            continue
        m = _WEEK_TAIL_RE.fullmatch(raw[i:])
        if not m:
            continue
        weeks = _parse_week_ranges(m.group(1))
        if not weeks:
            continue
        spec = m.group(1)
        head = re.split(r"[,，、]", spec)[0]
        cands.append((i, weeks, m.group(2) or "",
                      bool(re.search(_WEEK_RANGE_SEP, spec)),
                      _week_boundary_ok(raw, i),
                      bool(re.search(_WEEK_RANGE_SEP, head))))
    if not cands:
        return [], "", raw

    clean = [c for c in cands if c[4]]
    if clean:
        # 边界干净时取最靠左的：「2-6,9-15」比它的尾巴「9-15」更完整
        i, weeks, oe = max(clean, key=lambda c: (c[5], c[3], -c[0]))[:3]
    else:
        # 全是脏边界：房间号和周次粘在一起了（文约1012-6,9-15）。
        # 首选「第一段就是区间」的候选，再取最靠右的，把多出的数字留给房间号。
        i, weeks, oe = max(cands, key=lambda c: (c[5], c[3], c[0]))[:3]
    if oe in ("单", "双"):
        oe += "周"
    return weeks, oe, raw[:i].strip()


def _split_course_cell(text: str) -> dict[str, Any]:
    """
    拆一个课程条目：课程名 / 教师 / 地点 / 周次 / 单双周。

    典型输入：「高等数学(一)(上)卜 庆刚 W9阶 2-6,9-18, /」
    典型输出：title=高等数学(一)(上) teacher=卜庆刚 location=9阶 weeks=[(2,6),(9,18)]

    解析顺序是踩坑踩出来的，别随手调换：
    ① 清洗（括号里的空格、破折号、末尾分隔符、错位的房间号）
    ② 周次——必须排在地点前面，否则「W文约101 2-6」的 101 会被当成周次
    ③ 地点——先认 W 前缀标记，再把被折行切碎的碎片拼回去
    ④ 剩下的切课程名 / 教师
    """
    info: dict[str, Any] = {"title": "", "teacher": "", "location": "",
                            "weeks": [], "odd_even": ""}
    raw = re.sub(r"\s+", " ", (text or "")).strip()
    if not raw:
        return info

    # --- ① 清洗 ---------------------------------------------------------
    # 折行会把括号和里面的字拆开：「( 上)」「(1 )」
    raw = re.sub(r"([(（])\s+", r"\1", raw)
    raw = re.sub(r"\s+([)）])", r"\1", raw)
    # 破折号两侧的空格：「2- 6」「9 -15」先粘回「2-6」「9-15」
    raw = re.sub(rf"\s*{_WEEK_RANGE_SEP}\s*", "-", raw)
    # 左侧「上午/下午」的标记偶尔会和第一格内容被识别成同一块
    raw = re.sub(r"(上午|下午|晚上|中午|下年|上年)\s*", " ", raw).strip()
    # 末尾的分隔符（「/」被读成「1」时，尾巴上会多一个孤零零的数字）
    raw = re.sub(r"[\s,，、;；/·•。.]+$", "", raw)
    raw = re.sub(r"\s*[.·•]\s*\d{1,2}$", "", raw)
    raw = re.sub(r"[\s,，、;；/·•。.]+$", "", raw)
    m = re.match(r"^(.*\d\s*-\s*\d{1,2})\s+\d{1,2}$", raw)
    if m and _parse_week_ranges(m.group(1)):
        raw = m.group(1)
    # 课程名末尾的罗马数字 I 常被读成竖线（综合英语I → 综合英语|）。
    # 竖线可能落在行尾，也可能夹在课程名和教师之间，两种都改回来。
    raw = re.sub(r"(?<=[\u4e00-\u9fff])\|(?=\s|$)", "I", raw)
    if raw.endswith("|"):
        raw = raw[:-1] + "I"
    # 房间号被挪了一位：「W经济楼10 81-12」其实是「W经济楼108 1-12」。
    # 只在「移位后拼出的数字不可能是教学周」时动手，避免误伤。
    m = _ROOM_SHIFT_RE.search(raw)
    if (m and re.search(_WEEK_RANGE_SEP, m.group("rest"))
            and _parse_week_ranges(m.group("rest"))
            and not _parse_week_ranges(m.group("lead") + m.group("rest"))):
        raw = (raw[:m.start("loc")] + m.group("loc") + m.group("lead")
               + " " + m.group("rest"))
    if not raw or raw in _SECTION_WORDS:
        return info

    # --- ② 周次：通常写在末尾（2-6,9-18 / 17~18 / 1-16单周） -------------
    weeks, odd_even, rest = _take_week_suffix(raw)
    if weeks:
        info["weeks"], info["odd_even"], raw = weeks, odd_even, rest
        # 「W文约209 双1 2-18」：单双周标记把周次的首位数字挤到了自己后面
        m = re.search(r"(单|双)\s*(\d{1,2})$", raw)
        if m and not info["odd_even"]:
            a, b = info["weeks"][0]
            merged = int(f"{m.group(2)}{a}") if a < 10 else a
            if 1 <= merged <= 30:
                info["weeks"][0] = (merged, max(b, merged))
                info["odd_even"] = m.group(1) + "周"
                raw = raw[:m.start()].strip()
    # 「W文约209 双 12-18」：单双周写在周次前面，单独摘出来
    tail = raw.split(" ")[-1] if raw.split(" ") else ""
    if tail in ("单", "双") and not info["odd_even"]:
        info["odd_even"] = tail + "周"
        raw = raw[: raw.rfind(tail)].strip()

    # --- ③ 地点：先摘掉 W 前缀标记（W9阶 / W文约209 / W2机房） ------------
    m = _LOC_TOKEN_RE.search(raw)
    if m:
        loc = m.group(1).strip().rstrip(",，、.")
        if loc:
            info["location"] = loc
            raw = (raw[:m.start()] + " " + raw[m.end():]).strip()

    tokens = [t for t in raw.split(" ") if t.strip()]
    # 纯标点的碎块（「.」「·」）和「.1」这类分隔符误读，直接丢掉
    tokens = [t for t in tokens
              if not re.fullmatch(r"[.,·•。、;；:：_\-]+", t)
              and not re.fullmatch(r"[.·•]\d{1,2}", t)]
    # 开头单独成词的「上/下」（上午/下午被折行拆成两块）
    while len(tokens) > 1 and tokens[0] in _SECTION_WORDS:
        tokens.pop(0)

    # 「W9」+「阶」、「W文」+「约101」这类被切碎的地点，拼回去
    if info["location"]:
        merged_tokens: list[str] = []
        for tok in tokens:
            # 折行会把「W科教城体育场」切成「W科教」+「城体育场」，
            # 后面的碎片只要还带地点特征就并回去。
            absorb = (tok in _LOC_SUFFIX
                      or re.fullmatch(r"[约\d]{1,5}", tok)
                      or (len(tok) <= 8 and _LOC_HINT_RE.search(tok)))
            if absorb:
                info["location"] += tok
                continue
            merged_tokens.append(tok)
        tokens = merged_tokens
    else:
        # 没有 W 标记：取最后一个带地点特征的词，并把紧邻的前缀一起收进来
        idx = None
        for i, tok in enumerate(tokens):
            if len(tok) >= 2 and _LOC_HINT_RE.search(tok):
                idx = i
        if idx is not None and idx > 1:
            info["location"] = tokens[idx - 1] + tokens[idx]
            tokens = tokens[:idx - 1] + tokens[idx + 1:]
        elif idx is not None:
            info["location"] = tokens[idx]
            tokens = tokens[:idx] + tokens[idx + 1:]

    # --- ④ 剩下的切分成 课程名 + 教师 --------------------------------
    title_parts: list[str] = []
    if len(tokens) == 1:
        # 只剩一个词：正常情况下它就是课程名，但「…(上)官威」这种
        # 教师名被粘在末尾的写法很常见，先试着拆出来。
        # 只在「括号后面跟 2-3 个汉字」时动手：课程名不会在括号后再接两三个字，
        # 而姓名几乎总是 2-3 个字（「官」这类罕见姓靠姓氏表认不出来，所以不能只靠表）。
        tok = tokens[0]
        m = re.match(r"^(.{2,}[)）])\s*([\u4e00-\u9fff]{2,3})$", tok)
        if m:
            title_parts = [m.group(1)]
            info["teacher"] = m.group(2)
        else:
            title_parts = tokens[:]
    else:
        for i, tok in enumerate(tokens):
            if i == len(tokens) - 1:
                info["teacher"] = tok
            else:
                title_parts.append(tok)

    # 「(上)官威」：括号是课程名的一部分，被 OCR 粘到了教师前面
    m = re.match(r"^[（(]([^）)]{1,4})[）)]\s*(.+)$", info["teacher"] or "")
    if m:
        title_parts.append(f"({m.group(1)})")
        info["teacher"] = m.group(2)

    # 英文教师名常被空格切成两段（Okhotnikov Egor / Okhotnik ov Egor）
    latin = r"[A-Za-z][A-Za-z.\- ]*"
    while (info["teacher"] and title_parts
           and re.fullmatch(latin, info["teacher"]) and re.fullmatch(latin, title_parts[-1])):
        prev = title_parts.pop()
        # 小写接小写说明单词被折行截断了（Okhotnik|ov），直接拼；
        # 其余情况按空格拼（Okhotnikov|Egor）
        joiner = "" if (prev[-1:].islower() and info["teacher"][:1].islower()) else " "
        info["teacher"] = prev + joiner + info["teacher"]

    # 教师姓被 OCR 甩到课程名末尾（「…(上)卜」+「庆刚」），用姓氏表认回来
    if title_parts and info["teacher"] and len(info["teacher"]) <= 2:
        tail = title_parts[-1]
        if tail and tail[-1] in _CN_SURNAMES and len(info["teacher"]) + 1 <= 4:
            info["teacher"] = tail[-1] + info["teacher"]
            title_parts[-1] = tail[:-1]
        elif (len(info["teacher"]) == 1 and len(tail) >= 2
              and tail[-2] in _CN_SURNAMES and len(info["teacher"]) + 2 <= 4):
            # 名字被折行切成「卜庆」+「刚」
            info["teacher"] = tail[-2:] + info["teacher"]
            title_parts[-1] = tail[:-2]

    info["title"] = " ".join(p for p in title_parts if p).strip() or raw
    return info


# 默认作息表：第 1 节 08:00 起，每节 45 分钟，课间 5 分钟。
# 各校略有差异（有的 8:20 上课、有的每节 40 分钟），所以课表导入的日程
# 一律标记为「待核对」，并在备注里写明「按默认作息推算」。
COURSE_PERIOD_TIMES: dict[int, tuple[int, int, int, int]] = {
    1: (8, 0, 8, 45), 2: (8, 50, 9, 35), 3: (9, 55, 10, 40),
    4: (10, 45, 11, 30), 5: (11, 35, 12, 20),
    6: (14, 0, 14, 45), 7: (14, 50, 15, 35), 8: (15, 55, 16, 40),
    9: (16, 45, 17, 30),
    10: (19, 0, 19, 45), 11: (19, 50, 20, 35), 12: (20, 40, 21, 25),
}


def _period_clock(period: int, which: int) -> tuple[int, int]:
    """取第 period 节的开始（which=0）或结束（which=1）时刻；超出表格就顺延。"""
    times = COURSE_PERIOD_TIMES.get(period)
    if times:
        return (times[0], times[1]) if which == 0 else (times[2], times[3])
    base = COURSE_PERIOD_TIMES[max(COURSE_PERIOD_TIMES)]
    extra = max(0, period - max(COURSE_PERIOD_TIMES))
    start = datetime(2000, 1, 1, base[2], base[3]) + timedelta(minutes=5 + extra * 50)
    end = start + timedelta(minutes=45)
    return (start.hour, start.minute) if which == 0 else (end.hour, end.minute)


def _this_monday(today: "date | None" = None) -> "date":
    """本周一。课表首周日期没设置时的兜底锚点。"""
    day = today or datetime.now().date()
    return day - timedelta(days=day.weekday())


def _weeks_label(weeks: list[tuple[int, int]]) -> str:
    """[(2,6),(9,18)] → 「2-6、9-18」。"""
    return "、".join(f"{a}-{b}" if a != b else str(a) for a, b in weeks)


def timetable_to_events(cells: list[dict[str, Any]],
                        anchor_monday: "date") -> list[Event]:
    """
    把课表单元格转成日程。

    anchor_monday 是「学期第 1 周的周一」，由用户在设置里指定；
    没指定时默认取本周一，日期会偏，因此事件标记为待核对。
    """
    events: list[Event] = []
    for cell in cells:
        wd: int = cell["weekday"]
        p0, p1 = cell["periods"]
        h0, m0 = _period_clock(p0, 0)
        h1, m1 = _period_clock(p1, 1)

        for info in cell.get("courses") or _split_cell_courses(cell["text"]):
            title = info["title"].strip()
            if len(title) < 2 or re.fullmatch(r"[\d\s()（）.．·、/\\-]+", title):
                continue                     # 表格里的空壳、噪声

            weeks: list[tuple[int, int]] = info["weeks"]
            first_week = weeks[0][0] if weeks else 1
            day = anchor_monday + timedelta(days=(first_week - 1) * 7 + wd)

            byday = _WEEKDAY_ICS[wd]
            total = sum(b - a + 1 for a, b in weeks) if weeks else 16
            odd_even = info["odd_even"]
            if odd_even == "单周":
                rrule = f"FREQ=WEEKLY;INTERVAL=2;BYDAY={byday};COUNT={max(1, (total + 1) // 2)}"
            elif odd_even == "双周":
                day += timedelta(days=7)
                rrule = f"FREQ=WEEKLY;INTERVAL=2;BYDAY={byday};COUNT={max(1, total // 2)}"
            else:
                rrule = f"FREQ=WEEKLY;BYDAY={byday};COUNT={max(1, total)}"

            bits = ["课表导入 · 按默认作息表推算时间"]
            if weeks:
                bits.append(f"第 {_weeks_label(weeks)} 周")
            if odd_even:
                bits.append(odd_even)
            if info["teacher"]:
                bits.append(f"教师 {info['teacher']}")
            bits.append(f"第 {p0} 节" if p0 == p1 else f"第 {p0}-{p1} 节")

            events.append(Event(
                title=title,
                start=datetime.combine(day, dt_time(h0, m0)),
                end=datetime.combine(day, dt_time(h1, m1)),
                location=info["location"],
                note=" · ".join(bits),
                priority="中",
                rrule=rrule,
                vague=True,
                vague_reason="课程时间与首周日期由课表推算，请核对",
                source="ocr",
                raw=cell["text"],
            ))
    return events


def _timetable_lines(cells: list[dict[str, Any]]) -> str:
    """
    把课表还原成「人话」文本行，供 OCR 详情展示，也供大模型使用。

    比「星期一\\n高等数学\\n2-6,9-18」这种压平文本信息量大得多：
    大模型看到「星期一 第1-2节 高等数学(一)(上) 卜庆刚 W9阶 第2-6、9-18周」
    才有可能给出正确结果。
    """
    lines: list[str] = []
    for cell in cells:
        p0, p1 = cell["periods"]
        wd = "一二三四五六日"[cell["weekday"]]
        for info in cell.get("courses") or _split_cell_courses(cell["text"]):
            parts = [f"星期{wd}", f"第{p0}-{p1}节" if p0 != p1 else f"第{p0}节",
                     info["title"]]
            if info["teacher"]:
                parts.append(info["teacher"])
            if info["location"]:
                parts.append(info["location"])
            if info["weeks"]:
                parts.append(f"第{_weeks_label(info['weeks'])}周{info['odd_even']}")
            lines.append(" ".join(p for p in parts if p))
    return "\n".join(lines)


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

【字段列表结构：教务详情 / 排班表 / 时间轴清单】
11. 如果文本是「时间节点 + 若干字段行」的形状（例如「10:45 / 场地：回龙2311 /
    教师：张伟 / 课程性质简称：必修 / 每周四 共16次」），**必须把同一时间节点下的
    所有行合并成 1 个事件**，事件数量等于时间节点数量，绝不能一行一条。
12. 以下内容**绝对不能当 title**，只能写进 location / note：
    - 带字段前缀的行：「场地：」「校区：」「教室：」「教师：」「课程性质简称：」
      「选课备注：」「重修标：」「备注：」「学分：」
    - 单独成行的地点（如「回龙2311(多)」「三江楼0802」）
    - 单独成行的人名
    - 「必修」「选修」「考试」「考查」这类课程性质值
    - 「每周四 · 共 16 次」这类循环说明
    如果该时间节点下找不到真正的课程名/会议名，title 用空字符串 ""，不要编造，
    更不要把场地或教师名顶上去。
13. 字段映射：场地/校区/教室/地点 → location（具体房号优先，校区可并入或忽略）；
    教师、课程性质、选课备注、重修标、学分 → note；「每周四 共16次」→ 同时写入
    rrule 与 note。
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

    # --- 7.0 循环日程补齐结束条件 -------------------------------------
    # 界面上已取消全局「兜底周数」，改由这一处统一给「没有结束条件」的循环
    # 日程写入默认次数（COUNT）。这样导出的 .ics 一定能在第 N 次后自动停止，
    # 既不会变成永久重复，用户也能在日程表里逐条改成自己想要的数量。
    for ev in events:
        if not ev.is_recurring:
            continue
        if get_recur_count(ev.rrule) or "UNTIL" in ev.rrule.upper():
            continue
        ev.rrule = set_recur_count(ev.rrule, RECUR_COUNT_DEFAULT)

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
    """
    构造一段刻意「脏乱」的示例文本：江苏大学课表 + 群聊通知 + 备忘混排。

    内容取自江苏大学能源与动力工程学院的真实场景（教学楼、作息、课程名），
    日期全部相对"今天"生成，保证现场演示永不过期。
    其中带「每周X」的条目会被自动识别为循环日程。
    """
    today = datetime.now()
    monday = today - timedelta(days=today.weekday())
    d = lambda n: (monday + timedelta(days=n)).strftime("%m月%d日")  # noqa: E731
    return f"""【2026-2027 学年第一学期课表 · 能源与动力工程学院】
每周一 第1-2节 高等数学A(I) 三江楼0802 刘海龙
每周一 第3-4节 大学英语(I) 三山楼402
每周二 第5-6节 工程制图基础 京江2号楼2205 刘顺
每周二、周四 第7-8节 大学物理A(II) 三山楼406
每周三 第3-4节 工程热力学A 能动楼301 刘璐
每周三 第9-11节 大学物理实验A(II) 三江楼附楼301
每周五 第5-6节 工程力学C 三山楼615

【能动学院 2026 级班级群】
辅导员：{d(2)} 下午2点 三江楼305 开年级大会，带学生证，迟到要记考勤
班长：{d(3)} 晚上19:00 讲堂群110 听「能动大讲堂」学术报告，可以盖第二课堂章
学委：{d(4)} 23:59 提交《工程热力学》课程设计报告 DDL 不能补交

【个人备忘】
{d(1)} 09:00-11:00 图书馆五楼研讨间 小组讨论节能减排大赛方案
{d(5)} 08:00-09:30 校医院 入学体检复查 空腹
{d(6)} 全天 京江报告厅 江苏大学大学生 AI 创新大赛决赛答辩 要带电脑和 U 盘
本周五 下午4点 研究生楼106 找导师确认课题方向
明天中午12点 一食堂二楼 和队友碰一下分工
11月30日 晚上8点半 腾讯会议 和学长请教保研经验
"""


# ---------------------------------------------------------------------------
# 10. 页面样式（静态 CSS，绝不拼接任何用户输入，避免 XSS）
# ---------------------------------------------------------------------------

_CSS = """
<style>
/* ==========================================================================
   视觉系统：排版 / 间距 / 圆角 / 动效全部走 :root 的令牌
   颜色两套值由 _css_tokens() 注入，这里不写死任何颜色
   ========================================================================== */

/* ---------- 1. 基础排版 ---------- */
.stApp {
    background: var(--app-bg);
    background-attachment: fixed;
    color: var(--ink);
    font-family: var(--font-sans);
    font-size: var(--fs-body);
    line-height: var(--lh-body);
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
    text-rendering: optimizeLegibility;
}

/* 正文：中文行高 1.75，段间留半个行距，不做首行缩进 */
.stApp p, .stApp li {
    color: var(--ink);
    font-size: var(--fs-body);
    line-height: var(--lh-body);
}
.stApp p { margin: 0 0 .7em; }
.stApp li { margin-bottom: .25em; }
.stApp li:last-child, .stApp p:last-child { margin-bottom: 0; }

/* 标题：字重 600（不是 700/800），大标题收紧字距 */
.stApp h1, .stApp h2, .stApp h3,
.stApp h4, .stApp h5, .stApp h6 {
    color: var(--ink);
    font-weight: 600;
    line-height: var(--lh-tight);
    letter-spacing: var(--track-snug);
    margin: 0 0 .45em;
}
.stApp h1 { font-size: var(--fs-h1); letter-spacing: var(--track-tight); }
.stApp h2 { font-size: var(--fs-h2); margin-top: 1.4em; }
.stApp h3 { font-size: var(--fs-h3); }
.stApp h4, .stApp h5, .stApp h6 { font-size: var(--fs-sm); }

/* 说明文字 / 辅助文字 */
.stApp small,
.stApp [data-testid="stCaptionContainer"],
.stApp [data-testid="stCaptionContainer"] p {
    color: var(--muted-2);
    font-size: var(--fs-xs);
    line-height: var(--lh-snug);
    letter-spacing: 0;
}
.stApp [data-testid="stCaptionContainer"] { margin-bottom: .4rem; }

.stApp label { font-size: var(--fs-sm); font-weight: 500; color: var(--ink-2); }
.stApp strong, .stApp b { font-weight: 600; color: var(--ink); }
.stApp hr {
    border: none;
    border-top: 1px solid var(--line);
    margin: var(--space-5) 0;
}
.stApp code { font-family: var(--font-mono); font-size: .92em; }

/* 链接：品牌强调色，下划线只在 hover 出现 */
.stApp [data-testid="stMarkdownContainer"] a {
    color: var(--accent);
    text-decoration: none;
    border-bottom: 1px solid transparent;
    transition: border-color var(--dur-fast) var(--ease-out),
                color var(--dur-fast) var(--ease-out);
}
.stApp [data-testid="stMarkdownContainer"] a:hover {
    color: var(--accent-hover);
    border-bottom-color: currentColor;
}

/* ---------- 2. 版心与节奏 ---------- */
.block-container {
    max-width: var(--content-max);
    padding: var(--space-6) var(--space-5) var(--space-8);
}

/* 区块标题：一条 3px 品牌色竖线 + 21px 字 */
.section-title {
    display: flex;
    align-items: center;
    gap: .65rem;
    font-size: var(--fs-h2);
    font-weight: 600;
    letter-spacing: var(--track-snug);
    line-height: var(--lh-tight);
    color: var(--ink);
    margin: var(--space-7) 0 var(--space-4);
}
.section-title::before {
    content: "";
    width: 3px;
    height: 1.05em;
    border-radius: var(--r-pill);
    background: var(--brand-grad);
    flex: none;
}

/* ---------- 3. 首屏品牌条 ---------- */
.hero {
    position: relative;
    overflow: hidden;
    background: var(--brand-grad);
    border-radius: var(--r-xl);
    padding: 1.9rem 2rem 1.8rem;
    margin-bottom: var(--space-5);
    box-shadow: var(--hero-shadow);
}
.hero::after {
    content: "";
    position: absolute;
    right: -70px; top: -90px;
    width: 260px; height: 260px;
    border-radius: 50%;
    background: rgba(255,255,255,.14);
}
.hero::before {
    content: "";
    position: absolute;
    left: 30%; bottom: -130px;
    width: 280px; height: 280px;
    border-radius: 50%;
    background: rgba(255,255,255,.09);
}
.hero-badge {
    display: inline-block;
    font-size: var(--fs-2xs);
    font-weight: 600;
    letter-spacing: var(--track-wide);
    color: var(--brand-fg);
    background: rgba(255,255,255,.16);
    border: 1px solid rgba(255,255,255,.28);
    border-radius: var(--r-pill);
    padding: .3rem .8rem;
    margin-bottom: var(--space-4);
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
}
.hero h1 {
    color: var(--brand-fg) !important;
    font-size: var(--fs-display);
    font-weight: 600;
    line-height: 1.12;
    letter-spacing: var(--track-tight);
    margin: 0 0 var(--space-3);
    max-width: 22em;
}
.hero p {
    color: rgba(255,255,255,.95) !important;
    font-size: var(--fs-sm);
    line-height: 1.6;
    margin: 0;
    max-width: 34em;
}

/* ---------- 4. 卡片族：白底 + 细边框 + 极轻阴影 ---------- */
.metric-row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: var(--space-3);
    margin: var(--space-2) 0 var(--space-5);
}
.metric-card {
    background: var(--card-bg);
    border: 1px solid var(--card-border);
    border-radius: var(--r-lg);
    padding: var(--space-4);
    box-shadow: var(--card-shadow);
}
.metric-card .label {
    font-size: var(--fs-xs);
    font-weight: 500;
    color: var(--muted-2);
    margin-bottom: .35rem;
}
.metric-card .value {
    font-size: 1.75rem;
    font-weight: 600;
    letter-spacing: var(--track-snug);
    line-height: 1.15;
    color: var(--ink);
}
.metric-card .value.warn { color: var(--warn-strong); }
.metric-card .value.danger { color: var(--bad-strong); }
.metric-card .value.ok { color: var(--ok-strong); }

.settings-card {
    background: var(--card-bg);
    border: 1px solid var(--card-border);
    border-radius: var(--r-lg);
    padding: var(--space-4);
    margin: var(--space-2) 0 var(--space-4);
    box-shadow: var(--card-shadow);
}
.settings-card .k { font-size: var(--fs-xs); color: var(--muted-2); }
.settings-hint {
    font-size: var(--fs-xs);
    color: var(--muted-2);
    line-height: var(--lh-snug);
}

/* ---------- 5. 提示条 ---------- */
.notice {
    border-radius: var(--r-md);
    padding: .85rem 1.1rem;
    font-size: var(--fs-sm);
    line-height: 1.65;
    margin: var(--space-3) 0 var(--space-4);
    border: 1px solid var(--card-border);
    border-left-width: 3px;
    border-left-style: solid;
}
.notice-info { background: var(--notice-info-bg); border-left-color: var(--brand-3); color: var(--notice-info-fg); }
.notice-warn { background: var(--notice-warn-bg); border-left-color: var(--warn-strong); color: var(--notice-warn-fg); }
.notice-ok   { background: var(--notice-ok-bg); border-left-color: var(--ok-strong); color: var(--notice-ok-fg); }
.notice-bad  { background: var(--notice-bad-bg); border-left-color: var(--bad-strong); color: var(--notice-bad-fg); }

/* ---------- 6. 标签 ---------- */
.chip {
    display: inline-block;
    font-size: var(--fs-2xs);
    font-weight: 500;
    line-height: 1.5;
    border-radius: var(--r-pill);
    padding: .18rem .6rem;
    margin-right: .4rem;
    margin-bottom: .3rem;
}
.chip-ok   { background: var(--chip-ok-bg);   color: var(--chip-ok-fg);   border: 1px solid var(--chip-ok-bd); }
.chip-warn { background: var(--chip-warn-bg); color: var(--chip-warn-fg); border: 1px solid var(--chip-warn-bd); }
.chip-bad  { background: var(--chip-bad-bg);  color: var(--chip-bad-fg);  border: 1px solid var(--chip-bad-bd); }
.chip-info { background: var(--chip-info-bg); color: var(--chip-info-fg); border: 1px solid var(--chip-info-bd); }

/* ---------- 7. 日历 ---------- */
.cal-title {
    text-align: center;
    font-size: var(--fs-h3);
    font-weight: 600;
    letter-spacing: var(--track-snug);
    color: var(--ink);
    line-height: 2.1;
}
.cal-title span {
    font-size: var(--fs-2xs);
    font-weight: 500;
    color: var(--muted-2);
    margin-left: .4rem;
    letter-spacing: 0;
}
.cal-wd {
    text-align: center;
    font-size: var(--fs-2xs);
    font-weight: 600;
    color: var(--muted-2);
    letter-spacing: .06em;
    padding: .25rem 0 .35rem;
}
.cal-side {
    background: var(--card-bg);
    border: 1px solid var(--card-border);
    border-radius: var(--r-lg);
    padding: var(--space-4) var(--space-5);
    box-shadow: var(--card-shadow);
    min-height: 100%;
}
.cal-side h4 {
    margin: 0 0 var(--space-3);
    font-size: var(--fs-h3);
    font-weight: 600;
    letter-spacing: var(--track-snug);
    color: var(--ink);
}
.cal-ev {
    display: flex;
    gap: .7rem;
    align-items: flex-start;
    padding: .6rem .75rem;
    margin-bottom: .5rem;
    border-radius: var(--r-md);
    background: var(--cal-ev-bg);
    border: 1px solid var(--cal-ev-border);
}
.cal-ev .t {
    font-size: var(--fs-2xs);
    font-weight: 600;
    color: var(--cal-time-fg);
    min-width: 2.6rem;
    font-variant-numeric: tabular-nums;
}
.cal-ev .m {
    font-size: var(--fs-sm);
    color: var(--ink);
    font-weight: 500;
    line-height: 1.45;
}
.cal-ev .s { font-size: var(--fs-2xs); color: var(--muted-2); margin-top: .15rem; }
.cal-ev.bad { background: var(--cal-bad-bg); border-color: var(--cal-bad-bd); }
.cal-ev.bad .t { color: var(--bad-strong); }
.cal-ev.vague { background: var(--cal-vague-bg); border-color: var(--cal-vague-bd); }
.cal-ev.vague .t { color: var(--warn-strong); }
.cal-empty { font-size: var(--fs-xs); color: var(--muted-3); padding: var(--space-3) 0; }

/* 日历格子：42 个按钮压成等宽方块，选中态用品牌色填充。
   注意：st.button 带 help 时外面会多包一层 span，`.stButton > button` 匹配不到，
   所以按钮统一按 data-testid 选。 */
.st-key-cal_grid [data-testid="stBaseButton-secondary"],
.st-key-cal_grid [data-testid="stBaseButton-primary"] {
    min-height: 2.4rem;
    height: 2.4rem;
    padding: 0 !important;
    border-radius: var(--r-md) !important;
    font-size: var(--fs-2xs) !important;
    font-weight: 500;
    font-variant-numeric: tabular-nums;
    justify-content: center;
    box-shadow: none !important;
}
/* 「13·」这类两位数会被省略号截断，放开内层裁剪 */
.st-key-cal_grid [data-testid^="stBaseButton"] div,
.st-key-cal_grid [data-testid^="stBaseButton"] span,
.st-key-cal_grid [data-testid^="stBaseButton"] p {
    overflow: visible !important;
    text-overflow: clip !important;
    white-space: nowrap !important;
    padding: 0 !important;
    margin: 0 !important;
    justify-content: center;
    line-height: 1 !important;
}
.st-key-cal_grid [data-testid="stBaseButton-primary"] {
    background: var(--cal-today-bg) !important;
    color: var(--cal-today-fg) !important;
    padding: 0 !important;
}
.st-key-cal_grid [data-testid="stBaseButton-primary"] * { color: var(--cal-today-fg) !important; }
/* 翻月按钮做成小圆钮，不抢视线（按 key 精确命中，别用 :first-child，
   否则日历每一行都是各自父元素的 first-child，会把所有日子一起改成药丸） */
.st-key-cal_prev [data-testid^="stBaseButton"],
.st-key-cal_next [data-testid^="stBaseButton"] {
    border-radius: var(--r-pill) !important;
    min-height: 2rem;
    height: 2rem;
}
.st-key-cal_grid [data-testid^="stBaseButton"]:hover { filter: brightness(1.06); }

/* ---------- 8. 免责声明与页脚 ---------- */
.disclaimer {
    margin-top: var(--space-7);
    padding: var(--space-5);
    border-radius: var(--r-lg);
    background: var(--disc-bg);
    border: 1px solid var(--disc-border);
    color: var(--disc-text);
    font-size: var(--fs-xs);
    line-height: 1.85;
}
.disclaimer .title {
    font-size: var(--fs-sm);
    font-weight: 600;
    color: var(--ink-2);
    margin-bottom: .5rem;
}
.disclaimer strong { color: var(--ink-2); }

.app-footer {
    margin: var(--space-6) 0 var(--space-5);
    padding-top: var(--space-5);
    border-top: 1px solid var(--line);
    text-align: center;
    color: var(--muted-2);
    font-size: var(--fs-2xs);
    line-height: 2;
}
.app-footer .fv { font-weight: 600; color: var(--ink-3); letter-spacing: .01em; }
.app-footer .fsep { color: var(--muted-3); margin: 0 .5rem; }
.app-footer .fcredit { color: var(--muted-2); }
.app-footer .fcredit b { font-weight: 600; color: var(--ink-3); }
.app-footer .fmodel {
    display: inline-block;
    padding: .1rem .55rem;
    border-radius: var(--r-pill);
    background: var(--chip-info-bg);
    color: var(--chip-info-fg);
    border: 1px solid var(--chip-info-bd);
    font-weight: 600;
}

/* ---------- 9. 按钮：胶囊 + 轻微变亮 ---------- */
.stButton > button,
.stDownloadButton > button,
[data-testid="stBaseButton-secondary"],
[data-testid="stBaseButton-secondaryFormSubmit"],
[data-testid="stBaseButton-primary"] {
    border-radius: var(--r-pill) !important;
    min-height: 2.5rem;
    padding: .4rem 1.1rem;
    font-size: var(--fs-sm);
    font-weight: 500;
    letter-spacing: 0;
    transition: background var(--dur-fast) var(--ease-out),
                border-color var(--dur-fast) var(--ease-out),
                color var(--dur-fast) var(--ease-out),
                box-shadow var(--dur-fast) var(--ease-out),
                transform var(--dur-fast) var(--ease-out);
}
.stButton > button:hover, .stDownloadButton > button:hover,
[data-testid="stBaseButton-secondary"]:hover,
[data-testid="stBaseButton-primary"]:hover { transform: none; }

/* 次要按钮：浅底 + 细边框，两种主题都由令牌决定 */
[data-testid="stBaseButton-secondary"],
[data-testid="stBaseButton-secondaryFormSubmit"],
[data-testid="stBaseButton-secondaryFormSubmit"] {
    background: var(--btn-bg) !important;
    border: 1px solid var(--btn-border) !important;
    color: var(--btn-text) !important;
    box-shadow: var(--btn-shadow);
}
[data-testid="stBaseButton-secondary"]:hover,
[data-testid="stBaseButton-secondaryFormSubmit"]:hover {
    background: var(--btn-hover-bg) !important;
    border-color: var(--btn-hover-border) !important;
    color: var(--btn-hover-text) !important;
    box-shadow: var(--btn-hover-shadow);
}

/* 主按钮：品牌渐变，hover 只做轻微变亮（不位移、不发光） */
[data-testid="stBaseButton-primary"] {
    background: var(--brand-grad) !important;
    border: 1px solid transparent !important;
    color: var(--brand-fg) !important;
    font-weight: 600;
    box-shadow: 0 1px 2px var(--brand-glow);
}
[data-testid="stBaseButton-primary"]:hover {
    filter: brightness(1.06);
    box-shadow: 0 2px 10px var(--brand-glow-hover);
}

/* 主按钮 / 选中 Tab 内部的 markdown 段落会被 `.stApp p` 的正文色带走，
   必须把整棵子树钉成品牌前景色，否则浅色主题下是"深字压粉底"。 */
[data-testid="stBaseButton-primary"], [data-testid="stBaseButton-primary"] *,
.stTabs [aria-selected="true"], .stTabs [aria-selected="true"] * {
    color: var(--brand-fg) !important;
    -webkit-text-fill-color: var(--brand-fg) !important;
}

/* ---------- 10. 输入框 ---------- */
.stTextArea textarea, .stTextInput input, .stNumberInput input, .stDateInput input {
    background: var(--input-bg) !important;
    border: 1px solid var(--input-border) !important;
    border-radius: var(--r-md) !important;
    color: var(--input-text) !important;
    font-size: var(--fs-sm) !important;
    line-height: 1.6 !important;
    box-shadow: none;
    transition: border-color var(--dur-fast) var(--ease-out),
                box-shadow var(--dur-fast) var(--ease-out);
}
.stTextArea textarea { padding: .7rem .85rem !important; }
.stTextArea textarea::placeholder, .stTextInput input::placeholder {
    color: var(--field-ph) !important;
}
.stTextArea textarea:focus, .stTextInput input:focus,
.stNumberInput input:focus, .stDateInput input:focus {
    border-color: var(--focus-bd) !important;
    box-shadow: 0 0 0 3px var(--focus-ring) !important;
    outline: none !important;
}

/* ---------- 11. Tab：胶囊分段控件 ---------- */
.stTabs [data-baseweb="tab-list"], .stTabs [role="tablist"] {
    gap: .35rem;
    border-bottom: none;
    padding-bottom: .5rem;
    flex-wrap: nowrap;
    overflow-x: auto;
    scrollbar-width: none;
}
.stTabs [data-baseweb="tab-list"]::-webkit-scrollbar,
.stTabs [role="tablist"]::-webkit-scrollbar { display: none; }
.stTabs [data-testid="stTab"], .stTabs [data-baseweb="tab"] {
    height: 2.5rem !important;
    padding: 0 1.05rem !important;
    min-width: max-content !important;
    border-radius: var(--r-pill) !important;
    font-size: var(--fs-sm);
    font-weight: 500;
    color: var(--tab-text) !important;
    background: var(--tab-bg);
    border: 1px solid var(--tab-border);
    transition: background var(--dur-fast) var(--ease-out),
                color var(--dur-fast) var(--ease-out);
}
.stTabs [data-testid="stTab"]:hover, .stTabs [data-baseweb="tab"]:hover {
    color: var(--ink) !important;
}
.stTabs [aria-selected="true"] {
    background: var(--brand-grad) !important;
    border-color: transparent !important;
    font-weight: 600;
    box-shadow: 0 1px 2px var(--brand-glow);
}
.stTabs [data-baseweb="tab-highlight"], .stTabs [data-baseweb="tab-border"],
.stTabs [role="tablist"] > div:not([role="tab"]),
.stTabs [role="tablist"]::after { display: none !important; }

/* ---------- 12. 侧边栏（唯一保留毛玻璃的地方） ---------- */
[data-testid="stSidebar"] {
    background: var(--sidebar-bg);
    border-right: 1px solid var(--sidebar-border);
    backdrop-filter: blur(24px) saturate(160%);
    -webkit-backdrop-filter: blur(24px) saturate(160%);
}
[data-testid="stSidebar"] .stMarkdown h3 {
    font-size: var(--fs-sm);
    font-weight: 600;
    color: var(--ink-3) !important;
    margin-bottom: .3rem;
}
[data-testid="stSidebar"] .stMarkdown h4 {
    font-size: var(--fs-sm);
    font-weight: 600;
    color: var(--ink-3) !important;
}
/* 侧边栏内部节奏：一组控件之间留 10px，分隔线上下各留 16px */
[data-testid="stSidebar"] [data-testid="stVerticalBlock"] { gap: .62rem; }
[data-testid="stSidebar"] hr { margin: var(--space-4) 0 !important; }
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] {
    line-height: 1.6;
}
[data-testid="stSidebar"] [data-testid^="stBaseButton"] {
    min-height: 2.25rem;
    font-size: var(--fs-xs);
}

/* ---------- 13. 动效：一次性淡入上移，尊重系统"减少动态效果" ---------- */
@keyframes riseIn {
    from { opacity: 0; transform: translateY(8px); }
    to   { opacity: 1; transform: none; }
}
@media (prefers-reduced-motion: no-preference) {
    .hero { animation: riseIn var(--dur-slow) var(--ease-out) both; }
    .section-title { animation: riseIn var(--dur-base) var(--ease-out) both; }
}
@media (prefers-reduced-motion: reduce) {
    .stApp *, .stApp *::before, .stApp *::after {
        animation-duration: .001ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: .001ms !important;
    }
}

/* ---------- 14. 响应式 ---------- */
@media (max-width: 880px) {
    .block-container { padding: var(--space-5) var(--space-4) var(--space-7); }
    .hero { padding: 1.6rem 1.4rem 1.5rem; border-radius: var(--r-lg); }
    .section-title { margin-top: var(--space-6); font-size: 1.1875rem; }
    .metric-row { grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); }
}
@media (max-width: 640px) {
    .block-container { padding: var(--space-4) var(--space-3) var(--space-6); }
    /* 窄屏不横向滚动，直接折成两行，四个入口都看得见 */
    .stTabs [data-baseweb="tab-list"], .stTabs [role="tablist"] {
        flex-wrap: wrap;
        overflow-x: visible;
        row-gap: .4rem;
    }
    .stTabs [data-testid="stTab"], .stTabs [data-baseweb="tab"] {
        flex: 1 1 calc(50% - .4rem);
        justify-content: center;
        padding: 0 .45rem !important;
        font-size: var(--fs-xs);
    }
    .hero h1 { font-size: 1.625rem; }
    .hero p { font-size: var(--fs-xs); }
    .section-title { font-size: 1.125rem; margin: var(--space-5) 0 var(--space-3); }
    .cal-side { padding: var(--space-4); }
    .disclaimer { padding: var(--space-4); }
}
</style>
"""


# ---------------------------------------------------------------------------
# 主题令牌：明 / 暗两套 CSS 变量
# ---------------------------------------------------------------------------

THEME_LIGHT = "浅色"
THEME_DARK = "深色"
THEME_OPTIONS = [THEME_LIGHT, THEME_DARK]

FONT_SMALL = "小"
FONT_NORMAL = "标准"
FONT_LARGE = "大"
FONT_HUGE = "特大"
FONT_OPTIONS = [FONT_SMALL, FONT_NORMAL, FONT_LARGE, FONT_HUGE]
FONT_SCALES = {FONT_SMALL: 0.92, FONT_NORMAL: 1.0, FONT_LARGE: 1.08, FONT_HUGE: 1.16}

_TOKENS_COMMON = {
    # ---------- 字体 ----------
    # 只用系统字体，不打包任何专有字体；中文单独列一组回退
    "font-sans": ("-apple-system, BlinkMacSystemFont, \"SF Pro Display\", \"SF Pro Text\", "
                  "\"Segoe UI\", Roboto, \"Helvetica Neue\", \"PingFang SC\", "
                  "\"Hiragino Sans GB\", \"Microsoft YaHei\", \"Source Han Sans SC\", "
                  "Arial, sans-serif"),
    "font-mono": ("ui-monospace, SFMono-Regular, \"SF Mono\", Menlo, Consolas, "
                  "\"Liberation Mono\", monospace"),
    # ---------- 字号阶梯（7 级，够用且不打架） ----------
    "fs-display": "clamp(1.75rem, 1.05rem + 2.1vw, 2.5rem)",   # 首屏主标题 28→40
    "fs-h1": "1.625rem",                                        # 26
    "fs-h2": "1.3125rem",                                       # 21 区块标题
    "fs-h3": "1.0625rem",                                       # 17 卡片标题
    "fs-body": "1rem",                                          # 16 正文
    "fs-sm": "0.9375rem",                                       # 15 控件 / 次要正文
    "fs-xs": "0.8125rem",                                       # 13 说明文字
    "fs-2xs": "0.75rem",                                        # 12 标签 / 徽标
    # ---------- 行高与字距（中西文分开） ----------
    "lh-tight": "1.2",
    "lh-snug": "1.45",
    "lh-body": "1.75",                                          # 中文正文舒适行高
    "track-tight": "-0.02em",                                   # 大标题收紧
    "track-snug": "-0.01em",
    "track-wide": "0.14em",                                     # 全大写小标签放开
    # ---------- 间距尺度 ----------
    "space-1": "0.25rem",
    "space-2": "0.5rem",
    "space-3": "0.75rem",
    "space-4": "1rem",
    "space-5": "1.5rem",
    "space-6": "2rem",
    "space-7": "2.75rem",
    "space-8": "3.5rem",
    # ---------- 圆角 ----------
    "r-sm": "8px",
    "r-md": "12px",
    "r-lg": "18px",
    "r-xl": "26px",
    "r-pill": "999px",
    # ---------- 动效 ----------
    "dur-fast": "150ms",
    "dur-base": "320ms",
    "dur-slow": "560ms",
    "ease-out": "cubic-bezier(.22,.61,.36,1)",
    # ---------- 版心 ----------
    "content-max": "1080px",
}

_TOKENS_LIGHT = {
    **_TOKENS_COMMON,
    # ---------- 底色与文字（中性灰白体系，品牌色只做强调） ----------
    "app-bg": (
        "radial-gradient(1100px 560px at 50% -18%, rgba(192,42,102,.06), transparent 62%),"
        "linear-gradient(180deg, #F8F7FA 0%, #F5F5F7 44%, #F3F3F5 100%)"
    ),
    "body-bg": "#F5F5F7",
    "img-brightness": "1",
    "ink": "#1D1D1F",
    "ink-2": "#333338",
    "ink-3": "#48484E",
    "muted": "#5F5F66",
    "muted-2": "#6E6E73",
    "muted-3": "#7A7A80",
    "line": "rgba(0,0,0,.08)",

    # ---------- 品牌色（渐变四档，白字全部 ≥4.5:1） ----------
    "brand-1": "#BF5433",
    "brand-2": "#C02A66",
    "brand-3": "#8E3FBF",
    "brand-4": "#5B62E0",
    "brand-grad": "linear-gradient(118deg,#BF5433 0%,#C02A66 34%,#8E3FBF 68%,#5B62E0 100%)",
    "brand-fg": "#FFFFFF",
    "brand-glow": "rgba(192,42,102,.28)",
    "brand-glow-hover": "rgba(192,42,102,.38)",
    "accent": "#B01F5A",
    "accent-hover": "#8C1747",
    "logo": "#8E3D6B",

    # ---------- 面 / 卡片（白卡片 + 细边框 + 极轻阴影） ----------
    "surface-1": "#FFFFFF",
    "surface-2": "#FFFFFF",
    "card-bg": "#FFFFFF",
    "card-bg-3": "#FFFFFF",
    "card-border": "rgba(0,0,0,.07)",
    "card-border-2": "rgba(0,0,0,.09)",
    "card-shadow": "0 1px 2px rgba(0,0,0,.04), 0 6px 20px rgba(0,0,0,.045)",
    "card-shadow-lg": "0 1px 2px rgba(0,0,0,.04), 0 14px 36px rgba(0,0,0,.07)",
    "hero-shadow": "0 14px 36px rgba(140,25,75,.20)",

    # ---------- 表单控件 ----------
    "input-bg": "#FFFFFF",
    "input-border": "rgba(0,0,0,.10)",
    "input-text": "#1D1D1F",
    "input-shadow": "0 1px 2px rgba(0,0,0,.03)",
    "field-bg": "#FFFFFF",
    "field-bd": "rgba(0,0,0,.12)",
    "field-bd-hover": "rgba(0,0,0,.22)",
    "field-ph": "#86868B",
    "field-dis-fg": "#9A9AA0",
    "field-dis-bg": "rgba(0,0,0,.035)",
    "focus-ring": "rgba(192,42,102,.20)",
    "focus-bd": "#C02A66",

    # ---------- 按钮 ----------
    "btn-bg": "rgba(0,0,0,.045)",
    "btn-border": "rgba(0,0,0,.08)",
    "btn-text": "#1D1D1F",
    "btn-hover-bg": "rgba(0,0,0,.075)",
    "btn-hover-border": "rgba(0,0,0,.12)",
    "btn-hover-text": "#000000",
    "btn-shadow": "0 1px 2px rgba(0,0,0,.04)",
    "btn-hover-shadow": "0 2px 8px rgba(0,0,0,.07)",
    "btn-dis-fg": "#9A9AA0",

    # ---------- Tab ----------
    "tab-bg": "rgba(0,0,0,.035)",
    "tab-border": "rgba(0,0,0,.05)",
    "tab-text": "#5F5F66",

    # ---------- 侧边栏（唯一保留毛玻璃的地方） ----------
    "sidebar-bg": "rgba(250,250,252,.78)",
    "sidebar-border": "rgba(0,0,0,.06)",

    # ---------- 语义色 ----------
    "chip-ok-bg": "rgba(43,168,124,.10)",
    "chip-ok-fg": "#0A6B54",
    "chip-ok-bd": "rgba(43,168,124,.22)",
    "chip-warn-bg": "rgba(206,132,20,.10)",
    "chip-warn-fg": "#7E5208",
    "chip-warn-bd": "rgba(206,132,20,.22)",
    "chip-bad-bg": "rgba(206,42,78,.10)",
    "chip-bad-fg": "#9E1F3A",
    "chip-bad-bd": "rgba(206,42,78,.22)",
    "chip-info-bg": "rgba(120,90,220,.10)",
    "chip-info-fg": "#4B33AE",
    "chip-info-bd": "rgba(120,90,220,.22)",

    "notice-info-bg": "rgba(120,90,220,.055)",
    "notice-info-fg": "#412F84",
    "notice-warn-bg": "rgba(206,132,20,.06)",
    "notice-warn-fg": "#6F4807",
    "notice-ok-bg": "rgba(43,168,124,.06)",
    "notice-ok-fg": "#085C45",
    "notice-bad-bg": "rgba(206,42,78,.06)",
    "notice-bad-fg": "#861B33",

    "warn-strong": "#7E5208",
    "bad-strong": "#B0233F",
    "ok-strong": "#0A6B54",

    # ---------- 日历 ----------
    "cal-ev-bg": "rgba(0,0,0,.025)",
    "cal-ev-border": "rgba(0,0,0,.05)",
    "cal-bad-bg": "rgba(206,42,78,.06)",
    "cal-bad-bd": "rgba(206,42,78,.16)",
    "cal-vague-bg": "rgba(206,132,20,.07)",
    "cal-vague-bd": "rgba(206,132,20,.18)",
    "cal-time-fg": "#8E3FBF",
    "cal-day-fg": "#333338",
    "cal-day-muted": "#6E6E73",
    "cal-today-bg": "#C02A66",
    "cal-today-fg": "#FFFFFF",
    "cal-day-hover": "rgba(0,0,0,.05)",

    # ---------- 数据表行底色（Python 侧喂给 data_editor） ----------
    "row-bad-bg": "#FCEEF1",
    "row-bad-fg": "#9E1F3A",
    "row-warn-bg": "#FCF5E6",
    "row-warn-fg": "#7E5208",

    # ---------- 弹层 / 气泡 / Toast / 代码 ----------
    "pop-bg": "#FFFFFF",
    "pop-bd": "rgba(0,0,0,.08)",
    "pop-shadow": "0 10px 34px rgba(0,0,0,.14)",
    "pop-item-hover": "rgba(0,0,0,.05)",
    "tip-bg": "#FFFFFF",
    "tip-fg": "#1D1D1F",
    "tip-bd": "rgba(0,0,0,.08)",
    "toast-bg": "#FFFFFF",
    "toast-fg": "#1D1D1F",
    "toast-bd": "rgba(0,0,0,.08)",
    "code-bg": "rgba(0,0,0,.045)",
    "code-fg": "#3A2F55",
    "code-bd": "rgba(0,0,0,.06)",
    "code-kw": "#8E3FBF",
    "code-str": "#086B52",
    "code-num": "#A8420F",

    # ---------- 表格外壳 / 选中 / 滚动条 ----------
    "grid-bg": "#FFFFFF",
    "grid-bd": "rgba(0,0,0,.08)",
    "grid-head-fg": "#48484E",
    "grid-toolbar": "rgba(255,255,255,.94)",
    "sel-bg": "rgba(192,42,102,.18)",
    "sel-fg": "#1D1D1F",
    "scroll-thumb": "rgba(0,0,0,.20)",
    "scroll-thumb-hover": "rgba(0,0,0,.34)",

    # ---------- 杂项 ----------
    "disc-bg": "#FFFFFF",
    "disc-border": "rgba(0,0,0,.07)",
    "disc-text": "#5F5F66",
    "stepper-bg": "rgba(192,42,102,.08)",
    "stepper-bd": "rgba(192,42,102,.18)",
    "stepper-fg": "#B01F5A",
}

_TOKENS_DARK = {
    **_TOKENS_COMMON,
    # ---------- 底色与文字 ----------
    "app-bg": (
        "radial-gradient(1100px 560px at 50% -18%, rgba(192,42,102,.10), transparent 62%),"
        "linear-gradient(180deg, #131315 0%, #0E0E10 44%, #0B0B0D 100%)"
    ),
    "body-bg": "#0B0B0D",
    "img-brightness": ".9",
    "ink": "#F5F5F7",
    "ink-2": "#DCDCE1",
    "ink-3": "#C5C5CB",
    "muted": "#A1A1A6",
    "muted-2": "#9A9AA0",
    "muted-3": "#8E8E94",
    "line": "rgba(255,255,255,.10)",

    # ---------- 品牌色 ----------
    "brand-1": "#BF5433",
    "brand-2": "#C02A66",
    "brand-3": "#8E3FBF",
    "brand-4": "#5B62E0",
    "brand-grad": "linear-gradient(118deg,#BF5433 0%,#C02A66 34%,#8E3FBF 68%,#5B62E0 100%)",
    "brand-fg": "#FFFFFF",
    "brand-glow": "rgba(192,42,102,.34)",
    "brand-glow-hover": "rgba(192,42,102,.46)",
    "accent": "#F075A6",
    "accent-hover": "#FF9CC2",
    "logo": "#FFB3D9",

    # ---------- 面 / 卡片 ----------
    "surface-1": "#1B1B1E",
    "surface-2": "#202024",
    "card-bg": "#1B1B1E",
    "card-bg-3": "#18181B",
    "card-border": "rgba(255,255,255,.08)",
    "card-border-2": "rgba(255,255,255,.10)",
    "card-shadow": "0 1px 2px rgba(0,0,0,.5)",
    "card-shadow-lg": "0 12px 32px rgba(0,0,0,.5)",
    "hero-shadow": "0 14px 36px rgba(0,0,0,.55)",

    # ---------- 表单控件 ----------
    "input-bg": "rgba(255,255,255,.06)",
    "input-border": "rgba(255,255,255,.12)",
    "input-text": "#F5F5F7",
    "input-shadow": "0 1px 2px rgba(0,0,0,.3)",
    "field-bg": "rgba(255,255,255,.06)",
    "field-bd": "rgba(255,255,255,.14)",
    "field-bd-hover": "rgba(255,255,255,.24)",
    "field-ph": "#8E8E94",
    "field-dis-fg": "#6E6E73",
    "field-dis-bg": "rgba(255,255,255,.03)",
    "focus-ring": "rgba(240,117,166,.26)",
    "focus-bd": "#F075A6",

    # ---------- 按钮 ----------
    "btn-bg": "rgba(255,255,255,.08)",
    "btn-border": "rgba(255,255,255,.12)",
    "btn-text": "#F5F5F7",
    "btn-hover-bg": "rgba(255,255,255,.14)",
    "btn-hover-border": "rgba(255,255,255,.20)",
    "btn-hover-text": "#FFFFFF",
    "btn-shadow": "0 1px 2px rgba(0,0,0,.3)",
    "btn-hover-shadow": "0 2px 10px rgba(0,0,0,.35)",
    "btn-dis-fg": "#6E6E73",

    # ---------- Tab ----------
    "tab-bg": "rgba(255,255,255,.06)",
    "tab-border": "rgba(255,255,255,.08)",
    "tab-text": "#A1A1A6",

    # ---------- 侧边栏 ----------
    "sidebar-bg": "rgba(20,20,23,.72)",
    "sidebar-border": "rgba(255,255,255,.07)",

    # ---------- 语义色 ----------
    "chip-ok-bg": "rgba(63,196,150,.14)",
    "chip-ok-fg": "#7FE7C2",
    "chip-ok-bd": "rgba(63,196,150,.26)",
    "chip-warn-bg": "rgba(232,168,64,.14)",
    "chip-warn-fg": "#F5C97E",
    "chip-warn-bd": "rgba(232,168,64,.26)",
    "chip-bad-bg": "rgba(238,90,110,.14)",
    "chip-bad-fg": "#FF9DAE",
    "chip-bad-bd": "rgba(238,90,110,.26)",
    "chip-info-bg": "rgba(160,120,255,.16)",
    "chip-info-fg": "#CBA8FF",
    "chip-info-bd": "rgba(160,120,255,.30)",

    "notice-info-bg": "rgba(160,120,255,.08)",
    "notice-info-fg": "#D3C2FF",
    "notice-warn-bg": "rgba(232,168,64,.08)",
    "notice-warn-fg": "#F3CE8C",
    "notice-ok-bg": "rgba(63,196,150,.08)",
    "notice-ok-fg": "#8FE8C7",
    "notice-bad-bg": "rgba(238,90,110,.08)",
    "notice-bad-fg": "#FFA8B7",

    "warn-strong": "#F0B45C",
    "bad-strong": "#FF7E96",
    "ok-strong": "#4FD3A5",

    # ---------- 日历 ----------
    "cal-ev-bg": "rgba(255,255,255,.05)",
    "cal-ev-border": "rgba(255,255,255,.08)",
    "cal-bad-bg": "rgba(238,90,110,.10)",
    "cal-bad-bd": "rgba(238,90,110,.22)",
    "cal-vague-bg": "rgba(232,168,64,.10)",
    "cal-vague-bd": "rgba(232,168,64,.22)",
    "cal-time-fg": "#CBA8FF",
    "cal-day-fg": "#DCDCE1",
    "cal-day-muted": "#9A9AA0",
    "cal-today-bg": "#C02A66",
    "cal-today-fg": "#FFFFFF",
    "cal-day-hover": "rgba(255,255,255,.08)",

    # ---------- 数据表行底色（会被 canvas 滤镜翻成深色） ----------
    "row-bad-bg": "#FCEEF1",
    "row-bad-fg": "#9E1F3A",
    "row-warn-bg": "#FCF5E6",
    "row-warn-fg": "#7E5208",

    # ---------- 弹层 / 气泡 / Toast / 代码 ----------
    "pop-bg": "#202024",
    "pop-bd": "rgba(255,255,255,.10)",
    "pop-shadow": "0 12px 36px rgba(0,0,0,.6)",
    "pop-item-hover": "rgba(255,255,255,.08)",
    "tip-bg": "#2A2A2E",
    "tip-fg": "#F5F5F7",
    "tip-bd": "rgba(255,255,255,.12)",
    "toast-bg": "#2A2A2E",
    "toast-fg": "#F5F5F7",
    "toast-bd": "rgba(255,255,255,.12)",
    "code-bg": "rgba(255,255,255,.06)",
    "code-fg": "#E7E1F6",
    "code-bd": "rgba(255,255,255,.08)",
    "code-kw": "#D9B4FF",
    "code-str": "#8FE8C7",
    "code-num": "#F5C97E",

    # ---------- 表格外壳 / 选中 / 滚动条 ----------
    "grid-bg": "#131315",
    "grid-bd": "rgba(255,255,255,.10)",
    "grid-head-fg": "#C5C5CB",
    "grid-toolbar": "rgba(32,32,36,.94)",
    "sel-bg": "rgba(240,117,166,.32)",
    "sel-fg": "#FFF3FA",
    "scroll-thumb": "rgba(255,255,255,.20)",
    "scroll-thumb-hover": "rgba(255,255,255,.34)",

    # ---------- 杂项 ----------
    "disc-bg": "#18181B",
    "disc-border": "rgba(255,255,255,.08)",
    "disc-text": "#A1A1A6",
    "stepper-bg": "rgba(238,90,146,.14)",
    "stepper-bd": "rgba(238,90,146,.28)",
    "stepper-fg": "#FF8FB4",
}


# ---------------------------------------------------------------------------
_CSS_WIDGETS = """
<style>
/* ---------- 全局：选中文本、原生控件配色、滚动条 ----------
   color-scheme 由 _css_tokens() 按主题写入 :root，这里不要再覆盖，
   否则深色模式下浏览器原生滚动条 / 日期面板仍是亮色。 */
::selection { background: var(--sel-bg) !important; color: var(--sel-fg) !important; }
* { scrollbar-color: var(--scroll-thumb) transparent; scrollbar-width: thin; }
*::-webkit-scrollbar { width: 10px; height: 10px; }
*::-webkit-scrollbar-track { background: transparent; }
*::-webkit-scrollbar-thumb {
    background: var(--scroll-thumb); border-radius: 999px;
    border: 2px solid transparent; background-clip: content-box;
}
*::-webkit-scrollbar-thumb:hover { background: var(--scroll-thumb-hover); background-clip: content-box; }

/* 浏览器自动填充：默认会给白底，深色下会闪一块白 */
input:-webkit-autofill, input:-webkit-autofill:hover, input:-webkit-autofill:focus,
textarea:-webkit-autofill, select:-webkit-autofill {
    -webkit-text-fill-color: var(--input-text) !important;
    -webkit-box-shadow: 0 0 0 1000px var(--field-bg) inset !important;
    caret-color: var(--input-text);
}

/* Streamlit 的无障碍跳转链接默认是浏览器原生的链接蓝，深色下几乎看不见 */
[data-testid="stSkipToContent"],
[data-testid="stSkipToContent"] a,
[data-testid="stSkipToContent"] a:visited {
    background: var(--surface-1) !important; color: var(--accent) !important;
    border: 1px solid var(--pop-bd); border-radius: var(--r-sm); padding: 6px 12px;
}
[data-testid="stSkipToContent"] * { color: var(--accent) !important; }

/* 链接：正文里的 a / :visited 用强调色，保证两种主题都可读 */
.stApp [data-testid="stMarkdownContainer"] a,
.stApp [data-testid="stMarkdownContainer"] a:visited { color: var(--accent) !important; }
.stApp [data-testid="stMarkdownContainer"] a:hover { color: var(--accent-hover) !important; }

/* ---------- 侧边栏 ----------
   Streamlit 会给侧边栏容器单独写死 config 里的 textColor（浅色主题是深灰），
   子元素里没有自己颜色的会继承它 —— 深色下就是"深底深字"。只改容器，
   不要用 `*` 通配，否则会把侧边栏里的彩色标签一起压成同色。 */
[data-testid="stSidebar"] { color: var(--ink) !important; }
[data-testid="stSidebar"] [data-testid="stCaptionContainer"],
[data-testid="stSidebar"] small { color: var(--muted) !important; }

/* ---------- 单选 / 复选 / 开关 ---------- */
[data-testid="stRadio"] label, [data-testid="stCheckbox"] label,
[data-testid="stToggle"] label { color: var(--ink) !important; }
[data-baseweb="radio"] div:first-child,
[data-baseweb="checkbox"] div:first-child {
    border-color: var(--field-bd) !important; background: transparent !important;
}
[data-baseweb="radio"] [aria-checked="true"] div:first-child,
[data-baseweb="checkbox"] [aria-checked="true"] div:first-child {
    border-color: var(--accent) !important;
}
[data-baseweb="radio"] [aria-checked="true"] div:first-child > div,
[data-baseweb="checkbox"] [aria-checked="true"] div:first-child > div {
    background: var(--accent) !important;
}
[data-testid="stToggle"] [data-baseweb="checkbox"] div[role="checkbox"] {
    background: var(--field-bd) !important;
}

/* ---------- 滑块 / 步进器 ---------- */
[data-testid="stSlider"] [role="slider"] { background: var(--accent) !important; }
[data-testid="stSlider"] [data-baseweb="slider"] > div > div > div:first-child {
    background: var(--accent) !important;
}
[data-testid="stSlider"] [data-testid="stSliderThumbValue"] {
    color: var(--brand-fg) !important;
    -webkit-text-fill-color: var(--brand-fg) !important;
}
[data-testid="stSlider"] [data-testid="stSliderTickBarMin"],
[data-testid="stSlider"] [data-testid="stSliderTickBarMax"] { color: var(--muted) !important; }

/* ---------- 下拉框（Streamlit 1.66 用 react-aria ComboBox） ----------
   它不是 <select> 也不是 BaseWeb，而是一个 div[role=group] 包着
   input[role=combobox]，底色写在 Emotion 类上，只能按 role 打补丁。 */
[data-testid="stSelectbox"] [role="group"],
[data-testid="stMultiSelect"] [role="group"],
.react-aria-ComboBox [role="group"] {
    background: var(--field-bg) !important;
    border: 1px solid var(--field-bd) !important;
    border-radius: var(--r-md) !important;
    color: var(--input-text) !important;
}
[data-testid="stSelectbox"] input,
[data-testid="stMultiSelect"] input {
    background: transparent !important;
    color: var(--input-text) !important;
    -webkit-text-fill-color: var(--input-text) !important;
}
[data-testid="stSelectbox"] input::placeholder,
[data-testid="stMultiSelect"] input::placeholder { color: var(--field-ph) !important; }
[data-testid="stSelectbox"] button,
[data-testid="stMultiSelect"] button { background: transparent !important; color: var(--muted) !important; }
[data-testid="stSelectbox"] svg,
[data-testid="stMultiSelect"] svg { fill: currentColor !important; }
[data-testid="stSelectbox"]:focus-within [role="group"],
[data-testid="stMultiSelect"]:focus-within [role="group"] {
    border-color: var(--focus-bd) !important;
    box-shadow: 0 0 0 4px var(--focus-ring) !important;
}

/* ---------- 原生表单控件 ----------
   Streamlit 1.66 的下拉框已经换成原生 <select>，靠 color-scheme 就能变暗；
   这里再兜一层，保证自绘背景/边框与页面一致。 */
.stApp select, .stApp [data-testid="stSelectbox"] select {
    background: var(--field-bg) !important;
    color: var(--input-text) !important;
    border: 1px solid var(--field-bd) !important;
    border-radius: var(--r-md) !important;
    padding: .35rem .6rem;
}
.stApp select option, .stApp [data-testid="stSelectbox"] option {
    background: var(--pop-bg); color: var(--ink);
}
.stApp input[type="range"] { accent-color: var(--accent); }
.stApp input[type="checkbox"], .stApp input[type="radio"] { accent-color: var(--accent); }

/* ---------- 下拉框（收起态） ---------- */
[data-baseweb="select"] > div { background: transparent !important; border: none !important; }
[data-baseweb="select"] svg { color: var(--muted) !important; fill: currentColor; }
[data-baseweb="select"] input { color: var(--input-text) !important; }

/* ---------- 下拉菜单 / 日期选择器 / 弹出层 ---------- */
[data-baseweb="popover"] > div,
[data-baseweb="popover"] [data-baseweb="menu"],
[data-baseweb="popover"] ul[role="listbox"],
[data-baseweb="menu"] ul,
[role="listbox"],
[data-baseweb="calendar"],
[data-baseweb="popover"] [data-baseweb="calendar"] > div {
    background: var(--pop-bg) !important;
    color: var(--ink) !important;
    border: 1px solid var(--pop-bd) !important;
    box-shadow: var(--pop-shadow) !important;
}
[role="option"], [data-baseweb="menu"] li { color: var(--ink) !important; }
[role="option"]:hover, [role="option"][aria-selected="true"],
[data-baseweb="menu"] li:hover {
    background: var(--pop-item-hover) !important; color: var(--ink) !important;
}
[data-baseweb="calendar"] * { color: var(--ink) !important; }
[data-baseweb="calendar"] button:hover { background: var(--pop-item-hover) !important; }
[data-baseweb="calendar"] [aria-selected="true"] {
    background: var(--accent) !important; color: var(--brand-fg) !important;
}

/* ---------- Toast ---------- */
[data-testid="stToast"] {
    background: var(--toast-bg) !important; color: var(--toast-fg) !important;
    border: 1px solid var(--toast-bd) !important;
    box-shadow: var(--pop-shadow) !important; border-radius: var(--r-md) !important;
}
[data-testid="stToast"] * { color: var(--toast-fg) !important; }
[data-testid="stToast"] svg { fill: currentColor !important; }

/* ---------- 悬停提示气泡 ---------- */
[data-testid="stTooltipContent"], [data-testid="stTooltipContent"] *,
[role="tooltip"], [role="tooltip"] * { color: var(--tip-fg) !important; }
[data-testid="stTooltipContent"], [role="tooltip"] {
    background: var(--tip-bg) !important;
    border: 1px solid var(--tip-bd) !important;
    box-shadow: var(--pop-shadow) !important;
}
[data-testid="stTooltipHoverTarget"] svg { color: var(--muted) !important; fill: currentColor; }

/* ---------- 提示条 st.success / info / warning / error ---------- */
[data-testid="stAlert"] {
    background: var(--notice-info-bg) !important;
    border: 1px solid var(--card-border) !important;
    border-radius: var(--r-md) !important;
}
[data-testid="stAlert"] [data-testid="stAlertContentInfo"],
[data-testid="stAlert"] [data-testid="stAlertContentSuccess"],
[data-testid="stAlert"] [data-testid="stAlertContentWarning"],
[data-testid="stAlert"] [data-testid="stAlertContentError"] { color: var(--ink) !important; }
[data-testid="stAlert"] p, [data-testid="stAlert"] li { color: var(--ink) !important; }
[data-testid="stAlert"] svg { fill: currentColor !important; }

/* ---------- 代码块与行内代码 ----------
   Streamlit 给行内 code 的底色是主题里的浅紫，深色模式下会变成一块亮斑，
   而且它是 <code> 自身的 background，必须单独覆盖。 */
.stApp :not(pre) > code {
    background: var(--code-bg) !important;
    color: var(--code-fg) !important;
    border: 1px solid var(--code-bd) !important;
    border-radius: var(--r-sm); padding: .08em .38em;
}
.stApp pre, .stApp [data-testid="stCode"] pre {
    background: var(--code-bg) !important;
    border: 1px solid var(--code-bd) !important;
    border-radius: var(--r-md) !important;
}
.stApp pre code { background: transparent !important; border: none !important; }
.stApp code, .stApp [data-testid="stCode"] code { color: var(--code-fg) !important; }
/* 语法高亮的 token 颜色来自 Streamlit 主题，逐个按令牌重设 */
.stApp [data-testid="stCode"] code span { color: var(--code-fg) !important; }
.stApp [data-testid="stCode"] .token.keyword,
.stApp [data-testid="stCode"] .token.operator,
.stApp [data-testid="stCode"] .token.builtin,
.stApp [data-testid="stCode"] .token.class-name { color: var(--code-kw) !important; }
.stApp [data-testid="stCode"] .token.string,
.stApp [data-testid="stCode"] .token.attr-value { color: var(--code-str) !important; }
.stApp [data-testid="stCode"] .token.number { color: var(--code-num) !important; }
.stApp [data-testid="stCode"] .token.comment,
.stApp [data-testid="stCode"] .token.punctuation { color: var(--muted-2) !important; }

/* ---------- 展开器 ---------- */
[data-testid="stExpander"] details {
    background: var(--card-bg-3) !important;
    border: 1px solid var(--card-border) !important;
    border-radius: var(--r-lg) !important;
}
[data-testid="stExpander"] summary {
    background: transparent !important;
    border-radius: var(--r-md);
}
[data-testid="stExpander"] summary:hover { background: var(--pop-item-hover) !important; }
[data-testid="stExpander"] summary,
[data-testid="stExpander"] summary * { color: var(--ink-2) !important; }
[data-testid="stExpander"] summary:hover,
[data-testid="stExpander"] summary:hover * { color: var(--accent) !important; }
[data-testid="stExpander"] summary svg { fill: currentColor !important; }
[data-testid="stExpander"] [data-testid="stExpanderDetails"] { color: var(--ink) !important; }

/* ---------- 上传组件 ---------- */
[data-testid="stFileUploaderDropzone"] {
    background: var(--field-bg) !important;
    border: 1px dashed var(--field-bd) !important;
}
[data-testid="stFileUploaderDropzoneInstructions"],
[data-testid="stFileUploaderDropzoneInstructions"] * { color: var(--muted) !important; }
[data-testid="stFileUploaderDropzone"] button {
    color: var(--btn-text) !important;
    background: var(--field-bg) !important;
    border: 1px solid var(--field-bd) !important;
    border-radius: var(--r-md) !important;
    box-shadow: none !important;
}
/* 按钮内部的图标底座自带浅色底，深色下像一块灰斑，统一清空 */
[data-testid="stFileUploaderDropzone"] button * { background: transparent !important; }
[data-testid="stFileUploaderDropzone"] button * { color: var(--btn-text) !important; }
[data-testid="stFileUploaderDropzone"] button:hover { border-color: var(--focus-bd) !important; }
[data-testid="stFileUploaderDropzone"] button:hover {
    background: var(--btn-hover-bg) !important;
    border-color: var(--btn-hover-border) !important;
}
[data-testid="stFileUploaderDropzone"] svg { fill: currentColor !important; }
[data-testid="stFileUploaderFile"], [data-testid="stFileUploaderFileName"] {
    color: var(--ink) !important; background: transparent !important;
}

/* ---------- 日期输入 ----------
   外层 stDateInputField 只是壳，真正的年月日在 stDateInputFieldsScroller 里，
   而且每个 input 都被 Streamlit 写死了深色文字。 */
[data-testid="stDateInputField"],
[data-testid="stDateInput"] [data-baseweb="input"],
[data-testid="stDateInputFieldsScroller"] {
    background: var(--field-bg) !important;
    color: var(--input-text) !important;
    border-radius: var(--r-md) !important;
}
[data-testid="stDateInput"] input,
[data-testid="stDateInputFieldsScroller"] input,
[data-testid="stDateInput"] [data-baseweb="input"] span,
[data-testid="stDateInputFieldsScroller"] span {
    color: var(--input-text) !important;
    -webkit-text-fill-color: var(--input-text) !important;
}
[data-testid="stDateInput"] svg { fill: var(--muted) !important; }

/* ---------- 进度条 / 加载中 ---------- */
[data-testid="stProgress"] [role="progressbar"] > div { background: var(--accent) !important; }
[data-testid="stProgress"] [role="progressbar"] { background: var(--field-bd) !important; }
[data-testid="stSpinner"] { color: var(--muted) !important; }
[data-testid="stSpinner"] svg { stroke: var(--accent) !important; }

/* ---------- 数据表外壳（canvas 本体在深色模式里另有处理） ---------- */
[data-testid="stDataFrame"], [data-testid="stDataEditor"] {
    background: var(--grid-bg) !important;
    border: 1px solid var(--grid-bd) !important;
    border-radius: var(--r-lg) !important; overflow: hidden;
}
[data-testid="stElementToolbar"], [data-testid="stElementToolbarButtonContainer"] {
    background: var(--grid-toolbar) !important;
    border: 1px solid var(--grid-bd) !important; border-radius: var(--r-sm);
}
[data-testid="stElementToolbar"] button,
[data-testid="stIconMaterial"] { color: var(--grid-head-fg) !important; }
[data-testid="stElementToolbar"] svg,
[data-testid="stIconMaterial"] { fill: currentColor !important; }

/* 主按钮 / 选中 Tab 内部的 markdown 段落会被 `.stApp p` 的正文色带走，
   必须把整棵子树钉成品牌前景色，否则浅色主题下是"深字压粉底"。 */
[data-testid="stBaseButton-primary"], [data-testid="stBaseButton-primary"] *,
.stTabs [aria-selected="true"], .stTabs [aria-selected="true"] * {
    color: var(--brand-fg) !important;
    -webkit-text-fill-color: var(--brand-fg) !important;
}
[data-testid="stBaseButton-primary"] {
    background: var(--brand-grad) !important;
    border: 1px solid rgba(255,255,255,.38) !important;
}

/* ---------- 按钮禁用态：两种主题都要看得出"不能点" ---------- */
[data-testid="stBaseButton-secondary"]:disabled,
[data-testid="stBaseButton-secondaryFormSubmit"]:disabled,
[data-testid="stBaseButton-primary"]:disabled {
    color: var(--btn-dis-fg) !important;
    border-color: var(--field-bd) !important;
    background: var(--field-dis-bg) !important;
    opacity: 1 !important; cursor: not-allowed !important;
    transform: none !important; box-shadow: none !important;
}
[data-testid="stBaseButton-secondary"]:focus-visible,
[data-testid="stBaseButton-secondaryFormSubmit"]:focus-visible,
[data-testid="stBaseButton-primary"]:focus-visible {
    outline: none !important;
    box-shadow: 0 0 0 4px var(--focus-ring) !important;
}
.stTextArea textarea:disabled, .stTextInput input:disabled {
    color: var(--field-dis-fg) !important;
    background: var(--field-dis-bg) !important;
    -webkit-text-fill-color: var(--field-dis-fg) !important;
}

/* ---------- 图片：深色下压一点亮度，避免截图白块刺眼 ---------- */
.stApp [data-testid="stImage"] img, .stApp [data-testid="stImageContainer"] img {
    filter: brightness(var(--img-brightness));
    transition: filter .2s ease;
}
</style>
"""

_CSS_DARK_ONLY = """
<style>
/* ---------- 只有深色模式才需要的一段 ---------- */

/* 数据表是 canvas（glide-data-grid），CSS 变量管不到它内部绘制色。
   这里只对 canvas 元素本身做一次“反向 + 补色”，把它从浅色纸片翻成深色；
   不做全局 invert，页面其它元素一律走变量。 */
[data-testid="stDataFrame"] canvas,
[data-testid="stDataEditor"] canvas {
    filter: invert(1) hue-rotate(180deg);
}
/* canvas 里的滚动条/边框在翻转后需要重新对齐容器 */
[data-testid="stDataFrame"] [data-testid="stDataFrameResizable"] { background: var(--grid-bg); }

/* 深色下阴影要看不出"浮起白边"，把浅色高光去掉 */
.hero-badge { border-color: rgba(255,255,255,.34) !important; }
</style>
"""


def _is_dark() -> bool:
    """当前是否深色模式。"""
    return st.session_state.get("ui_theme", THEME_LIGHT) == THEME_DARK


def _system_prefers_dark() -> bool:
    """浏览器 / 系统是否偏好深色。取不到就当作浅色。"""
    try:
        theme = st.context.theme
        kind = theme.get("type") if hasattr(theme, "get") else getattr(theme, "type", None)
        return str(kind).lower() == "dark"
    except Exception:  # noqa: BLE001 - 老版本 / 无前端上下文
        return False


def _initial_theme() -> str:
    """
    首次进入时用哪个主题，优先级：URL 参数 > 系统偏好 > 浅色。

    用 URL 参数（?theme=dark）而不是 localStorage 存偏好，是因为它能在
    服务端渲染第一帧之前就读到——刷新时不会先闪一下浅色再跳成深色。
    """
    try:
        raw = st.query_params.get("theme")
    except Exception:  # noqa: BLE001
        raw = None
    if raw in ("dark", "light"):
        return THEME_DARK if raw == "dark" else THEME_LIGHT
    return THEME_DARK if _system_prefers_dark() else THEME_LIGHT


def _remember_theme(theme: str) -> None:
    """把选择写回 URL，刷新 / 分享链接都能保持同一套配色。"""
    try:
        if theme == THEME_DARK:
            st.query_params["theme"] = "dark"
        else:
            st.query_params["theme"] = "light"
        st.session_state.ui_theme_pick = theme
    except Exception:  # noqa: BLE001
        pass


def _css_tokens() -> str:
    """
    把当前主题的调色板写成 CSS 变量，并顺带缩放跟字号。

    字号的做法：`html { font-size }` 直接改变 rem 基准，Streamlit 自带的组件
    （按钮、标签、说明文字都是 rem）会等比放大；自有样式也统一用 rem 书写，
    因此一处改动就能让整个界面在「小 / 标准 / 大 / 特大」之间平滑切换。

    `color-scheme` 一并切换：它决定浏览器原生滚动条、日期选择器、自动填充、
    表单控件这些"CSS 管不到"的部分用亮色还是暗色皮肤。
    """
    dark = _is_dark()
    tokens = _TOKENS_DARK if dark else _TOKENS_LIGHT
    scale = FONT_SCALES.get(st.session_state.get("ui_font", FONT_NORMAL), 1.0)
    body = "".join(f"--{k}:{v};" for k, v in tokens.items())
    scheme = "dark" if dark else "light"
    return (
        "<style>:root{" + body
        + f"--fs:{scale};font-size:calc(16px * {scale});"
        + f"color-scheme:{scheme};"
        # 首帧底色：避免页面从浏览器默认白底闪到深色
        + f"background:var(--body-bg);"
        + "}html,body{background:var(--body-bg);}"
        + "</style>"
    )


def _inject_css() -> None:
    """注入顺序：令牌 → 业务样式 → 原生组件补丁 → 深色专属补丁。"""
    st.markdown(_css_tokens(), unsafe_allow_html=True)
    st.markdown(_CSS, unsafe_allow_html=True)
    st.markdown(_CSS_WIDGETS, unsafe_allow_html=True)
    if _is_dark():
        st.markdown(_CSS_DARK_ONLY, unsafe_allow_html=True)



def _html(html: str) -> None:
    """
    渲染**纯静态** HTML 片段。

    安全约定：本函数只接受开发者在代码中写死的字符串，
    任何来自用户的内容都必须经过 html.escape 后才能传入。
    """
    st.markdown(html, unsafe_allow_html=True)


def _token(name: str) -> str:
    """
    取当前主题的设计令牌值。

    给 pandas Styler / canvas 这类"只吃字面量颜色、不认 CSS 变量"的地方用；
    能写 CSS 的地方一律直接写 var(--令牌名)，不要走这里。
    """
    tokens = _TOKENS_DARK if _is_dark() else _TOKENS_LIGHT
    return tokens.get(name, "")


def _chip(text: str, kind: str = "info") -> str:
    return f'<span class="chip chip-{kind}">{text}</span>'


def _tip(name: str, desc: str) -> str:
    """
    按钮 / 控件的悬停说明文案。

    约定：第一行先点明「这是哪个按钮」，空一行再写它做什么，
    这样鼠标停一会儿弹出的提示能第一眼对上控件。
    """
    return f"**{name}**\n\n{desc}"


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
        "clear_origin": "",           # 确认条归属：ingest / table / sidebar
        "uploader_nonce": 0,          # 换 key 用：清空时把已选图片一并丢弃
        # --- 外观偏好（全部只存内存，不落盘） ---
        "ui_theme": _initial_theme(),   # 浅色 / 深色（URL 参数 > 系统偏好 > 浅色）
        "ui_font": FONT_NORMAL,       # 小 / 标准 / 大 / 特大
        "settings_open": False,       # 侧边栏设置面板是否展开
        "use_ai": True,               # 是否优先调用大模型
        "default_remind": 15,         # 全局提醒（分钟）
        "calendar_name": "我的 AI 日程",
        "raw_text_input": "",
        "total_parsed": 0,
        "pending_toast": "",          # 跨 rerun 传递的操作提示
        # --- 日历 ---
        "cal_year": datetime.now().year,
        "cal_month": datetime.now().month,
        "cal_selected": datetime.now().date(),
        # --- 重复设置面板的状态 ---
        "recur_target_uid": "",       # 当前正在设置的日程
        "recur_enabled": False,       # 「是否重复」开关
        "recur_freq": RECUR_FREQ_WEEKLY,
        "recur_count": RECUR_COUNT_DEFAULT,
        "recur_start_date": datetime.now().date(),  # 循环开始日期
        "recur_advanced_open": False,  # 高级：手写 RRULE 展开状态
        # --- 课表识别 ---
        # course_anchor 由设置面板的 date_input 自己维护（widget key），
        # 这里只记录「用户是否手动指定过」，决定导入后是否提示日期为推算值。
        "course_anchor_set": False,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


# ---------------------------------------------------------------------------
# 11b. 统一的「清空」入口
# ---------------------------------------------------------------------------
# 说明：本应用的设计是「多次输入追加、不覆盖」，所以新的一批日程会接在旧日程后面。
# 想从头开始必须先清空。之前只有「清空输入」一个按钮，它只清文本框、不动已解析的
# 日程，很容易被当成「清空这一次的全部内容」而误用，于是出现「新日程和旧日程叠在
# 一起」的现象。这里把清空拆成两条明确的路：
#   · 清空输入 —— 只擦掉文本框和已选图片，已录入的日程保持不变
#   · 清空全部日程 —— 日程、解析报告、输入框、已选图片全部归零，回到初始状态
# 所有清空动作都走 on_click 回调：回调在脚本重跑前执行，此时改写 text_area 绑定的
# session_state 不会触发 StreamlitAPIException。

def _reset_all() -> None:
    """彻底重置，回到「刚打开页面」的状态。只能作为按钮回调调用。"""
    st.session_state.events = []
    st.session_state.last_report = None
    st.session_state.last_error = ""
    st.session_state.last_ocr = []
    st.session_state.total_parsed = 0
    # 输入区一并清空：否则用户清完日程、直接再点解析，旧文本又被解析一遍
    st.session_state.raw_text_input = ""
    # 换掉 uploader 的 key，已选图片随之丢弃（否则旧截图会被重复 OCR）
    st.session_state.uploader_nonce += 1
    # 重复设置面板指向的日程已不存在，复位避免面板停在空气上
    st.session_state.recur_target_uid = ""
    st.session_state.recur_enabled = False
    st.session_state.recur_count = RECUR_COUNT_DEFAULT
    # 表格按新的 key 重建，旧的下拉框选项一并丢掉
    st.session_state.editor_key += 1
    st.session_state.editor_hash = ""
    for stale in ("del_pick", "recur_pick"):
        st.session_state.pop(stale, None)
    st.session_state.confirm_clear = False
    st.session_state.clear_origin = ""
    st.session_state.pending_toast = "已清空全部日程，可以重新录入了"


def _cb_request_clear(origin: str) -> None:
    """第一次点击：只亮出确认条，不动数据。"""
    st.session_state.confirm_clear = True
    st.session_state.clear_origin = origin


def _cb_cancel_clear() -> None:
    st.session_state.confirm_clear = False
    st.session_state.clear_origin = ""


def render_clear_all_button(origin: str, label: str = "🗑️ 清空全部日程") -> None:
    """「清空全部日程」的触发按钮，可放进任意列。确认条由 render_clear_all_confirm 渲染。"""
    st.button(
        label,
        key=f"clear_all_{origin}",
        width="stretch",
        on_click=_cb_request_clear,
        args=(origin,),
        help=_tip(label, "清空已录入的全部日程、解析报告、输入框内容和已选图片，"
                         "回到刚打开页面的空白状态。点一次只会亮出确认条，确定后才会真删。"),
    )


def render_clear_all_confirm(origin: str) -> None:
    """两段式确认条：放在按钮下方（建议整行，别塞进窄列）。"""
    if not (st.session_state.get("confirm_clear")
            and st.session_state.get("clear_origin") == origin):
        return
    count = len(st.session_state.events)
    _html(
        f'<div class="notice notice-warn" style="margin:8px 0 10px 0;font-size:.78rem;">'
        f'⚠️ 将清空 <b>{count}</b> 条日程，并清空输入框与已选图片。'
        f'此操作不可撤销。</div>'
    )
    c_yes, c_no = st.columns(2)
    with c_yes:
        st.button("确定清空", key=f"clear_yes_{origin}", type="primary",
                  width="stretch", on_click=_reset_all,
                  help=_tip("确定清空", "确认执行清空：日程、报告、输入框与图片一次性归零，"
                                        "无法撤销。"))
    with c_no:
        st.button("取消", key=f"clear_no_{origin}", width="stretch",
                  on_click=_cb_cancel_clear,
                  help=_tip("取消", "放弃这次清空，所有数据保持原样。"))


def render_clear_all_control(origin: str, label: str = "🗑️ 清空全部日程") -> None:
    """按钮 + 确认条的组合，用于整行放置（日程表 / 侧边栏）。"""
    render_clear_all_button(origin, label)
    render_clear_all_confirm(origin)


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
    tt_cells: list[dict[str, Any]] = []
    ocr_ok = 0
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
                "layout": res.get("layout"),
            })
            if res["ok"]:
                warnings.extend(res["warnings"])
                ocr_ok += 1
                if res.get("cells"):
                    # 课表走结构化通道，不再把压平文本喂给规则引擎，
                    # 否则「星期一」「高等数学」会被当成两条独立日程。
                    tt_cells.extend(res["cells"])
                    lay = res["layout"]
                    warnings.append(
                        f"第 {idx} 张图片识别为课表：{lay['weekdays']} 天 × "
                        f"{lay['periods']} 节，共 {lay['cells']} 个课程单元格。"
                    )
                else:
                    ocr_texts.append(res["text"])
            else:
                warnings.append(f"第 {idx} 张图片：{res['error']}")

    combined = "\n".join([t for t in [text] + ocr_texts if t.strip()]).strip()

    # --- 3. 空输入兜底 --------------------------------------------------
    if not combined and not tt_cells:
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

    if combined:
        try:
            rule_events = parse_with_rules(combined)
        except Exception:  # noqa: BLE001
            log(f"规则解析异常: {traceback.format_exc()}")
            rule_events = []

    # 课表走确定性通道：行列已知，不需要模型去猜
    if tt_cells:
        try:
            rule_events = rule_events + timetable_to_events(
                tt_cells, st.session_state.get("course_anchor") or _this_monday())
        except Exception:  # noqa: BLE001
            log(f"课表解析异常: {traceback.format_exc()}")
            warnings.append("课表结构已识别，但转换成日程时出错，已回退为文本解析。")
            if not rule_events and combined:
                try:
                    rule_events = parse_with_rules(combined)
                except Exception:  # noqa: BLE001
                    rule_events = []

    ai_config = get_ai_config()
    if use_ai and ai_config["available"] and combined:
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
        if tt_cells:
            engine_label = "课表结构化 + AI"
    elif ai_events:
        final_events = ai_events
        engine_label = "AI 解析"
    elif rule_events:
        final_events = rule_events
        if tt_cells:
            engine_label = "课表结构化识别"
        else:
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
    report["ocr_count"] = ocr_ok
    report["timetable_cells"] = len(tt_cells)
    report["vague"] = sum(1 for ev in st.session_state.events if ev.vague)
    st.session_state.last_report = report

    if report["added"] == 0 and report["raw_count"] > 0:
        st.session_state.last_report["all_duplicate"] = True


# ---------------------------------------------------------------------------
# 13. 侧边栏
# ---------------------------------------------------------------------------

def _cb_toggle_settings() -> None:
    """展开 / 收起侧边栏设置面板。"""
    st.session_state.settings_open = not st.session_state.settings_open


def _cb_theme_pick() -> None:
    """
    主题变化时立刻写回 ui_theme。

    必须走 on_change 回调：CSS 在脚本最开头注入，若等到控件实例化时才改 state，
    这次 rerun 用的还是旧主题，用户会觉得「点了没反应，要再点一下」。
    """
    st.session_state.ui_theme = st.session_state.ui_theme_pick
    _remember_theme(st.session_state.ui_theme)


def _cb_font_pick() -> None:
    """字号同理，回调里同步，本次 rerun 即生效。"""
    st.session_state.ui_font = st.session_state.ui_font_pick


def _cb_course_anchor() -> None:
    """用户手动指定了学期首周，之后的课表导入就不再提示「日期为推算值」。"""
    st.session_state.course_anchor_set = True


def render_settings_panel() -> None:
    """侧边栏设置面板：外观 → AI 解析 → OCR 识别 → 课表识别 → 默认提醒。"""
    # ---------- 外观 ----------
    st.markdown("#### ☀️ 界面与外观")
    st.radio(
        "明暗模式",
        THEME_OPTIONS,
        index=THEME_OPTIONS.index(st.session_state.ui_theme),
        key="ui_theme_pick",
        horizontal=True,
        format_func=lambda x: f"☀️ {x}" if x == THEME_LIGHT else f"🌙 {x}",
        on_change=_cb_theme_pick,
        help=_tip("明暗模式", "在浅色（奶油柔光底）与深色（暗夜紫）之间切换，立即生效。"
                              "只影响本机显示，不影响导出内容。"),
    )
    st.select_slider(
        "界面字号",
        options=FONT_OPTIONS,
        value=st.session_state.ui_font,
        key="ui_font_pick",
        on_change=_cb_font_pick,
        help=_tip("界面字号", "整体放大或缩小页面文字：小 / 标准 / 大 / 特大。"
                              "按钮、表格、日历会一起缩放，导出文件不受影响。"),
    )

    # ---------- AI ----------
    st.markdown("#### 🤖 AI 解析")
    ai_config = get_ai_config()
    if ai_config["available"]:
        _html(
            _chip("已连接", "ok")
            + f'<span style="font-size:.75rem;color:var(--muted-2);">'
              f'{_escape(ai_config["model"])}</span>'
        )
        st.checkbox(
            "优先使用 AI 解析", key="use_ai",
            help=_tip("优先使用 AI 解析",
                      "勾选时先让大模型理解这段文字，再用本地规则补充漏项；"
                      "取消勾选则完全离线，只用本地规则引擎。"),
        )
        st.caption("**作用**：把口语化的整段话（微信群聊、语音转写）交给大模型，"
                   "理解出「标题 / 时间 / 地点」，比纯规则更懂上下文。")
    else:
        _html(_chip("未配置 API Key", "warn"))
        st.caption("**作用**：把口语化的整段话交给大模型，理解出标题、时间、地点。"
                   "现在没检测到密钥，会自动用**本地规则引擎**——格式规整的课表、"
                   "备忘录一样能解析，而且完全离线、不联网。")
        with st.expander("如何配置 API Key"):
            st.markdown(_read_config_hint())

    # ---------- OCR ----------
    st.markdown("#### 👁️ OCR 识别")
    engine_name, _ = get_ocr_engine()
    if engine_name == "rapidocr":
        _html(_chip("本地 OCR 已就绪", "ok"))
    elif engine_name == "pytesseract":
        _html(_chip("Tesseract 已就绪", "ok"))
    else:
        _html(_chip("OCR 未安装", "warn"))
    st.caption("**作用**：把课表、车票、聊天截图里的字读出来，再交给解析引擎。"
               "识别全程在本机内存完成，图片不上传、不落盘。")
    if not engine_name:
        with st.expander("如何安装 OCR"):
            st.caption("执行 `pip install -r requirements.txt` 即可启用图片识别"
                       "（Python 3.13+ 需 `pip install rapidocr onnxruntime`）。")

    # ---------- 课表识别 ----------
    st.markdown("#### 📅 课表识别")
    st.date_input(
        "学期第 1 周的周一",
        value=_this_monday(),
        key="course_anchor",
        on_change=_cb_course_anchor,
        help=_tip("学期第 1 周的周一",
                  "课表上写的是「第 9 周」「2-6,9-18 周」这种相对周次，"
                  "要从这个日期往后推算成具体哪天。填对了日期就准；"
                  "不填则按本周一算，导入的课程会标「待核对」。"),
    )
    st.caption("课表截图里的周次按这个日期换算；上课时间按默认作息表推算"
               "（第 1 节 08:00 起、每节 45 分钟），导入后可在日程表里逐条微调。")

    # ---------- 默认提醒 ----------
    st.markdown("#### 🔔 默认提醒")
    remind_labels = list(REMIND_OPTIONS.keys())
    current_label = _remind_label(st.session_state.default_remind)
    idx = remind_labels.index(current_label) if current_label in remind_labels else 1
    chosen = st.selectbox(
        "默认提醒", remind_labels, index=idx,
        help=_tip("默认提醒", "新日程默认提前多久提醒，导出 .ics 时写入 VALARM；"
                              "单条日程可以在表格的「提醒」列里单独改。"),
    )
    st.session_state.default_remind = REMIND_OPTIONS[chosen]

    st.caption("🔁 循环日程的**开始日期与重复次数**在「日程表 → 重复设置」里逐条调整，"
               "默认不循环，只有出现「每周一」「每天」这类字眼才会自动循环。")

    st.markdown("")
    st.button(
        "收起设置", key="settings_close", width="stretch",
        on_click=_cb_toggle_settings,
        help=_tip("收起设置", "把设置面板收起来，回到只看数据和日程的清爽状态。"
                              "偏好会保留，下次点开还在。"),
    )


def render_sidebar() -> None:
    with st.sidebar:
        _html(
            '<div style="padding:2px 0 10px 0;">'
            '<div style="font-size:1.1875rem;font-weight:800;color:var(--logo);'
            'letter-spacing:-.3px;">'
            '🗂️ 日程收纳台</div>'
            '<div style="font-size:.75rem;color:var(--muted-2);margin-top:2px;">'
            f'v{APP_VERSION} · 内存计算 · 不存数据</div></div>'
        )

        # --- 设置入口（AI / OCR / 外观都收在这里面） ---
        is_open = bool(st.session_state.settings_open)
        st.button(
            "✕ 收起设置" if is_open else "⚙️ 设置与外观",
            key="settings_toggle",
            width="stretch",
            on_click=_cb_toggle_settings,
            help=_tip(
                "收起设置" if is_open else "设置与外观",
                "收起设置面板，回到只看数据的视图。" if is_open else
                "在这里调整：明暗模式、界面字号、AI 解析开关、OCR 识别状态、默认提醒时间。",
            ),
        )
        if is_open:
            with st.container(border=True):
                render_settings_panel()
        else:
            # 收起时保留一行极简状态，不占地方又不至于完全看不到能力状态
            st.caption(f"{'🌙' if _is_dark() else '☀️'} {st.session_state.ui_theme}"
                       f" · 字号 {st.session_state.ui_font}")
            ai_ok = get_ai_config()["available"]
            ocr_name, _ = get_ocr_engine()
            st.caption(f"{'AI 已连接' if ai_ok else 'AI 未配置'}"
                       f" · {'OCR 就绪' if ocr_name else 'OCR 未安装'}")

        st.markdown("---")

        # --- 数据 ---
        st.markdown("### 📊 当前数据")
        total = len(st.session_state.events)
        conflict = sum(1 for ev in st.session_state.events if ev.conflict)
        vague = sum(1 for ev in st.session_state.events if ev.vague)
        st.markdown(
            f'<div style="font-size:.8125rem;line-height:2;color:var(--ink-2);">'
            f'日程总数：<b>{total}</b><br>'
            f'时间冲突：<b style="color:var(--bad-strong);">{conflict}</b><br>'
            f'待人工核对：<b style="color:var(--warn-strong);">{vague}</b>'
            f'</div>',
            unsafe_allow_html=True,
        )
        if total:
            render_clear_all_control("sidebar")
        else:
            st.caption("还没有日程。")

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
    """「清空文本框」回调：只擦掉输入区，不动已录入的日程。"""
    st.session_state.raw_text_input = ""
    st.session_state.last_report = None
    st.session_state.last_error = ""
    st.session_state.last_ocr = []
    # 换掉 uploader 的 key，让已选图片一起消失，
    # 否则下次解析会把上次那张截图再 OCR 一遍，日程就重复了
    st.session_state.uploader_nonce += 1


def render_ingest_tab() -> None:
    _html('<div class="section-title">把碎片日程丢进来</div>')
    _html(
        '<div class="notice notice-info">'
        '支持<b>纯文本粘贴</b>与<b>截图上传</b>，程序会自动识别标题、时间、地点、优先级与循环规则。'
        '已录入的日程<b>不会被覆盖</b>，可反复追加；想从空白开始，点下方「🗑️ 清空全部日程」。'
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
            # key 里带 nonce：清空时 n+1，等于换一个全新的上传控件，
            # 已选的旧图片会一起消失，不会被下一次解析重复 OCR
            key=f"upload_{st.session_state.uploader_nonce}",
            help=_tip("② 上传日程截图",
                      "支持 PNG / JPG / WEBP / BMP，单张不超过 10MB，最多 3 张。\n"
                      "图片只在本机内存里做 OCR，识别完即丢弃，不会上传。"),
        )
        if images:
            st.caption(f"已选择 {len(images)} 张图片（最多识别前 3 张）")
            for f in images[:3]:
                # 用固定像素宽度而非 use_column_width：后者在新版 Streamlit 已弃用
                try:
                    st.image(f, width=280)
                except Exception:  # noqa: BLE001
                    st.caption("（该图片无法预览，但仍会尝试识别）")

    # --- 已有日程提示：说明「追加」语义，并给出清空入口 ---
    existing = len(st.session_state.events)
    if existing:
        _html(
            f'<div class="notice notice-info" style="margin:14px 0 6px 0;font-size:.78rem;">'
            f'当前已有 <b>{existing}</b> 条日程。再次解析会<b>追加</b>在这 {existing} 条后面，'
            f'不会覆盖。想从空白重新开始，就点下面的「🗑️ 清空全部日程」。</div>'
        )

    st.markdown("")
    btn_cols = st.columns([1.15, 1, 1, 1.15])

    # 「加载示例」「清空文本框」「清空全部日程」必须通过 on_click 回调修改
    # session_state：回调在 rerun 开始时、text_area 实例化之前执行，否则会触发
    # StreamlitAPIException: cannot be modified after the widget is instantiated。
    with btn_cols[0]:
        do_parse = st.button(
            "✨ 开始解析", type="primary", width="stretch",
            help=_tip("✨ 开始解析",
                      "解析左侧文字和已上传的截图，把识别出的日程追加到日程表。"
                      "不会覆盖已有内容；重复的条目会自动去重。"),
        )
    with btn_cols[1]:
        st.button(
            "📚 加载示例", width="stretch",
            on_click=_cb_load_sample,
            help=_tip("📚 加载示例",
                      "把一段混排的江苏大学课表 / 班群通知 / DDL 示例填进文本框，"
                      "适合快速体验功能。填完再点「开始解析」。"),
        )
    with btn_cols[2]:
        st.button(
            "🧹 清空文本框", width="stretch", on_click=_cb_clear_input,
            help=_tip("🧹 清空文本框",
                      "只擦掉左侧文字和已选的截图，**已录入的日程不受影响**。"
                      "想连日程一起清掉，用右边的「🗑️ 清空全部日程」。"),
        )
    with btn_cols[3]:
        render_clear_all_button("ingest", label="🗑️ 清空全部日程")

    # 确认条整行渲染，避免挤在窄列里换行
    render_clear_all_confirm("ingest")

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
        # 侧边栏与统计卡在同一次 run 中更早渲染，不重跑一次会停留在解析前的数字。
        # 报告已写入 session_state，rerun 后照常展示。
        st.rerun()

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
    if report.get("timetable_cells"):
        chips.append(_chip(f"课表 {report['timetable_cells']} 格", "info"))
    if report.get("all_duplicate"):
        chips.append(_chip("全部为重复日程，未新增", "warn"))
    if chips:
        _html('<div style="margin-bottom:10px;">' + "".join(chips) + "</div>")

    if report.get("timetable_cells") and not st.session_state.get("course_anchor_set"):
        _html(
            '<div class="notice notice-info">📅 识别为课表并按行列还原。'
            '上课时间按默认作息表推算（第 1 节 08:00、每节 45 分钟），'
            '周次日期按<b>本周为第 1 周</b>换算——'
            '如果不对，到左侧「⚙️ 设置与外观 → 📅 课表识别」填上'
            '<b>学期第 1 周的周一</b>，重新解析即可对齐。</div>'
        )

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
            layout = item.get("layout")
            if layout:
                _html(
                    _chip(f"识别为课表 · {layout['weekdays']} 天 × {layout['periods']} 节", "ok")
                    + _chip(f"{layout['cells']} 个课程单元格", "info")
                )
            if item["ok"]:
                preview = item["text"][:600] + ("…" if len(item["text"]) > 600 else "")
                st.code(preview, language="text")
                if layout:
                    st.caption("以上是**还原表格结构后**的文本（星期 + 节次 + 课程 + 教师 + 地点 + 周次），"
                               "比逐行堆字更容易核对。")
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

    # 注意：data_editor 是 canvas，颜色由 JS 绘制，CSS 变量管不到这里，
    # 只能喂字面量。深色模式下这张表由 canvas 滤镜整体翻转，所以这里
    # 仍然给"浅色主题的值"，翻转后正好得到深色底 + 浅色字。
    bad_bg, bad_fg = _token("row-bad-bg"), _token("row-bad-fg")
    warn_bg, warn_fg = _token("row-warn-bg"), _token("row-warn-fg")

    def _row_css(row: pd.Series) -> list[str]:
        status = status_map.get(str(row.get("uid")), "")
        if status.startswith("🔴"):
            return [f"background-color: {bad_bg}; color: {bad_fg}"] * len(row)
        if status.startswith("⚠️"):
            return [f"background-color: {warn_bg}; color: {warn_fg}"] * len(row)
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
    st.session_state.recur_start_date = ev.start.date()


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


def _apply_recur_setting(uid: str, enabled: bool, freq_key: str, count: int,
                         start_date: date | None = None) -> None:
    """
    把面板上的设置写回目标日程。

    注意：本函数只能作为按钮的 on_click 回调执行。Streamlit 不允许在 widget
    渲染之后直接改它自己绑定的 session_state key，而回调在 widget 实例化之前运行，
    因此在这里同步开关/频率/次数/开始日期的状态是安全的。
    """
    target = next((e for e in st.session_state.events if e.uid == uid), None)
    if target is None:
        st.session_state.pending_toast = "目标日程已不存在，请重新选择"
        return

    if enabled:
        # 循环开始日期就是这条日程的第一次发生时间：改日期时整条日程一起平移，
        # 时分与时长保持不变，避免用户还要再去表格里改一遍。
        if start_date and start_date != target.start.date():
            delta = start_date - target.start.date()
            target.start = target.start + delta
            target.end = target.end + delta
            # 开始日期的星期几变了，循环基准日要跟着走，
            # 否则会出现「开始日是周二、却每周一重复」的矛盾规则。
            old_days = [d for d in _rrule_field(target.rrule, "BYDAY").split(",") if d]
            new_wd = _WEEKDAY_ICS[target.start.weekday()]
            if old_days and new_wd not in old_days:
                target.rrule = re.sub(
                    r";?\s*BYDAY=[^;]*", "", target.rrule, flags=re.IGNORECASE
                ).strip("; ")

        rrule = build_recur_rrule(freq_key, target.start, target.rrule)
        target.rrule = set_recur_count(
            rrule, max(RECUR_COUNT_MIN, min(RECUR_COUNT_MAX, count))
        )
        st.session_state.pending_toast = f"已设为「{describe_recurrence(target.rrule)}」"
        st.session_state.recur_freq = freq_key
        st.session_state.recur_count = max(RECUR_COUNT_MIN, min(RECUR_COUNT_MAX, count))
        st.session_state.recur_start_date = target.start.date()
    else:
        target.rrule = ""
        st.session_state.pending_toast = "已取消重复，改为单次日程"

    st.session_state.recur_enabled = bool(enabled)
    st.session_state.recur_target_uid = uid

    _recompute_conflicts()
    # 表格里的「重复」列内容变了，必须重建编辑器，否则旧缓存会盖掉本次修改
    st.session_state.editor_key += 1
    st.session_state.editor_hash = ""


def _cal_shift(delta: int) -> None:
    """日历翻月回调：跨年时自动进位 / 退位。"""
    year = int(st.session_state.cal_year)
    month = int(st.session_state.cal_month) + delta
    while month < 1:
        month += 12
        year -= 1
    while month > 12:
        month -= 12
        year += 1
    st.session_state.cal_year = year
    st.session_state.cal_month = month


def _cal_goto_today() -> None:
    """日历「回到今天」回调。"""
    today = datetime.now().date()
    st.session_state.cal_year = today.year
    st.session_state.cal_month = today.month
    st.session_state.cal_selected = today


def _cal_pick(day: date) -> None:
    """点击日历格子：选中该日，并把它同步为「循环开始日期」的默认值。"""
    st.session_state.cal_selected = day


def _occurs_on(ev: Event, day: date) -> bool:
    """
    判断某条日程（含循环日程）是否会在指定日期发生。

    只用于日历上的圆点标记与右侧当日清单，属于展示用途：
    周/日重复精确计算，月/年重复按自然月近似，足够满足"这一天有没有事"的判断。
    """
    start_day = ev.start.date()
    if day < start_day:
        return False
    if not ev.is_recurring:
        return day == start_day

    freq = _rrule_field(ev.rrule, "FREQ")
    interval_raw = _rrule_field(ev.rrule, "INTERVAL")
    interval = int(interval_raw) if interval_raw.isdigit() and int(interval_raw) > 0 else 1
    count = get_recur_count(ev.rrule)

    if freq == "DAILY":
        diff = (day - start_day).days
        if diff % interval:
            return False
        return not count or (diff // interval + 1) <= count

    if freq == "WEEKLY":
        byday = [d for d in _rrule_field(ev.rrule, "BYDAY").split(",") if d]
        if byday and _WEEKDAY_ICS[day.weekday()] not in byday:
            return False
        weeks = (day - start_day).days // 7
        if weeks % interval:
            return False
        if not count:
            return True
        days_per_cycle = len(byday) or 1
        order = sorted(byday, key=lambda d: _WEEKDAY_ICS.index(d)) if byday else []
        pos = order.index(_WEEKDAY_ICS[day.weekday()]) + 1 if order else 1
        return (weeks // interval) * days_per_cycle + pos <= count

    if freq == "MONTHLY":
        months = (day.year - start_day.year) * 12 + (day.month - start_day.month)
        if months % interval or day.day != min(start_day.day, 28):
            return False
        return not count or (months // interval + 1) <= count

    if freq == "YEARLY":
        years = day.year - start_day.year
        if years % interval or (day.month, day.day) != (start_day.month, start_day.day):
            return False
        return not count or (years // interval + 1) <= count

    # 无法归类的规则退回「只在首次发生日出现」，避免日历上凭空多出事项
    return day == start_day


def events_on_day(day: date) -> list[Event]:
    """返回指定日期会发生的全部日程，按开始时间排序。"""
    hits = [ev for ev in st.session_state.events if _occurs_on(ev, day)]
    return sorted(hits, key=lambda e: e.start)


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
        help=_tip("选择要设置重复的日程",
                  "先挑一条日程，再决定它是否重复、多久重复一次、总共重复几次。"),
    )
    target_ev = next((e for e in st.session_state.events
                      if e.uid == st.session_state.recur_target_uid), None)

    enabled = st.toggle(
        "是否重复",
        key="recur_enabled",
        help=_tip("是否重复",
                  "关闭＝这条日程只发生一次；开启后可以设置重复频率、开始日期与次数。"),
    )

    if enabled:
        freq = st.radio(
            "重复频率",
            RECUR_FREQ_OPTIONS,
            key="recur_freq",
            horizontal=True,
            help=_tip("重复频率",
                      "每周重复 / 每两周重复 / 每天重复。切换频率不会改变星期几，"
                      "「每周一三五」改成每两周后仍然是一、三、五，只是隔周。"),
        )

        # ---- 循环开始日期 -------------------------------------------------
        st.markdown(
            '<div style="font-size:.8125rem;font-weight:600;color:var(--ink-3);'
            'margin:12px 0 4px;">循环开始日期</div>',
            unsafe_allow_html=True,
        )
        d_col, w_col = st.columns([2.6, 1], gap="small")
        with d_col:
            start_pick = st.date_input(
                "循环开始日期",
                key="recur_start_date",
                format="YYYY-MM-DD",
                label_visibility="collapsed",
                help=_tip("循环开始日期",
                          "这一天算作循环的第 1 次。改它会连同年月日一起平移整条日程，"
                          "时分和时长保持不变，星期几也会跟着重算。"),
            )
        with w_col:
            _html(
                '<div style="height:38px;display:flex;align-items:center;'
                'justify-content:center;background:var(--chip-info-bg);'
                'border:1px solid var(--chip-info-bd);border-radius:var(--r-pill);'
                'font-size:.8125rem;font-weight:700;color:var(--chip-info-fg);">'
                f'周{"一二三四五六日"[start_pick.weekday()]}</div>'
            )

        count = int(st.session_state.recur_count)

        # ---- 次数步进器：➖ ｜ 中间数字 ｜ ➕ ----
        st.markdown(
            '<div style="font-size:.8125rem;font-weight:600;color:var(--ink-3);'
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
                    help=_tip("－ 减少一次",
                              f"把重复次数减 1，最少 {RECUR_COUNT_MIN} 次。"),
                )
            with c_num:
                _html(
                    # 与左右按钮等高（40px）并用 flex 居中，避免数字上下错位
                    '<div style="height:40px;display:flex;align-items:center;'
                    'justify-content:center;background:var(--stepper-bg);'
                    'border:1px solid var(--stepper-bd);'
                    'border-radius:var(--r-md);">'
                    f'<span style="font-size:1.5rem;font-weight:800;color:var(--stepper-fg);">{count}</span>'
                    '<span style="font-size:.8125rem;font-weight:600;color:var(--muted);'
                    'margin-left:4px;">次</span></div>'
                )
            with c_plus:
                st.button(
                    "＋", key="recur_plus", on_click=_bump_recur_count, args=(1,),
                    width="stretch", disabled=count >= RECUR_COUNT_MAX,
                    help=_tip("＋ 增加一次",
                              f"把重复次数加 1，最多 {RECUR_COUNT_MAX} 次。"),
                )

        # 实时预览：点「应用」前就能看到最终规则与结束日期
        base_dt = target_ev.start if target_ev else datetime.now()
        start_for_preview = datetime.combine(start_pick, base_dt.time())
        preview = set_recur_count(
            build_recur_rrule(freq, start_for_preview,
                              target_ev.rrule if target_ev else ""),
            count,
        )
        last_dt = _nth_occurrence(preview, start_for_preview, count)
        st.caption(
            f"第 1 次：**{start_for_preview:%Y-%m-%d %H:%M}**"
            f"（周{'一二三四五六日'[start_pick.weekday()]}）"
            f"　·　规则：**{describe_recurrence(preview)}**"
            + (f"　·　预计最后一次 {last_dt:%Y-%m-%d}" if last_dt else "")
        )
    else:
        freq = st.session_state.recur_freq
        count = int(st.session_state.recur_count)
        start_pick = st.session_state.recur_start_date
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
            on_click=_apply_recur_setting,
            args=(picked_uid, enabled, freq, count, start_pick),
            help=_tip("✅ 应用重复设置",
                      "把上面的开关、频率、开始日期和次数写进这条日程，"
                      "并同步到导出的 .ics 里。"),
        )
    with b2:
        st.button(
            "🚫 取消这条的重复", key="remove_recur", width="stretch",
            help=_tip("🚫 取消这条的重复",
                      "把这条日程改回只发生一次，不会删除日程本身。"),
            on_click=_apply_recur_setting,
            args=(picked_uid, False, freq, count, start_pick),
        )


def _calendar_matrix(year: int, month: int) -> list[list[date | None]]:
    """
    生成一个「周一开头」的月历矩阵，用于渲染日历网格。

    calendar.monthrange 返回 (1 号是周几, 本月天数)，其中周一 = 0，
    与国内日历习惯一致，无需再做偏移。
    """
    first_weekday, days_in_month = calendar.monthrange(year, month)
    cells: list[date | None] = [None] * first_weekday
    cells += [date(year, month, d) for d in range(1, days_in_month + 1)]
    while len(cells) % 7:
        cells.append(None)
    return [cells[i:i + 7] for i in range(0, len(cells), 7)]


def _render_calendar() -> None:
    """
    月历视图：可翻月、可点日期，右侧联动显示当天全部安排。

    交互全部由 st.button 承担（HTML 里的 JS 在 Streamlit 中不会执行），
    这样点击才真正能改状态；外观靠 CSS 把它压成日历格子。
    """
    year = int(st.session_state.cal_year)
    month = int(st.session_state.cal_month)
    selected: date = st.session_state.cal_selected

    left, right = st.columns([1, 1.05], gap="medium")

    with left:
        # key 让整块日历拿到 .st-key-cal-grid 钩子，才能把 42 个按钮压成日历格子
        with st.container(border=True, key="cal_grid"):
            head_prev, head_title, head_next = st.columns([1, 3.4, 1], gap="small")
            with head_prev:
                st.button("‹", key="cal_prev", on_click=_cal_shift, args=(-1,),
                          width="stretch", help=_tip("‹ 上个月", "翻到上一个月的月历。"))
            with head_title:
                _html(
                    '<div class="cal-title">'
                    f'{year} 年 {month} 月'
                    '<span>点日期看当天安排</span></div>'
                )
            with head_next:
                st.button("›", key="cal_next", on_click=_cal_shift, args=(1,),
                          width="stretch", help=_tip("› 下个月", "翻到下一个月的月历。"))

            wd_cols = st.columns(7, gap="small")
            for idx, col in enumerate(wd_cols):
                with col:
                    _html(f'<div class="cal-wd">{"一二三四五六日"[idx]}</div>')

            today = datetime.now().date()
            for week in _calendar_matrix(year, month):
                row_cols = st.columns(7, gap="small")
                for col, day in zip(row_cols, week):
                    with col:
                        if day is None:
                            _html('<div style="height:38px;"></div>')
                            continue
                        day_events = events_on_day(day)
                        has = len(day_events)
                        # 有安排的日期在日号后加一个中点，作为"有日程"的视觉提示
                        label = str(day.day) if not has else f"{day.day}·"
                        tip = (f"{day:%m月%d日} 有 {has} 项安排" if has
                               else f"{day:%m月%d日} 暂无安排")
                        if day == today:
                            tip += "（今天）"
                        st.button(
                            label,
                            key=f"cal_day_{day:%Y%m%d}",
                            on_click=_cal_pick,
                            args=(day,),
                            width="stretch",
                            type="primary" if day == selected else "secondary",
                            help=_tip(f"选中 {day:%m月%d日}", tip + "。点一下，右侧会列出这天的全部安排。"),
                        )

    with right:
        with st.container(border=True):
            st.button("↩ 回到今天", key="cal_today",
                      on_click=_cal_goto_today, width="stretch",
                      help=_tip("↩ 回到今天",
                                "不管当前翻到哪个月，都直接跳回本月并选中今天。"))

            sel_events = events_on_day(selected)
            weekday_cn = "一二三四五六日"[selected.weekday()]
            iso_week = selected.isocalendar().week
            head = (
                f'<div class="cal-side">'
                f'<h4>{selected:%Y 年 %m 月 %d 日} · 周{weekday_cn}'
                f'<span style="font-size:.6875rem;font-weight:600;color:var(--muted-2);'
                f'margin-left:8px;">第 {iso_week} 周</span></h4>'
            )
            if not sel_events:
                body = ('<div class="cal-empty">这一天没有安排。'
                        '可以到「录入解析」页追加日程，或在下方表格里新增一行。</div>')
            else:
                chunks: list[str] = []
                for ev in sel_events:
                    cls = "bad" if ev.conflict else ("vague" if ev.vague else "")
                    flag = "🔴 " if ev.conflict else ("⚠️ " if ev.vague else "")
                    extra = ""
                    if ev.location:
                        extra += f'<div class="s">📍 {_escape(ev.location)}</div>'
                    if ev.is_recurring:
                        extra += (f'<div class="s">🔁 '
                                  f'{_escape(describe_recurrence(ev.rrule))}</div>')
                    chunks.append(
                        f'<div class="cal-ev {cls}">'
                        f'<div class="t">{ev.start:%H:%M}</div>'
                        f'<div><div class="m">{flag}{_escape(ev.title)}</div>{extra}</div>'
                        f'</div>'
                    )
                body = "".join(chunks)

            month_total = sum(
                len(events_on_day(d))
                for w in _calendar_matrix(year, month) for d in w if d
            )
            tail = (
                '<div style="font-size:.75rem;color:var(--muted);margin-top:12px;'
                'padding-top:10px;border-top:1px dashed var(--line);">'
                f'本月共 <b>{month_total}</b> 项安排　·　'
                f'循环日程 {sum(1 for e in st.session_state.events if e.is_recurring)} 条'
                '</div>'
            )
            _html(head + body + tail + "</div>")


def render_schedule_tab() -> None:
    events: list[Event] = st.session_state.events

    if not events:
        _html(
            '<div class="notice notice-info" style="margin-top:16px;">'
            '📭 还没有日程。<br>'
            '请到「录入解析」页粘贴文字或上传截图，或点击「加载示例」快速体验。'
            '</div>'
        )
        if st.button(
            "📚 一键加载示例日程", type="primary",
            help=_tip("📚 一键加载示例日程",
                      "跳过录入步骤，直接把示例课表解析成 14 条日程，"
                      "用来快速看看表格、日历和导出长什么样。"),
        ):
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

    # --- 月历视图 ---
    _html('<div class="section-title">月历视图</div>')
    _render_calendar()

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
            "重复", width="medium", disabled=True,
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
                if st.button(
                    "删除选中这一条", key="del_btn",
                    help=_tip("删除选中这一条",
                              "从日程表里删掉下拉框选中的那一条，其他日程不受影响。"),
                ):
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
                render_clear_all_button("table", label="清空全部日程")

            # 二次确认（与录入页共用同一套逻辑），避免误点把现场演示数据全清空
            render_clear_all_confirm("table")
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
            color = "var(--accent)" if is_today else "var(--cal-day-muted)"
            st.markdown(
                f'<div style="text-align:center;font-size:.6875rem;font-weight:700;color:{color};">'
                f'{day:%m/%d}<br>周{weekday_cn[day.weekday()]}</div>',
                unsafe_allow_html=True,
            )
            day_events = [e for e in events if e.start.date() == day.date()]
            if not day_events:
                st.markdown(
                    '<div style="text-align:center;color:var(--muted-3);font-size:.6875rem;'
                    'padding:12px 0;">—</div>',
                    unsafe_allow_html=True,
                )
                continue
            for ev in sorted(day_events, key=lambda e: e.start)[:6]:
                if ev.conflict:
                    bg, fg = "var(--cal-bad-bg)", "var(--bad-strong)"
                elif ev.vague:
                    bg, fg = "var(--cal-vague-bg)", "var(--warn-strong)"
                else:
                    bg, fg = "var(--chip-info-bg)", "var(--chip-info-fg)"
                st.markdown(
                    f'<div style="background:{bg};color:{fg};border-radius:var(--r-sm);'
                    f'padding:6px 7px;margin-bottom:5px;font-size:.6875rem;line-height:1.35;">'
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
            help=_tip("日历名称",
                      "导入手机 / Outlook 后显示的日历分组名，留空则用「我的 AI 日程」。\n"
                      "只影响 .ics 里的 X-WR-CALNAME，不改变文件里的日程内容。"),
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
        f'其余按默认 <b>{RECUR_COUNT_DEFAULT}</b> 次（约一学期）兜底，'
        '可在「日程表 → 重复设置」里逐条修改开始日期与次数。<br>'
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
                help=_tip("📅 下载 .ics 日历",
                          "标准 iCalendar 格式，双击就能导入 iPhone / 安卓 / Outlook 日历，"
                          "提醒和重复规则一并写入。"),
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
                help=_tip("📊 下载 CSV 表格",
                          "每行一条日程的表格文件，带 UTF-8 BOM，"
                          "用 Excel / WPS 直接打开不会乱码。"),
            )
    with dl3:
        if md_text:
            st.download_button(
                "📝 下载 Markdown",
                data=md_text.encode("utf-8"),
                file_name=f"ai_schedule_{stamp}.md",
                mime="text/markdown",
                width="stretch",
                help=_tip("📝 下载 Markdown",
                          "按日期分组的纯文本表格，适合贴进笔记、文档或提交材料里。"),
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
    with st.expander("界面字太小 / 想要深色模式，在哪调？"):
        st.markdown(
            "点侧边栏顶部的 **⚙️ 设置与外观**：\n\n"
            "- **明暗模式**：浅色（奶油柔光）↔ 深色（暗夜紫），立刻生效\n"
            "- **界面字号**：小 / 标准 / 大 / 特大，按钮、表格、日历一起缩放\n\n"
            "这两个只影响本机显示，换台电脑打开还是默认外观；导出文件不受影响。\n"
            "同一个面板里还收着 **AI 解析状态**、**OCR 识别状态** 和 **默认提醒时间**。"
        )
    with st.expander("每个按钮是干什么的？"):
        st.markdown(
            "把鼠标停在任意按钮 / 下拉 / 开关上，停一小会儿就会弹出说明，"
            "格式是：**第一行写按钮名，空一行写它做什么**。\n\n"
            "例如停在「✨ 开始解析」上会看到：\n\n"
            "> **✨ 开始解析**\n>\n"
            "> 解析左侧文字和已上传的截图，把识别出的日程追加到日程表。"
            "不会覆盖已有内容；重复的条目会自动去重。"
        )
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
    with st.expander("为什么新录入的日程会和上一次的叠在一起？"):
        st.markdown(
            "这是**刻意设计**的：录入页每次解析都是**追加**，不会覆盖已录入的内容，"
            "方便你把微信记录、课表、备忘录分几次丢进来。\n\n"
            "想从空白开始，用录入页右侧的 **🗑️ 清空全部日程**（日程表页、侧边栏也有同一个入口）："
            "它会一次性清掉所有日程、解析报告、输入框里的文字和已上传的截图，"
            "点一次只是亮出确认条，再点 **确定清空** 才真正生效。\n\n"
            "注意区分两个按钮：**🧹 清空文本框** 只擦掉文字和截图，"
            "已录入的日程**不会**被动；**🗑️ 清空全部日程** 才是全清。"
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
        '开发者不承担责任。本应用为个人学习与演示作品，不提供任何形式的服务保证。<br>'
        '6. <strong>第三方日历</strong>：导出的 .ics 文件由你自行导入第三方日历软件，'
        '其数据处理遵循对应软件的隐私政策。'
        '</div>'
    )
    _html(
        '<div class="app-footer">'
        '<div>'
        f'<span class="fv">{APP_TITLE} · v{APP_VERSION}</span>'
        '<span class="fsep">|</span>'
        '<span>Streamlit + OCR + LLM</span>'
        '</div>'
        '<div class="fcredit">'
        '由 <span class="fmodel">'
        f'{APP_MODEL}'
        '</span> 模型在 <b>'
        f'{APP_BUILDER}'
        '</b> 构建</div>'
        '</div>'
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
    # 先初始化会话状态，_inject_css 才能读到用户选的明暗 / 字号
    init_state()
    _inject_css()

    _html(
        '<div class="hero">'
        '<div class="hero-badge">AI · OCR · ICS</div>'
        f'<h1>{APP_TITLE}</h1>'
        f'<p>{APP_SUBTITLE}</p>'
        '</div>'
    )

    render_sidebar()

    # 上一步操作留下的提示（st.toast 无法跨 rerun 存活，需中转）。
    # 放在 tabs 之前，这样无论在哪个页签操作都能立刻看到。
    if st.session_state.get("pending_toast"):
        st.toast(st.session_state.pending_toast)
        st.session_state.pending_toast = ""

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
