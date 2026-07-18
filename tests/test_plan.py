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
