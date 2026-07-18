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
    find_scheduled_configs,
    load_plan,
    resolve_day_date,
    save_plan,
)


def _sample_plan() -> ExperimentPlan:
    """新形式: Schedule は Plan 直下(offset)、Period は start + 名簿。"""
    return ExperimentPlan(
        protocol="OFL_Holmes_ver251004",
        within_factors=["paired", "unpaired"],
        cc_config=CCConfig(config_dir="config_OFL_2025", photometry_param="20Hz_470_405nm.json"),
        days=[
            PlanDay(label="day01", offset=0, phase="exposure", task_param="OAFC_shock_exposure.json"),
            PlanDay(label="day02", offset=1, phase="conditioning", task_param="cond.json"),
            PlanDay(label="day03", offset=2, phase="test", task_param="test.json",
                    photometry_param="no_stim.json"),
        ],
        periods=[
            ExperimentPeriod(
                name="cohort1",
                period=Period(start=date(2026, 4, 26), end=date(2026, 5, 5)),
                mice=[
                    PlanMouse(prj="prj27-3-5", mouse_id="m1", sex="m",
                              ear_tag="R1L2", mating_id="mat-7",
                              birth_date="251201", termination="260430", fail=True,
                              age_day_2=54, actual_bw_day_2=22.1,
                              bench={"day01": "B10", "day02": "B10", "day03": "B10"},
                              weight={"day01": 23.4},
                              task_param={"day02": "special.json"},
                              within_factor={"day01": "paired"}),
                    PlanMouse(prj="prj27-3-5", mouse_id="m2", sex="f",
                              bench={"day01": "B10", "day02": "B12", "day03": "B12"}),
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
    assert loaded.protocol == "OFL_Holmes_ver251004"
    assert loaded.within_factors == ["paired", "unpaired"]
    assert loaded.cc_config.config_dir == "config_OFL_2025"
    assert len(loaded.days) == 3
    assert loaded.days[1].offset == 1
    assert loaded.days[1].task_param == "cond.json"
    assert len(loaded.periods) == 1
    p0 = loaded.periods[0]
    assert p0.name == "cohort1"
    assert p0.period.start == date(2026, 4, 26)
    assert p0.mice[0].sex == "m"
    assert p0.mice[0].bench["day02"] == "B10"
    assert p0.mice[0].weight["day01"] == 23.4
    assert p0.mice[0].task_param["day02"] == "special.json"
    assert p0.mice[0].within_factor["day01"] == "paired"
    # basic-info fields round-trip
    assert p0.mice[0].ear_tag == "R1L2"
    assert p0.mice[0].mating_id == "mat-7"
    assert p0.mice[0].birth_date == "251201"
    assert p0.mice[0].termination == "260430"
    assert p0.mice[0].fail is True
    assert p0.mice[0].age_day_2 == 54
    assert p0.mice[0].actual_bw_day_2 == 22.1
    # age is derived, never a stored field
    assert not hasattr(p0.mice[0], "age") or "age" not in p0.mice[0].model_fields
    # defaults stay omitted for the second mouse
    assert p0.mice[1].fail is False
    assert p0.mice[1].ear_tag is None


def test_none_and_defaults_omitted_in_yaml():
    plan = _sample_plan()
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "x.yaml")
        save_plan(plan, p)
        text = open(p, encoding="utf-8").read()
    assert "null" not in text            # exclude_defaults + exclude_none
    assert "\\u" not in text             # 日本語などがエスケープされない(今回は無いが不変条件)
    assert "\n  date:" not in text       # schedule の各 day は具体日付を持たない(offset のみ)
    assert "\ndays:" in text and "\nperiods:" in text
    assert "\nmice:" not in text         # トップレベル mice は無い(period 内のみ)
    assert "\nwithin_factors:" in text   # Plan 直下の候補リスト
    assert "daily_time:" not in text     # 廃止済みフィールドは書き出さない
    assert "mouse_list:" not in text
    assert "\nschedule:" not in text


def test_offset_date_resolution():
    plan = _sample_plan()
    per = plan.periods[0]
    assert resolve_day_date(per, plan.days[0]) == date(2026, 4, 26)   # start + 0
    assert resolve_day_date(per, plan.days[2]) == date(2026, 4, 28)   # start + 2
    # start 未設定 -> None
    assert resolve_day_date(ExperimentPeriod(name="x"), plan.days[0]) is None


def test_photometry_resolution():
    plan = _sample_plan()
    assert plan.resolve_photometry_param(plan.days[0]) == "20Hz_470_405nm.json"  # 既定
    assert plan.resolve_photometry_param(plan.days[2]) == "no_stim.json"          # day 個別


def test_find_today_yesterday_tomorrow():
    with tempfile.TemporaryDirectory() as d:
        save_plan(_sample_plan(), os.path.join(d, "OFL_Holmes_2026.yaml"))
        # cohort1 start 4/26 -> day01=4/26, day02=4/27, day03=4/28
        found = find_scheduled_configs(d, ref_date=date(2026, 4, 27))
    assert len(found) == 3
    by_offset = {s.offset: s for s in found}
    assert set(by_offset) == {-1, 0, 1}
    assert by_offset[0].day_label == "day02"
    assert by_offset[0].date == date(2026, 4, 27)
    assert by_offset[0].rel_label_ja == "今日"
    assert by_offset[0].task_param == "cond.json"
    assert by_offset[0].config_dir == "config_OFL_2025"
    assert by_offset[0].period_name == "cohort1"
    assert by_offset[1].photometry_param == "no_stim.json"        # day03 個別
    assert by_offset[-1].photometry_param == "20Hz_470_405nm.json"  # day01 既定
    assert "今日" in by_offset[0].display_label()
    assert "config_OFL_2025" in by_offset[0].display_label()


def test_find_sorted_and_windowed():
    with tempfile.TemporaryDirectory() as d:
        save_plan(_sample_plan(), os.path.join(d, "OFL_Holmes_2026.yaml"))
        only_today = find_scheduled_configs(d, ref_date=date(2026, 4, 26), window_days=0)
        assert [s.day_label for s in only_today] == ["day01"]
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
        mice=[PlanMouse(prj="p", mouse_id="x", bench={"day01": "B11"})],
    ))
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "OFL_Holmes_2026.yaml")
        save_plan(plan, p)
        loaded = load_plan(p)
        # cohort2 の start(6/1) 基準で走査
        found = find_scheduled_configs(d, ref_date=date(2026, 6, 1))
    assert len(loaded.periods) == 2
    assert len(loaded.days) == 3            # Schedule は 1 つを共有
    assert loaded.periods[1].mice[0].bench["day01"] == "B11"
    # 6/1 近傍は cohort2 のみ(cohort1 は 4 月で範囲外)
    assert {s.period_name for s in found} == {"cohort2"}
    today = [s for s in found if s.offset == 0][0]
    assert today.day_label == "day01" and today.date == date(2026, 6, 1)


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
