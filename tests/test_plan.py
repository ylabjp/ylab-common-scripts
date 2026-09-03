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
from pathlib import Path

# src レイアウトを直接 import できるようにする(インストール前でも検証可能に)。
_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from ylabcommon.models.plan import (  # noqa: E402
    CCConfig,
    ExperimentTrial,
    ExperimentPlan,
    Period,
    PlanDay,
    PlanMouse,
    ProgramStep,
    default_sessions,
    find_scheduled_configs,
    find_scheduled_mice,
    format_day_code,
    format_mouse_code,
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
        program=[
            ProgramStep(phase="1", task_param="OAFC_shock_exposure.json",
                        photometry_param="20Hz_470_405nm.json"),
            ProgramStep(phase="1", task_param="cond.json",
                        photometry_param="20Hz_470_405nm.json"),
            ProgramStep(phase="2", task_param="test.json", photometry_param="no_stim.json"),
        ],
        trials=[
            ExperimentTrial(
                name="cohort1",
                period=Period(start=date(2026, 4, 26), end=date(2026, 5, 5)),
                mice=[
                    PlanMouse(prj="prj27-3-5", mouse_id="m1", sex="m",
                              ear_tag="R1L2", mating_id="mat-7", cond="DEM-Cumin",
                              birth_date=date(2025, 12, 1), termination=date(2026, 4, 30), fail=True,
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
    assert len(loaded.program) == 3
    day2 = loaded.resolve_trial(loaded.trials[0])[1]
    assert day2.day == 2 and day2.offset == 1 and day2.label == "day2"
    assert day2.task_param == "cond.json"
    assert len(loaded.trials) == 1
    p0 = loaded.trials[0]
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
    assert p0.mice[0].birth_date == date(2025, 12, 1)      # ISO date, like period.start
    assert p0.mice[0].termination == date(2026, 4, 30)
    assert p0.mice[0].fail is True
    # the legacy day-2 field names come back as a baseline with its day written down
    assert p0.mice[0].baseline_day == -2
    assert p0.mice[0].baseline_age == 54
    assert p0.mice[0].baseline_bw == 22.1
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
    days = plan.trials[0].days
    assert [d.day for d in days] == [-1, 1, 2]
    assert [d.label for d in days] == ["day-1", "day1", "day2"]
    assert resolve_day_date(plan.trials[0], days[1]) == date(2026, 4, 26)   # day1 = start


def test_legacy_periods_key_loads_as_trials():
    """旧キー ``periods:`` も読める。保存は新キー ``trials:`` に統一される。"""
    raw = {
        "days": [{"day": 1}],
        "periods": [{"name": "old", "period": {"start": "2026-04-26"},
                     "mice": [{"mouse_id": "m1", "bench": {"day1": "B10"}}]}],
    }
    plan = ExperimentPlan.model_validate(raw)
    assert len(plan.trials) == 1 and plan.trials[0].name == "old"
    assert plan.trials[0].mice[0].bench["day1"] == "B10"
    # 旧コード互換: plan.periods は同じリストを指す
    assert plan.periods is plan.trials
    # 新キーでも同じ結果
    new = ExperimentPlan.model_validate({**{k: v for k, v in raw.items() if k != "periods"},
                                         "trials": raw["periods"]})
    assert new.trials[0].name == "old"
    # 保存すると trials: だけになる
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "x.yaml")
        save_plan(plan, p)
        text = open(p, encoding="utf-8").read()
        assert "trials:" in text and "periods:" not in text
        assert load_plan(p).trials[0].mice[0].bench["day1"] == "B10"


def test_legacy_yymmdd_dates_load_as_iso():
    """旧形式の ``YYMMDD`` 文字列は ISO 日付として読み、保存も ISO で揃う。"""
    m = PlanMouse.model_validate({"mouse_id": "m", "birth_date": "250426",
                                  "termination": "250716"})
    assert m.birth_date == date(2025, 4, 26) and m.termination == date(2025, 7, 16)
    assert PlanMouse.model_validate({"birth_date": ""}).birth_date is None
    assert PlanMouse.model_validate({"birth_date": "2025-04-26"}).birth_date == date(2025, 4, 26)
    plan = ExperimentPlan(trials=[ExperimentTrial(name="t", mice=[m])])
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, "x.yaml")
        save_plan(plan, f)
        text = open(f, encoding="utf-8").read()
        assert "birth_date: 2025-04-26" in text        # ISO, unquoted like period.start
        assert "'250426'" not in text
        assert load_plan(f).trials[0].mice[0].birth_date == date(2025, 4, 26)


def test_freezing_protects_a_finished_trial_from_plan_edits():
    """実施を終えた Trial は凍結でき、以後 Plan を書き換えても記録が変わらない。"""
    plan = _sample_plan()
    past = plan.trials[0]
    past.days = [PlanDay(day=1), PlanDay(day=2), PlanDay(day=3)]
    running = ExperimentTrial(name="cohort2", period=Period(start=date(2026, 9, 1)),
                              days=[PlanDay(day=1), PlanDay(day=2), PlanDay(day=3)])
    plan.trials.append(running)

    before = [(d.day, d.phase, d.session, d.task_param) for d in plan.resolve_trial(past)]
    assert plan.freeze_trial(past) == 3 and plan.is_frozen(past)
    assert not plan.is_frozen(running)

    # Plan を書き換える(先頭にステップを挿入 + 末尾を削除)
    plan.program.insert(0, ProgramStep(phase="0", task_param="new.json"))
    plan.program.pop()

    after = [(d.day, d.phase, d.session, d.task_param) for d in plan.resolve_trial(past)]
    assert after == before, "凍結した Trial が Plan の編集で変わってしまった"
    # 実施前の Trial には新しい program が反映される
    assert plan.resolve_trial(running)[0].task_param == "new.json"

    # 凍結は保存・再読込を越えて維持される
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, "x.yaml")
        save_plan(plan, f)
        reloaded = load_plan(f)
        assert reloaded.is_frozen(reloaded.trials[0])
        assert [(x.day, x.phase, x.task_param)
                for x in reloaded.resolve_trial(reloaded.trials[0])] == [
            (b[0], b[1], b[3]) for b in before]


def test_none_and_defaults_omitted_in_yaml():
    plan = _sample_plan()
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "x.yaml")
        save_plan(plan, p)
        text = open(p, encoding="utf-8").read()
    assert "null" not in text            # exclude_defaults + exclude_none
    assert "\\u" not in text             # 日本語などがエスケープされない
    assert "\n  date:" not in text       # schedule の各 day は具体日付を持たない(day のみ)
    assert "\nprogram:" in text and "\ntrials:" in text   # program / trials に分離
    assert "\nperiods:" not in text     # 保存は新キーのみ
    assert "\nmice:" not in text         # トップレベル mice は無い(trial 内のみ)
    assert text.startswith("within_factors:")  # protocol 削除で先頭が within_factors に
    assert "protocol:" not in text       # protocol はモデルから削除済み
    assert "label:" not in text and "offset:" not in text  # 統合された day のみ書き出す
    assert "phase:" in text
    assert "daily_time:" not in text     # 廃止済みフィールドは書き出さない
    assert "mouse_list:" not in text
    assert "\nschedule:" not in text


def test_offset_date_resolution():
    plan = _sample_plan()
    per = plan.trials[0]
    days = plan.resolve_trial(per)
    assert resolve_day_date(per, days[0]) == date(2026, 4, 26)   # day1 = start + 0
    assert resolve_day_date(per, days[2]) == date(2026, 4, 28)   # day3 = start + 2
    # start 未設定 -> None
    assert resolve_day_date(ExperimentTrial(name="x"), days[0]) is None


def test_format_day_code_and_default_sessions():
    assert format_day_code(1, "1", 3) == "day01-phase01S03"
    assert format_day_code(15, "5-5", 2) == "day15-phase5-5S02"
    assert format_day_code(1) == "day01" and format_day_code(-1, "") == "day-1"
    assert default_sessions(["1", "1", "2", "1"]) == [1, 2, 1, 3]
    assert default_sessions(["1", "", "4"]) == [1, None, 1]


def test_default_sessions_defers_over_skipped_days():
    """skip の日は数えないので、以降の session 割り当てが順延する。"""
    # 休み無し: 素直な累積
    assert default_sessions(["1", "1", "1"]) == [1, 2, 3]
    # 真ん中が休み -> その日は None、次の日が 2 番目になる(順延)
    assert default_sessions(["1", "1", "1"], [False, True, False]) == [1, None, 2]
    # 週末 2 日を挟む: 1,2 のあと休み休み、再開して 3,4
    phases = ["1"] * 6
    skips = [False, False, True, True, False, False]
    assert default_sessions(phases, skips) == [1, 2, None, None, 3, 4]
    # phase が変わっても休みは数えない
    assert default_sessions(["1", "2", "2"], [False, True, False]) == [1, None, 1]
    # skips を渡さなければ従来どおり
    assert default_sessions(["1", "", "4"]) == [1, None, 1]


def test_skip_days_are_not_scheduled():
    """skip の日は CC への列挙(config / mice)から外れる。体重管理は day 単位で残る。"""
    plan = _sample_plan()
    # day2 (4/27) を休みにする -> その Trial の暦に枠を持たせる
    plan.trials[0].days = [PlanDay(day=1), PlanDay(day=2, skip=True),
                           PlanDay(day=3), PlanDay(day=4)]
    with tempfile.TemporaryDirectory() as d:
        save_plan(plan, os.path.join(d, "OFL_Holmes_2026.yaml"))
        reloaded = load_plan(os.path.join(d, "OFL_Holmes_2026.yaml"))
        assert reloaded.trials[0].days[1].skip is True    # skip は保存される
        found = find_scheduled_configs(d, ref_date=date(2026, 4, 27))
        mice = find_scheduled_mice(d, ref_date=date(2026, 4, 27))
    # 4/27 は休みなので出てこない(前後の day1 / day3 のみ)
    assert [s.day_label for s in found] == ["day1", "day3"]  # day2 は休み
    assert all(s.day_label != "day2" for s in mice)
    # 休みは数えないので、day3 は phase "1" の 2 回目
    assert [s.session for s in found] == [1, 2]
    # 体重は day2 のキーとして残っている
    assert reloaded.trials[0].mice[0].bw_before["day1"] == 23.4


def test_per_trial_schedule_overrides_the_shared_one():
    """Trial 専用の days があればそれを使い、無ければ共有 days を使う。"""
    plan = _sample_plan()
    shared = plan.trials[0]
    # cohort2 は同じ program を、休みを 1 日挟んだ暦へ割り当てる
    own = ExperimentTrial(
        name="cohort2",
        period=Period(start=date(2026, 6, 1)),
        days=[PlanDay(day=1), PlanDay(day=2, skip=True), PlanDay(day=3), PlanDay(day=4)],
        mice=[PlanMouse(prj="p", mouse_id="x", bench={"day1": "B11"})],
    )
    plan.trials.append(own)
    # 共有: days 未指定なら program を 1..N へ連続割り当て
    assert [d.day for d in plan.resolve_trial(shared)] == [1, 2, 3]
    # 専用: 休みを挟むので 3 ステップが day1 / day3 / day4 に載る
    got = [(d.day, d.skip, d.phase) for d in plan.resolve_trial(own)]
    assert got == [(1, False, "1"), (2, True, ""), (3, False, "1"), (4, False, "2")]
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "x.yaml")
        save_plan(plan, p)
        loaded = load_plan(p)
        assert [x.skip for x in loaded.trials[1].days] == [False, True, False, False]
        assert loaded.trials[0].days == []               # 共有のままの trial は空
        found = find_scheduled_configs(d, ref_date=date(2026, 6, 1))
    assert [(s.period_name, s.phase) for s in found] == [("cohort2", "1")]
    assert found[0].task_param == "OAFC_shock_exposure.json"


def test_per_mouse_phase_session_override():
    """同じ日でも個体ごとに phase / session をずらせる。"""
    plan = ExperimentPlan(
        program=[ProgramStep(phase="1", session=1, task_param="a.json")],
        trials=[ExperimentTrial(name="t", period=Period(start=date(2026, 4, 26)),
                                days=[PlanDay(day=1)], mice=[
            PlanMouse(mouse_id="m1", bench={"day1": "B10"}),
            PlanMouse(mouse_id="m2", bench={"day1": "B11"},
                      phase={"day1": "2"}, session={"day1": 5})])])
    with tempfile.TemporaryDirectory() as d:
        save_plan(plan, os.path.join(d, "x.yaml"))
        found = {s.mouse_id: s for s in find_scheduled_mice(d, ref_date=date(2026, 4, 26))}
        reloaded = load_plan(os.path.join(d, "x.yaml"))
    assert (found["m1"].phase, found["m1"].session) == ("1", 1)      # step の標準
    assert (found["m2"].phase, found["m2"].session) == ("2", 5)      # 個体で上書き
    assert found["m2"].day_code == "day01-phase02S05"                # 転送用コードにも効く
    assert reloaded.trials[0].mice[1].phase == {"day1": "2"}
    assert reloaded.trials[0].mice[1].session == {"day1": 5}


def test_photometry_resolution():
    plan = _sample_plan()
    days = plan.resolve_trial(plan.trials[0])
    assert days[0].photometry_param == "20Hz_470_405nm.json"   # step の標準
    assert days[2].photometry_param == "no_stim.json"          # step の標準(別値)


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
    assert by_offset[0].rel_label == "today"
    assert by_offset[0].rel_label_ja == "today"      # old name still reads
    assert by_offset[0].task_param == "cond.json"
    assert by_offset[0].config_dir == "config_OFL_2025"
    assert by_offset[0].period_name == "cohort1"
    # session の既定は同一 phase の累積: day2 は phase "1" の 2 回目 -> S02
    assert by_offset[0].session == 2
    assert by_offset[0].day_code == "day02-phase01S02"
    assert by_offset[1].photometry_param == "no_stim.json"        # day3 標準
    assert by_offset[-1].photometry_param == "20Hz_470_405nm.json"  # day1 標準
    assert "today" in by_offset[0].display_label()
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
    plan.trials.append(ExperimentTrial(
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
    assert len(loaded.trials) == 2
    assert len(loaded.program) == 3         # program は 1 つを共有
    assert loaded.trials[1].mice[0].bench["day1"] == "B11"
    # 6/1 近傍は cohort2 のみ(cohort1 は 4 月で範囲外)
    assert {s.period_name for s in found} == {"cohort2"}
    today = [s for s in found if s.offset == 0][0]
    assert today.day_label == "day1" and today.date == date(2026, 6, 1)


def test_per_mouse_photometry_round_trip():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "x.yaml")
        save_plan(_sample_plan(), p)
        loaded = load_plan(p)
    assert loaded.trials[0].mice[0].photometry_param["day2"] == "mouse_405_override.json"
    # a mouse with no override keeps an empty dict (default omitted in YAML)
    assert loaded.trials[0].mice[1].photometry_param == {}


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
    # 個体名は記録名の mouse 欄と同じ形(m1 は sex: m)。一覧で選んだ行と、CC が
    # Execution 欄へ流し込む値の見え方を揃えるため。
    assert "[B10]" in lbl and "m1-m" in lbl
    assert "m2-f" in by_id["m2"].display_label()


def test_format_mouse_code_appends_the_sex():
    """記録名の mouse 欄 = 個体 ID + 性別。区切りは `-`。"""
    assert format_mouse_code("B0000", "m") == "B0000-m"
    assert format_mouse_code("B0000", "f") == "B0000-f"
    # 表記ゆれは寄せる。計画は m / f だが、移行を通っていない値も来うる。
    assert format_mouse_code("B0000", "male") == "B0000-m"
    assert format_mouse_code("B0000", "Female") == "B0000-f"
    assert format_mouse_code("B0000", " M ") == "B0000-m"


def test_format_mouse_code_leaves_the_id_alone_without_a_usable_sex():
    """語彙に無い値は焼き付けない。記録名に入る欄なので、知らない文字列は通さない。"""
    assert format_mouse_code("B0000") == "B0000"
    assert format_mouse_code("B0000", "") == "B0000"
    assert format_mouse_code("B0000", "unknown") == "B0000"
    assert format_mouse_code("B0000", "?") == "B0000"


def test_format_mouse_code_does_not_double_up_an_id_that_already_has_the_sex():
    """``B1768-m`` に更に足して ``B1768-m-m`` にしない(冪等)。

    この書式より前から ID 自体に性別を書いていた個体が実在する
    (behavior-config の analysis-13-1*.yaml に 22 個体、全て sex と一致)。
    """
    assert format_mouse_code("B1768-m", "m") == "B1768-m"
    assert format_mouse_code("B1768-f", "f") == "B1768-f"
    assert format_mouse_code("B1768-M", "m") == "B1768-M"      # 表記はそのまま
    # 2 回通しても変わらない
    once = format_mouse_code("B0000", "m")
    assert format_mouse_code(once, "m") == once == "B0000-m"


def test_format_mouse_code_keeps_the_id_when_it_disagrees_with_the_sex():
    """ID の接尾辞と sex が食い違うときは ID を優先する。

    人が ID に書いた方を黙って上書きせず、``B1768-f-m`` のような読めない名前も
    作らない。食い違いは計画側で直す(現データには 0 件)。
    """
    assert format_mouse_code("B1768-f", "m") == "B1768-f"


def test_format_mouse_code_only_treats_a_trailing_sex_as_a_suffix():
    """`-` で終わる ID すべてを性別扱いしない。番号や枝番は接尾辞ではない。"""
    assert format_mouse_code("ET1330-2", "m") == "ET1330-2-m"
    assert format_mouse_code("B10-01", "f") == "B10-01-f"
    assert format_mouse_code("mf", "m") == "mf-m"        # ハイフンが無ければ足す


def test_format_mouse_code_without_an_id_stays_empty():
    """ID が無ければ空文字。CC の set_fname_fields は空欄を上書きしない
    ので、Execution 欄の手入力がそのまま残る。`-m` だけの欄を作らない。"""
    assert format_mouse_code("", "m") == ""
    assert format_mouse_code("   ", "m") == ""


def test_format_mouse_code_never_breaks_the_recording_file_name():
    """`_` は記録名の区切り。`m` / `f` しか通さないので混じりようがないこと。"""
    forbidden = set('<>:"/\\|?*_')
    for sex in ("m", "f", "male", "female", "MALE", "x", ""):
        code = format_mouse_code("B0000", sex)
        assert not (set(code) & forbidden), (sex, code)


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


def test_baseline_day_is_explicit_and_ages_from_it():
    """The baseline weighing carries its own day instead of assuming day -2."""
    legacy = PlanMouse.model_validate(
        {"mouse_id": "m1", "age_day_2": 36, "actual_bw_day_2": 15.9})
    assert (legacy.baseline_day, legacy.baseline_age, legacy.baseline_bw) == (-2, 36, 15.9)
    assert not legacy.model_extra          # the old keys do not linger as extras
    # age counts from the baseline day, so day -2 is the baseline age itself
    assert [legacy.age_on(d) for d in (-2, -1, 0, 1)] == [36, 37, 38, 39]

    # a trial that starts anywhere else states its own baseline day
    late = PlanMouse(mouse_id="m2", baseline_day=11, baseline_age=50, baseline_bw=20.0)
    assert late.age_on(11) == 50 and late.age_on(14) == 53
    assert PlanMouse(mouse_id="m3").age_on(1) is None      # nothing to count from

    # an explicit baseline_day wins over the -2 the legacy names imply
    both = PlanMouse.model_validate({"mouse_id": "m4", "baseline_day": 0, "age_day_2": 40})
    assert (both.baseline_day, both.baseline_age) == (0, 40)


def test_custom_columns_are_per_file():
    """Extra per-day columns are declared by the plan, values sit on the mouse."""
    from ylabcommon.models.plan import CustomColumn

    plan = ExperimentPlan(
        custom_columns=[CustomColumn(key="injection", label="注射"),
                        CustomColumn(key="cage", type="choice", options=["A", "B"]),
                        CustomColumn(key="odd", type="something-new")],
        program=[ProgramStep(phase="1")],
        trials=[ExperimentTrial(name="t", days=[PlanDay(day=1)],
                                mice=[PlanMouse(mouse_id="m1",
                                                custom={"injection": {"day1": "saline"}})])])
    assert [c.title for c in plan.custom_columns] == ["注射", "cage", "odd"]
    assert [c.is_choice for c in plan.custom_columns] == [False, True, False]
    # an unknown type must not make the file unreadable
    assert plan.custom_columns[2].type == "text"

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "custom.yaml"
        save_plan(plan, p)
        text = p.read_text(encoding="utf-8")
        assert "injection: {day1: saline}" in text      # one line, like the others
        back = load_plan(p)
        assert [(c.key, c.type, c.options) for c in back.custom_columns] == [
            ("injection", "text", []), ("cage", "choice", ["A", "B"]), ("odd", "text", [])]
        assert back.trials[0].mice[0].custom == {"injection": {"day1": "saline"}}
        # a plan without any keeps writing nothing
        assert "custom" not in save_and_read_plain(ExperimentPlan(program=[ProgramStep()]), d)


def save_and_read_plain(plan, d) -> str:
    p = Path(d) / "plain.yaml"
    save_plan(plan, p)
    return p.read_text(encoding="utf-8")


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


# ------------------------------------------------------------------ paradigm

def test_cc_config_carries_no_paradigm():
    """計画は paradigm を持たない。記録名の 2 番目は CC の Execution 欄で入力する。

    決めるのはその回の ``task_param`` で、``config_dir`` ではない
    (``config_3CSRTT_2022`` から 3CSRTT / Before-task / 3CSRTT-notask が出る)。
    予定の各エントリにも載らないことを固定しておく — 生えると CC 側が計画の値で
    Execution 欄を上書きし、同じ日の 2 セッション目が 1 セッション目のパラダイム名で
    記録される(実績で 838 日ある並び)。
    """
    plan = _sample_plan()
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "OFL_Holmes_2026.yaml")
        save_plan(plan, p)
        text = open(p, encoding="utf-8").read()
        mice = find_scheduled_mice(d, ref_date=date(2026, 4, 26), window_days=0)
        configs = find_scheduled_configs(d, ref_date=date(2026, 4, 26), window_days=0)
    assert not hasattr(plan.cc_config, "paradigm")
    assert "paradigm" not in text
    assert mice and not any(hasattr(m, "paradigm") for m in mice)
    assert configs and not any(hasattr(c, "paradigm") for c in configs)


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
