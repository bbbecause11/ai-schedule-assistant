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
    ok = bool(evs) and evs[0].start.hour == 10 and evs[0].start.minute == 0
    check("「第3-4节」换算为 10:00", ok, f"{evs[0].start:%H:%M}-{evs[0].end:%H:%M}" if evs else "")

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
    check("循环日程自动补上默认次数",
          all(app.get_recur_count(e.rrule) == app.RECUR_COUNT_DEFAULT
              for e in norm if e.is_recurring),
          str([e.rrule for e in norm if e.is_recurring]))


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


def main() -> int:
    print("=" * 68)
    print(" AI 智能日程收纳工具 · 自动化自查")
    print(f" 时间：{datetime.now():%Y-%m-%d %H:%M:%S}　Python {sys.version.split()[0]}")
    print("=" * 68)

    for fn in (test_text_only, test_time_parsing, test_structure_fields,
               test_dedup_conflict, test_export, test_recurrence,
               test_calendar_and_sample, test_ocr,
               test_ai_layer, test_merge, test_security):
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
