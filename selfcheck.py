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
        ("高等数学(一)(上)卜 庆刚 W9阶 2-6,9-18, /",
         "高等数学(一)(上)", "卜庆刚", "9阶", "2-6、9-18", ""),
        ("创新创业理论与实践(1) 李宁 W文约209 双 12-18, /",
         "创新创业理论与实践(1)", "李宁", "文约209", "12-18", "双周"),
        ("问题求解与程序设计( 上)官威 W文约101 2- 6,9-15 , /",
         "问题求解与程序设计(上)", "官威", "文约101", "2-6、9-15", ""),
        ("体育(1) 体育部 W科教 城体育场 2-17，/",
         "体育(1)", "体育部", "科教城体育场", "2-17", ""),
        ("新生研讨课 官威 W14 06 2-6,9-11, /",
         "新生研讨课", "官威", "1406", "2-6、9-11", ""),
        ("统计学 吴敏 W经济楼10 81-12,/",          # 房间号被挪了一位
         "统计学", "吴敏", "经济楼108", "1-12", ""),
        ("俄罗斯历史 Okhotnik ov Egor W9阶 9-12, 1",
         "俄罗斯历史", "Okhotnikov Egor", "9阶", "9-12", ""),
        ("综合英语| 刘畅 W文约 101 2-6,9-18,/",     # 罗马数字 I 被读成竖线
         "综合英语I", "刘畅", "文约101", "2-6、9-18", ""),
        ("马克思主义基本原理 陈静 WB105 2-6,9-16 .1",
         "马克思主义基本原理", "陈静", "B105", "2-6、9-16", ""),
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
          re.search(r"def _cb_toggle_ai_key.*?ai_key_input", src, re.S)
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
               test_loading_indicator):
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
