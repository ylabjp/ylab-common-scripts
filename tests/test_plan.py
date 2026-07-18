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
    PlanDay,
    PlanMouse,
    Period,
    DailyTime,
    find_scheduled_configs,
    load_plan,
    save_plan,
)


def _sample_plan() -> ExperimentPlan:
    return ExperimentPlan(
        protocol="OFL_Holmes_ver251004",
        schedule="Schedule_2026",
        mouse_list="Mouselist_PFCBehavior2025",
        period=Period(start=date(2026, 4, 26), end=date(2026, 5, 5)),
        daily_time=DailyTime(start="08:00", end="13:30"),
        cc_config=CCConfig(config_dir="config_OFL_2025", photometry_param="20Hz_470_405nm.json"),
        days=[
            PlanDay(label="day01", date=date(2026, 4, 26), phase="exposure",
                    task_param="OAFC_shock_exposure.json"),
            PlanDay(label="day02", date=date(2026, 4, 27), phase="conditioning",
                    task_param="OAFC_holmes_ver251004_conditioning.json"),
            PlanDay(label="day03", date=date(2026, 4, 28), phase="test",
                    task_param="OAFC_holmes_ver251004_test.json",
                    photometry_param="no_stim.json"),
        ],
        mice=[
            PlanMouse(prj="prj27-3-5", mouse_id="m1",
                      bench={"day01": "B10", "day02": "B10", "day03": "B10"}),
            PlanMouse(prj="prj27-3-5", mouse_id="m2",
                      bench={"day01": "B10", "day02": "B12", "day03": "B12"}),
        ],
    )


def test_round_trip():
    plan = _sample_plan()
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "OFL_Holmes_2026.yaml")
        save_plan(plan, p)
        loaded = load_plan(p)
    assert loaded.protocol == "OFL_Holmes_ver251004"
    assert loaded.period.start == date(2026, 4, 26)
    assert loaded.daily_time.end == "13:30"
    assert loaded.cc_config.config_dir == "config_OFL_2025"
    assert len(loaded.days) == 3
    assert loaded.days[1].task_param == "OAFC_holmes_ver251004_conditioning.json"
    assert loaded.mice[1].bench["day02"] == "B12"


def test_none_fields_omitted_in_yaml():
    plan = _sample_plan()
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "x.yaml")
        save_plan(plan, p)
        text = open(p, encoding="utf-8").read()
    # exclude_none により day01 の photometry_param(None) は書かれない
    assert "null" not in text
    # 日本語がエスケープされずそのまま出る
    assert "\\u" not in text


def test_photometry_resolution():
    plan = _sample_plan()
    # day03 は個別指定 -> no_stim.json、他は既定 -> 20Hz_470_405nm.json
    assert plan.resolve_photometry_param(plan.days[0]) == "20Hz_470_405nm.json"
    assert plan.resolve_photometry_param(plan.days[2]) == "no_stim.json"


def _write_sample_dir(d: str) -> None:
    save_plan(_sample_plan(), os.path.join(d, "OFL_Holmes_2026.yaml"))


def test_find_today_yesterday_tomorrow():
    with tempfile.TemporaryDirectory() as d:
        _write_sample_dir(d)
        # 基準日 = day02(2026-04-27) -> 昨日 day01 / 今日 day02 / 明日 day03
        found = find_scheduled_configs(d, ref_date=date(2026, 4, 27))
    assert len(found) == 3
    by_offset = {s.offset: s for s in found}
    assert set(by_offset) == {-1, 0, 1}
    assert by_offset[0].day_label == "day02"
    assert by_offset[0].rel_label_ja == "今日"
    assert by_offset[0].task_param == "OAFC_holmes_ver251004_conditioning.json"
    assert by_offset[0].config_dir == "config_OFL_2025"
    # 明日(day03)は個別 photometry 指定が反映される
    assert by_offset[1].photometry_param == "no_stim.json"
    # 昨日(day01)は既定 photometry
    assert by_offset[-1].photometry_param == "20Hz_470_405nm.json"
    # 表示ラベルに主要情報が含まれる
    assert "今日" in by_offset[0].display_label()
    assert "config_OFL_2025" in by_offset[0].display_label()


def test_find_sorted_and_windowed():
    with tempfile.TemporaryDirectory() as d:
        _write_sample_dir(d)
        # window=0 -> 当日のみ
        only_today = find_scheduled_configs(d, ref_date=date(2026, 4, 26), window_days=0)
        assert [s.day_label for s in only_today] == ["day01"]
        # window=2 で基準日 day01 -> 今日 day01 / 明日 day02 / 明後日 day03
        wide = find_scheduled_configs(d, ref_date=date(2026, 4, 26), window_days=2)
        assert [s.offset for s in wide] == [0, 1, 2]  # offset 昇順にソート


def test_find_empty_and_missing_dir():
    # 存在しないディレクトリ -> 空
    assert find_scheduled_configs("/no/such/dir", ref_date=date(2026, 4, 27)) == []
    with tempfile.TemporaryDirectory() as d:
        _write_sample_dir(d)
        # 予定に無い日 -> 空
        assert find_scheduled_configs(d, ref_date=date(2030, 1, 1)) == []


def test_invalid_file_skipped():
    with tempfile.TemporaryDirectory() as d:
        _write_sample_dir(d)
        with open(os.path.join(d, "broken.yaml"), "w", encoding="utf-8") as f:
            f.write(": : not valid : yaml : [")
        # 壊れたファイルがあっても正常分は取得できる
        found = find_scheduled_configs(d, ref_date=date(2026, 4, 27))
        assert len(found) == 3


def test_multiple_plans_aggregated():
    with tempfile.TemporaryDirectory() as d:
        _write_sample_dir(d)
        # 別プロトコルの計画を追加(同じ今日=day02 の日付に別 config)
        plan2 = ExperimentPlan(
            protocol="AnotherProtocol",
            cc_config=CCConfig(config_dir="config_SIT"),
            days=[PlanDay(label="s1", date=date(2026, 4, 27), phase="test",
                          task_param="sit_test.json")],
        )
        save_plan(plan2, os.path.join(d, "another.yaml"))
        found = find_scheduled_configs(d, ref_date=date(2026, 4, 27))
    # 今日(offset 0)は 2 件になる
    today = [s for s in found if s.offset == 0]
    assert len(today) == 2
    protocols = {s.protocol for s in today}
    assert protocols == {"OFL_Holmes_ver251004", "AnotherProtocol"}


def _multi_period_plan() -> ExperimentPlan:
    """複数 Period・日ごと体重つきの計画(新形式)。"""
    return ExperimentPlan(
        protocol="OFL_Holmes_ver251004",
        mouse_list="Mouselist_PFCBehavior2025",
        cc_config=CCConfig(config_dir="config_OFL_2025", photometry_param="20Hz_470_405nm.json"),
        daily_time=DailyTime(start="08:00", end="13:30"),
        periods=[
            ExperimentPeriod(
                name="cohort1",
                period=Period(start=date(2026, 4, 26), end=date(2026, 4, 27)),
                days=[
                    PlanDay(label="day01", date=date(2026, 4, 26), phase="exposure",
                            task_param="OAFC_shock_exposure.json"),
                    PlanDay(label="day02", date=date(2026, 4, 27), phase="conditioning",
                            task_param="cond.json"),
                ],
                mice=[
                    PlanMouse(prj="prj27-3-5", mouse_id="m1",
                              bench={"day01": "B10", "day02": "B12"},
                              weight={"day01": 23.4, "day02": 23.1}),
                ],
            ),
            ExperimentPeriod(
                name="cohort2",
                period=Period(start=date(2026, 6, 1), end=date(2026, 6, 1)),
                cc_config=CCConfig(config_dir="config_OFL_2025", photometry_param="no_stim.json"),
                days=[
                    PlanDay(label="day01", date=date(2026, 6, 1), phase="test",
                            task_param="test.json"),
                ],
                mice=[
                    PlanMouse(prj="prj27-3-9", mouse_id="m9",
                              bench={"day01": "B10"}, weight={"day01": 25.0}),
                ],
            ),
        ],
    )


def test_multi_period_round_trip():
    plan = _multi_period_plan()
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "multi.yaml")
        save_plan(plan, p)
        text = open(p, encoding="utf-8").read()
        loaded = load_plan(p)
    assert "periods:" in text
    # 新形式では旧トップレベルの days/mice/period は書かれない
    assert "\ndays:" not in text and "\nmice:" not in text and "\nperiod:" not in text
    assert len(loaded.periods) == 2
    assert loaded.periods[0].name == "cohort1"
    assert loaded.periods[0].days[1].task_param == "cond.json"
    assert loaded.periods[0].mice[0].bench["day02"] == "B12"
    assert loaded.periods[0].mice[0].weight["day01"] == 23.4
    # per-period cc_config の上書きが保持される
    assert loaded.periods[1].cc_config.photometry_param == "no_stim.json"


def test_legacy_plan_omits_periods_key():
    # 旧形式(単一 Period)の保存には periods キーが出ない
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "legacy.yaml")
        save_plan(_sample_plan(), p)
        text = open(p, encoding="utf-8").read()
    assert "periods:" not in text
    assert "\ndays:" in text  # 旧トップレベル days は残る


def test_weight_round_trip():
    plan = _sample_plan()
    plan.mice[0].weight = {"day01": 22.5, "day03": 22.9}
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "w.yaml")
        save_plan(plan, p)
        loaded = load_plan(p)
    assert loaded.mice[0].weight["day01"] == 22.5
    assert loaded.mice[0].weight["day03"] == 22.9


def test_per_mouse_task_param_round_trip():
    plan = _sample_plan()
    # day02 だけ 2 個体目が別 task を使う(標準を上書き)
    plan.mice[1].task_param = {"day02": "special_conditioning.json"}
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "tp.yaml")
        save_plan(plan, p)
        loaded = load_plan(p)
    assert loaded.mice[1].task_param["day02"] == "special_conditioning.json"
    # 上書きの無い個体は空のまま
    assert loaded.mice[0].task_param == {}


def test_backward_compat_effective_periods():
    # 旧形式(トップレベル period/days/mice)-> 単一 Period に合成
    eff = _sample_plan().effective_periods()
    assert len(eff) == 1
    assert eff[0].name == ""
    assert len(eff[0].days) == 3
    assert eff[0].mice[1].bench["day02"] == "B12"
    # periods 明示時はそのまま返す
    assert len(_multi_period_plan().effective_periods()) == 2
    # 完全に空の計画 -> 空
    assert ExperimentPlan().effective_periods() == []


def test_find_scheduled_across_periods():
    with tempfile.TemporaryDirectory() as d:
        save_plan(_multi_period_plan(), os.path.join(d, "multi.yaml"))
        # cohort1 の day02 (2026-04-27) を基準日 -> 昨日 day01 / 今日 day02
        found = find_scheduled_configs(d, ref_date=date(2026, 4, 27))
    assert {s.offset for s in found} == {-1, 0}
    today = [s for s in found if s.offset == 0][0]
    assert today.period_name == "cohort1"
    assert today.task_param == "cond.json"
    assert today.config_dir == "config_OFL_2025"
    with tempfile.TemporaryDirectory() as d:
        save_plan(_multi_period_plan(), os.path.join(d, "multi.yaml"))
        found2 = find_scheduled_configs(d, ref_date=date(2026, 6, 1))
    assert len(found2) == 1
    assert found2[0].period_name == "cohort2"
    # per-period cc_config 上書きの photometry が反映
    assert found2[0].photometry_param == "no_stim.json"


def test_shared_schedule_offset_dates():
    # Schedule を Plan 直下(全 Period 共通)に置き、Period は start だけ持つ。
    # 具体日付は start + offset で決まる。
    plan = ExperimentPlan(
        protocol="P",
        cc_config=CCConfig(config_dir="cfg"),
        days=[
            PlanDay(label="day01", offset=0, task_param="a.json"),
            PlanDay(label="day02", offset=1, task_param="b.json"),
        ],
        periods=[
            ExperimentPeriod(name="c1", period=Period(start=date(2026, 4, 26))),
            ExperimentPeriod(name="c2", period=Period(start=date(2026, 6, 1))),
        ],
    )
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "sched.yaml")
        save_plan(plan, p)
        loaded = load_plan(p)
    # Period 自身は days を持たない -> Plan 共通 days を使う
    assert loaded.periods[0].days == []
    assert [dd.label for dd in loaded.effective_days_for(loaded.periods[0])] == ["day01", "day02"]
    assert loaded.days[1].offset == 1
    # c1: day01(4/26)=昨日, day02(4/27)=今日。 c2(6月) は範囲外
    with tempfile.TemporaryDirectory() as d:
        save_plan(plan, os.path.join(d, "sched.yaml"))
        found = find_scheduled_configs(d, ref_date=date(2026, 4, 27))
    assert {(s.period_name, s.offset) for s in found} == {("c1", -1), ("c1", 0)}
    today = [s for s in found if s.offset == 0][0]
    assert today.date == date(2026, 4, 27)
    assert today.task_param == "b.json"


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
