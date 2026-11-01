# -*- coding: utf-8 -*-
"""
selfcheck.py —— AI 智能日程收纳工具 自动化自查脚本
=====================================================

用途：在不打开浏览器的情况下，验证解析引擎、去重/冲突/导出、OCR 分支、
      异常路径是否符合预期。可直接复跑：

    python selfcheck.py            # 跑全部用例
    python selfcheck.py -v         # 打印每条用例细节

设计说明：
    OCR 用例使用 mock 引擎，因此**无需安装 OCR 依赖**也能验证代码路径；
    若本机已安装真实引擎，额外跑一条真实推理用例。
"""

from __future__ import annotations

import io
import sys
import traceback
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd

import app

VERBOSE = "-v" in sys.argv
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(condition), detail))
    mark = "PASS" if condition else "FAIL"
    if VERBOSE or not condition:
        print(f"  [{mark}] {name}" + (f"  → {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


def make_image(w: int = 600, h: int = 200) -> bytes:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (w, h), "white")
    ImageDraw.Draw(img).text((20, 80), "Tomorrow 3pm meeting", fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
def test_text_only() -> None:
    section("A. 纯文本输入与容错")
    check("空字符串不崩溃且返回空列表", app.parse_with_rules("") == [])
    check("纯空白不崩溃", app.parse_with_rules("   \n\t \n") == [])
    check("纯符号行被过滤",
          all("!" not in e.title for e in app.parse_with_rules("!@#$%\n明天9点开会")))

    long_text = "明天上午9点 开会 " * 3000
    check("超长文本不崩溃", isinstance(app.parse_with_rules(long_text), list),
          f"{len(long_text)} 字")


def test_time_parsing() -> None:
    section("B. 时间解析准确性")

    cases = [
        ("明天下午3点 项目会", "项目会", 15, 0),
        ("11月20日 23:59 交材料", "交材料", 23, 59),
        ("2026-12-01 14:00 答辩", "答辩", 14, 0),
        ("十一月五日 下午三点半 答辩", "答辩", 15, 30),
        ("下周三下午2点 班会", "班会", 14, 0),
        ("晚上8点半 讨论", "讨论", 20, 30),
        ("今天上午9点 早课", "早课", 9, 0),
    ]
    for text, keyword, hh, mm in cases:
        evs = app.parse_with_rules(text)
        ok = any(keyword in e.title and e.start.hour == hh and e.start.minute == mm for e in evs)
        got = f"{evs[0].start:%m-%d %H:%M} {evs[0].title}" if evs else "无结果"
        check(f"「{text}」→ {hh:02d}:{mm:02d}", ok, got)

    # 课表第几节
    evs = app.parse_with_rules("每周二 第3-4节 数据结构 计算机楼C401")
    ok = bool(evs) and evs[0].start.strftime("%H:%M") == "09:55"
    check("「第3-4节」按默认作息表换算为 09:55",
          ok, f"{evs[0].start:%H:%M}-{evs[0].end:%H:%M}" if evs else "")

    # 跨天
    evs = app.parse_with_rules("今晚22点到次日2点 通宵赶项目")
    ok = bool(evs) and evs[0].start.hour == 22 and evs[0].end.hour == 2 \
        and evs[0].end.date() > evs[0].start.date()
    check("跨天 22:00→次日02:00", ok,
          f"{evs[0].start:%m-%d %H:%M}→{evs[0].end:%m-%d %H:%M}" if evs else "")

    evs = app.parse_with_rules("23:30-06:00 夜班值守")
    ok = bool(evs) and evs[0].end.date() > evs[0].start.date()
    check("跨天 23:30→06:00", ok, "跨日" if ok else "未跨日")

    # 结束早于开始
    evs = app.parse_with_rules("11月5日 15:00-09:00 奇怪时间")
    ok = bool(evs) and evs[0].end > evs[0].start
    check("结束早于开始时自动顺延", ok)

    # 模糊时间标注
    evs = app.parse_with_rules("11月5日 3点 开会")
    ok = bool(evs) and evs[0].vague and evs[0].start.hour == 15
    check("裸「3点」推断为 15:00 且标记 vague", ok, evs[0].vague_reason if evs else "")

    evs = app.parse_with_rules("11月5日 开会")
    ok = bool(evs) and evs[0].vague and evs[0].start.hour == 9
    check("只有日期时默认 09:00 且标记 vague", ok, evs[0].vague_reason if evs else "")

    # 全天
    evs = app.parse_with_rules("12月5日 全天 志愿者活动")
    ok = bool(evs) and evs[0].start.hour == 0 and evs[0].end.hour == 23
    check("「全天」识别为 00:00-23:59", ok)


def test_structure_fields() -> None:
    section("C. 结构化字段提取")
    text = "下周三下午2点 在图书馆三楼开班会，务必到场\n班长：记得带材料"
    evs = app.parse_with_rules(text)
    e = evs[0]
    check("地点提取", "图书馆" in e.location, e.location)
    check("优先级=高（含「务必」）", e.priority == "高", e.priority)
    check("循环规则为空（单次事件）", e.rrule == "", e.rrule or "(空)")
    check("标题不含时间", "2点" not in e.title and "下午" not in e.title, e.title)

    evs = app.parse_with_rules("每周一三五 上午8点 晨读")
    check("每周一三五 → 三个 BYDAY",
          bool(evs) and evs[0].rrule == "FREQ=WEEKLY;BYDAY=MO,WE,FR", evs[0].rrule if evs else "")

    evs = app.parse_with_rules("工作日 早上7点 晨跑")
    check("工作日 → MO-FR",
          bool(evs) and "MO,TU,WE,TH,FR" in evs[0].rrule, evs[0].rrule if evs else "")

    evs = app.parse_with_rules("11月20日 23:59 前提交课程论文 DDL")
    check("DDL 识别为高优先级", bool(evs) and evs[0].priority == "高", evs[0].priority if evs else "")


def test_dedup_conflict() -> None:
    section("D. 去重 / 冲突 / 过期过滤")
    txt = "明天下午3点 项目组例会\n明天 15:00 项目组例会\n明天15:00 项目组例会"
    evs = app.parse_with_rules(txt)
    norm, rep = app.normalize_events(evs)
    check("三条重复文本合并为一条", len(norm) == 1, f"原始{len(evs)} 归一{len(norm)}")
    check("去重计数正确", rep["duplicated"] == 2, str(rep))

    now = datetime.now()
    past = now - timedelta(days=7)
    manual = [
        app.Event("已过期的会", past, past + timedelta(hours=1)),
        app.Event("未来的会", now + timedelta(days=1), now + timedelta(days=1, hours=1)),
        app.Event("每周循环", past, past + timedelta(hours=1), rrule="FREQ=WEEKLY"),
    ]
    norm2, rep2 = app.normalize_events(manual)
    titles = [e.title for e in norm2]
    check("过期单次事件被过滤", "已过期的会" not in titles, str(titles))
    check("过期循环事件被保留", "每周循环" in titles, str(titles))
    check("过期计数正确", rep2["expired"] == 1, str(rep2))

    a = app.Event("会A", now + timedelta(hours=1), now + timedelta(hours=3))
    b = app.Event("会B", now + timedelta(hours=2), now + timedelta(hours=4))
    c = app.Event("会C", now + timedelta(hours=5), now + timedelta(hours=6))
    norm3, rep3 = app.normalize_events([a, b, c])
    check("重叠事件被标为冲突", a.conflict and b.conflict and not c.conflict)
    check("冲突计数=2", rep3["conflicts"] == 2, str(rep3))
    check("冲突对被正确列出", len(app.find_conflict_pairs(norm3)) == 1)


def test_export() -> None:
    section("E. 导出格式（ICS / CSV / Markdown）")
    evs = app.parse_with_rules(app.build_sample_text())
    norm, _ = app.normalize_events(evs)
    check("示例数据可解析出多条日程", len(norm) >= 10, f"{len(norm)} 条")

    ics = app.build_ics(norm, "测试日历", 15, 16)
    check("ICS 含 VCALENDAR 头尾",
          ics.startswith("BEGIN:VCALENDAR") and "END:VCALENDAR" in ics)
    check("ICS 事件数与日程数一致", ics.count("BEGIN:VEVENT") == len(norm),
          f"{ics.count('BEGIN:VEVENT')} vs {len(norm)}")
    check("ICS 含 VALARM 提醒", "BEGIN:VALARM" in ics and "TRIGGER:-PT15M" in ics)
    # 默认提醒支持多选：每个时间点各写一条 VALARM
    ev_m = app.Event("多提醒", datetime(2026, 11, 5, 9, 0), datetime(2026, 11, 5, 10, 0))
    ics_m = app.build_ics([ev_m], "m", [30, 60, 120], 16)
    check("多选提醒：每个时间点各写一条 VALARM",
          ics_m.count("BEGIN:VALARM") == 3
          and all(t in ics_m for t in ("TRIGGER:-PT30M", "TRIGGER:-PT1H",
                                       "TRIGGER:-PT2H")))
    check("旧版单个分钟数仍兼容（int → 一条提醒）",
          app.build_ics([ev_m], "m", 15, 16).count("BEGIN:VALARM") == 1)
    check("ICS 使用 CRLF 换行", "\r\n" in ics)
    over = [l for l in ics.split("\r\n") if len(l.encode("utf-8")) > 75]
    check("ICS 每行不超过 75 字节（RFC 5545）", not over, f"超长行 {len(over)}")
    check("ICS 循环事件带结束条件（COUNT 或 UNTIL）",
          "UNTIL=" in ics or "COUNT=" in ics or "RRULE" not in ics)

    # 时区：UTC+8 → UTC
    ev = app.Event("时区测试", datetime(2026, 11, 5, 9, 0), datetime(2026, 11, 5, 10, 0))
    single = app.build_ics([ev], "tz", 15, 16)
    check("本地 09:00(+8) 正确转为 01:00Z",
          "DTSTART:20261105T010000Z" in single,
          [l for l in single.split("\r\n") if "DTSTART" in l][0])

    # 转义
    ev2 = app.Event("含,逗号;分号\\反斜杠\n换行", datetime(2026, 11, 6, 9, 0),
                    datetime(2026, 11, 6, 10, 0))
    esc = app.build_ics([ev2], "esc", 0, 16)
    check("ICS 特殊字符已转义", "\\," in esc and "\\;" in esc and "\\\\" in esc and "\\n" in esc)

    csv_bytes = app.build_csv(norm, 15)
    check("CSV 带 UTF-8 BOM（Excel 不乱码）", csv_bytes[:3] == b"\xef\xbb\xbf")
    check("CSV 行数 = 日程数 + 表头",
          len(csv_bytes.decode("utf-8-sig").strip().splitlines()) == len(norm) + 1)

    md = app.build_markdown(norm, 15)
    check("Markdown 含表格结构", "| 时间 |" in md and md.count("| --- |") >= 1)
    check("Markdown 标题正确", md.startswith("# 📅 我的日程表"))


def test_recurrence() -> None:
    section("E2. 重复设置（开关 / 频率 / 次数）")
    start = datetime(2026, 11, 4, 14, 0)      # 2026-11-04 是周三

    # --- 频率构造 ---
    weekly = app.build_recur_rrule(app.RECUR_FREQ_WEEKLY, start)
    biweek = app.build_recur_rrule(app.RECUR_FREQ_BIWEEKLY, start)
    daily = app.build_recur_rrule(app.RECUR_FREQ_DAILY, start)
    check("每周重复 → FREQ=WEEKLY;BYDAY=WE", weekly == "FREQ=WEEKLY;BYDAY=WE", weekly)
    check("每两周重复 → INTERVAL=2",
          biweek == "FREQ=WEEKLY;INTERVAL=2;BYDAY=WE", biweek)
    check("每天重复 → FREQ=DAILY", daily == "FREQ=DAILY", daily)

    # 已有 BYDAY 时应保留（每周一三五 改成每两周，不能只剩一个日子）
    kept = app.build_recur_rrule(app.RECUR_FREQ_BIWEEKLY, start,
                                 "FREQ=WEEKLY;BYDAY=MO,WE,FR")
    check("切换频率时保留原 BYDAY", kept == "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE,FR", kept)

    # --- 次数（COUNT）---
    with_count = app.set_recur_count(weekly, 10)
    check("写入 COUNT", with_count == "FREQ=WEEKLY;BYDAY=WE;COUNT=10", with_count)
    check("读取 COUNT", app.get_recur_count(with_count) == 10)
    check("替换已有 COUNT",
          app.set_recur_count("FREQ=WEEKLY;BYDAY=WE;COUNT=5", 8).endswith("COUNT=8"))
    check("写 COUNT 时清除 UNTIL（RFC 互斥）",
          "UNTIL" not in app.set_recur_count(
              "FREQ=WEEKLY;BYDAY=WE;UNTIL=20261231T000000Z", 3))
    check("次数归零时移除 COUNT",
          app.set_recur_count("FREQ=WEEKLY;BYDAY=WE;COUNT=5", 0) == "FREQ=WEEKLY;BYDAY=WE")

    # --- 可读文案与反解 ---
    check("文案：每周三 · 共 10 次",
          app.describe_recurrence(with_count) == "每周三 · 共 10 次",
          app.describe_recurrence(with_count))
    check("文案：每两周的周三",
          app.describe_recurrence(biweek) == "每两周的周三",
          app.describe_recurrence(biweek))
    check("文案：每天", app.describe_recurrence(daily) == "每天")
    check("文案：空规则 → 空串", app.describe_recurrence("") == "")
    check("反解：每周", app.recur_freq_key(weekly) == app.RECUR_FREQ_WEEKLY)
    check("反解：每两周", app.recur_freq_key(biweek) == app.RECUR_FREQ_BIWEEKLY)
    check("反解：每天", app.recur_freq_key(daily) == app.RECUR_FREQ_DAILY)
    check("反解：无法归类返回空", app.recur_freq_key("FREQ=MONTHLY") == "")

    # --- 导出：COUNT 与 UNTIL 互斥 ---
    ev_count = app.Event("带次数", start, start + timedelta(hours=1),
                         rrule=with_count)
    ev_until = app.Event("按周数兜底", start, start + timedelta(hours=1),
                         rrule=weekly)
    ics = app.build_ics([ev_count, ev_until], "recur", 0, 16)
    lines = [l for l in ics.split("\r\n") if l.startswith("RRULE:")]
    check("带次数的日程导出 COUNT", any("COUNT=10" in l for l in lines), str(lines))
    check("带次数的日程不再追加 UNTIL",
          not any("COUNT=" in l and "UNTIL=" in l for l in lines),
          str(lines))
    check("未指定次数的日程仍按周数补 UNTIL",
          any("UNTIL=" in l and "COUNT=" not in l for l in lines), str(lines))

    # --- 表格展示与往返保真 ---
    rows = [ev_count.to_row(), ev_until.to_row()]
    check("表格「重复」列是可读文案而非 RRULE",
          "FREQ=" not in str(rows[0]["循环"]), str(rows[0]["循环"]))
    check("隐藏列 rrule_raw 保存原始规则",
          rows[0]["rrule_raw"] == with_count and rows[1]["rrule_raw"] == weekly)

    df = app.events_to_dataframe([ev_count, ev_until])
    check("DataFrame 含隐藏列 rrule_raw", "rrule_raw" in df.columns)
    back, _, _ = app.dataframe_to_events(df, [ev_count, ev_until])
    check("表格往返后 RRULE 不变",
          [e.rrule for e in back] == [with_count, weekly],
          str([e.rrule for e in back]))

    # 空单元格（NaN / pd.NA）不得被当成规则写回
    for blank in (None, np.nan, pd.NA):
        row = {"标题": "x", "rrule_raw": blank, "循环": "每周三"}
        ev = app.Event.from_row(row, app.Event("t", start, start + timedelta(hours=1)))
        check(f"空单元格 {type(blank).__name__} 不写入 RRULE", ev.rrule == "", ev.rrule)

    # 次数步进器的夹取
    check("次数下限夹在 1", max(app.RECUR_COUNT_MIN, min(app.RECUR_COUNT_MAX, 0)) == 1)
    check("次数上限夹在 60", max(app.RECUR_COUNT_MIN, min(app.RECUR_COUNT_MAX, 999)) == 60)

    # --- 循环开始日期：日期变了，BYDAY 必须跟着变 ---
    moved = datetime(2026, 11, 6, 14, 0)          # 挪到周五
    check("改开始日期后 BYDAY 跟着变成周五",
          "BYDAY=FR" in app.build_recur_rrule(app.RECUR_FREQ_WEEKLY, moved),
          app.build_recur_rrule(app.RECUR_FREQ_WEEKLY, moved))


def test_calendar_and_sample() -> None:
    section("E3. 日历（月历 / 当日命中）与江苏大学示例数据")

    # --- 某条日程是否在某天发生 ---
    monday_ev = app.Event("高数", datetime(2026, 11, 2, 8, 0),
                          datetime(2026, 11, 2, 9, 40),
                          rrule="FREQ=WEEKLY;BYDAY=MO;COUNT=3")
    check("循环日程在周一发生", app._occurs_on(monday_ev, date(2026, 11, 9)))
    check("循环日程不在周二发生", not app._occurs_on(monday_ev, date(2026, 11, 10)))
    check("循环开始日之前不发生", not app._occurs_on(monday_ev, date(2026, 10, 26)))
    check("COUNT 内最后一次仍发生", app._occurs_on(monday_ev, date(2026, 11, 16)))
    check("超过 COUNT 后不再发生", not app._occurs_on(monday_ev, date(2026, 11, 23)),
          "第 4 个周一应已结束")

    daily_ev = app.Event("晨跑", datetime(2026, 11, 2, 7, 0),
                         datetime(2026, 11, 2, 7, 30), rrule="FREQ=DAILY;COUNT=5")
    check("每天重复在次日发生", app._occurs_on(daily_ev, date(2026, 11, 3)))
    check("每天重复在第 5 天后停止", not app._occurs_on(daily_ev, date(2026, 11, 7)))

    two_day = app.Event("大物 A(II)", datetime(2026, 11, 3, 16, 0),
                        datetime(2026, 11, 3, 17, 40),
                        rrule="FREQ=WEEKLY;BYDAY=TU,TH;COUNT=16")
    check("周二/周四双日循环都命中",
          app._occurs_on(two_day, date(2026, 11, 5))
          and app._occurs_on(two_day, date(2026, 11, 10)))
    check("双日循环在周三不命中", not app._occurs_on(two_day, date(2026, 11, 4)))

    biweek = app.Event("两周一次", datetime(2026, 11, 2, 19, 0),
                       datetime(2026, 11, 2, 20, 0),
                       rrule="FREQ=WEEKLY;INTERVAL=2;BYDAY=MO;COUNT=8")
    check("每两周重复在下一周不命中", not app._occurs_on(biweek, date(2026, 11, 9)))
    check("每两周重复在间隔周命中", app._occurs_on(biweek, date(2026, 11, 16)))

    single = app.Event("一次性", datetime(2026, 11, 7, 10, 0),
                       datetime(2026, 11, 7, 11, 0))
    check("单次日程只在当天命中",
          app._occurs_on(single, date(2026, 11, 7))
          and not app._occurs_on(single, date(2026, 11, 8)))

    # --- 月历矩阵（周一开头）---
    weeks = app._calendar_matrix(2026, 10)
    check("月历矩阵每行 7 天", all(len(w) == 7 for w in weeks))
    check("2026-10-01 是周四 → 首行前 3 格留空",
          weeks[0][:3] == [None, None, None] and weeks[0][3] == date(2026, 10, 1))
    check("月历矩阵覆盖整月 31 天",
          sum(1 for w in weeks for d in w if d) == 31)
    check("月历矩阵日期连续不重复",
          sorted(d for w in weeks for d in w if d) ==
          sorted(date(2026, 10, i) for i in range(1, 32)))

    # --- 示例数据：江苏大学真实场景 ---
    sample = app.build_sample_text()
    check("示例含江大教学楼",
          all(k in sample for k in ("三江楼", "三山楼", "能动楼", "京江")), sample[:40])
    check("示例含循环课表字眼", "每周" in sample)

    parsed = app.parse_with_rules(sample)
    check("示例可解析出多条日程", len(parsed) >= 10, f"{len(parsed)} 条")
    locs = " ".join(e.location for e in parsed)
    check("「三江楼0802」被识别为地点", "三江楼" in locs, locs)
    check("「能动楼301」被识别为地点", "能动楼" in locs, locs)
    check("「京江2号楼2205」被识别为地点", "京江2号楼" in locs, locs)
    check("「每周二、周四」识别为两天",
          any("TU" in e.rrule and "TH" in e.rrule for e in parsed),
          str([e.rrule for e in parsed if e.is_recurring]))

    norm, _ = app.normalize_events(parsed)
    check("默认不循环：单次日程 rrule 为空",
          any(not e.is_recurring for e in norm),
          "示例里应同时存在单次与循环日程")
    # 旧行为：所有循环日程被强塞 COUNT=16，等于用网站默认值覆盖图片规则。
    # 新行为：图片写了多少次就是多少次，没写就不虚构。
    base = app.Event(title="课", start=datetime(2026, 11, 2, 8, 0),
                     end=datetime(2026, 11, 2, 9, 30))
    keep8 = app.Event(title="课", start=datetime(2026, 11, 2, 8, 0),
                      end=datetime(2026, 11, 2, 9, 30),
                      rrule="FREQ=WEEKLY;BYDAY=MO;COUNT=8")
    n2, _ = app.normalize_events([keep8])
    check("图片写了 8 次就保留 8 次，不被全局默认 16 覆盖",
          app.get_recur_count(n2[0].rrule) == 8, n2[0].rrule)

    no_count = app.Event(title="课", start=datetime(2026, 11, 2, 8, 0),
                         end=datetime(2026, 11, 2, 9, 30),
                         rrule="FREQ=WEEKLY;BYDAY=MO")
    n3, _ = app.normalize_events([no_count])
    check("图片没写次数时不虚构 COUNT",
          app.get_recur_count(n3[0].rrule) == 0, n3[0].rrule)


def test_ocr() -> None:
    section("F. OCR 分支（mock 引擎，无需安装 OCR 依赖）")
    data = make_image()
    original = app.get_ocr_engine

    try:
        class TupleEngine:  # rapidocr 1.x: (result, elapse)
            def __call__(self, arr):
                return ([[[[0, 0], [10, 0], [10, 10], [0, 10]], "明天下午3点 例会", 0.93],
                         [[[0, 20], [10, 20], [10, 30], [0, 30]], "教学楼A302", 0.88]], 0.05)

        app.get_ocr_engine = lambda: ("rapidocr", TupleEngine())
        r = app.ocr_image(data)
        check("1.x tuple 返回格式解析", r["ok"] and "例会" in r["text"], repr(r["text"]))
        check("置信度计算", r["confidence"] is not None and r["confidence"] > 0.8,
              str(r["confidence"]))

        class ObjEngine:  # rapidocr 3.x: 带 txts/scores 的对象
            def __call__(self, arr):
                return SimpleNamespace(txts=["11月5日 上午9点 答辩"], scores=[0.91])

        app.get_ocr_engine = lambda: ("rapidocr", ObjEngine())
        r = app.ocr_image(data)
        check("3.x 对象返回格式解析", r["ok"] and "答辩" in r["text"], repr(r["text"]))

        class LowEngine:
            def __call__(self, arr):
                return ([[[[0, 0], [10, 0], [10, 10], [0, 10]], "模糊内容文字", 0.4]], 0.05)

        app.get_ocr_engine = lambda: ("rapidocr", LowEngine())
        r = app.ocr_image(data)
        check("低置信度给出警告", r["ok"] and any("置信度" in w for w in r["warnings"]),
              str(r["warnings"]))

        class BoomEngine:
            def __call__(self, arr):
                raise RuntimeError("onnx crashed")

        app.get_ocr_engine = lambda: ("rapidocr", BoomEngine())
        r = app.ocr_image(data)
        check("引擎崩溃被捕获且返回友好提示",
              (not r["ok"]) and "识别失败" in r["error"], r["error"][:40])

        class EmptyEngine:
            def __call__(self, arr):
                return ([], 0.01)

        app.get_ocr_engine = lambda: ("rapidocr", EmptyEngine())
        r = app.ocr_image(data)
        check("识别结果为空时提示重新截图", (not r["ok"]) and "有效文字" in r["error"],
              r["error"][:40])

        app.get_ocr_engine = lambda: ("none", None)
        r = app.ocr_image(data)
        check("OCR 未安装时提示安装命令", (not r["ok"]) and "pip install" in r["error"],
              r["error"][:40])
    finally:
        app.get_ocr_engine = original

    # 图片校验分支（不依赖引擎）
    r = app.ocr_image(b"not-an-image")
    check("损坏图片被拒绝", (not r["ok"]) and "损坏" in r["error"], r["error"][:36])

    r = app.ocr_image(b"")
    check("空图片被拒绝", (not r["ok"]) and "为空" in r["error"], r["error"][:36])

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (10, 10), "white").save(buf, "PNG")
    r = app.ocr_image(buf.getvalue())
    check("过小图片被拒绝", (not r["ok"]) and "过小" in r["error"], r["error"][:36])

    r = app.ocr_image(b"x" * (11 * 1024 * 1024))
    check("超 10MB 图片被拒绝", (not r["ok"]) and "过大" in r["error"], r["error"][:36])

    buf = io.BytesIO()
    Image.new("RGB", (5000, 4000), "white").save(buf, "PNG")
    r = app.ocr_image(buf.getvalue())
    check("超大像素图被等比缩放", any("缩放" in w for w in r["warnings"]), str(r["warnings"]))

    # 真实引擎（如果装了）
    engine_name, _ = original()
    if engine_name != "none":
        print(f"  [INFO] 检测到真实 OCR 引擎：{engine_name}，执行真实推理用例…")
        try:
            r = app.ocr_image(make_image())
            check("真实引擎可执行（不抛异常）", r["ok"] or bool(r["error"]),
                  f"ok={r['ok']}")
        except Exception:  # noqa: BLE001
            check("真实引擎可执行（不抛异常）", False, traceback.format_exc(limit=2))
    else:
        print("  [INFO] 未安装真实 OCR 引擎，跳过真实推理（mock 分支已覆盖代码路径）")


def test_ai_layer() -> None:
    section("G. AI 层（无密钥 / 异常分支）")
    cfg = app.get_ai_config()
    check("无密钥时 available=False", cfg["available"] is False)
    check("默认 base_url 合理", cfg["base_url"].startswith("http"), cfg["base_url"])

    samples = [
        ('{"events":[{"title":"a"}]}', True),
        ('```json\n{"events":[]}\n```', True),
        ('前缀文字\n{"events":[{"title":"b"}]}\n后缀', True),
        ('[{"title":"c"}]', True),
        ("完全没有 JSON 的文本", False),
    ]
    for raw, should_ok in samples:
        try:
            app._extract_json_block(raw)
            check(f"JSON 提取：{raw[:22]!r}", should_ok)
        except app.AIError:
            check(f"JSON 提取：{raw[:22]!r}（应失败）", not should_ok)

    bad = dict(cfg)
    bad.update(api_key="sk-fake", base_url="http://127.0.0.1:9", timeout=2.0)
    try:
        app.parse_with_ai("明天下午3点开会", bad)
        check("不可达地址应抛 AIError", False)
    except app.AIError as exc:
        check("不可达地址返回友好 AIError", True, str(exc)[:46])
    except Exception as exc:  # noqa: BLE001
        check("不可达地址返回友好 AIError", False, f"抛出未包装异常 {type(exc).__name__}")


def test_merge() -> None:
    section("H. 双引擎结果融合")
    now = datetime.now().replace(microsecond=0)
    ai_ev = app.Event("项目组例会", now + timedelta(days=1), now + timedelta(days=1, hours=1),
                      location="", note="", priority="中")
    rule_ev = app.Event("项目组例会", now + timedelta(days=1), now + timedelta(days=1, hours=1),
                        location="腾讯会议", note="来源：班长", priority="高")
    merged = app.merge_ai_and_rules([ai_ev], [rule_ev])
    check("融合后条数不重复", len(merged) == 1, str(len(merged)))
    check("AI 空缺字段由规则补全", merged[0].location == "腾讯会议", merged[0].location)
    check("优先级由规则补全", merged[0].priority == "高", merged[0].priority)

    only_rule = app.Event("另一个会", now + timedelta(days=3), now + timedelta(days=3, hours=1))
    merged2 = app.merge_ai_and_rules([ai_ev], [rule_ev, only_rule])
    check("AI 遗漏条目被补齐", len(merged2) == 2, str(len(merged2)))
    check("补齐条目标记为待核对",
          any(e.vague and "规则引擎" in e.vague_reason for e in merged2))


def test_timetable() -> None:
    section("J. 课表版面重建（网格 / 单元格解析）")
    from app import OcrBlock

    # --- 1. 网格识别：5 个以上星期表头 + 左侧节次编号才算课表 -----------
    def block(text, x, y, w=40, h=14):
        return OcrBlock(text, 0.95, x, y, x + w, y + h)

    blocks = []
    for i, wd in enumerate("一二三四五六日"):
        blocks.append(block(f"星期{wd}", 120 + i * 150, 10))
    for p in range(1, 13):
        blocks.append(block(str(p), 20, 40 + (p - 1) * 60))
    blocks.append(block("高等数学 王老师 W9阶 2-6,9-18", 130, 60, 120, 20))
    grid = app._detect_timetable_grid(blocks)
    check("认出 7 列 × 12 节的课表网格",
          grid is not None and grid.weekday_count == 7 and grid.period_count == 12,
          f"{grid.weekday_count if grid else '-'} × {grid.period_count if grid else '-'}")

    # 普通聊天截图（没有星期表头）不应被误判成课表
    plain = [block(f"第{i}行随便写的字", 20, i * 30) for i in range(20)]
    check("普通截图不会被误判为课表", app._detect_timetable_grid(plain) is None)

    # --- 2. 单元格解析：各种真实书写形态 --------------------------------
    cases = [
        # 输入、期望 title / teacher / location / weeks / 单双周
        # W 前缀是楼号的一部分，必须保留（曾丢 W 变成「9阶」，用户点破）
        ("高等数学(一)(上)卜 庆刚 W9阶 2-6,9-18, /",
         "高等数学(一)(上)", "卜庆刚", "W9阶", "2-6、9-18", ""),
        ("创新创业理论与实践(1) 李宁 W文约209 双 12-18, /",
         "创新创业理论与实践(1)", "李宁", "W文约209", "12-18", "双周"),
        ("问题求解与程序设计( 上)官威 W文约101 2- 6,9-15 , /",
         "问题求解与程序设计(上)", "官威", "W文约101", "2-6、9-15", ""),
        ("体育(1) 体育部 W科教 城体育场 2-17，/",
         "体育(1)", "体育部", "W科教城体育场", "2-17", ""),
        ("新生研讨课 官威 W14 06 2-6,9-11, /",
         "新生研讨课", "官威", "W1406", "2-6、9-11", ""),
        ("统计学 吴敏 W经济楼10 81-12,/",          # 房间号被挪了一位
         "统计学", "吴敏", "W经济楼108", "1-12", ""),
        ("俄罗斯历史 Okhotnik ov Egor W9阶 9-12, 1",
         "俄罗斯历史", "Okhotnikov Egor", "W9阶", "9-12", ""),
        ("综合英语| 刘畅 W文约 101 2-6,9-18,/",     # 罗马数字 I 被读成竖线
         "综合英语I", "刘畅", "W文约101", "2-6、9-18", ""),
        ("马克思主义基本原理 陈静 WB105 2-6,9-16 .1",
         "马克思主义基本原理", "陈静", "WB105", "2-6、9-16", ""),
        ("形势与政策(1) 冯慧 WJ1117 17-18, /",
         "形势与政策(1)", "冯慧", "WJ1117", "17-18", ""),
        # 英文教师名里的小写 W 不是楼号，不能干扰地点匹配
        ("大学英语(2) Wang W2机房 1-16周, /",
         "大学英语(2)", "Wang", "W2机房", "1-16", ""),
    ]
    for text, title, teacher, loc, weeks, oe in cases:
        info = app._split_course_cell(text)
        got = (info["title"], info["teacher"], info["location"],
               app._weeks_label(info["weeks"]), info["odd_even"])
        check(f"解析「{text[:12]}…」",
              got == (title, teacher, loc, weeks, oe),
              f"得 {got}")

    # --- 3. 一格两门课 + 跨节重复 ---------------------------------------
    two = app._split_cell_courses(
        "形势与政策(1) 冯慧 W1111 17-18, / 俄罗斯历史 Okhotnikov Egor W9阶 9-12, /")
    check("一格两门课被拆开", len(two) == 2, str([t["title"] for t in two]))
    dup = app._split_cell_courses("体育(1) 体育部 W9阶 2-17, / 体育(1) 体育部 W9阶 2-17, /")
    check("一格内重复的同一门课去重", len(dup) == 1, str(len(dup)))

    # --- 4. 课表 → 日程：节次时刻、周次、单双周 -------------------------
    cells = [{
        "weekday": 0, "period": 1, "periods": (1, 2),
        "text": "高等数学(一)(上) 卜庆刚 W9阶 2-6,9-18, /",
    }]
    anchor = date(2026, 9, 7)          # 一个周一
    events = app.timetable_to_events(cells, anchor)
    check("课表转成 1 条日程", len(events) == 1, str(len(events)))
    if events:
        ev = events[0]
        check("上课日期落在第 2 周的周一",
              ev.start.date() == date(2026, 9, 14), str(ev.start))
        check("第 1-2 节对应 08:00-09:35",
              (ev.start.strftime("%H:%M"), ev.end.strftime("%H:%M")) == ("08:00", "09:35"),
              f"{ev.start:%H:%M}-{ev.end:%H:%M}")
        check("周次写进备注", "第 2-6、9-18 周" in ev.note, ev.note)
        check("循环规则按总周数生成", "COUNT=15" in ev.rrule, ev.rrule)
        check("课表日程标记为待核对", ev.vague)

    double = app.timetable_to_events([{
        "weekday": 2, "period": 1, "periods": (1, 2),
        "text": "创新创业理论与实践(1) 李宁 W文约209 双 12-18, /",
    }], anchor)
    check("双周课用 INTERVAL=2 且从第 12 周开始",
          bool(double) and "INTERVAL=2" in double[0].rrule
          and double[0].start.isocalendar()[1] >= 1,
          double[0].rrule if double else "")


def test_time_blocks() -> None:
    section("L. 时间块归并（字段列表型截图）")

    # 教务系统课程详情：一个时间节点 + 若干字段行
    text = (
        "2026 年 10 月 29 日 · 周四 第 44 周\n"
        "10:45\n场地：回龙2311(多)\n校区：回龙\n教师：张伟\n"
        "课程性质简称：必修\n选课备注：\n重修标：\n每周四 · 共 16 次\n"
        "14:50\n职业生涯发展与规划(6-7节)\n校区：回龙\n场地：回龙1212(多)\n"
        "课程性质简称：必修\n每周四 · 共 16 次\n"
    )
    events = app.parse_with_rules(text)
    check("事件数等于时间节点数（不是字段数）", len(events) == 2, str(len(events)))

    by_time = {e.start.strftime("%H:%M"): e for e in events}
    first, second = by_time.get("10:45"), by_time.get("14:50")

    check("没课名的时间块用「时间 日程」兜底",
          bool(first) and first.title.strip() == "10:45 日程",
          first.title if first else "缺失")
    check("属性词没有变成标题",
          all(b not in e.title for e in events
              for b in ("场地", "校区", "教师", "必修", "备注", "重修标")),
          " | ".join(e.title for e in events))
    check("具体场地进 location", bool(first) and "回龙2311" in first.location,
          first.location if first else "")
    check("校区并入 location 而非覆盖", bool(second) and "回龙1212" in second.location,
          second.location if second else "")
    check("教师 / 课程性质进备注",
          bool(first) and "张伟" in first.note and "必修" in first.note,
          first.note if first else "")
    check("「共 16 次」转成 COUNT",
          bool(first) and "COUNT=16" in first.rrule, first.rrule if first else "")
    check("课程名被正确选为主标题",
          bool(second) and second.title.startswith("职业生涯发展与规划"),
          second.title if second else "")
    check("字段列表型日程标记为待核对",
          all(e.vague for e in events), str([e.vague for e in events]))

    # 无冒号 / 无分隔符：教务系统复制出来常丢掉分隔符
    plain = ("2026年10月30日·周五第44周\n08:00\n高等数学(一)(上)\n"
             "场地三江楼0802\n教师卜庆刚\n课程性质简称必修\n每周四·共16次\n")
    ev2 = app.parse_with_rules(plain)
    check("无冒号的字段行也能归并", len(ev2) == 1, str(len(ev2)))
    check("无冒号时地点仍正确",
          bool(ev2) and "三江楼0802" in ev2[0].location,
          ev2[0].location if ev2 else "")

    # 节次式时间节点
    per = ("2026年10月29日 周四\n第1-2节\n数据结构\n场地：三江楼0401\n"
           "教师：王强\n课程性质简称：必修\n")
    ev3 = app.parse_with_rules(per)
    check("「第1-2节」也能当时间节点", len(ev3) == 1, str(len(ev3)))
    check("节次换算成钟点",
          bool(ev3) and ev3[0].start.strftime("%H:%M") == "08:00",
          ev3[0].start.strftime("%H:%M") if ev3 else "")

    # 反例：普通备忘不能被当成字段列表合并
    memo = "周三 下午2点 项目评审 会议室\n周四 上午9点 面试 线上"
    check("普通两行备忘不会被合并", len(app.parse_with_rules(memo)) == 2,
          str(len(app.parse_with_rules(memo))))

    # 反例：车票识别不受影响
    trip = app.parse_with_rules("G7045 09:56 镇江 10:27 常州 2026年10月31日")
    check("车票仍识别为 1 条行程", len(trip) == 1, str([e.title for e in trip]))

    # 12306 订单页：车次单独占一行，前后没有「乘/站」字样，靠票据特征词兜底
    t12306 = app.parse_with_rules(
        "订单号：EN94148162\n复制\n车票当日当次有效\n19:42\nG7520\n20:05\n"
        "常州\n10月31日(周六)\n镇江\n已退票\n成人票\n二等座02车01F号")
    check("12306 订单页（车次独占一行）识别为 1 条行程",
          len(t12306) == 1, str([e.title for e in t12306]))
    check("12306 行程日期取自票面（10-31）",
          bool(t12306) and t12306[0].start.strftime("%m-%d") == "10-31",
          t12306[0].start.strftime("%Y-%m-%d") if t12306 else "")
    check("座位 / 订单号 / 退票状态写进备注",
          bool(t12306) and "已退票" in t12306[0].note
          and "EN94148162" in t12306[0].note,
          t12306[0].note if t12306 else "")

    # 机票行程卡：航班号 + 起飞/降落 + 机场（含航站楼），整段合成一条
    flight = app.parse_with_rules(
        "去点评\n07-27\n吉祥航空 HO1105\n2026\n"
        "08:45 上海浦东T2\n10:50 厦门高崎 T4")
    check("机票行程卡识别为 1 条（不再拆成起飞/降落两条）",
          len(flight) == 1, str([e.title for e in flight]))
    check("机票标题带航班号与航线",
          bool(flight)
          and flight[0].title == "HO1105 上海浦东T2 → 厦门高崎T4",
          flight[0].title if flight else "")
    check("机票日期取票面年份（07-27 · 2026）",
          bool(flight)
          and flight[0].start.strftime("%Y-%m-%d") == "2026-07-27",
          flight[0].start.strftime("%Y-%m-%d") if flight else "")
    check("机票地点是出发机场（含航站楼）",
          bool(flight) and flight[0].location == "上海浦东T2",
          flight[0].location if flight else "")

    # 订单号 EN94148162 是「2 字母 + 8 位数字」，不能被当成航班号
    check("订单号不会被当成航班号",
          bool(flight) and "EN" not in flight[0].title)
    import re as _re
    m_class = _re.search(r"(?<![A-Za-z0-9])([GDCKZT]\d{1,4})(?!\d)", "计算机楼C102")
    check("教室编号仍不算车次",
          m_class is not None and not app._train_like(m_class, "计算机楼C102"))

    # 过期 ≠ 丢失：normalize 把过期条目带回报告，补录时 keep_past 放行
    now = datetime(2026, 11, 1, 15, 0)
    past_ev = app.Event("HO1105", datetime(2026, 7, 27, 8, 45),
                        datetime(2026, 7, 27, 10, 50))
    future_ev = app.Event("开会", datetime(2026, 11, 3, 9),
                          datetime(2026, 11, 3, 10))
    kept, rep = app.normalize_events([past_ev, future_ev], now=now)
    check("过期日程默认被过滤", [e.title for e in kept] == ["开会"],
          str([e.title for e in kept]))
    check("被过滤的条目会带回报告（页面上要能说清识别到了什么）",
          [e.title for e in rep.get("expired_events", [])] == ["HO1105"])
    kept2, _rep2 = app.normalize_events([past_ev, future_ev], now=now,
                                        keep_past=True)
    check("补录模式（keep_past）不过滤过去行程",
          [e.title for e in kept2] == ["HO1105", "开会"])

    # 报告文案：只有过期时不再误写「全部为重复日程」，并给出补录入口
    src_all = open("app.py", encoding="utf-8").read()
    check("全部过期时不再误报「全部为重复日程」",
          'rep["all_duplicate"] = dup > 0' in src_all and '"all_expired"' in src_all)
    check("报告里提供「仍然添加」补录入口",
          "add_expired" in src_all and "def _cb_add_expired" in src_all)

    # --- 会议邀请（腾讯会议 / 钉钉） ------------------------------------
    meet = app.parse_with_rules(
        "【腾讯会议】\n会议主题：算法组周会\n"
        "会议时间：2026/11/05 19:30-21:00 (GMT+08:00)\n会议号：123 456 789\n"
        "点击链接入会：https://meeting.tencent.com/dm/qAbCdEfGh",
        anchor=datetime(2026, 11, 1))
    check("腾讯会议卡识别为 1 条",
          len(meet) == 1, str([e.title for e in meet]))
    check("会议标题取「会议主题」而不是时间行",
          bool(meet) and meet[0].title == "算法组周会",
          meet[0].title if meet else "")
    check("会议时间含 GMT 尾巴也能取对",
          bool(meet) and meet[0].start.strftime("%m-%d %H:%M") == "11-05 19:30"
          and meet[0].end.strftime("%H:%M") == "21:00",
          f"{meet[0].start:%m-%d %H:%M}" if meet else "")
    check("会议号进了备注",
          bool(meet) and "123456789" in meet[0].note,
          meet[0].note if meet else "")

    meet_w = app.parse_with_rules(
        "【腾讯会议】\n会议主题：研究生组会\n会议时间：每周三 19:30-21:00\n"
        "会议号：456 789 123", anchor=datetime(2026, 11, 2))
    check("「每周三」循环会议：日期推进到最近的周三且带循环规则",
          len(meet_w) == 1
          and meet_w[0].start.strftime("%A") == "Wednesday"
          and meet_w[0].rrule == "FREQ=WEEKLY;BYDAY=WE",
          f"{meet_w[0].start if meet_w else None} "
          f"{meet_w[0].rrule if meet_w else ''}")

    meet_d = app.parse_with_rules(
        "【钉钉会议】\n主题：项目日会\n时间：11月9日 09:30-09:50\n会议ID：222 333 444",
        anchor=datetime(2026, 11, 1))
    check("钉钉会议（无年份日期）识别正常",
          len(meet_d) == 1 and meet_d[0].start.strftime("%m-%d") == "11-09",
          str([(e.start, e.title) for e in meet_d]))

    # 腾讯会议「Started by」版式：没有「时间：」字段，时刻 / 日期 / 会议号
    # 都是独立行（真实用户截图的布局）
    started = app.parse_with_rules(
        "Started by 冯昊诚\n364 293 371\n18:23\n19:23\n1 hrs\n"
        "2026/11/01\n(GMT+08:00)\n2026/11/01\n腾讯会议",
        anchor=datetime(2026, 11, 1))
    check("「Started by」版式识别为 1 条（不再产出 08:00 乱码）",
          len(started) == 1
          and started[0].start.strftime("%Y-%m-%d %H:%M") == "2026-11-01 18:23",
          str([(e.start, e.title) for e in started]))
    check("「Started by」标题带 App 名与发起人",
          bool(started) and started[0].title == "腾讯会议（冯昊诚 发起）",
          started[0].title if started else "")
    check("独立成行的会议号进了备注",
          bool(started) and "364293371" in started[0].note,
          started[0].note if started else "")
    check("GMT+08:00 里的 08:00 不会被当成时刻",
          bool(started) and started[0].start.strftime("%H:%M") == "18:23")

    overnight = app.parse_with_rules(
        "Started by 李文\n777 888 999\n23:30\n00:30\n1 hrs\n"
        "2026/11/20\n(GMT+08:00)\n2026/11/21\n腾讯会议",
        anchor=datetime(2026, 11, 1))
    check("跨天会议：结束日期取票面第二行日期（23:30 → 次日 00:30）",
          len(overnight) == 1
          and overnight[0].start.strftime("%m-%d %H:%M") == "11-20 23:30"
          and overnight[0].end.strftime("%m-%d %H:%M") == "11-21 00:30",
          str([(e.start, e.end) for e in overnight]))

    # 反向保护：普通日程里只是「提到」腾讯会议，不能被兜底版式吞掉
    mention = app.parse_with_rules(
        "11月30日 晚上8点半 腾讯会议 和学长请教保研经验", anchor=datetime(2026, 11, 1))
    check("普通日程提到「腾讯会议」不被会议通道吞掉",
          len(mention) == 1 and mention[0].title != "腾讯会议"
          and mention[0].location != "线上会议",
          str([(e.title, e.location) for e in mention]))

    # --- 演出门票（演唱会 / 话剧 / 音乐节） -----------------------------
    show = app.parse_with_rules(
        "大麦\n【周杰伦嘉年华世界巡回演唱会-上海站】\n时间：2026.12.12 周六 19:00\n"
        "场馆：上海体育场\n票档：看台1280元\n座位：看台二区12排8号",
        anchor=datetime(2026, 11, 1))
    check("演唱会门票识别为 1 条（19:00 开演）",
          len(show) == 1 and show[0].start.strftime("%Y-%m-%d %H:%M")
          == "2026-12-12 19:00",
          str([(e.start, e.title) for e in show]))
    check("演出场馆进地点、票档进备注",
          bool(show) and "上海体育场" in show[0].location
          and "1280" in show[0].note,
          f"{show[0].location if show else ''} | {show[0].note if show else ''}")

    show2 = app.parse_with_rules(
        "【2026楠溪江星巢秘境音乐节】\n时间：12月26日 周六 13:00\n"
        "地点：温州楠溪江音乐节现场", anchor=datetime(2026, 11, 1))
    check("音乐节门票（无年份）识别正常",
          len(show2) == 1 and show2[0].start.strftime("%Y-%m-%d %H:%M")
          == "2026-12-26 13:00",
          str([(e.start, e.title) for e in show2]))

    # 单行车票（车次在最前 +「铁路12306」表头）
    t1l = app.parse_with_rules(
        "●铁路12306\nG7045 09:56 镇江 10:27 常州 2026年10月31日",
        anchor=datetime(2026, 10, 1))
    check("单行车票（车次在最前）识别为 1 条",
          len(t1l) == 1 and t1l[0].end.strftime("%H:%M") == "10:27"
          and "镇江" in t1l[0].location,
          str([(e.start, e.end, e.title, e.location) for e in t1l]))

    # 12306「本人车票」页：一张截图多张票，每张各出一条
    multi = app.parse_with_rules(
        "11:52\n79+\n本人车票\n13:09\nK782\n16:35\n昆山\n南京\n10月31日(周六)\n"
        "已支付\n成人票\n硬座 14车 013号\n¥40.5\n改签\n退票\n"
        "乘车日期：2026年11月1日 周日\n订单号：E984335980\n复制\n车票当日当次有效\n"
        "04:26\nK1149\n08:36\n南京\n11月1日(周日)\n昆山\n已支付\n硬座 06车 070号\n"
        "订单号：E989624393\n10:10\nG7589\n11:34\n昆山南\n11月1日(周日)\n杭州东",
        anchor=datetime(2026, 11, 1))
    check("多票截图识别出 3 条行程",
          len(multi) == 3,
          str([(e.title, f"{e.start:%m-%d %H:%M}") for e in multi]))
    check("每张票的日期各归各（不都挂到第一张的日期）",
          [f"{e.start:%m-%d}" for e in multi] == ["10-31", "11-01", "11-01"],
          str([f"{e.start:%m-%d}" for e in multi]))
    check("状态栏时间（11:52）不会变成日程",
          all(f"{e.start:%H:%M}" != "11:52" for e in multi) and len(multi) == 3)
    check("「退票」按钮文字不再误标为「已退票」状态",
          all("已退票" not in e.note for e in multi),
          str([e.note for e in multi]))

    # 检票卡片（12306 首页小卡）：站名在时刻**上方**，卡片里满是 UI 词
    gate = app.parse_with_rules(
        "G7561\n铁路12306\n中国铁路\n杭州东\n宁波\n11月1日\n回\n"
        "20:13\n21:09\n乘车码\n检票口:24B 25B\n02车01B号二等座\n"
        "以上信息仅供参考，请以现场公告为准。", anchor=datetime(2026, 11, 1))
    check("检票卡片识别为 1 条（站名取自时刻上方）",
          len(gate) == 1
          and gate[0].start.strftime("%m-%d %H:%M") == "11-01 20:13"
          and gate[0].end.strftime("%H:%M") == "21:09",
          str([(e.start, e.end, e.title) for e in gate]))
    check("检票卡片标题是「站名 → 站名 车次」，不是「乘车码 → 乘车码」",
          bool(gate) and gate[0].title == "杭州东 → 宁波 G7561",
          gate[0].title if gate else "")
    check("座位与检票口进备注（乘车码 / 中国铁路 等 UI 词被排除）",
          bool(gate) and "02车01B号" in gate[0].note
          and "检票口 24B 25B" in gate[0].note
          and "乘车码" not in gate[0].title and "乘车码" not in gate[0].location,
          gate[0].note if gate else "")

    # 语义化识别（举一反三）：全新的 UI 词（会员码 / 电子发票）不在噪声表里，
    # 但因为「不像地名」（城市词表 / 地点后缀 / 方位词都不命中）被自动排除；
    # 站名（徐州 / 厦门北）按词义识别，不靠「出现在第几行」。
    novel = app.parse_with_rules(
        "G9999\n铁路12306\n会员码\n徐州\n厦门北\n12月5日\n回\n"
        "08:00\n09:30\n会员码\n电子发票\n检票口:3A\n02车05F号二等座",
        anchor=datetime(2026, 11, 1))
    check("陌生 UI 词（不在噪声表）不会被当成站名",
          len(novel) == 1 and novel[0].title == "徐州 → 厦门北 G9999",
          str([(e.title, e.location) for e in novel]))
    check("地名判定是语义级的（词表 / 后缀 / 方位词），位置无关",
          app._is_station_name("徐州") and app._is_station_name("厦门北")
          and not app._is_station_name("会员码")
          and not app._is_station_name("电子发票"))
    src_all = open("app.py", encoding="utf-8").read()
    check("AI 提示词教模型按语义识别卡片（不按位置）",
          "按语义识别，不按位置" in src_all
          and "OCR 会打乱" in src_all)
    check("城市词表与地点后缀判定已内置",
          "_CITY_LEXICON" in src_all and "_PLACE_SUFFIX_RE" in src_all
          and "_PLACE_DIR_RE" in src_all)

    # 教室地点的跨校形态：常州大学科教城用字母代号楼（W9），江苏大学用楼名+房号
    cls = app.parse_with_rules("11月5日 14:00 高等数学 W9-304",
                               anchor=datetime(2026, 11, 1))
    check("字母代号楼（W9-304）识别为地点、不混进标题",
          len(cls) == 1 and cls[0].location == "W9-304"
          and cls[0].title == "高等数学",
          str([(e.title, e.location) for e in cls]))
    cls2 = app.parse_with_rules("11月7日 14:00 大学物理 三江楼 305",
                                anchor=datetime(2026, 11, 1))
    check("楼名+房号（三江楼 305）识别为地点",
          len(cls2) == 1 and "三江楼" in cls2[0].location
          and cls2[0].title == "大学物理",
          str([(e.title, e.location) for e in cls2]))
    _train_txt = "11月8日 09:56 镇江 G7045 10:27 常州"
    _train_loc = app.parse_with_rules(_train_txt,
                                      anchor=datetime(2026, 11, 1))[0].location
    check("车次（无分隔符 字母+数字）不会被当成教室",
          "G7045" not in _train_loc and "W" not in _train_loc,
          repr(_train_loc))
    check("AI 提示词包含教学楼代号说明（W9-304 / 三江楼304）",
          "W9-304" in src_all and "三江楼304" in src_all
          and "字母代号楼" in src_all)

    # 反向保护：普通通知不被演出 / 会议通道劫持
    normal = app.parse_with_rules(
        "11月3日 19:00 迎新晚会彩排\n地点：大学生活动中心", anchor=datetime(2026, 11, 2))
    check("普通通知不被演出通道劫持",
          len(normal) == 1 and "彩排" in normal[0].title,
          str([(e.title, e.start) for e in normal]))

    # 反例：字段不足 2 行时不做归并（避免误伤单条日程）
    single = "10月31日 14:00 实验室安全培训\n场地：实验楼A201"
    check("字段不足 2 行时不触发归并",
          len(app.parse_with_rules(single)) == 1,
          str(len(app.parse_with_rules(single))))


def test_course_cell() -> None:
    section("N. 教务系统单元格解析（课程名/地点/教师分离）")

    # 教务单元格的标准形状：课程名 + (节次)周次 + 一串「字段:值」
    sample = ("高等数学D (3-4节)3-18周/校区：回龙校区/场地：回龙1303(多)/"
              "教师:徐秀丽/课程性质简称:必修/选课备注:/学分:4.0/重修标记:")
    entries = app.parse_timetable_cell(sample)
    check("单元格解析出 1 条", len(entries) == 1, str(len(entries)))
    if entries:
        e = entries[0]
        check("课程名进标题", e["title"] == "高等数学D", e["title"])
        check("教师进教师字段", e["teacher"] == "徐秀丽", e["teacher"])
        check("场地进地点", "回龙1303" in e["location"], e["location"])
        check("校区并入地点", "回龙校区" in e["location"], e["location"])
        check("周次区间", e["weeks"] == [(3, 18)], str(e["weeks"]))
        check("节次范围", e["periods"] == (3, 4), str(e["periods"]))

    # 「/」是字段分隔符，绝不是课程分隔符
    check("字段不会被当成独立课程",
          all("场地" not in e["title"] and "教师" not in e["title"]
              and "校区" not in e["title"] and "必修" not in e["title"]
              for e in entries),
          str([e["title"] for e in entries]))

    # 一格多课：同一格里上下排两门
    two = ("马克思主义基本原理 (1-2节)2-16周/校区:回龙校区/场地:三江楼0501/"
           "教师:陈静/课程性质简称:必修/学分:4.0/重修标记:\n"
           "大学生心理健康教育 (1-2节)2-16周/校区:回龙校区/场地:三江楼0502/"
           "教师:杨倩婧/课程性质简称:必修/学分:4.0/重修标记:")
    entries = app.parse_timetable_cell(two)
    check("一格两门课拆成 2 条", len(entries) == 2, str([e["title"] for e in entries]))
    check("各自的地点不串", entries and
          "0501" in entries[0]["location"] and "0502" in entries[1]["location"],
          str([e["location"] for e in entries]))

    # 多区间周次：「3-7,10-12,15-18周」只会在最后一段带「周」
    multi = "思想道德与法治 (6-8节)3-7,10-12,15-18周/场地:回龙1113(多)/教师:陈盈诺"
    entries = app.parse_timetable_cell(multi)
    check("多区间周次全部保留",
          bool(entries) and entries[0]["weeks"] == [(3, 7), (10, 12), (15, 18)],
          str(entries[0]["weeks"]) if entries else "")

    # OCR 把「4-10周」读成「410周」（连字符丢了）
    entries = app.parse_timetable_cell(
        "职业生涯发展与规划 (6-7节)410周(双)/场地:回龙1212(多)/教师:范珈祯")
    check("三位数周次拆回区间",
          bool(entries) and entries[0]["weeks"] == [(4, 10)],
          str(entries[0]["weeks"]) if entries else "")
    check("双周标记", bool(entries) and entries[0]["odd_even"] == "双周",
          entries[0]["odd_even"] if entries else "")

    # OCR 的形近误读：「救师」「数师」也要认
    entries = app.parse_timetable_cell("数据结构 (1-2节)1-16周/救师:王强/场地:三江楼0301")
    check("OCR 形近「救师」→ 教师", bool(entries) and entries[0]["teacher"] == "王强",
          entries[0]["teacher"] if entries else "")

    # 上一门课的字段值粘在下一门课名前面
    entries = app.parse_timetable_cell(
        "管理学原理 (5-6节)1-16周/场地:经济楼201/教师:周涛/学分:4.0\n"
        "会计学原理 (5-6节)1-16周/场地:经济楼202/教师:吴敏/学分:4.0")
    check("上一门的学分不会粘在下一门标题前",
          bool(entries) and all("4.0" not in e["title"] for e in entries),
          str([e["title"] for e in entries]))

    # 旧的 W 前缀格式（无字段前缀）不受影响
    legacy = app._split_cell_courses("高等数学(一)(上)卜 庆刚 W9阶 2-6,9-18, /")
    check("旧 W 前缀格式仍兼容",
          bool(legacy) and legacy[0]["title"] == "高等数学(一)(上)",
          str(legacy[0]["title"]) if legacy else "")


def test_period_table() -> None:
    section("O. 自定义作息表（节次 → 绝对时间）")

    # ---- 1. 默认模板：第6-7节 = 14:00-15:35 ---------------------------
    app._set_period_rows([])
    span = app._period_span(6, 7)
    check("默认表第6-7节 = 14:00-15:35",
          span[:2] == ((14, 0), (15, 35)) and span[2] is True, str(span))
    check("默认表没有第 13 节 → 不瞎猜", app._period_lookup(13) is None)

    # ---- 2. 自定义表：6-7节 = 14:50-16:25 ----------------------------
    app._set_period_rows([
        {"name": "第1节", "start": "08:00", "end": "08:45"},
        {"name": "第2节", "start": "08:55", "end": "09:40"},
        {"name": "第3节", "start": "10:00", "end": "10:45"},
        {"name": "第4节", "start": "10:55", "end": "11:40"},
        {"name": "第6节", "start": "14:50", "end": "15:35"},
        {"name": "第7节", "start": "15:45", "end": "16:25"},
        {"name": "晚修", "start": "19:00", "end": "21:30"},
    ])
    span = app._period_span(6, 7)
    check("自定义表第6-7节 = 14:50-16:25",
          span[:2] == ((14, 50), (16, 25)) and span[2] is True, str(span))
    check("跨节 = 第6节开始 + 第7节结束",
          span[0] == (14, 50) and span[1] == (16, 25), str(span))
    check("没配置的第5节查不到", app._period_lookup(5) is None)

    # 全表都没有「第X节」式名称时，按开始时间排序取行序号
    app._set_period_rows([
        {"name": "早读", "start": "07:30", "end": "07:55"},
        {"name": "上午", "start": "08:00", "end": "11:40"},
        {"name": "下午", "start": "14:00", "end": "17:00"},
    ])
    check("无「第X节」名称时按行序号回退",
          app._period_lookup(2) == ((8, 0), (11, 40)), str(app._period_lookup(2)))
    check("行序号越界 → None", app._period_lookup(4) is None)

    # ---- 3. 解析路径：节次查表 ---------------------------------------
    app._set_period_rows([
        {"name": "第1节", "start": "08:00", "end": "08:45"},
        {"name": "第2节", "start": "08:55", "end": "09:40"},
        {"name": "第6节", "start": "14:50", "end": "15:35"},
        {"name": "第7节", "start": "15:45", "end": "16:25"},
        {"name": "晚修", "start": "19:00", "end": "21:30"},
    ])
    evs = app.parse_with_rules(
        "2026年10月29日 周四\n第6-7节\n高等数学\n场地：回龙2311\n每周四 · 共 16 次\n")
    check("第6-7节 → 14:50-16:25",
          bool(evs) and evs[0].start.strftime("%H:%M") == "14:50"
          and evs[0].end.strftime("%H:%M") == "16:25",
          f"{evs[0].start:%H:%M}-{evs[0].end:%H:%M}" if evs else "")

    # 「晚修」别名也能当时间节点
    evs2 = app.parse_with_rules(
        "2026年10月28日 周二\n晚修\n程序设计实训\n场地：计算机楼301\n每周二 · 共 12 次\n")
    check("「晚修」按作息表换算",
          bool(evs2) and evs2[0].start.strftime("%H:%M") == "19:00"
          and evs2[0].end.strftime("%H:%M") == "21:30",
          f"{evs2[0].start:%H:%M}-{evs2[0].end:%H:%M}" if evs2 else "")

    # 未配置的节次：标记待确认，绝不瞎猜
    evs3 = app.parse_with_rules(
        "2026年10月30日 周五\n第12节\n晚自习\n场地：三江楼201\n")
    check("未配置节次标记待确认",
          bool(evs3) and evs3[0].vague and "作息表" in evs3[0].vague_reason,
          evs3[0].vague_reason if evs3 else "")

    # ---- 4. 绝对时间优先于节次 ---------------------------------------
    app._set_period_rows([
        {"name": "第3节", "start": "10:00", "end": "10:45"},
        {"name": "第4节", "start": "10:55", "end": "11:40"},
    ])
    evs4 = app.parse_with_rules(
        "2026年10月30日 周五\n10:45 第3-4节 习题课\n场地：三江楼101\n")
    check("「10:45 第3-4节」开始用绝对时间",
          bool(evs4) and evs4[0].start.strftime("%H:%M") == "10:45",
          f"{evs4[0].start:%H:%M}" if evs4 else "")
    check("结束时间用第 4 节下课补齐",
          bool(evs4) and evs4[0].end.strftime("%H:%M") == "11:40",
          f"{evs4[0].end:%H:%M}" if evs4 else "")

    # ---- 5. AI 提示词注入 ---------------------------------------------
    app._set_period_rows([
        {"name": "第1节", "start": "08:00", "end": "08:45"},
        {"name": "第2节", "start": "08:55", "end": "09:40"},
    ])
    text = app._period_table_text()
    check("作息表注入成一行文本", "第1节 08:00-08:45" in text, text[:60])
    check("AI 提示词包含作息表占位符与查表规则",
          "{period_table}" in app.AI_SYSTEM_PROMPT
          and "必须用它换算" in app.AI_SYSTEM_PROMPT)

    app._set_period_rows([])          # 还原默认，避免影响后续用例


def test_theme_guard() -> None:
    section("K. 主题令牌一致性（防硬编码 / 防漏定义）")
    import re

    src = open("app.py", encoding="utf-8").read()
    light, dark = app._TOKENS_LIGHT, app._TOKENS_DARK

    check("明暗两套令牌键名完全一致",
          set(light) == set(dark),
          f"浅色多出 {sorted(set(light) - set(dark))}；"
          f"深色多出 {sorted(set(dark) - set(light))}")
    check("每个令牌都是非空字符串",
          all(isinstance(v, str) and v.strip() for v in list(light.values()) + list(dark.values())))

    # 样式块里除"白色叠加层"外不应再有颜色字面量
    lines = src.split("\n")

    def block(name: str) -> str:
        start = next(i for i, l in enumerate(lines) if l.startswith(name))
        end = next(i for i in range(start + 1, len(lines)) if lines[i].rstrip() == '"""')
        return "\n".join(lines[start:end])

    color_re = re.compile(r"#[0-9A-Fa-f]{3,8}\b|\brgba?\([^)]*\)|\bhsla?\([^)]*\)")
    offenders: list[str] = []
    for name in ("_CSS =", "_CSS_WIDGETS =", "_CSS_DARK_ONLY ="):
        for m in color_re.finditer(block(name)):
            token = m.group(0).replace(" ", "")
            # 唯一允许的字面量：品牌渐变上的白色高光/描边，两种主题共用
            if re.fullmatch(r"rgba\(255,255,255,\.[0-9]+\)", token):
                continue
            offenders.append(f"{name} {token}")
    check("样式表里没有散落硬编码颜色（白色高光除外）",
          not offenders, str(offenders[:6]))

    # 所有 var(--x) 引用的变量都必须有定义
    defined = set(light) | {"fs"}
    used = set(re.findall(r"var\(--([a-z0-9\-]+)\)", src))
    missing = sorted(used - defined)
    check("var(--x) 引用的变量都有定义", not missing, str(missing))

    # 主题注入：color-scheme 与首帧底色
    check("按主题输出 color-scheme（原生滚动条/日期面板跟着变）",
          'color-scheme:{scheme}' in src or "color-scheme:" in src)
    check("首帧先铺底色，避免刷新时闪白",
          "html,body{background:var(--body-bg);}" in src)
    check("支持 ?theme=dark 深链，刷新保持主题",
          'st.query_params.get("theme")' in src and "_remember_theme(" in src)
    check("提供了跟随系统偏好的兜底",
          "_system_prefers_dark" in src and "st.context.theme" in src)

    # 反色滤镜只允许出现在数据表 canvas 这条规则里，不允许是全局反色
    css_rules = re.findall(r"([^{}]+)\{([^{}]*)\}", block("_CSS_DARK_ONLY ="))
    invert_rules = [(sel.strip(), body.strip()) for sel, body in css_rules
                    if "invert" in body]
    check("invert 只作用在数据表 canvas 的选择器上",
          bool(invert_rules) and all("canvas" in sel for sel, _ in invert_rules),
          str([sel for sel, _ in invert_rules]))
    all_rules = re.findall(r"([^{}]+)\{([^{}]*)\}", src)
    stray = [sel.strip() for sel, body in all_rules if "invert" in body and "canvas" not in sel]
    check("全站样式中没有兜底式的 filter:invert", not stray, str(stray[:3]))


def test_recurrence() -> None:
    section("M. 重复规则：图片写什么就导出什么")

    # ---- 1. 自然语言 -> RRULE 的精确映射 ------------------------------
    cases = [
        # 原文、期望 RRULE
        ("每周四 · 共 16 次", "FREQ=WEEKLY;BYDAY=TH;COUNT=16"),
        ("每每周四·共16次", "FREQ=WEEKLY;BYDAY=TH;COUNT=16"),      # OCR 叠字
        ("每周一，1-5周", "FREQ=WEEKLY;BYDAY=MO;COUNT=5"),
        ("第1-16周", "FREQ=WEEKLY;COUNT=16"),
        ("1-16周 单周", "FREQ=WEEKLY;INTERVAL=2;COUNT=8"),
        ("1-16周 双周", "FREQ=WEEKLY;INTERVAL=2;COUNT=8"),
        ("单双周交替", "FREQ=WEEKLY;INTERVAL=2"),
        ("周一至周五", "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"),
        ("每周一三五", "FREQ=WEEKLY;BYDAY=MO,WE,FR"),
        ("每两周的周三", "FREQ=WEEKLY;INTERVAL=2;BYDAY=WE"),
        ("每天 · 共 30 次", "FREQ=DAILY;COUNT=30"),
        ("每月", "FREQ=MONTHLY"),
        ("连续8周", "FREQ=WEEKLY;COUNT=8"),
        ("每周四 1-8周", "FREQ=WEEKLY;BYDAY=TH;COUNT=8"),
        ("每周四 9-16周", "FREQ=WEEKLY;BYDAY=TH;COUNT=8"),
        ("2-6,9-18周", "FREQ=WEEKLY;COUNT=15"),
    ]
    for text, want in cases:
        got = app.extract_recurrence(text, datetime(2026, 10, 29, 10, 45)).rrule()
        check(f"「{text}」→ {want}", got == want, got or "(空)")

    # ---- 2. 不该被误判成循环的写法 -----------------------------------
    for text in ("下周三下午2点 开班会", "第3周开会", "10:45-11:30 上课", "教室 A201"):
        got = app.extract_recurrence(text)
        check(f"「{text}」不产生重复规则", got.rrule() == "", got.rrule())

    # ---- 3. 不连续周次要生成 EXDATE 跳过空档 --------------------------
    info = app.extract_recurrence("2-6,9-18周")
    check("2-6,9-18 之间跳 2 周", info.skip_weeks == [5, 6], str(info.skip_weeks))

    cells = [{"weekday": 3, "period": 1, "periods": (1, 2),
              "text": "高等数学 王老师 W9阶 2-6,9-18, /"}]
    evs = app.timetable_to_events(cells, date(2026, 9, 7))
    check("课表 2-6,9-18 周 → COUNT=15",
          bool(evs) and evs[0].rrule == "FREQ=WEEKLY;BYDAY=TH;COUNT=15",
          evs[0].rrule if evs else "")
    ics = app.build_ics(evs)
    check("ICS 里写了 EXDATE 排掉空档周",
          "EXDATE:" in ics and "COUNT=15" in ics,
          [l for l in ics.split("\r\n") if "EXDATE" in l][:1])

    # ---- 4. 网站设置不能覆盖图片规则 ---------------------------------
    with_count = app.Event(title="课", start=datetime(2026, 11, 2, 8, 0),
                           end=datetime(2026, 11, 2, 9, 30),
                           rrule="FREQ=WEEKLY;BYDAY=MO;COUNT=5")
    norm, _ = app.normalize_events([with_count])
    check("图片写 5 次就保留 5 次（不被默认 16 覆盖）",
          app.get_recur_count(norm[0].rrule) == 5, norm[0].rrule)

    bare = app.Event(title="课", start=datetime(2026, 11, 2, 8, 0),
                     end=datetime(2026, 11, 2, 9, 30),
                     rrule="FREQ=WEEKLY;BYDAY=MO")
    norm2, _ = app.normalize_events([bare])
    check("图片没写次数就不虚构 COUNT",
          app.get_recur_count(norm2[0].rrule) == 0, norm2[0].rrule)

    # ---- 5. 字段列表型截图：整块只用一套规则 -------------------------
    text = (
        "2026 年 10 月 29 日 · 周四 第 44 周\n"
        "10:45\n职业生涯发展与规划(6-7节)\n"
        "场地：回龙2311(多)\n校区：回龙\n教师：张伟\n"
        "课程性质简称：必修\n每周四 · 共 16 次\n"
        "14:50\n大学生心理健康教育\n场地：回龙1212(多)\n每每周四 · 共 16 次\n"
    )
    block_events = app.parse_with_rules(text)
    check("两个时间块 → 两条日程", len(block_events) == 2, str(len(block_events)))
    check("每条都带同一套图片规则",
          all(e.rrule == "FREQ=WEEKLY;BYDAY=TH;COUNT=16" for e in block_events),
          str([e.rrule for e in block_events]))
    check("地点/属性没有各自带一套规则",
          len({e.rrule for e in block_events}) == 1,
          str({e.rrule for e in block_events}))
    check("规则标记为来自图片",
          all(e.recur_source == "image" for e in block_events),
          str([e.recur_source for e in block_events]))

    # ---- 6. 单双周按「第 N 周」的奇偶对齐 ---------------------------
    odd = app.parse_with_rules(
        "2026 年 10 月 26 日 · 周一 第 44 周\n"
        "08:00\n物理实验\n场地：实验楼A201\n1-16周 单周\n"
        "14:00\n体育(1)\n场地：科教城体育场\n1-16周 双周\n"
    )
    by_time = {e.start.strftime("%H:%M"): e for e in odd}
    check("偶数周里的单周课顺延一周",
          by_time.get("08:00") and by_time["08:00"].start.date() == date(2026, 11, 2),
          str(by_time["08:00"].start.date()) if by_time.get("08:00") else "")
    check("偶数周里的双周课本周就上",
          by_time.get("14:00") and by_time["14:00"].start.date() == date(2026, 10, 26),
          str(by_time["14:00"].start.date()) if by_time.get("14:00") else "")


def test_period_editor() -> None:
    """作息表编辑器：不能再用 st.data_editor（会丢编辑），必须用原生控件。"""
    import re

    section("H. 作息表编辑器")
    src = open("app.py", encoding="utf-8").read()

    # ① 编辑器本体不能用 data_editor（只查真正的调用，忽略文档字符串里的说明）
    body = src.split("def _render_period_editor", 1)[-1].split("\ndef ", 1)[0]
    code = "\n".join(line for line in body.splitlines()
                      if not line.strip().startswith("#"))
    code = re.sub(r'"""[\s\S]*?"""', "", code)   # 去掉 docstring
    check("作息表编辑器不用 st.data_editor（动态行数下会丢编辑）",
          "st.data_editor" not in code and "data_editor(" not in code,
          "num_rows='dynamic' 时 data_editor 元素 ID 含完整数据，"
          "编辑会让 ID 变化 → 前端刚提交的编辑被丢弃")

    # ② 用原生控件：文本框 + 时间选择器
    check("用 st.text_input 填节次名称", "text_input(" in body)
    check("用 st.time_input 选起止时间（手机上能直接调出时间选择器）",
          "time_input(" in body)
    check("每个控件都有稳定 key（值由 session_state 保管，不会漂移）",
          'key=f"period_name_' in body and 'key=f"period_start_' in body)

    # ③ 编辑器放在弹窗里（桌面宽、手机满屏），不在侧边栏窄表格里
    check("编辑器用 st.dialog 弹窗承载",
          "@st.dialog(" in src)
    check("弹窗背景按主题上色（深色不能是白底）",
          'section[role="dialog"]' in src and "--surface-1" in src)

    # ④ 时间输入框的显示值是 div 不是 input，得单独上色
    check("time_input 的显示值（div）也上了色",
          "stTimeInputTimeDisplay" in src)

    # ⑤ 「＋ 添加一节」放在**所有行下面**。
    #    新行总是追加在末尾，按钮紧跟末尾就能连着加，不用滚回顶部找按钮。
    head = body.split("new_rows: list", 1)[0]
    add_pos = head.find("period_add")
    check("「添加一节」按钮在表格下方（跟着末尾行，方便连着加）",
          add_pos < 0,                      # 不再出现在表格渲染之前
          "按钮应渲染在所有行之后")
    check("「添加一节」按钮确实存在于编辑器里",
          "period_add" in body)
    check("「当前生效」提示与「添加一节」同在底部操作条",
          'key="period_add_bar"' in body
          and body.index("period_add_bar") > body.index("new_rows: list"))

    # ⑥ 弹窗内容的文字色必须显式覆盖。
    #    st.dialog 由 React portal 渲染到 body 之下，**不在 .stApp 里**，
    #    所以 `.stApp label` / `.stApp h2` / `.stApp [data-testid=stCaptionContainer]`
    #    这些规则一条都命不中，文字会落回 Streamlit 浅色主题的默认文字色，
    #    在深色背景上就是那层几乎看不见的暗紫。必须显式补作用域。
    check("弹窗内的标题/说明/标签都显式用了主题令牌",
          '[data-testid="stDialog"] h1' in src
          and '[data-testid="stDialog"] [data-testid="stCaptionContainer"]' in src
          and '[data-testid="stDialog"] [data-testid="stWidgetLabel"]' in src,
          "弹窗内容不在 .stApp 内，漏掉就会退回浅色主题默认色")
    check("弹窗文字色走令牌而非硬编码",
          re.search(r'\[data-testid="stDialog"\][^{]*\{\s*color:\s*var\(--ink\)', src)
          is not None)
    check("弹窗关闭按钮也上了色（默认是浅色主题的暗色，深色下看不见）",
          'button[aria-label="Close"]' in src)

    # ⑦ 弹窗毛玻璃。
    #    关键：底色必须**半透明**（rgba(...,a) 且 a<1）。
    #    不透明底 + backdrop-filter 是无效组合 —— 无物可糊，玻璃感彻底消失。
    #    之前手机端侧边栏踩过同一个坑，必须防回归。
    glass = re.search(
        r"section\[role=\"dialog\"\]\s*\{([^}]*)\}", src)
    check("弹窗卡片带 backdrop-filter（毛玻璃）",
          glass is not None and "backdrop-filter" in glass.group(1))
    check("弹窗底色是半透明的（不透明底 = 毛玻璃失效）",
          glass is not None
          and re.search(r"background:\s*var\(--dialog-bg\)", glass.group(1)) is not None
          and re.search(r'"dialog-bg":\s*"rgba\([^)]*,\s*0?\.\d+\)"', src) is not None,
          "--dialog-bg 必须是带小 alpha 的 rgba")
    check("弹窗有独立的遮罩层令牌（把背后页面压暗，玻璃才立体）",
          '"dialog-scrim"' in src)


def test_security() -> None:
    section("I. 安全与合规静态检查")
    src = open("app.py", encoding="utf-8").read()
    import re

    check("无硬编码密钥（sk- 前缀）",
          not re.search(r"sk-[A-Za-z0-9]{16,}", src))
    check("无 eval / exec / os.system",
          not re.search(r"\beval\(|\bexec\(|os\.system", src))
    check("无文件写入操作",
          not re.search(r"open\([^)]*['\"][wa]", src))
    check("无数据库依赖",
          not re.search(r"sqlite3|sqlalchemy|pymysql|psycopg", src, re.I))
    check("密钥仅从环境变量/Secrets 读取",
          "os.environ.get(\"AI_API_KEY\")" in src and "_read_secret" in src)
    # 所有动态 HTML 中的用户内容都应经过 _escape
    check("提供了 HTML 转义工具", "def _escape(" in src)
    check("用户可编辑文本走表格渲染而非 HTML",
          "st.data_editor" in src)
    # 密钥绝不落盘：只允许存在 session_state（内存）
    check("用户填的密钥只存 session_state，不写文件",
          "st.session_state.ai_api_key" in src
          and not re.search(r"open\([^)]*ai_api_key", src))


def test_ai_key_dialog() -> None:
    """AI 接口配置：侧边栏独立入口 + 小弹窗填写 + 密钥安全。"""
    section("H2. AI 接口配置弹窗")
    src = open("app.py", encoding="utf-8").read()
    import re

    # ① 入口在侧边栏，且是独立按钮（不是塞在设置面板里）
    sidebar = src.split("def render_sidebar", 1)[-1]
    check("侧边栏有独立的「AI 接口」入口",
          "ai_key_toggle" in sidebar and "AI 接口" in sidebar)
    check("侧边栏入口显示 AI 配置状态（已连接 / 未配置）",
          "已连接" in sidebar and "未配置" in sidebar)
    # 入口必须在「设置与外观」按钮**之前**：
    # 设置面板一展开就占掉大半屏，放后面入口会被推到视口外，够不着。
    check("AI 接口入口排在「设置与外观」之前（保证始终可见可点）",
          sidebar.find("ai_key_toggle") < sidebar.find("settings_toggle")
          and sidebar.find("ai_key_toggle") > 0)

    # ② 配置走弹窗（width="small" = 小窗口）
    check("AI 配置用小弹窗承载（width=\"small\"）",
          re.search(r'@st\.dialog\("🤖 AI 接口配置",\s*width="small"', src)
          is not None)
    check("弹窗有三个字段：API Key / 接口地址 / 模型名",
          "ai_key_input" in src and "ai_base_input" in src
          and "ai_model_input" in src)

    # ③ 密钥输入必须是 password 类型，且界面上不回显明文
    check("API Key 输入框用 password 类型（页面上显示圆点）",
          re.search(r'st\.text_input\(\s*"API Key",\s*type="password"', src)
          is not None)
    check("侧边栏只显示掩码后的密钥（前 4 + 尾 4）",
          "_mask_key(" in src and "def _mask_key(" in src)

    # ④ 设置面板里不再有旧的密钥配置指引（已挪走）
    settings = src.split("def render_settings_panel", 1)[-1].split("\ndef ", 1)[0]
    check("设置面板里不再有「如何配置 API Key」展开块",
          "如何配置 API Key" not in settings)
    check("设置面板改为指引去侧边栏入口填",
          "AI 接口" in settings)

    # ⑤ 三个按钮都必须用 on_click 回调：
    #    st.dialog 是独立 fragment，按钮分支里的 st.rerun() 只重跑弹窗，
    #    侧边栏（在主脚本里）不会更新；而直接改 widget 的 session_state
    #    会抛 StreamlitWidgetAlreadyInstantiatedError。
    for name in ("ai_key_save", "ai_key_test", "ai_key_clear"):
        m = re.search(rf'st\.button\("[^"]+",\s*key="{name}".*?\)', src, re.S)
        check(f"「{name}」按钮用 on_click 回调（fragment 内要显式 st.rerun）",
              m is not None and "on_click=" in m.group(0))
    check("回调里显式 st.rerun()（让主脚本跟着刷新）",
          re.search(r"def _cb_save_ai_key.*?st\.rerun\(\)", src, re.S) is not None
          and re.search(r"def _cb_clear_ai_key.*?st\.rerun\(\)", src, re.S)
          is not None)

    # ⑥ 打开弹窗时把已保存的值同步到输入框
    check("打开弹窗时同步已保存的值到输入框",
          re.search(r"def _prepare_ai_dialog.*?ai_key_input", src, re.S)
          is not None)
    check("text_input 不传 value=（否则 widget state 会覆盖它）",
          re.search(r'st\.text_input\(\s*"API Key",\s*type="password"', src)
          is not None)

    # ⑦ 服务商预设：接口地址是最容易填错的一项，给下拉框自动填
    check("有服务商预设表（选了自动填地址和模型名）",
          "AI_PROVIDERS" in src and "def _cb_pick_provider" in src)
    check("服务商下拉用 on_change 回调自动填充",
          "ai_provider_pick" in src and "_cb_pick_provider" in src)
    # 默认必须是「-」（未选择），不能默认 DeepSeek ——
    # 否则会出现「下拉显示 DeepSeek、下面地址却是空的」这种自相矛盾的状态。
    check("服务商默认是「-」而不是默认某一家",
          'PROVIDER_NONE = "-"' in src
          and "AI_PROVIDER_OPTIONS: list[str] = [PROVIDER_NONE] + list(AI_PROVIDERS.keys())" in src)
    check("反查不到服务商时返回「-」而不是默认 DeepSeek",
          re.search(r"def _guess_provider.*?return PROVIDER_NONE", src, re.S)
          is not None)
    check("选回「-」时把地址和模型名一起清空（保持三者状态一致）",
          re.search(r"def _cb_pick_provider.*?PROVIDER_NONE.*?"
                    r"ai_base_input = \"\"", src, re.S) is not None)
    # 占位符统一用「-」，表示「没填」
    check("输入框占位符统一用「-」",
          re.search(r'st\.text_input\(\s*"API Key",\s*type="password",\s*key="ai_key_input",\s*placeholder="-"', src)
          is not None
          and re.search(r'key="ai_base_input",\s*placeholder="-"', src) is not None
          and re.search(r'key="ai_model_input",\s*placeholder="-"', src) is not None)
    # 服务商要排第一：先知道用哪家，才知道去哪儿申请 API Key
    dlg = src.split('@st.dialog("🤖 AI 接口配置"', 1)[-1]
    check("「服务商」排在「API Key」之前（先选服务商再填密钥）",
          dlg.find('"服务商"') > 0
          and dlg.find('"服务商"') < dlg.find('"API Key"'))
    # 预设里的地址必须能拼出 /chat/completions —— 即填到兼容层为止，
    # 不能再带 /chat/completions（会拼成 .../chat/completions/chat/completions）
    prov = re.search(r"AI_PROVIDERS[^=]*=\s*\{(.*?)\n\}", src, re.S)
    check("预设地址列表存在且格式正确",
          prov is not None and prov.group(1).count('"base_url"') >= 5)
    urls = re.findall(r'"base_url":\s*"([^"]+)"', src)
    check("所有预设地址都以兼容层结尾（不含 /chat/completions）",
          bool(urls) and all("/chat/completions" not in u for u in urls),
          str(urls[:3]))
    check("预设地址都是 https 或本地 http",
          all(u.startswith("https://") or u.startswith("http://localhost")
              for u in urls))
    check("能按已填地址反查服务商（重新打开时回填下拉）",
          "def _guess_provider" in src and "_guess_provider(" in src)

    # ⑧ 测试连接失败时给可操作建议，不能只甩状态码
    check("连接失败时给具体建议（不只报 404/401）",
          "def _ai_error_hint" in src and "接口地址填错" in src)

    # ⑨ 弹窗打开方式：必须在侧边栏按钮分支里**直接调用** dialog，
    #    不能用 `xxx_open` 持久标记逐轮重叫。后者在点 X 关闭后标记仍是 True，
    #    下一次任何 rerun（例如点「设置与外观」）都会把弹窗重新叫出来。
    section("H3. 弹窗打开方式与提示气泡")
    check("AI 弹窗在按钮分支里直接打开（不靠持久标记）",
          re.search(r'if st\.button\([\s\S]*?key="ai_key_toggle"[\s\S]*?'
                    r'_prepare_ai_dialog\(\)[\s\S]*?_show_ai_key_dialog\(\)', src)
          is not None)
    check("作息表弹窗同样在按钮分支里直接打开",
          re.search(r'if st\.button\("⏰ 作息时间表",\s*key="period_toggle"[\s\S]*?'
                    r'_show_period_dialog\(\)', src) is not None)
    check("弹窗不再依赖 period_open / ai_key_open 持久标记",
          not re.search(r"st\.session_state\.(period_open|ai_key_open)", src)
          and "def _cb_toggle_period" not in src
          and "def _cb_ai_dismiss" not in src)
    check("弹窗内的「添加 / 删除」只重跑 fragment（整页 rerun 会把弹窗关掉）",
          src.count('st.rerun(scope="fragment")') >= 2)
    # 侧边栏是一列紧挨着的按钮：hover 气泡会向上盖住上一个按钮，
    # 且点完按钮后焦点还在、气泡不消失，所以入口一律不带 help=。
    check("侧边栏入口不再用 hover 气泡（避免遮挡上一个按钮）",
          "填写大模型 API Key、接口地址与模型名" not in src
          and "在这里调整：明暗模式、界面字号" not in src
          and "课表上写的「第6-7节」「晚修」按这张表换算" not in src
          and "新日程默认提前多久提醒，导出 .ics 时写入 VALARM" not in src)

    # ⑩ 悬停提示气泡：不能盖住按钮并挡住点击
    # 实测 DOM：<div role="tooltip">（定位层） > <div data-testid="stTooltipContent">（气泡本身）
    #            > <div data-testid="stMarkdownContainer"> > <p>
    # 外观只能画在 data-testid 这一层，定位层必须保持透明，否则套出双层气泡框。
    tip_css = src[src.find("/* ---------- 悬停提示气泡"):src.find("/* ---------- 提示条 st.success")]
    tip = re.search(r'\[data-testid="stTooltipContent"\]\s*\{([^}]*)\}', tip_css, re.S)
    check("提示气泡的主样式挂在 stTooltipContent 上", tip is not None)
    check("提示气泡设置了 pointer-events: none（遮住按钮时也不挡点击）",
          tip is not None and "pointer-events: none" in tip.group(1),
          "否则鼠标移进气泡后既点不到按钮，也触发不了离开即关闭")
    check("提示气泡限宽（说明文字不再铺成一大块）",
          tip is not None and "max-width" in tip.group(1))
    # 气泡是圆角矩形：Streamlit 默认圆角极小，深色下看着像一块直角警告框
    check("提示气泡是圆角矩形",
          tip is not None and "border-radius: var(--r-md)" in tip.group(1),
          "气泡要圆角，不要直角")
    # Streamlit 的 Tooltip 自带 overflow:auto + maxHeight，多两句就冒滚动条
    check("提示气泡不显示滚动条",
          tip is not None and "scrollbar-width: none" in tip.group(1),
          "气泡里不该出现纵向滑动条")
    check("提示气泡隐藏 webkit 滚动条",
          "::-webkit-scrollbar { width: 0 !important; height: 0 !important;"
          in src)
    check("提示气泡字号 ≤ .72rem",
          tip is not None and "font-size: .72rem" in tip.group(1),
          "字号要小一点，别喧宾夺主")

    # --- 气泡不能被边框切掉文字（切字 bug 的成因都要堵住）---
    # ① 容器不得限高 / 不得裁剪：Streamlit 自带 overflow:auto + maxHeight，
    #    文字一换行就裁掉半行，再叠上"隐藏滚动条"就成了"文字被压扁"
    check("提示气泡不限高（内容多就长高，不裁字）",
          tip is not None and "max-height: none" in tip.group(1),
          "限高会把换行的第二行切掉")
    check("提示气泡不裁剪内容",
          tip is not None and "overflow: visible" in tip.group(1))
    # ② 子元素字号必须跟随容器：Streamlit 用 `*{font-size: sm}` 把每个子元素
    #    顶回 14px，只改容器会让"容器按小字号算高、文字按 14px 排"→ 截断
    check("气泡内子元素字号跟随容器（否则容器矮、文字高被切）",
          '[data-testid="stTooltipContent"] * { font-size: inherit !important; }' in src)
    # ③ Popper 定位层也可能带 maxHeight / overflow:hidden，一并放开
    check("气泡的定位层也不裁剪",
          'div:has(> [data-testid="stTooltipContent"])' in src
          and "max-height: none" in src[src.find("div:has(> [data-testid"):
                                        src.find("div:has(> [data-testid") + 160])
    # ④ 字号必须用 rem：4 档界面字号是改 html font-size 实现的，
    #    写成 px 就只有"标准"下正常，放大后会再次挤爆
    check("气泡字号用 rem（四档字号下都不变形）",
          tip is not None and "font-size: .72rem" in tip.group(1)
          and "font-size: 11px" not in tip_css and "font-size: 12px" not in tip_css)

    # --- 居中 / 单层：真实 DOM 里踩过的两个坑 ---
    # ⑤ 定位层不能跟着画背景：否则气泡外面又套一圈同色边框，像两层气泡
    check("气泡的定位层不画背景（否则出现双层气泡框）",
          '[role="tooltip"]:not([data-testid="stTooltipContent"])' in tip_css
          and "background: transparent !important" in tip_css)
    # ⑥ markdown 容器自带 margin-bottom:-16px，会把气泡高度算成「一行-16px」，
    #    文字从底部溢出、看起来不居中
    check("清掉气泡内 markdown 容器的负边距（否则文字不居中/溢出）",
          re.search(r'\[data-testid="stMarkdownContainer"\][^{]*\{[^}]*margin: 0 !important',
                    tip_css, re.S) is not None)
    # ⑦ 别用 `[role="tooltip"] *` 设字号：它会把气泡自己也匹配进去，
    #    让气泡去继承 16px 的定位层，字反而变大
    check("字号规则不能误伤气泡自身（禁写 [role=tooltip] *）",
          '[role="tooltip"] * { font-size: inherit' not in tip_css)

    # --- 气泡文案：一条一行、最多 24 字，不许再出现三段式长说明 ---
    tip_calls = re.findall(r"_tip\(\s*[^,]+,\s*((?:f?\"(?:[^\"\\\\]|\\\\.)*\"\s*)+)\)",
                           src, re.S)
    descs = []
    for raw in tip_calls:
        for lit in re.findall(r'f?"((?:[^"\\]|\\.)*)"', raw):
            # f-string 里的 {RECUR_COUNT_MIN} 之类是占位，不算字数
            descs.append(re.sub(r"\{[^}]*\}", "", lit))
    check("气泡文案都不超过 24 字",
          descs and max(len(d) for d in descs) <= 24,
          "最长的一条：" + max(descs, key=len) if descs else "没找到任何 _tip 文案")
    check("气泡改成「名字 + 一句」的单行格式",
          'return f"**{name}**　{desc}"' in src,
          "不要再用「名字 / 空行 / 长段」的三段式")

    # 月历是 42 个紧挨着的小按钮：任何 hover 气泡都会盖住相邻格子，
    # 而且指过去的过程中经过别的格子会连环弹出好几层气泡，所以整块日历不带 help=
    cal_src = src[src.find("def _render_calendar"):]
    cal_src = cal_src[:cal_src.find("\ndef ")]
    check("月历按钮不再挂 hover 气泡（42 个格子会互相叠住）",
          "help=" not in cal_src, f"仍有 {cal_src.count('help=')} 处 help=")
    check("月历翻月 / 回到今天按钮仍可用",
          'key="cal_prev"' in cal_src and 'key="cal_next"' in cal_src
          and 'key="cal_today"' in cal_src)

    # ⑪ 默认提醒：复选框多选，最多 3 个
    check("默认提醒是复选框多选（最多 3 个）",
          "REMIND_MAX = 3" in src
          and "on_change=_cb_collect_reminds" in src
          and "disabled=(not checked and len(selected) >= REMIND_MAX)" in src)
    check("提醒选项含 30 分钟 / 1 小时 / 2 小时 / 4 小时",
          all(f'"提前 {x}": {m}' in src for x, m in
              (("30 分钟", 30), ("1 小时", 60), ("2 小时", 120), ("4 小时", 240))))
    check("多选提醒导出到 CSV 时用「+」连接",
          "remind_text = _remind_labels(default_remind)" in src)

    # ⑫ 手机浏览器状态栏 / 底部工具栏颜色跟随主题
    check("手机状态栏颜色跟随主题（theme-color）",
          "def _inject_theme_color" in src
          and "s('theme-color'" in src
          and "_inject_theme_color()" in src)


def test_placeholder_style() -> None:
    """占位符颜色：必须同时压 color 和 -webkit-text-fill-color。

    输入框正文样式用 -webkit-text-fill-color 把整块文字刷成正文色，
    它在 WebKit / Blink 里优先级高于 color —— 只改 color 的话
    占位符仍会被渲染成正文白/黑，看起来像「已经输入了内容」。
    """
    src = open("app.py", encoding="utf-8").read()
    # 取占位符规则块（含 textarea / input 的 ::placeholder）
    block = ""
    for marker in ("::placeholder",):
        idx = 0
        while True:
            i = src.find(marker, idx)
            if i < 0:
                break
            start = src.rfind("}", 0, i) + 1
            end = src.find("}", i) + 1
            block += src[start:end] + "\n"
            idx = end
    check("占位符样式存在", "::placeholder" in block, block[:160])
    check("占位符压暗了 color（区别于正文，避免被误认成已输入）",
          "var(--field-ph)" in block and "color:" in block,
          block[:200])
    check("占位符同时覆盖 -webkit-text-fill-color（否则改 color 无效）",
          "-webkit-text-fill-color" in block,
          "只设 color 会被输入框的 -webkit-text-fill-color 覆盖")

    # 深浅两套主题的 field-ph 都要比 input-text 弱
    import app as _app
    dark = _app._TOKENS_DARK["field-ph"]
    light = _app._TOKENS_LIGHT["field-ph"]

    def _lum(hexv: str) -> float:
        r, g, b = (int(hexv[i:i + 2], 16) / 255 for i in (1, 3, 5))

        def f(c: float) -> float:
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)

    def _cr(a: str, b: str) -> float:
        l1, l2 = _lum(a), _lum(b)
        hi, lo = max(l1, l2), min(l1, l2)
        return (hi + 0.05) / (lo + 0.05)

    # 深色主题：占位符对输入框底色对比度 1.8~3.2（明显弱但可读）
    d = _cr(dark, "#3F3E40")
    check("深色占位符对比度在 1.8~3.2（既弱又可读）",
          1.8 <= d <= 3.2, f"{dark} → {d:.2f}:1")
    # 浅色主题：占位符对白底对比度 2.4~3.2
    l = _cr(light, "#FFFFFF")
    check("浅色占位符对比度在 2.4~3.2（既弱又可读）",
          2.4 <= l <= 3.2, f"{light} → {l:.2f}:1")


def test_loading_indicator() -> None:
    """加载动画：耗时操作期间必须有可见的转圈提示。

    这里固化的三条都是实测踩出来的坑：
      ① 动画必须是**纯 CSS @keyframes** —— Streamlit 每次 rerun 都会重建
         DOM，JS 定时器会被打断，靠 JS 驱动的动画会卡在某一帧不动；
      ② 指示器必须画在**主区域（tabs 之外）** —— st.tabs 里的内容在
         同步阻塞期间**不会**推到前端，画在 tab 里用户根本看不到；
      ③ 卡片必须**固定定位**在视口内 —— 用户点的是页面下方的按钮，
         页面已经滚下去，流式布局的卡片会停在视口外看不见。
    """
    src = open("app.py", encoding="utf-8").read()
    import re

    # ① 纯 CSS 动画
    check("有加载动画的关键帧定义（必须是纯 CSS，不能靠 JS）",
          "@keyframes load-spin" in src
          and "@keyframes load-slide" in src)
    check("加载动画不依赖 JS 定时器",
          "setInterval" not in src and "requestAnimationFrame" not in src,
          "Streamlit rerun 会打断 JS 定时器，动画会卡住")
    check("转圈指示器绑定在 CSS 动画上",
          re.search(r"\.load-ring\s*\{[^}]*animation:\s*load-spin", src)
          is not None)

    # ② 指示器在 tabs 之外
    main = src.split("def main(", 1)[-1]
    tabs_idx = main.find("st.tabs(")
    loader_idx = main.find("_busy_slot")
    check("加载指示器画在 tabs 之外（tab 里的元素阻塞时不推前端）",
          loader_idx > 0 and tabs_idx > 0 and loader_idx < tabs_idx,
          "指示器必须排在 st.tabs() 之前")

    # ③ 固定定位，任何滚动位置都看得见
    card = re.search(r"\.load-card\s*\{([^}]*)\}", src)
    check("加载卡片固定定位在视口内（页面滚动时也看得见）",
          card is not None and "position: fixed" in card.group(1))
    check("弹窗内的加载卡片改为原位显示（fixed 会跑出弹窗外）",
          re.search(r'\[data-testid="stDialog"\] \.load-card\s*\{[^}]*position: static',
                    src) is not None)

    # ④ 覆盖到真正的耗时点
    check("图片 OCR 识别有进度提示",
          re.search(r'_busy\(\s*f?"正在识别第', src) is not None)
    check("AI 解析有进度提示",
          "_busy(\"大模型正在理解你的日程" in src)
    check("API Key 测试连接有进度提示",
          "_loading(\"正在测试连接" in src)

    # ⑤ 进度文案可原地更新（多张图片时能看到第几张）
    check("提供 _busy() 原地更新进度的接口",
          "def _busy(" in src and "_busy_slot" in src)


def test_chat_notices() -> None:
    """聊天截图式群通知：整段识别为一条，标题/地点/时间不被拆碎。"""
    section("J. 聊天截图式通知（群消息）")
    import app

    cases = [
        ("航博园", datetime(2026, 10, 16),
         "#通知 ✈️\n10月17日江苏航博园活动，请以下21名同学注意：\n"
         "⏰集合时间：明天上午8:05前（务必准时！）\n"
         "📍集合地点：五棵松（不要迟到！）",
         "2026-10-17 08:05", "航博园", "五棵松"),
        ("达人秀", datetime(2026, 10, 23),
         "动力2509 韩雪淇 13031954... 管理员\n@全体成员\n"
         "拍摄任务：“江六达人秀”拍摄\n时间：10.30（下周五）18：30-20：30\n"
         "地点：西山排球场旁的小广场",
         "2026-10-30 18:30", "达人秀", "西山排球场"),
        ("全体合影", datetime(2026, 10, 26),
         "主席团 沈方鑫 管理员\n@全体成员 【新媒体中心V16.0全体合影拍摄通知】 "
         "⚠️全体成员请注意，明日(10月27日)中午[12:30]，我们将在[大礼堂门口]"
         "进行新媒体中心大合照拍摄，合照预计13:00结束。",
         "2026-10-27 12:30", "合影", "大礼堂"),
        ("篮球赛", datetime(2026, 11, 1),
         "辅导员 盛富鹏 群主\n各位同学：\n今天（11月1日）下午3:30，"
         "第二十五届“新生杯”男子篮球赛决赛将在校体育馆主馆开赛。",
         "2026-11-01 15:30", "篮球赛", "体育馆"),
        ("团建", datetime(2026, 10, 20),
         "新媒体中心2026-2027第一学期第一次团建活动拟于10月24日（星期六）"
         "下午14:00～16:00举行。\n❗️活动地点：江苏大学六食堂、JSU草坪",
         "2026-10-24 14:00", "团建", "六食堂"),
        ("微电影", datetime(2026, 10, 19),
         "#通知 🎬\n10月20日校园微电影拍摄活动，请以下15名同学注意：\n"
         "⏰集合时间：明天下午2:30前\n📍集合地点：图书馆报告厅",
         "2026-10-20 14:30", "微电影", "图书馆报告厅"),
        ("迎新晚会", datetime(2026, 11, 2),
         "@全体成员 【迎新晚会彩排通知】 全体成员请注意，明日(11月3日)"
         "晚上[19:00]，我们将在[大学生活动中心]进行迎新晚会彩排。",
         "2026-11-03 19:00", "迎新晚会", "大学生活动中心"),
    ]
    for name, anchor, text, start, title_kw, loc_kw in cases:
        evs = app.parse_with_rules(text, anchor=anchor)
        check(f"{name}：整段识别为 1 条", len(evs) == 1, f"{len(evs)} 条")
        if not evs:
            continue
        e = evs[0]
        check(f"{name}：开始时间正确",
              e.start.strftime("%Y-%m-%d %H:%M") == start,
              e.start.strftime("%Y-%m-%d %H:%M"))
        check(f"{name}：标题含「{title_kw}」", title_kw in e.title, e.title)
        check(f"{name}：地点含「{loc_kw}」", loc_kw in e.location, e.location)

    # 反向保护：普通多行日程不能被聊天通道整段吞掉
    evs = app.parse_with_rules(
        "10月24日 09:00 高数 三江楼301；10:00 英语 三山楼205",
        anchor=datetime(2026, 10, 20))
    check("普通多行日程不被聊天通道劫持", len(evs) == 2, f"{len(evs)} 条")


def test_output_selfcheck() -> None:
    """输出前自检：日程数据 + ICS 文件，未通过必须阻断下载。"""
    section("K2. 输出前自检（交付门禁）")
    import app

    evs = app.parse_with_rules(app.build_sample_text())
    rep = app.validate_events(evs)
    check("正常日程数据自检通过", rep.ok,
          str([i.message for i in rep.errors]))

    bad = app.Event("坏事件", datetime(2026, 11, 1, 10),
                    datetime(2026, 11, 1, 9))
    check("结束早于开始被判为错误", not app.validate_events([bad]).ok)

    empty = app.Event("", datetime(2026, 11, 1, 10),
                      datetime(2026, 11, 1, 11))
    check("空标题被判为错误", not app.validate_events([empty]).ok)

    ics = app.build_ics(evs, "测试日历", [30, 60], 16)
    rep2 = app.validate_ics(ics, evs)
    check("正常 ICS 自检通过", rep2.ok,
          str([i.message for i in rep2.errors]))
    check("ICS 每行不超过 75 字节",
          max(len(ln.encode("utf-8")) for ln in ics.split("\r\n")) <= 75)
    check("截断的 ICS 被判为错误",
          not app.validate_ics(ics.replace("END:VCALENDAR", ""), evs).ok)

    src = open("app.py", encoding="utf-8").read()
    check("日程表预览前调用 validate_events",
          "preview_report = validate_events(events)" in src)
    check("导出前同时校验日程与 ICS",
          "ics_report = (validate_ics(ics_text, events)" in src)
    check("自检未通过时阻止下载",
          "allow_download = bool(ics_text) and event_report.ok and ics_report.ok"
          in src and "if allow_download:" in src)


def test_all_day() -> None:
    """全天日程：00:00–23:59 在界面与导出里统一写成「全天」。"""
    section("K3. 全天日程（00:00-23:59 → 全天）")
    import app

    day = date(2026, 11, 1)
    allday = app.Event("江苏大学大学生 AI 创新大赛决赛",
                       datetime.combine(day, datetime.min.time()),
                       datetime.combine(day, datetime.max.time()).replace(microsecond=0))
    normal = app.Event("上课", datetime(2026, 11, 1, 9), datetime(2026, 11, 1, 10, 30))

    check("00:00–23:59 判定为全天", allday.is_all_day)
    check("普通时段不判定为全天", not normal.is_all_day)
    check("次日 00:00 收尾也判定为全天",
          app.Event("x", datetime(2026, 11, 1, 0, 0),
                    datetime(2026, 11, 2, 0, 0)).is_all_day)
    check("零时长事件不会误判为全天",
          not app.Event("ddl", datetime(2026, 11, 1, 0, 0),
                        datetime(2026, 11, 1, 0, 0)).is_all_day)

    # --- .ics：必须写成 VALUE=DATE，DTEND 不含端 ---
    ics = app.build_ics([allday, normal], "测试", 0, 16)
    lines = ics.split("\r\n")
    check("全天日程导出 DTSTART;VALUE=DATE",
          "DTSTART;VALUE=DATE:20261101" in lines, str([l for l in lines if l.startswith("DTSTART")]))
    check("全天日程 DTEND 是次日（不含端）",
          "DTEND;VALUE=DATE:20261102" in lines)
    check("普通日程仍写具体时刻",
          any(l.startswith("DTSTART:2026") for l in lines))
    check("全天 + 普通混排时 ICS 自检通过",
          app.validate_ics(ics, [allday, normal]).ok,
          str([i.message for i in app.validate_ics(ics, [allday, normal]).errors]))
    check("全天日程被写成普通时刻时自检报错",
          not app.validate_ics(ics.replace("DTSTART;VALUE=DATE:20261101",
                                           "DTSTART:20261101T000000Z"),
                               [allday, normal]).ok)

    # --- CSV / Markdown ---
    row = app.build_csv([allday], [30]).decode("utf-8-sig").splitlines()[1]
    check("CSV 写「日期 + 全天」", "2026-11-01 全天" in row, row)
    check("CSV 不再出现 00:00 / 23:59", "00:00" not in row and "23:59" not in row, row)
    check("CSV 时长按完整一天记 1440", ",1440," in row, row)
    md = app.build_markdown([allday], [30])
    check("Markdown 写「全天」",
          any(l.startswith("| 全天 |") for l in md.splitlines()), md[:200])

    # --- 表格：显示「全天」，编辑后还能解析回来 ---
    df = app.events_to_dataframe([allday, normal])
    check("表格时间列显示「2026-11-01 全天」",
          str(df.loc[0, "开始"]) == "2026-11-01 全天", str(df.loc[0, "开始"]))
    check("表格不再显示 00:00 / 23:59",
          "00:00" not in str(df.loc[0, "开始"]) and "23:59" not in str(df.loc[0, "结束"]))
    back, _, _ = app.dataframe_to_events(df, [allday, normal])
    check("表格往返后仍是全天", back[0].is_all_day, str(back[0].start))
    check("表格往返后普通日程时间不变",
          (back[1].start, back[1].end) == (normal.start, normal.end))

    # 用户把某条改成「2026-11-05 全天」→ 变成那天整日
    edited = df.copy()
    edited.loc[1, "开始"] = "2026-11-05 全天"
    edited.loc[1, "结束"] = "2026-11-05 全天"
    moved, _, _ = app.dataframe_to_events(edited, [allday, normal])
    check("在表格里写「YYYY-MM-DD 全天」可改成整日",
          moved[1].is_all_day and moved[1].start.date() == date(2026, 11, 5),
          f"{moved[1].start} ~ {moved[1].end}")

    # 文本里写「全天」解析出来的也必须吃掉 00:00–23:59
    parsed = app.parse_with_rules("11月1日 全天 大礼堂 AI 创新大赛决赛", anchor=datetime(2026, 10, 28))
    check("解析「全天」得到整日事件",
          bool(parsed) and parsed[0].is_all_day,
          str([(e.start, e.end) for e in parsed]))

    # 全天日程不参与冲突判定（否则当天所有定时日程都会被标红）
    busy = app.Event("上课", datetime(2026, 11, 1, 9), datetime(2026, 11, 1, 10, 30))
    check("全天日程不与当天定时日程冲突",
          app.find_conflict_pairs([allday, busy]) == [],
          str(app.find_conflict_pairs([allday, busy])))
    two = app.Event("开会", datetime(2026, 11, 1, 10), datetime(2026, 11, 1, 11))
    check("两条定时日程重叠仍然算冲突",
          len(app.find_conflict_pairs([busy, two])) == 1)

    # 整日 + 循环：DTSTART 是 DATE，UNTIL 也必须是 DATE（RFC 5545 值类型一致）
    week_allday = app.Event("每周一 全天例会",
                            datetime(2026, 11, 2, 0, 0), datetime(2026, 11, 2, 23, 59),
                            rrule="FREQ=WEEKLY;BYDAY=MO")
    ics2 = app.build_ics([week_allday], "t", 0, 16)
    rrule_line = next(l for l in ics2.split("\r\n") if l.startswith("RRULE:"))
    check("整日循环日程的 UNTIL 是日期而非时间戳",
          rrule_line.endswith("UNTIL=20270222"), rrule_line)
    check("整日循环日程 ICS 自检通过",
          app.validate_ics(ics2, [week_allday]).ok,
          str([i.message for i in app.validate_ics(ics2, [week_allday]).errors]))


def test_share() -> None:
    """右上角分享按钮 + 分享面板（微信二维码 / QQ / 微博 / 复制链接）。"""
    section("L. 分享")
    import app

    src = open("app.py", encoding="utf-8").read()

    # ① 原生 Share 藏掉，换成自己的按钮
    check("隐藏 Streamlit 原生 Share 按钮",
          '[data-testid="stAppDeployButton"],' in src
          and "display: none !important;" in src,
          "原生 Share 的内容写死，改不了，只能藏")
    # src 里 `.st-key-share_fab {` 有多处（手机端侧边栏展开时还有一条隐藏规则），
    # 定义块要找带 position: fixed 的那处
    import re as _re
    fab_pos = next(p for p in _re.finditer(r"\.st-key-share_fab \{", src)
                   if "position: fixed" in src[p.start():p.start() + 200]).start()
    check("自定义分享按钮固定在上层（右上角）",
          "position: fixed" in src[fab_pos:fab_pos + 200], src[fab_pos:fab_pos + 120])
    fab_css = src[fab_pos:fab_pos + 700]
    check("分享按钮默认贴最右边（right 不超过 1rem）",
          "right: .7rem" in fab_css, fab_css[:120])
    check("云端有原生 ⋮ 菜单时自动让位（否则两个图标会叠）",
          'body:has([data-testid="stMainMenu"] button) .st-key-share_fab { right: 3.2rem; }' in src)
    check("分享按钮在 main 里渲染",
          "_render_share_button()" in src and src.count("_render_share_button") >= 2)

    # ② 图标：内联 SVG 做 mask（不依赖 emoji / 图标字体）
    check("分享图标是内联 SVG mask（弯曲箭头）",
          "-webkit-mask: url(\"__SHARE_ICON__\")" in src
          and "_share_icon_css_url" in src)
    check("注入 CSS 时替换了图标占位符",
          '_CSS_WIDGETS.replace("__SHARE_ICON__", _share_icon_css_url())' in src)
    url = app._share_icon_css_url()
    check("图标 data URI 编码正确（无裸 < > 空格）",
          url.startswith("data:image/svg+xml;utf8,")
          and not any(ch in url for ch in "<> \""))

    # ③ 面板内容
    for label, kw in (("QQ", "connect.qq.com/widget/shareqq"),
                      ("微博", "service.weibo.com/share/share.php")):
        check(f"分享面板有「{label}」入口", kw in src)
    check("分享面板有「复制链接」",
          'st.button("🔗 复制"' in src and "_COPY_LINK_JS" in src)
    check("复制链接有 execCommand 兜底（非 HTTPS / 手势过期也能复制）",
          "execCommand('copy')" in src and "navigator.clipboard" in src)
    check("微信用二维码（segno 缺失时自动降级）",
          "segno.make(" in src and "二维码生成失败" in src)
    check("二维码库写进依赖清单",
          "segno" in open("requirements.txt", encoding="utf-8").read())

    # ④ 链接按钮要和站内按钮同款（否则深色下是一块白）
    check("链接按钮套用站内次要按钮样式",
          '[data-testid="stBaseLinkButton-secondary"]' in src
          and "var(--btn-bg)" in src[src.find('[data-testid="stBaseLinkButton-secondary"]'):
                                     src.find('[data-testid="stBaseLinkButton-secondary"]') + 400])

    # ⑤ 手机端：三列不许叠成三行，否则弹窗比屏幕还高
    check("分享面板在窄屏下保持一行三列",
          '.st-key-share_body [data-testid="stHorizontalBlock"] { flex-wrap: nowrap !important; }' in src)

    # ⑤b 竖排间距不能压太狠：圆角胶囊按钮和网址框只隔 1~2px 时，
    #     视觉上就是「按钮压在文字上」（真实踩过，用户截图反馈过）
    body_css = src[src.find(".st-key-share_body {"):src.find(".st-key-share_body {") + 120]
    check("分享面板竖排间距不小于 0.4rem（否则按钮压住网址框）",
          "gap: .5rem" in body_css or "gap: .45rem" in body_css, body_css[:80])
    # ⑤c st.markdown 的容器自带 margin-bottom:-16px，会把网址框的高度吃掉 16px，
    #     导致下面的按钮盖上来 —— 面板内必须清零
    check("分享面板内清掉了 stMarkdownContainer 的负边距",
          '.st-key-share_body [data-testid="stMarkdownContainer"]' in src)

    # ⑥ 网址：去掉查询串再分享
    check("分享的网址去掉了查询串",
          'url.split("?")[0].split("#")[0]' in src)

    # ⑦ 真跑一下二维码，确保能生成真正的 PNG
    png = app._share_qr_png("https://example.streamlit.app/")
    check("二维码能生成为 PNG", png is not None and png[:4] == b"\x89PNG",
          "segno 不可用时应返回 None 而不是抛异常")


def test_feedback() -> None:
    """问题反馈：右上角胶囊按钮 → 弹窗（类型/描述/截图）→ 发到开发者邮箱。"""
    section("L2. 问题反馈")
    import app

    src = open("app.py", encoding="utf-8").read()

    # ① 按钮与位置
    check("按钮文案是「有问题？点我反馈」", '"有问题？点我反馈"' in src)
    check("反馈按钮在 main 里渲染且先于分享按钮",
          "_render_feedback_button()\n    _render_share_button()" in src)
    check("反馈按钮固定在分享按钮左侧（right 3.3rem > 分享的 .7rem）",
          ".st-key-fb_fab {" in src and "right: 3.3rem;" in src
          and "top: .55rem;" in src[src.find(".st-key-fb_fab {"):
                                    src.find(".st-key-fb_fab {") + 120])
    check("云端有 ⋮ 菜单时反馈按钮跟着分享按钮让位",
          'body:has([data-testid="stMainMenu"] button) .st-key-fb_fab { right: 5.8rem; }' in src)
    check("手机端侧边栏浮层展开时隐藏两个浮动按钮（否则压住侧边栏的收起按钮）",
          '.stApp:has([data-testid="stSidebar"][data-collapsed="false"]) .st-key-fb_fab,'
          in src and '.stApp:has([data-testid="stSidebar"][data-collapsed="false"]) .st-key-share_fab'
          in src)
    # 隐藏规则必须只在手机端媒体查询里：桌面端侧边栏是钉在左侧的，不遮按钮
    media_pos = src.rfind("@media (max-width: 640px)", 0, src.find(".st-key-fb_fab"))
    close_pos = src.find("\n}", src.find("stSidebarCollapseButton"))
    check("该隐藏规则写在 640px 手机端媒体查询内",
          0 < media_pos < close_pos and close_pos < src.find("def _feedback_env_info"))
    check("窄屏收紧字号（≤380px）", "@media (max-width: 380px)" in src
          and ".st-key-fb_fab button" in src[src.find("@media (max-width: 380px)"):
                                             src.find("@media (max-width: 380px)") + 200])

    # ② 弹窗内容：类型 / 描述 / 截图
    types = app._FEEDBACK_TYPES
    check("错误类型 5~8 个且每个 ≤24 字",
          5 <= len(types) <= 8 and all(len(t) <= 24 for t in types),
          f"{len(types)} 个，最长 {max(len(t) for t in types)} 字")
    check("类型覆盖历届修 bug 的高频类别",
          all(any(kw in t for t in types)
              for kw in ("识别错", "漏", "截图", "界面", "导出")))
    check("截图可多选且限 3 张、只收图片",
          "accept_multiple_files=True" in src and "_FEEDBACK_MAX_SHOTS = 3" in src
          and '"png", "jpg", "jpeg", "webp"' in src)
    check("截图张数在弹窗里也截断（防伪造请求）",
          "(shots or [])[:_FEEDBACK_MAX_SHOTS]" in src)

    # ③ 邮件
    check("收件邮箱是 bbbecause11@outlook.com",
          app._FEEDBACK_TO == "bbbecause11@outlook.com", app._FEEDBACK_TO)
    fb_src = src[src.find("def _smtp_config"):src.find("@st.dialog(\"📮 问题反馈\"")]
    check("SMTP 密码从 secrets 读取而非硬编码",
          'st.secrets.get("smtp"' in src
          and 'password = str(cfg.get("password"' in fb_src)
    check("邮件带环境信息（版本号，方便对版本定位）",
          f"应用版本：v{app.APP_VERSION}" in src or "APP_VERSION" in
          src[src.find("_feedback_env_info"):src.find("_feedback_env_info") + 400])
    env_src = src[src.find("def _feedback_env_info"):src.find("def _feedback_mailto")]
    check("提交时间用北京时间（云端服务器是 UTC，naive now 会差 8 小时）",
          "timezone(timedelta(hours=8))" in env_src and "北京时间" in env_src)

    # ④ 降级链路：没配 SMTP 不抛异常，退回 mailto
    #    （cfg 传空壳模拟「没配 secrets」——本地配了真 secrets 也不能真发信）
    ok, msg = app._send_feedback("测试", "描述", [], cfg={})
    check("未配置 SMTP 时优雅降级（返回 False 而不是抛异常）",
          ok is False and "SMTP" in msg, msg)
    mailto = app._feedback_mailto("📷 识别错了", "测试")
    check("mailto 兜底链接指向收件邮箱",
          mailto.startswith(f"mailto:{app._FEEDBACK_TO}?subject="), mailto[:50])

    # ⑤ 邮件组装：附件、正文、超大截图跳过（不发真邮件，纯本地组装）
    class _Shot:
        def __init__(self, name, size):
            self.name = name
            self._d = b"\x89PNG" * size
        def getvalue(self):
            return self._d
    cfg = {"host": "x", "user": "a@b.c", "to": app._FEEDBACK_TO}
    m, skipped = app._build_feedback_message(
        cfg, "📷 识别错了", "测试描述",
        [_Shot("ok.png", 10), _Shot("big.png", 1024 * 1024 + 10)])
    atts = list(m.iter_attachments())
    check("正文先写、附件后挂（顺序反了会抛 TypeError）",
          m.get_body() is not None and len(atts) == 1
          and atts[0].get_filename() == "ok.png")
    check("超过 4MB 的截图自动跳过并在正文注明",
          skipped == ["big.png"] and "big.png" in m.get_body().get_content())
    check("正文带应用版本号",
          f"应用版本：v{app.APP_VERSION}" in m.get_body().get_content())


def test_layout_optimizations() -> None:
    """布局优化：顶部不再被注入块白吃 80px；手机端 Hero / 指标卡收紧。"""
    section("N. 布局优化（首屏空间）")
    src = open("app.py", encoding="utf-8").read()

    # ① 只装 <style> 的注入块必须不占位：它们是 0 高，但 flex gap 会给每块留 16px
    check("CSS 注入块不参与布局（否则顶部白吃 80px）",
          '[data-testid="stElementContainer"]:has([data-testid="stMarkdownContainer"] > style:only-child)' in src)
    # ② 但必须限定 style:only-child —— 加载动画那块里 <style> 和卡片是兄弟
    check("隐藏规则不会误伤加载动画（限定 style:only-child）",
          "style:only-child" in src and "load-card" in src)
    # ③ 手机端 Hero 收紧：藏徽标、压内边距、字号降一档
    mobile = src[src.find("@media (max-width: 640px)"):]
    mobile = mobile[:mobile.find("@media", 10)] if "@media" in mobile[10:] else mobile
    check("手机端隐藏 Hero 徽标（省 30px）", ".hero-badge { display: none; }" in src)
    check("手机端 Hero 内边距/字号收紧",
          ".hero h1 { font-size: 1.5rem;" in src and "padding: 1.15rem 1.15rem 1.1rem;" in src)
    check("手机端指标卡收紧", ".metric-card { padding: .6rem .8rem;" in src)
    # ④ 桌面端不受影响：徽标只在 640px 以下隐藏
    check("桌面端 Hero 徽标仍然显示（规则写在 640px 媒体查询里）",
          src.find("@media (max-width: 640px)") < src.find(".hero-badge { display: none; }"))

    # ④d 版心顶部内边距必须 ≥ 固定 Header 高度（实测 60px）：
    #    小于它，首屏第一张卡片的上缘会被 Header 实色底盖掉（手机端尤甚）；
    #    4.2rem 用 rem 书写，随界面字号同步缩放（特大字号 Header 也会变高）
    check("版心顶部内边距 ≥ 4rem（否则卡片上缘被固定 Header 盖住）",
          "padding: 4.2rem var(--space-5) var(--space-8);" in src
          and "padding: 4.2rem var(--space-4) var(--space-7);" in src
          and "padding: 4.2rem var(--space-3) var(--space-6);" in src)

    # ⑤ 二维码悬停不再出现空工具条（Streamlit 的 stElementToolbar 空壳）
    check("分享面板隐藏图片元素工具条（二维码上方的空气泡）",
          '.st-key-share_body [data-testid="stElementToolbar"] { display: none !important; }' in src)

    # ⑥ 解析报告不再显示耗时（用户不关心跑了几秒）
    check("解析报告的提示里不再出现「耗时」",
          "耗时 {elapsed}s" not in src and 'report["elapsed"]' not in src)

    # ⑤ 备份工具在位：每次改版前先留一份可回滚的快照
    from pathlib import Path
    check("备份脚本存在（tools/backup.py）",
          Path("tools/backup.py").exists())
    check("backups/ 不进仓库（只留本地）",
          "backups/" in Path(".gitignore").read_text(encoding="utf-8"))


def test_version_policy() -> None:
    """版本号：主.次.修订，按改动量决定升哪一位（不是每轮都 +0.0.1）。"""
    section("M. 版本号规则")
    import re
    import app

    src = open("app.py", encoding="utf-8").read()
    readme = open("README.md", encoding="utf-8").read()

    check("APP_VERSION 是 X.Y.Z 三段式",
          re.fullmatch(r"\d+\.\d+\.\d+", app.APP_VERSION) is not None,
          app.APP_VERSION)
    check("README 顶部写的版本和代码一致",
          f"当前版本 **v{app.APP_VERSION}**" in readme,
          f"代码 {app.APP_VERSION}")
    check("README 写明了三级规则（大版本 / 功能版本 / 小修）",
          "大版本" in readme and "功能版本" in readme and "小修" in readme
          and "第一位 +1" in readme and "第二位 +1" in readme and "第三位 +1" in readme)
    check("代码里 APP_VERSION 上方有规则注释",
          "按改动量决定升哪一位" in src[src.find("APP_VERSION") - 400: src.find("APP_VERSION")])
    # 「小修」只动最后一位，但别像以前那样一路堆到 .13：
    # 修订位超过个位数，说明该按「功能版本」升中间那位了
    patch = int(app.APP_VERSION.split(".")[2])
    check("修订位保持在个位数（小修只 +1，堆到两位数就该升次版本）",
          0 <= patch <= 9, app.APP_VERSION)


def main() -> int:
    print("=" * 68)
    print(" AI 智能日程收纳工具 · 自动化自查")
    print(f" 时间：{datetime.now():%Y-%m-%d %H:%M:%S}　Python {sys.version.split()[0]}")
    print("=" * 68)

    for fn in (test_text_only, test_time_parsing, test_structure_fields,
               test_dedup_conflict, test_export, test_recurrence,
               test_calendar_and_sample, test_ocr,
               test_ai_layer, test_merge, test_timetable, test_time_blocks, test_recurrence, test_course_cell, test_period_table, test_security, test_theme_guard,
               test_placeholder_style, test_period_editor, test_ai_key_dialog,
               test_loading_indicator, test_chat_notices,
               test_output_selfcheck, test_all_day, test_share,
               test_feedback, test_version_policy,
               test_layout_optimizations):
        try:
            fn()
        except Exception:  # noqa: BLE001
            print(f"\n[ERROR] {fn.__name__} 抛出异常：")
            traceback.print_exc()
            RESULTS.append((fn.__name__, False, "用例执行异常"))

    total = len(RESULTS)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = total - passed

    section("自查汇总")
    print(f"  用例总数：{total}")
    print(f"  通过：{passed}")
    print(f"  失败：{failed}")
    if failed:
        print("\n  失败明细：")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"    · {name}  → {detail}")
    print("\n" + ("  ✅ 全部通过" if failed == 0 else "  ❌ 存在失败用例"))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
