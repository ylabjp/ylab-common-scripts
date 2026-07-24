# -*- coding: utf-8 -*-
"""ylabcommon.models.plan の単体テスト。

pytest でも、依存の軽い環境で `python tests/test_plan.py` として直接実行しても
動くようにしてある(重い conftest を経由せず検証したいため)。
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date

# src レイアウトを直接 import できるようにする(インストール前でも検証可能に)。
_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from ylabcommon.models.plan import (  # noqa: E402
    CCConfig,
    ExperimentPeriod,
    ExperimentPlan,
    Period,
    PlanDay,
    PlanMouse,
    default_sessions,
    find_scheduled_configs,
    find_scheduled_mice,
    format_day_code,
    load_plan,
    resolve_day_date,
    save_plan,
)


def _sample_plan() -> ExperimentPlan:
    """新形式: Schedule は Plan 直下(1 始まりの day)、Period は start + 名簿。

    protocol は持たない(= ファイル名で表す)。マウスの日ごと辞書は day ラベル
    (``dayN``、offset ではなく通日番号)でキーする。
    """
    return ExperimentPlan(
        within_factors=["paired", "unpaired"],
        water_restriction_ratio=0.85,
        daily_evaporation_ml=1.2,
        cc_config=CCConfig(config_dir="config_OFL_2025"),
        days=[
            PlanDay(day=1, phase="1", task_param="OAFC_shock_exposure.json",
                    photometry_param="20Hz_470_405nm.json"),
            PlanDay(day=2, phase="1", task_param="cond.json",
                    photometry_param="20Hz_470_405nm.json"),
            PlanDay(day=3, phase="2", task_param="test.json", photometry_param="no_stim.json"),
        ],
        periods=[
            ExperimentPeriod(
                name="cohort1",
                period=Period(start=date(2026, 4, 26), end=date(2026, 5, 5)),
                mice=[
                    PlanMouse(prj="prj27-3-5", mouse_id="m1", sex="m",
                              ear_tag="R1L2", mating_id="mat-7", cond="DEM-Cumin",
                              birth_date="251201", termination="260430", fail=True,
                              age_day_2=54, actual_bw_day_2=22.1,
                              bench={"day1": "B10", "day2": "B10", "day3": "B10"},
                              bw_before={"day1": 23.4}, bw_after={"day1": 24.1},
                              water_adjust={"day1": 1.7},
                              task_param={"day2": "special.json"},
                              photometry_param={"day2": "mouse_405_override.json"},
                              within_factor={"day1": "paired"}),
                    PlanMouse(prj="prj27-3-5", mouse_id="m2", sex="f",
                              bench={"day1": "B10", "day2": "B12", "day3": "B12"}),
                ],
            ),
        ],
    )


def test_round_trip():
    plan = _sample_plan()
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "OFL_Holmes_2026.yaml")
        save_plan(plan, p)
        loaded = load_plan(p)
    assert loaded.within_factors == ["paired", "unpaired"]
    assert loaded.water_restriction_ratio == 0.85
    assert loaded.daily_evaporation_ml == 1.2
    assert loaded.cc_config.config_dir == "config_OFL_2025"
    assert len(loaded.days) == 3
    assert loaded.days[1].day == 2 and loaded.days[1].offset == 1 and loaded.days[1].label == "day2"
    assert loaded.days[1].task_param == "cond.json"
    assert len(loaded.periods) == 1
    p0 = loaded.periods[0]
    assert p0.name == "cohort1"
    assert p0.period.start == date(2026, 4, 26)
    assert p0.mice[0].sex == "m"
    assert p0.mice[0].bench["day2"] == "B10"
    assert p0.mice[0].bw_before["day1"] == 23.4
    assert p0.mice[0].bw_after["day1"] == 24.1
    assert p0.mice[0].water_adjust["day1"] == 1.7
    assert p0.mice[0].task_param["day2"] == "special.json"
    assert p0.mice[0].within_factor["day1"] == "paired"
    # basic-info fields round-trip
    assert p0.mice[0].ear_tag == "R1L2"
    assert p0.mice[0].mating_id == "mat-7"
    assert p0.mice[0].birth_date == "251201"
    assert p0.mice[0].termination == "260430"
    assert p0.mice[0].fail is True
    assert p0.mice[0].age_day_2 == 54
    assert p0.mice[0].actual_bw_day_2 == 22.1
    # defaults stay omitted for the second mouse
    assert p0.mice[1].fail is False
    assert p0.mice[1].ear_tag is None


def test_legacy_label_offset_loads_as_day():
    """旧 {label, offset} 形式は day(= offset + 1)へ変換して読める。"""
    plan = ExperimentPlan.model_validate({
        "protocol": "legacy",     # extra="ignore" で読み飛ばされる
        "days": [
            {"label": "day-1", "offset": -2},   # -> day -1
            {"label": "day01", "phase": "1"},   # offset 省略 -> day 1
            {"label": "day02", "offset": 1},    # -> day 2
        ],
        "periods": [{"name": "p", "period": {"start": "2026-04-26"}, "mice": []}],
    })
    assert not hasattr(ExperimentPlan, "protocol") or "protocol" not in plan.model_dump()
    assert [d.day for d in plan.days] == [-1, 1, 2]
    assert [d.label for d in plan.days] == ["day-1", "day1", "day2"]
    assert resolve_day_date(plan.periods[0], plan.days[1]) == date(2026, 4, 26)   # day1 = start


def test_none_and_defaults_omitted_in_yaml():
    plan = _sample_plan()
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "x.yaml")
        save_plan(plan, p)
        text = open(p, encoding="utf-8").read()
    assert "null" not in text            # exclude_defaults + exclude_none
    assert "\\u" not in text             # 日本語などがエスケープされない
    assert "\n  date:" not in text       # schedule の各 day は具体日付を持たない(day のみ)
    assert "\ndays:" in text and "\nperiods:" in text
    assert "\nmice:" not in text         # トップレベル mice は無い(period 内のみ)
    assert text.startswith("within_factors:")  # protocol 削除で先頭が within_factors に
    assert "protocol:" not in text       # protocol はモデルから削除済み
    assert "label:" not in text and "offset:" not in text  # 統合された day のみ書き出す
    assert "\n- day: 1" in text
    assert "daily_time:" not in text     # 廃止済みフィールドは書き出さない
    assert "mouse_list:" not in text
    assert "\nschedule:" not in text


def test_offset_date_resolution():
    plan = _sample_plan()
    per = plan.periods[0]
    assert resolve_day_date(per, plan.days[0]) == date(2026, 4, 26)   # day1 = start + 0
    assert resolve_day_date(per, plan.days[2]) == date(2026, 4, 28)   # day3 = start + 2
    # start 未設定 -> None
    assert resolve_day_date(ExperimentPeriod(name="x"), plan.days[0]) is None


def test_format_day_code_and_default_sessions():
    assert format_day_code(1, "1", 3) == "day01-phase01S03"
    assert format_day_code(15, "5-5", 2) == "day15-phase5-5S02"
    assert format_day_code(1) == "day01" and format_day_code(-1, "") == "day-1"
    assert default_sessions(["1", "1", "2", "1"]) == [1, 2, 1, 3]
    assert default_sessions(["1", "", "4"]) == [1, None, 1]


def test_photometry_resolution():
    plan = _sample_plan()
    assert plan.resolve_photometry_param(plan.days[0]) == "20Hz_470_405nm.json"  # day 標準
    assert plan.resolve_photometry_param(plan.days[2]) == "no_stim.json"          # day 標準(別値)


def test_find_today_yesterday_tomorrow():
    with tempfile.TemporaryDirectory() as d:
        save_plan(_sample_plan(), os.path.join(d, "OFL_Holmes_2026.yaml"))
        # cohort1 start 4/26 -> day1=4/26, day2=4/27, day3=4/28
        found = find_scheduled_configs(d, ref_date=date(2026, 4, 27))
    assert len(found) == 3
    by_offset = {s.offset: s for s in found}
    assert set(by_offset) == {-1, 0, 1}
    assert by_offset[0].day_label == "day2"
    assert by_offset[0].date == date(2026, 4, 27)
    assert by_offset[0].rel_label_ja == "今日"
    assert by_offset[0].task_param == "cond.json"
    assert by_offset[0].config_dir == "config_OFL_2025"
    assert by_offset[0].period_name == "cohort1"
    # session の既定は同一 phase の累積: day2 は phase "1" の 2 回目 -> S02
    assert by_offset[0].session == 2
    assert by_offset[0].day_code == "day02-phase01S02"
    assert by_offset[1].photometry_param == "no_stim.json"        # day3 標準
    assert by_offset[-1].photometry_param == "20Hz_470_405nm.json"  # day1 標準
    assert "今日" in by_offset[0].display_label()
    assert "day02-phase01S02" in by_offset[0].display_label()
    assert "config_OFL_2025" in by_offset[0].display_label()


def test_find_sorted_and_windowed():
    with tempfile.TemporaryDirectory() as d:
        save_plan(_sample_plan(), os.path.join(d, "OFL_Holmes_2026.yaml"))
        only_today = find_scheduled_configs(d, ref_date=date(2026, 4, 26), window_days=0)
        assert [s.day_label for s in only_today] == ["day1"]
        wide = find_scheduled_configs(d, ref_date=date(2026, 4, 26), window_days=2)
        assert [s.offset for s in wide] == [0, 1, 2]


def test_find_empty_and_missing_dir():
    assert find_scheduled_configs("/no/such/dir", ref_date=date(2026, 4, 27)) == []
    with tempfile.TemporaryDirectory() as d:
        save_plan(_sample_plan(), os.path.join(d, "OFL_Holmes_2026.yaml"))
        assert find_scheduled_configs(d, ref_date=date(2030, 1, 1)) == []


def test_invalid_file_skipped():
    with tempfile.TemporaryDirectory() as d:
        save_plan(_sample_plan(), os.path.join(d, "OFL_Holmes_2026.yaml"))
        with open(os.path.join(d, "broken.yaml"), "w", encoding="utf-8") as f:
            f.write(": : not valid : yaml : [")
        found = find_scheduled_configs(d, ref_date=date(2026, 4, 27))
        assert len(found) == 3


def test_multiple_periods_share_schedule():
    plan = _sample_plan()
    plan.periods.append(ExperimentPeriod(
        name="cohort2",
        period=Period(start=date(2026, 6, 1)),
        mice=[PlanMouse(prj="p", mouse_id="x", bench={"day1": "B11"})],
    ))
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "OFL_Holmes_2026.yaml")
        save_plan(plan, p)
        loaded = load_plan(p)
        # cohort2 の start(6/1) 基準で走査
        found = find_scheduled_configs(d, ref_date=date(2026, 6, 1))
    assert len(loaded.periods) == 2
    assert len(loaded.days) == 3            # Schedule は 1 つを共有
    assert loaded.periods[1].mice[0].bench["day1"] == "B11"
    # 6/1 近傍は cohort2 のみ(cohort1 は 4 月で範囲外)
    assert {s.period_name for s in found} == {"cohort2"}
    today = [s for s in found if s.offset == 0][0]
    assert today.day_label == "day1" and today.date == date(2026, 6, 1)


def test_per_mouse_photometry_round_trip():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "x.yaml")
        save_plan(_sample_plan(), p)
        loaded = load_plan(p)
    assert loaded.periods[0].mice[0].photometry_param["day2"] == "mouse_405_override.json"
    # a mouse with no override keeps an empty dict (default omitted in YAML)
    assert loaded.periods[0].mice[1].photometry_param == {}


def test_find_scheduled_mice_resolves_overrides():
    with tempfile.TemporaryDirectory() as d:
        save_plan(_sample_plan(), os.path.join(d, "OFL_Holmes_2026.yaml"))
        found = find_scheduled_mice(d, ref_date=date(2026, 4, 27))
    assert len(found) == 6                        # 3 days x 2 mice
    today = [s for s in found if s.offset == 0]
    by_id = {s.mouse_id: s for s in today}
    assert set(by_id) == {"m1", "m2"}
    # m1: per-mouse task & photometry overrides win; slot from bench; cond carried
    assert by_id["m1"].day_label == "day2"
    assert by_id["m1"].day_code == "day02-phase01S02"   # day2 = phase "1" の 2 回目
    assert by_id["m1"].session == 2
    assert by_id["m1"].task_param == "special.json"
    assert by_id["m1"].photometry_param == "mouse_405_override.json"
    assert by_id["m1"].slot == "B10"
    assert by_id["m1"].cond == "DEM-Cumin"
    assert by_id["m1"].prj == "prj27-3-5"
    assert by_id["m1"].config_dir == "config_OFL_2025"
    # m2: falls back to the day-standard task and the day-standard photometry
    assert by_id["m2"].task_param == "cond.json"
    assert by_id["m2"].photometry_param == "20Hz_470_405nm.json"
    assert by_id["m2"].slot == "B12"
    assert by_id["m2"].sex == "f"
    # day3 photometry override (day-level) reaches both mice
    day3 = {s.mouse_id: s for s in found if s.offset == 1}
    assert day3["m1"].photometry_param == "no_stim.json"
    assert day3["m2"].photometry_param == "no_stim.json"
    lbl = by_id["m1"].display_label()
    assert "[B10]" in lbl and "m1" in lbl


def test_find_scheduled_mice_window0_and_sort():
    with tempfile.TemporaryDirectory() as d:
        save_plan(_sample_plan(), os.path.join(d, "OFL_Holmes_2026.yaml"))
        only_today = find_scheduled_mice(d, ref_date=date(2026, 4, 26), window_days=0)
    assert [s.day_label for s in only_today] == ["day1", "day1"]   # day1 only
    assert all(s.slot == "B10" for s in only_today)
    assert {s.task_param for s in only_today} == {"OAFC_shock_exposure.json"}  # day standard
    assert {s.photometry_param for s in only_today} == {"20Hz_470_405nm.json"}  # day standard
    m1 = [s for s in only_today if s.mouse_id == "m1"][0]
    assert m1.within_factor == "paired"
    # sort key (offset, slot, prj, mouse_id): same slot/prj -> m1 before m2
    assert [s.mouse_id for s in only_today] == ["m1", "m2"]


def _run_standalone() -> int:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
