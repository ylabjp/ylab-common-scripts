# -*- coding: utf-8 -*-
"""実験計画(スケジュール)の共有スキーマとローダ。

behavior-config/controller-plan 以下に置く YAML 形式の実験計画ファイルを
読み書きするための単一の情報源(single source of truth)。

- 予定編集 GUI (behavior-config/controller-plan/plan_editor.py) がこのモデルで
  読み書き・バリデーションを行う。
- CC controller (behavior-controller) は :func:`find_scheduled_configs` を使って
  「今日/昨日/明日」に予定された config を config 選択ダイアログに列挙する。

YAML の日付は ``2026-04-26`` のような ISO 形式で書く。PyYAML が自動で
``datetime.date`` に変換し、pydantic がそれを受け取る。
"""
from __future__ import annotations

import re
from datetime import date as DateType
from datetime import timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "PLAN_DIR_NAME",
    "PLAN_FILE_GLOB",
    "Period",
    "CCConfig",
    "PlanDay",
    "PlanMouse",
    "ExperimentPeriod",
    "ExperimentPlan",
    "ScheduledConfig",
    "load_plan",
    "save_plan",
    "iter_plan_files",
    "load_plans",
    "resolve_day_date",
    "find_scheduled_configs",
    "format_day_code",
    "default_sessions",
    "ScheduledMouse",
    "find_scheduled_mice",
]

# 旧スキーマの day ラベル ("day1" / "day-1" / "day01") から通日番号を取り出す。
_DAY_LABEL_RE = re.compile(r"^day(-?\d+)$")


def _label_number(label) -> Optional[int]:
    """``"day-1"`` -> -1, ``"day01"`` -> 1。パースできなければ None。"""
    if not isinstance(label, str):
        return None
    m = _DAY_LABEL_RE.match(label.strip())
    return int(m.group(1)) if m else None

# behavior-config 直下の予定ディレクトリ名。CC controller / GUI 双方が参照する。
# 計画ファイルは controller-expdata/<フォルダ>/ 以下(旧 controller-plan から移設)。
# iter_plan_files は rglob で再帰探索するのでサブフォルダ配下も拾う。
PLAN_DIR_NAME = "controller-expdata"
# 予定ディレクトリ内で計画ファイルとして扱う glob パターン。
PLAN_FILE_GLOB = "*.yaml"

# 相対日ラベル(offset 日 -> 日本語表記)。
_REL_LABEL_JA = {-2: "一昨日", -1: "昨日", 0: "今日", 1: "明日", 2: "明後日"}
_REL_KEY = {-1: "yesterday", 0: "today", 1: "tomorrow"}


class Period(BaseModel):
    """実験期間。"""

    start: Optional[DateType] = None
    end: Optional[DateType] = None


class CCConfig(BaseModel):
    """予定と CC controller の config を結び付ける既定値。

    - ``config_dir``: behavior-config/controller-cc 以下の config フォルダ名
      (例 ``config_OFL_2025``)。

    photometry パラメータは plan 既定を持たず、各 day で task_param と並べて
    :attr:`PlanDay.photometry_param` に個別指定する。
    """

    config_dir: str = ""


class PlanDay(BaseModel):
    """スケジュール 1 日分。CC 参照の中核。

    Schedule は Period 非依存の上位概念(:class:`ExperimentPlan` の ``days``)。
    実施日は ``day``(1 始まりの通日。day 1 = Period 開始日)で持ち、具体的な日付は
    ``Period.start + (day - 1)`` で算出する(:func:`resolve_day_date`)。baseline 計量日は
    day 0 / day -1 のように 0・負値で表す。

    - ``day``: 1 始まりの通日 (day 1 = Period 開始日)。マウスの日ごと辞書のキーは
      ``f"day{day}"``(例 ``day1`` / ``day-1``)。``offset``(= day - 1)は後方互換の
      プロパティとして提供する。
    - ``phase``: フェーズ。数字または ``5-5`` のような文字列。
    - ``session``: 同一 phase 内の session 番号。未指定なら GUI / :func:`default_sessions`
      が同一 phase の累積(出現順)を既定として補完する。
    - ``task_param``: この日の標準 task パラメータ名 (config_dir/param_files_task/ 以下)。
      個体ごとの上書きは :class:`PlanMouse` の ``task_param`` を参照。
    - ``photometry_param``: この日の標準 photometry パラメータ名 (task_param と並列)。
      個体ごとの上書きは :class:`PlanMouse` の ``photometry_param`` を参照。

    後方互換: 旧形式の ``{label, offset}`` を読み込むと ``day = offset + 1``
    (offset 省略時は 0 -> day 1)に変換する。
    """

    day: int
    phase: str = ""
    session: Optional[int] = None
    task_param: Optional[str] = None
    photometry_param: Optional[str] = None
    note: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _from_legacy(cls, data):
        if isinstance(data, dict) and "day" not in data and (
                "offset" in data or "label" in data):
            if data.get("offset") is not None:
                day = int(data["offset"]) + 1
            else:
                n = _label_number(data.get("label"))
                day = n if n is not None else 1
            data = {k: v for k, v in data.items() if k not in ("label", "offset")}
            data["day"] = day
        return data

    @property
    def offset(self) -> int:
        """Period 開始日からの日数(= day - 1)。日付解決に使う。"""
        return self.day - 1

    @property
    def label(self) -> str:
        """マウスの日ごと辞書のキー(``f"day{day}"``)。"""
        return f"day{self.day}"


class PlanMouse(BaseModel):
    """マウス 1 個体分の名簿と、日ごとの実験台(operant chamber)割当。

    ``bench`` は day ラベル -> チャンバー名 (例 ``{"day01": "B10"}``) の辞書。
    ``bw_before`` / ``bw_after`` は day ラベル -> 給水前 / 給水後の体重 g の辞書
    (例 ``{"day01": 23.4}``)。給水管理では bw_before を当日体重として用いる。
    ``water_adjust`` は day ラベル -> その日に実際に与えた水分量 ml の辞書
    (実績値。BodyWeight.Water_adjust 由来)。GUI が算出する推奨給水量とは別に保持する。
    標準体重 std_bw は保存せず日齢と settings.yaml から算出する。
    ``task_param`` は day ラベル -> その個体・その日に使う task パラメータ名の辞書。
    day の標準 (:class:`PlanDay` の ``task_param``) を上書きしたい日だけ入れる
    (標準と同じ日は入れない)。``photometry_param`` も同様に day ラベル -> その個体・
    その日に使う photometry パラメータ名の辞書で、day 標準を個体単位で上書き
    したい日だけ入れる(:func:`find_scheduled_mice` が個体別 → day の順で解決)。
    ``within_factor`` は day ラベル -> その個体・その日の within-subject 因子水準の
    辞書。取りうる値は :attr:`ExperimentPlan.within_factors`(Plan 直下の候補リスト)
    から選ぶ。標準は無く、指定した日だけ入れる。
    ``user`` は day ラベル -> その個体・その日の実験実施者(例 ``{"day01": "Etani"}``)
    の辞書。候補は settings.yaml の ``users`` リスト。指定した日だけ入れる。
    その他の当日測定値は ``extra`` に自由に保持できる(後方互換のため許容)。

    個体の基礎情報:
    - ``ear_tag``: 耳パンチ識別 (R1/L1/... の組み合わせ)。候補は settings.yaml。
    - ``mating_id``: 交配 ID (文字列)。
    - ``birth_date`` / ``termination``: 生年月日 / 終了日 (共に ``YYMMDD`` 文字列)。
      日齢は保存せず、GUI 側で termination(無ければ当日) - birth_date として算出する。
    - ``fail``: 実験失敗フラグ。
    - ``age_day_2`` / ``actual_bw_day_2``: day-2 時点の日齢 / 実測体重 (g)。
    """

    model_config = ConfigDict(extra="allow")

    prj: Optional[str] = None
    cond: Optional[str] = None
    ear_tag: Optional[str] = None
    sex: Optional[str] = None
    mouse_id: Optional[str] = None
    mating_id: Optional[str] = None
    birth_date: Optional[str] = None       # YYMMDD
    termination: Optional[str] = None      # YYMMDD
    fail: bool = False
    age_day_2: Optional[int] = None
    actual_bw_day_2: Optional[float] = None
    bench: Dict[str, str] = Field(default_factory=dict)
    bw_before: Dict[str, float] = Field(default_factory=dict)
    bw_after: Dict[str, float] = Field(default_factory=dict)
    water_adjust: Dict[str, float] = Field(default_factory=dict)
    task_param: Dict[str, str] = Field(default_factory=dict)
    photometry_param: Dict[str, str] = Field(default_factory=dict)
    within_factor: Dict[str, str] = Field(default_factory=dict)
    user: Dict[str, str] = Field(default_factory=dict)
    note: Optional[str] = None


class ExperimentPeriod(BaseModel):
    """1 つの実験期間(Period)。1 ファイルに複数持てる。

    ``period.start`` と ``mice``(名簿)を持つ。Schedule(日程)は Plan 直下の
    :attr:`ExperimentPlan.days` に 1 つ置いて全 Period で共有し、具体日付は
    start + offset で決める。
    """

    name: str = ""
    period: Optional[Period] = None
    mice: List[PlanMouse] = Field(default_factory=list)


class ExperimentPlan(BaseModel):
    """1 プロトコル分の実験計画。controller-expdata 以下の 1 YAML に対応。

    protocol はファイル名(と置き場フォルダ)で表す方針のため、モデルには持たない。
    旧ファイルの ``protocol:`` など未知のトップレベルキーは ``extra="ignore"`` で
    読み飛ばす(移行後の再保存で消える)。

    ``days`` は全 Period 共通の Schedule(各日は 1 始まりの ``day`` を持つ)。``periods``
    は それぞれ ``start`` と名簿を持ち、具体日付は ``start + (day - 1)`` で決まる。
    ``within_factors`` は within-subject 因子の候補リスト。Per-day で各個体・各日の
    :attr:`PlanMouse.within_factor` を選ぶときの選択肢になる。

    給水(絶水)管理:
    - ``water_restriction_ratio``: 目標体重の割合 (例 0.85 = 予測自由摂取体重の 85%)。
    - ``daily_evaporation_ml``: 1 日あたりの水分蒸発量 (ml)。給水量の算出に加味する。
    予測自由摂取体重は settings.yaml の標準体重に対し、day-2 の実測体重
    (:attr:`PlanMouse.actual_bw_day_2` / :attr:`PlanMouse.age_day_2`) の比を掛けて
    求める(算出は GUI 側。標準体重データが behavior-config にあるため)。
    """

    model_config = ConfigDict(extra="ignore")

    within_factors: List[str] = Field(default_factory=list)
    water_restriction_ratio: Optional[float] = None
    daily_evaporation_ml: Optional[float] = None
    cc_config: CCConfig = Field(default_factory=CCConfig)
    days: List[PlanDay] = Field(default_factory=list)
    periods: List[ExperimentPeriod] = Field(default_factory=list)

    @property
    def day_labels(self) -> List[str]:
        return [d.label for d in self.days]

    def resolve_photometry_param(self, day: PlanDay) -> Optional[str]:
        """その day の photometry パラメータ (plan 既定は廃止)。"""
        return day.photometry_param


class ScheduledConfig(BaseModel):
    """特定の基準日から見た「予定された config」1 件。

    CC controller の config 選択ダイアログに 1 行として並ぶ。
    """

    offset: int  # 基準日からの日数 (-1=昨日, 0=今日, +1=明日)
    rel_key: str  # "yesterday" / "today" / "tomorrow" / "+N" など
    rel_label_ja: str  # 昨日 / 今日 / 明日 など
    date: DateType
    day_label: str = ""  # マウス辞書キーと同じ ``dayN``
    day_code: str = ""   # day + phase + session を符号化 (例 ``day01-phase01S03``)。CC 転送用
    phase: str = ""
    session: Optional[int] = None
    plan_name: str = ""  # 由来した YAML ファイル名 (拡張子なし)。protocol はここ(=ファイル名)で表す
    period_name: str = ""  # 由来した Period 名 (複数 Period のとき)
    config_dir: str = ""
    task_param: Optional[str] = None
    photometry_param: Optional[str] = None

    def display_label(self) -> str:
        """選択ダイアログ 1 行分の日本語表示文字列。"""
        task = self.task_param or "(task未指定)"
        code = (self.day_code + " ") if self.day_code else ""
        origin = self.plan_name + (f":{self.period_name}" if self.period_name else "")
        return (
            f"【{self.rel_label_ja} {self.date.isoformat()}】 "
            f"{code}{self.config_dir} / {task}  «{origin}»"
        )


class ScheduledMouse(BaseModel):
    """特定基準日に予定された「個体 × 実験台(slot)」1 件。

    CC controller / video recorder が「今日のマウス / Slot を選ぶ」ための情報。
    config(``config_dir`` / ``task_param`` / ``photometry_param``)に加えて、個体メタ
    (``prj`` / ``cond`` / ``mouse_id`` / ``within_factor`` / ``slot`` 等)を持つ。
    ``task_param`` / ``photometry_param`` は **個体別上書き → day**の順で
    解決済みの実効値。
    """

    offset: int
    rel_key: str
    rel_label_ja: str
    date: DateType
    day_label: str = ""
    day_code: str = ""        # day + phase + session を符号化 (例 ``day01-phase01S03``)。CC 転送用
    phase: str = ""
    session: Optional[int] = None
    plan_name: str = ""       # 由来した YAML ファイル名 (拡張子なし)。protocol はここ(=ファイル名)で表す
    period_name: str = ""
    config_dir: str = ""
    task_param: Optional[str] = None
    photometry_param: Optional[str] = None
    # 個体 (mouse) メタ
    slot: str = ""            # その day の実験台 (experimental_slot / bench)
    mouse_id: str = ""
    prj: str = ""
    cond: str = ""
    sex: str = ""
    ear_tag: str = ""
    within_factor: str = ""   # その day の水準

    def display_label(self) -> str:
        """選択リスト 1 行分の日本語表示文字列。"""
        who = self.mouse_id or "(no id)"
        slot = f"[{self.slot}] " if self.slot else ""
        cond = f"/{self.cond}" if self.cond else ""
        task = self.task_param or "(task未指定)"
        return (
            f"【{self.rel_label_ja} {self.date.isoformat()}】 {slot}{who} "
            f"{self.prj}{cond} — {self.config_dir} / {task}"
        )


def load_plan(path: Union[str, Path]) -> ExperimentPlan:
    """YAML の実験計画を読み込み :class:`ExperimentPlan` を返す。"""
    path = Path(path)
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return ExperimentPlan.model_validate(data)


def save_plan(plan: ExperimentPlan, path: Union[str, Path]) -> None:
    """実験計画を YAML として書き出す(既定値/None 項目は省略して読みやすく)。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # exclude_defaults: 空の periods / days / mice や未設定項目を書かず簡潔に保つ。
    data = plan.model_dump(mode="python", exclude_defaults=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            data,
            f,
            allow_unicode=True,  # 日本語をそのまま出力
            sort_keys=False,     # モデル定義順を維持して可読性を保つ
            default_flow_style=False,
        )


def iter_plan_files(plan_dir: Union[str, Path]) -> List[Path]:
    """予定ディレクトリ内の計画 YAML を(名前順に)列挙する。存在しなければ空。"""
    plan_dir = Path(plan_dir)
    if not plan_dir.is_dir():
        return []
    # サブフォルダ(plans/ など)も許容しつつ、隠し/テンポラリを避ける。
    files = [
        p
        for p in sorted(plan_dir.rglob(PLAN_FILE_GLOB))
        if p.is_file() and not p.name.startswith((".", "_"))
    ]
    return files


def load_plans(plan_dir: Union[str, Path]) -> List[Tuple[Path, ExperimentPlan]]:
    """予定ディレクトリ内の全計画を読み込む。壊れたファイルは skip(警告出力)。"""
    result: List[Tuple[Path, ExperimentPlan]] = []
    for p in iter_plan_files(plan_dir):
        try:
            result.append((p, load_plan(p)))
        except Exception as e:  # noqa: BLE001 - 1 ファイルの破損で全体を止めない
            print(f"[ylabcommon.plan] 計画ファイルの読み込みに失敗: {p}: {e}")
    return result


def _rel_labels(offset: int) -> Tuple[str, str]:
    """offset -> (rel_key, rel_label_ja)。"""
    key = _REL_KEY.get(offset, f"{offset:+d}")
    label = _REL_LABEL_JA.get(offset, f"{offset:+d}日")
    return key, label


def _phase_code(phase: str) -> str:
    """数値 phase は 2 桁ゼロ詰め("1"->"01")、それ以外("5-5")はそのまま。"""
    p = (phase or "").strip()
    return f"{int(p):02d}" if p.isdigit() else p


def format_day_code(day: int, phase: str = "", session: Optional[int] = None) -> str:
    """day + phase + session を 1 文字列に符号化する。

    例: day=1, phase="1", session=3 -> ``day01-phase01S03``。phase 無しなら ``day01``。
    GUI の per-day / by-mouse 行見出しと、CC controller への転送(:class:`ScheduledConfig`
    の ``day_code``)で共通に使う。
    """
    code = f"day{day:02d}"
    if (phase or "").strip():
        s = f"S{session:02d}" if session is not None else ""
        code += f"-phase{_phase_code(phase)}{s}"
    return code


def default_sessions(phases: List[str]) -> List[Optional[int]]:
    """phase 列 -> 各日の既定 session(同一 phase の出現順の累積)。

    phase が空の日は None。例: ``["1","1","2","1"]`` -> ``[1, 2, 1, 3]``。
    """
    out: List[Optional[int]] = []
    counts: Dict[str, int] = {}
    for ph in phases:
        p = (ph or "").strip()
        if not p:
            out.append(None)
            continue
        counts[p] = counts.get(p, 0) + 1
        out.append(counts[p])
    return out


def resolve_day_date(
    period: "ExperimentPeriod", day: PlanDay
) -> Optional[DateType]:
    """具体日付 = Period.start + day.offset。start 未設定なら None。"""
    start = period.period.start if period.period else None
    if start is None:
        return None
    return start + timedelta(days=day.offset)


def find_scheduled_configs(
    plan_dir: Union[str, Path],
    ref_date: Optional[DateType] = None,
    window_days: int = 1,
) -> List[ScheduledConfig]:
    """基準日の前後 ``window_days`` 日に予定された config を列挙する。

    controller-plan 内の全 YAML を走査し、``ref_date`` を中心に
    ``[-window_days, +window_days]`` の範囲に日付が入る day を集める。
    既定 (``window_days=1``) では 昨日 / 今日 / 明日。

    戻り値は offset(昇順) -> plan_name -> period_name -> day_label の順にソートされる。
    各行の ``day_code`` に day + phase + session を符号化して CC に渡す。
    """
    if ref_date is None:
        ref_date = DateType.today()

    found: List[ScheduledConfig] = []
    for path, plan in load_plans(plan_dir):
        plan_name = path.stem
        cc = plan.cc_config
        sess_def = default_sessions([d.phase for d in plan.days])
        for period in plan.periods:
            for i, day in enumerate(plan.days):
                d = resolve_day_date(period, day)
                if d is None:
                    continue
                offset = (d - ref_date).days
                if abs(offset) > window_days:
                    continue
                rel_key, rel_label_ja = _rel_labels(offset)
                session = day.session if day.session is not None else sess_def[i]
                found.append(
                    ScheduledConfig(
                        offset=offset,
                        rel_key=rel_key,
                        rel_label_ja=rel_label_ja,
                        date=d,
                        day_label=day.label,
                        day_code=format_day_code(day.day, day.phase, session),
                        phase=day.phase,
                        session=session,
                        plan_name=plan_name,
                        period_name=period.name,
                        config_dir=cc.config_dir,
                        task_param=day.task_param,
                        photometry_param=day.photometry_param,
                    )
                )

    found.sort(key=lambda s: (s.offset, s.plan_name, s.period_name, s.day_label))
    return found


def find_scheduled_mice(
    plan_dir: Union[str, Path],
    ref_date: Optional[DateType] = None,
    window_days: int = 1,
) -> List[ScheduledMouse]:
    """基準日の前後 ``window_days`` 日に予定された「個体 × 実験台」を列挙する。

    :func:`find_scheduled_configs` の個体版。CC controller / video recorder が
    「今日のマウス / Slot を選ぶ」ために使う。1 個体 × 1 予定日 = 1 :class:`ScheduledMouse`。
    ``task_param`` / ``photometry_param`` は個体別上書き → day の順で解決する。

    ``window_days=0`` なら当日のみ。戻り値は offset(昇順)→ slot → prj → mouse_id 順。
    """
    if ref_date is None:
        ref_date = DateType.today()

    found: List[ScheduledMouse] = []
    for path, plan in load_plans(plan_dir):
        plan_name = path.stem
        cc = plan.cc_config
        sess_def = default_sessions([d.phase for d in plan.days])
        for period in plan.periods:
            for i, day in enumerate(plan.days):
                d = resolve_day_date(period, day)
                if d is None:
                    continue
                offset = (d - ref_date).days
                if abs(offset) > window_days:
                    continue
                rel_key, rel_label_ja = _rel_labels(offset)
                label = day.label
                session = day.session if day.session is not None else sess_def[i]
                day_code = format_day_code(day.day, day.phase, session)
                for m in period.mice:
                    task = (m.task_param.get(label) if label else None) or day.task_param
                    photo = (
                        (m.photometry_param.get(label) if label else None)
                        or day.photometry_param
                    )
                    found.append(
                        ScheduledMouse(
                            offset=offset,
                            rel_key=rel_key,
                            rel_label_ja=rel_label_ja,
                            date=d,
                            day_label=label,
                            day_code=day_code,
                            phase=day.phase,
                            session=session,
                            plan_name=plan_name,
                            period_name=period.name,
                            config_dir=cc.config_dir,
                            task_param=task,
                            photometry_param=photo,
                            slot=(m.bench.get(label, "") if label else ""),
                            mouse_id=m.mouse_id or "",
                            prj=m.prj or "",
                            cond=m.cond or "",
                            sex=m.sex or "",
                            ear_tag=m.ear_tag or "",
                            within_factor=(m.within_factor.get(label, "") if label else ""),
                        )
                    )

    found.sort(key=lambda s: (s.offset, s.slot, s.prj, s.mouse_id))
    return found
