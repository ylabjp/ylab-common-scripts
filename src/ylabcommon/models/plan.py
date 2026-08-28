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
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

# YAML の読み書きは libyaml (C 実装) があればそれを使う。計画ファイル約100件
# (5.4MB) の一括読み込みで実測 約5倍 (20.7s -> 4.2s)。Occupancy の全件スキャンや
# find_scheduled_* が効く。純 Python 版と結果は完全に一致する(パース結果・出力
# ともにバイト単位で同一)ので、無い環境では黙って従来どおりにフォールバックする。
try:
    from yaml import CSafeLoader as _YamlLoader, CSafeDumper as _YamlDumper
    USING_LIBYAML = True
except ImportError:  # pragma: no cover - libyaml 無しの環境
    from yaml import SafeLoader as _YamlLoader, SafeDumper as _YamlDumper
    USING_LIBYAML = False

# マウスの日ごと辞書。保存時はこれらだけ 1 行のフロー形式で書き、縦に伸びるのを防ぐ
# (意味もキー集合も変えない。``bench: {day1: B10, day2: B10}`` のように出る)。
PERDAY_DICT_KEYS = ("bench", "bw_before", "bw_after", "water_adjust", "phase", "session",
                    "task_param", "photometry_param", "within_factor", "user")


class _FlowMap(dict):
    """1 行(フロー形式)で書き出す辞書。"""


def _represent_flow_map(dumper, data):
    return dumper.represent_mapping("tag:yaml.org,2002:map", data, flow_style=True)


_YamlDumper.add_representer(_FlowMap, _represent_flow_map)


__all__ = [
    "PLAN_DIR_NAME",
    "PLAN_FILE_GLOB",
    "PERDAY_DICT_KEYS",
    "day_label_number",
    "USING_LIBYAML",
    "Period",
    "CCConfig",
    "PlanDay",
    "ProgramStep",
    "ResolvedDay",
    "PlanMouse",
    "CustomColumn",
    "RESERVED_CUSTOM_KEYS",
    "ExperimentTrial",
    "ExperimentPeriod",
    "ExperimentPlan",
    "ScheduledConfig",
    "DuplicateKeyError",
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


def day_label_number(label) -> Optional[int]:
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

# 相対日ラベル(offset 日 -> 表示文字列)。表示は英語に統一している。
_REL_LABEL = {-2: "2 days ago", -1: "yesterday", 0: "today",
              1: "tomorrow", 2: "in 2 days"}
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


class ProgramStep(BaseModel):
    """実験プログラムの 1 ステップ。Plan 直下 (:attr:`ExperimentPlan.program`)。

    プログラムは「何を・どの順で」だけを持ち、**日付も day 番号も持たない**。
    配列の index が実施順そのもの。実時間への割り当ては Trial が行う
    (:meth:`ExperimentPlan.resolve_trial`)。
    """

    phase: str = ""
    session: Optional[int] = None
    task_param: Optional[str] = None
    photometry_param: Optional[str] = None
    note: Optional[str] = None


class PlanDay(BaseModel):
    """Trial の 1 日 = プログラムを実時間へ割り当てる枠 (:attr:`ExperimentTrial.days`)。

    Trial は「モデルの実時間への適応」なので ``day`` が必須。具体的な日付は
    ``Period.start + (day - 1)`` (:func:`resolve_day_date`)。

    - ``day``: 通日 (day 1 = Trial 開始日)。マウスの日ごと辞書のキーは ``f"day{day}"``。
    - ``skip``: プログラムのステップを割り当てない日。ステップを消費しないので以降が
      順延する。baseline(計量のみの前日程。day <= 0)も会期中の休みも同じ扱いで、
      違うのは day 番号だけ。体重管理は day 単位なので、skip の日も
      :class:`PlanMouse` の ``bw_before`` などのキーとしては残る。
    - ``step``: **その日に実施した内容を確定(凍結)**したもの。入っている日は Plan の
      ``program`` を参照せず(消費もせず)この値を使う。実施期間を終えた Trial は
      :meth:`ExperimentPlan.freeze_trial` で凍結し、**あとから Plan を書き換えても
      過去の記録が変わらない**ようにする。
    """

    day: int
    skip: bool = False
    step: Optional["ProgramStep"] = None    # 確定した実施内容(凍結)
    note: Optional[str] = None

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _from_legacy(cls, data):
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if "day" not in data and ("offset" in data or "label" in data):
            if data.get("offset") is not None:
                data["day"] = int(data["offset"]) + 1
            else:
                n = day_label_number(data.get("label"))
                data["day"] = n if n is not None else 1
        data.pop("label", None)
        data.pop("offset", None)
        return data

    @property
    def consumes_step(self) -> bool:
        """プログラムのステップを 1 つ消費する日か(``skip`` の否定)。"""
        return not self.skip

    @property
    def offset(self) -> int:
        return self.day - 1

    @property
    def label(self) -> str:
        return f"day{self.day}"


class ResolvedDay(BaseModel):
    """Trial の 1 日に、割り当てられたステップを重ねたもの。

    既存の利用側(GUI / CC / 週シート)は「day と phase/task を併せ持つ 1 日」を
    前提にしているので、:meth:`ExperimentPlan.resolve_trial` はこの形で返す。
    """

    day: int
    skip: bool = False
    phase: str = ""
    session: Optional[int] = None
    task_param: Optional[str] = None
    photometry_param: Optional[str] = None
    note: Optional[str] = None

    @property
    def offset(self) -> int:
        return self.day - 1

    @property
    def label(self) -> str:
        return f"day{self.day}"


#: 旧: day -2 固定を前提にした項目名 -> 基準日つきの項目名。
_LEGACY_BASELINE = {"age_day_2": "baseline_age", "actual_bw_day_2": "baseline_bw"}

#: :attr:`CustomColumn.type` に取れる値。
CUSTOM_TEXT = "text"
CUSTOM_CHOICE = "choice"

#: 追加列のキーに使えない名前。日ごとの値の名前とぶつかると「どちらの列か」が
#: 決まらなくなるので、名前空間で逃げずに**衝突させて弾く**(GUI が入力時に検査)。
#: ``day`` / ``date`` / ``skip`` は day そのものの語なので併せて予約する。
RESERVED_CUSTOM_KEYS = frozenset(PERDAY_DICT_KEYS) | {"day", "date", "skip", "note"}


class CustomColumn(BaseModel):
    """この Plan だけに足す、日ごとの自由記入欄 (:attr:`ExperimentPlan.custom_columns`)。

    プロトコルによって記録したい項目は違うので、モデルに項目を増やすのではなく
    **ファイル単位で列を宣言**する。値は個体の :attr:`PlanMouse.custom` に
    ``{列キー: {day ラベル: 値}}`` で入り、他の日ごとの値と同じ扱いになる。

    - ``key``: YAML に書かれる列キー。個体側の辞書のキーでもある。
    - ``label``: 画面の見出し。省略時は ``key``。
    - ``type``: ``text``(1 行フリーテキスト)または ``choice``(ドロップダウン)。
    - ``options``: ``choice`` のときの選択肢。**この Plan の中だけで有効**。
      空欄も選べるよう、GUI は先頭に空を足す。自由入力も許すので、
      候補に無い値が既に入っていても失われない。
    """

    key: str
    label: str = ""
    type: str = CUSTOM_TEXT
    options: List[str] = Field(default_factory=list)

    @field_validator("type", mode="before")
    @classmethod
    def _known_type(cls, v):
        """未知の型は text 扱い(将来の型を書いたファイルでも読めなくならない)。"""
        s = str(v or "").strip().lower()
        return s if s in (CUSTOM_TEXT, CUSTOM_CHOICE) else CUSTOM_TEXT

    @property
    def title(self) -> str:
        """画面に出す名前。"""
        return self.label or self.key

    @property
    def is_choice(self) -> bool:
        return self.type == CUSTOM_CHOICE


class PlanMouse(BaseModel):
    """マウス 1 個体分の名簿と、日ごとの実験台(operant chamber)割当。

    ``bench`` は day ラベル -> チャンバー名 (例 ``{"day01": "B10"}``) の辞書。
    ``bw_before`` / ``bw_after`` は day ラベル -> 給水前 / 給水後の体重 g の辞書
    (例 ``{"day01": 23.4}``)。給水管理では bw_before を当日体重として用いる。
    ``water_adjust`` は day ラベル -> その日に実際に与えた水分量 ml の辞書
    (実績値。BodyWeight.Water_adjust 由来)。GUI が算出する推奨給水量とは別に保持する。
    標準体重 std_bw は保存せず日齢と settings.yaml から算出する。
    ``phase`` / ``session`` は day ラベル -> その個体・その日の phase / session の辞書。
    同じ日でも個体によって進度がずれることがあるので、その日の標準(ステップ)を
    個体単位で上書きしたい日だけ入れる。
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
    - ``birth_date`` / ``termination``: 生年月日 / 終了日。``period.start`` と同じ
      ISO 日付 (``2025-04-26``) で持つ。旧形式の ``YYMMDD`` 文字列 (``'250426'``) も
      読み込め、``20YY`` として解釈する。日齢は保存せず、GUI 側で
      termination(無ければ当日) - birth_date として算出する。
    - ``fail``: 実験失敗フラグ。
    - ``baseline_day`` / ``baseline_age`` / ``baseline_bw``: 給水制限を始める前の
      **基準計量**。「どの day に測ったか」(``baseline_day``)と、その日の日齢
      (``baseline_age``)・実測体重 g (``baseline_bw``)。予測体重はこの 3 つを
      起点にする(:meth:`age_on`)。旧形式の ``age_day_2`` / ``actual_bw_day_2``
      は day -2 固定の書き方で、読み込み時に ``baseline_day: -2`` を補って変換する。
      基準日が固定でなくなったので、Trial の day 軸は -2 始まりに縛られない。
    """

    model_config = ConfigDict(extra="allow")

    prj: Optional[str] = None
    cond: Optional[str] = None
    ear_tag: Optional[str] = None
    sex: Optional[str] = None
    mouse_id: Optional[str] = None
    mating_id: Optional[str] = None
    birth_date: Optional[DateType] = None      # ISO 日付 (period.start と同じ形式)
    termination: Optional[DateType] = None     # ISO 日付。実験継続中なら未設定
    fail: bool = False
    baseline_day: Optional[int] = None         # 基準計量を行った day
    baseline_age: Optional[int] = None         # その day の日齢
    baseline_bw: Optional[float] = None        # その day の実測体重 (g)

    @model_validator(mode="before")
    @classmethod
    def _baseline_from_legacy(cls, data):
        """``age_day_2`` / ``actual_bw_day_2`` を基準日つきの形へ移す。

        旧項目名は「day -2 で測ったもの」という前提を名前に埋め込んでいた。
        基準日を ``baseline_day`` として外に出し、旧形式は -2 として読む
        (実データ 934 個体中 931 個体で ``age_day_2`` が day -2 の日齢と一致)。
        """
        if not isinstance(data, dict):
            return data
        if not any(k in data for k in _LEGACY_BASELINE):
            return data
        data = dict(data)
        for old, new in _LEGACY_BASELINE.items():
            v = data.pop(old, None)
            if v is not None and data.get(new) is None:
                data[new] = v
        if data.get("baseline_day") is None:
            data["baseline_day"] = -2
        return data

    def age_on(self, day: int) -> Optional[int]:
        """その day の日齢。基準計量からの差分で出す。"""
        if self.baseline_age is None or self.baseline_day is None:
            return None
        return self.baseline_age + (day - self.baseline_day)

    @field_validator("birth_date", "termination", mode="before")
    @classmethod
    def _yymmdd_to_date(cls, v):
        """旧形式の ``YYMMDD`` 文字列を ISO 日付に変換して読む(YY -> 20YY)。"""
        if isinstance(v, str):
            t = v.strip()
            if not t:
                return None
            if len(t) == 6 and t.isdigit():
                return DateType(2000 + int(t[0:2]), int(t[2:4]), int(t[4:6]))
        return v

    bench: Dict[str, str] = Field(default_factory=dict)
    bw_before: Dict[str, float] = Field(default_factory=dict)
    bw_after: Dict[str, float] = Field(default_factory=dict)
    water_adjust: Dict[str, float] = Field(default_factory=dict)
    phase: Dict[str, str] = Field(default_factory=dict)
    session: Dict[str, int] = Field(default_factory=dict)
    task_param: Dict[str, str] = Field(default_factory=dict)
    photometry_param: Dict[str, str] = Field(default_factory=dict)
    within_factor: Dict[str, str] = Field(default_factory=dict)
    user: Dict[str, str] = Field(default_factory=dict)
    #: Plan が宣言した追加列の値。``{列キー: {day ラベル: 値}}``。
    custom: Dict[str, Dict[str, str]] = Field(default_factory=dict)
    note: Optional[str] = None


class ExperimentTrial(BaseModel):
    """1 つの実験 Trial(旧称 Period)。1 ファイルに複数持てる。

    ``period.start``(実施期間)と ``mice``(名簿)を持ち、具体日付は start + offset。
    ``period`` は開始/終了日の範囲そのものなので名称を保つ。

    Trial は Plan の ``program`` を実時間へ展開したものそのものなので、日程
    (``days``)は必ず自分で持つ。開始曜日が違えば休み(:attr:`PlanDay.skip`)の
    位置も変わるため、共有する日程というものは無い。空のときだけ
    「開始日から休み無しで program を順に割り当てる」とみなす。
    """

    name: str = ""
    period: Optional[Period] = None
    # 実時間への適応。空なら「開始日から連続で program を割り当て(休み無し)」。
    days: List[PlanDay] = Field(default_factory=list)
    mice: List[PlanMouse] = Field(default_factory=list)


#: 旧名。``periods:`` 時代のコードを壊さないための別名。
ExperimentPeriod = ExperimentTrial


class ExperimentPlan(BaseModel):
    """1 プロトコル分の実験計画。controller-expdata 以下の 1 YAML に対応。

    protocol はファイル名(と置き場フォルダ)で表す方針のため、モデルには持たない。
    旧ファイルの ``protocol:`` など未知のトップレベルキーは ``extra="ignore"`` で
    読み飛ばす(移行後の再保存で消える)。

    ``days`` は全 Trial 共通の Schedule(各日は 1 始まりの ``day`` を持つ)。``trials``
    は それぞれ ``start`` と名簿を持ち、具体日付は ``start + (day - 1)`` で決まる。
    旧キー ``periods:`` も読み込めるが(:attr:`trials` の別名)、保存時は ``trials:``
    で書き出す。旧コード向けに ``plan.periods`` プロパティも残してある。
    ``within_factors`` は within-subject 因子の候補リスト。Per-day で各個体・各日の
    :attr:`PlanMouse.within_factor` を選ぶときの選択肢になる。

    給水(絶水)管理:
    - ``bodyweight_management``: この計画で体重管理を行うか。**既定は False**。
      週シート(体重表)に出るのは True の計画だけで、取り込みも True の計画にしか
      書き戻さない。1 個体は複数の計画に載りうる(課題本体と Before-task、
      並行する別プロトコル)が、**体重を管理するのはそのうち 1 つだけ**。ここで
      絞らないと同じ個体が週シートに何列も出て、どの列に打った値が計画のどれに
      入るのかが決まらなくなる。
    - ``water_restriction_ratio``: 目標体重の割合 (例 0.85 = 予測自由摂取体重の 85%)。
    - ``daily_evaporation_ml``: 1 日あたりの水分蒸発量 (ml)。給水量の算出に加味する。
    予測自由摂取体重は settings.yaml の標準体重に対し、基準計量
    (:attr:`PlanMouse.baseline_bw` / :attr:`PlanMouse.baseline_age`) の比を掛けて
    求める(算出は GUI 側。標準体重データが behavior-config にあるため)。
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    within_factors: List[str] = Field(default_factory=list)
    custom_columns: List["CustomColumn"] = Field(default_factory=list)
    bodyweight_management: bool = False
    water_restriction_ratio: Optional[float] = None
    daily_evaporation_ml: Optional[float] = None
    cc_config: CCConfig = Field(default_factory=CCConfig)
    program: List[ProgramStep] = Field(default_factory=list)
    # 読み込みは trials: / periods: の両方を受ける(旧ファイル互換)。書き出しは trials:。
    trials: List[ExperimentTrial] = Field(
        default_factory=list, validation_alias=AliasChoices("trials", "periods"))

    @property
    def periods(self) -> List[ExperimentTrial]:
        """旧名。``trials`` と同じリストを返す(``plan.periods[0]`` 等の既存コード用)。"""
        return self.trials

    @model_validator(mode="before")
    @classmethod
    def _split_legacy_days(cls, data):
        """旧形式(plan 直下の ``days`` に day と phase が同居)を分解して読む。

        ``days`` の各要素から、ステップを持つ日を :attr:`program` の並びへ、日付の
        枠(day / baseline / skip)を各 Trial の ``days`` へ移す。全 Trial が同じ
        ``days`` を共有していたので、自前の ``days`` を持たない Trial にそれを配る。
        """
        if not isinstance(data, dict) or "program" in data or "days" not in data:
            return data
        data = dict(data)
        legacy = [d for d in (data.pop("days", None) or []) if isinstance(d, dict)]
        program, frames = [], []
        for raw in legacy:
            day = PlanDay.model_validate(raw)
            # Plan level has no skip flag: a step simply exists when the day was
            # given an assignment. "skip" in the old files is exactly "no task
            # assigned", so absence of an assignment is what makes a day a frame.
            has_step = bool(raw.get("task_param") or (raw.get("phase") or "").strip())
            if not has_step:
                # 割り当ての無い日 = ステップを消費しない日。baseline(day <= 0 の
                # 計量日)も会期中の休みも同じ扱いで、違うのは day 番号だけ。
                day.skip = True
            else:
                day.skip = False
                program.append(ProgramStep(
                    phase=raw.get("phase") or "", session=raw.get("session"),
                    task_param=raw.get("task_param"), photometry_param=raw.get("photometry_param"),
                    note=raw.get("note")))
            frames.append({"day": day.day, "skip": day.skip})
        data["program"] = program
        key = "trials" if "trials" in data else ("periods" if "periods" in data else None)
        if key:
            data[key] = [t if (not isinstance(t, dict) or t.get("days")) else {**t, "days": frames}
                         for t in (data.get(key) or [])]
        return data

    def resolve_trial(self, trial: "ExperimentTrial") -> List[ResolvedDay]:
        """Trial の各日に program のステップを順に割り当てた結果を返す。

        ``program`` の日だけがステップを 1 つ消費するので、baseline / skip を挟むと
        以降のステップは自動的に順延する。``trial.days`` が空なら、開始日から
        program を連続で割り当てる(休み無し)。
        """
        days = list(trial.days) or [PlanDay(day=i + 1) for i in range(len(self.program))]
        # session の既定は「同一 phase の累積」。消費しない日は数えない。
        steps = iter(self.program)
        out: List[ResolvedDay] = []
        counts: Dict[str, int] = {}
        for d in days:
            if not d.consumes_step:
                out.append(ResolvedDay(day=d.day, skip=True, note=d.note))
                continue
            if d.step is not None:
                # 凍結済み: program は参照も消費もしない(過去の記録は不変)
                out.append(ResolvedDay(
                    day=d.day, phase=d.step.phase, session=d.step.session,
                    task_param=d.step.task_param,
                    photometry_param=d.step.photometry_param,
                    note=d.note or d.step.note))
                continue
            st = next(steps, None)
            if st is None:
                out.append(ResolvedDay(day=d.day, skip=True, note=d.note))
                continue
            sess = st.session
            if sess is None and (st.phase or "").strip():
                counts[st.phase] = counts.get(st.phase, 0) + 1
                sess = counts[st.phase]
            elif (st.phase or "").strip():
                counts[st.phase] = counts.get(st.phase, 0) + 1
            out.append(ResolvedDay(
                day=d.day, phase=st.phase, session=sess,
                task_param=st.task_param, photometry_param=st.photometry_param,
                note=d.note or st.note))
        return out

    def freeze_trial(self, trial: "ExperimentTrial") -> int:
        """Trial の実施内容を確定させ、以後 Plan の変更を受けないようにする。

        現在 :meth:`resolve_trial` が返す内容を各日の :attr:`PlanDay.step` に焼き付ける。
        Plan の ``program`` は今後も編集され得るが、凍結した Trial の記録は動かない。
        戻り値は凍結した日数。既に凍結済みの日はそのまま。
        """
        n = 0
        for day, r in zip(trial.days, self.resolve_trial(trial)):
            if day.skip or day.step is not None:
                continue
            if not (r.phase or r.task_param or r.photometry_param or r.session):
                continue
            day.step = ProgramStep(
                phase=r.phase, session=r.session, task_param=r.task_param,
                photometry_param=r.photometry_param)
            n += 1
        return n

    def is_frozen(self, trial: "ExperimentTrial") -> bool:
        """実施日のすべてが凍結済みか。"""
        work = [d for d in trial.days if not d.skip]
        return bool(work) and all(d.step is not None for d in work)

    def days_for(self, trial: "ExperimentTrial") -> List[ResolvedDay]:
        """後方互換の別名。:meth:`resolve_trial` と同じ。"""
        return self.resolve_trial(trial)

    def resolve_photometry_param(self, day: PlanDay) -> Optional[str]:
        """その day の photometry パラメータ (plan 既定は廃止)。"""
        return day.photometry_param


class ScheduledConfig(BaseModel):
    """特定の基準日から見た「予定された config」1 件。

    CC controller の config 選択ダイアログに 1 行として並ぶ。
    """

    offset: int  # 基準日からの日数 (-1=昨日, 0=今日, +1=明日)
    rel_key: str  # "yesterday" / "today" / "tomorrow" / "+N" など
    rel_label: str     # yesterday / today / tomorrow など
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

    @property
    def rel_label_ja(self) -> str:
        """旧名。CC controller 側が読んでいるので残す(中身は英語)。"""
        return self.rel_label

    def display_label(self) -> str:
        """選択ダイアログ 1 行分の表示文字列(英語)。"""
        task = self.task_param or "(no task)"
        code = (self.day_code + " ") if self.day_code else ""
        origin = self.plan_name + (f":{self.period_name}" if self.period_name else "")
        return (
            f"[{self.rel_label} {self.date.isoformat()}] "
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
    rel_label: str
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

    @property
    def rel_label_ja(self) -> str:
        """旧名。CC controller 側が読んでいるので残す(中身は英語)。"""
        return self.rel_label

    def display_label(self) -> str:
        """選択リスト 1 行分の表示文字列(英語)。"""
        who = self.mouse_id or "(no id)"
        slot = f"[{self.slot}] " if self.slot else ""
        cond = f"/{self.cond}" if self.cond else ""
        task = self.task_param or "(no task)"
        return (
            f"[{self.rel_label} {self.date.isoformat()}] {slot}{who} "
            f"{self.prj}{cond} — {self.config_dir} / {task}"
        )


#: 計画ファイルだと判る(トップレベルの)キー。
_PLAN_MARKER_KEYS = ("program:", "trials:", "periods:", "days:", "cc_config:")


def _looks_like_plan(path: Path) -> bool:
    """拡張子に頼らず、中身が計画ファイルらしいかを見る。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            head = f.read(4096)
    except (OSError, UnicodeDecodeError):
        return False
    return any(head.startswith(k) or f"\n{k}" in head for k in _PLAN_MARKER_KEYS)


class DuplicateKeyError(ValueError):
    """同じキーが 2 度書かれている YAML。後勝ちで前が消えるため読まない。"""


def _check_duplicate_keys(text: str, name: str) -> None:
    """トップレベルのキー重複を検出して弾く。

    YAML は同じキーが 2 度あると**後ろで上書き**する。``periods:`` が 2 つ
    書かれた計画ファイルが実在し、前半の Trial(3 個体)がどのツールからも
    見えないまま失われた。黙って半分だけ読むより読み込みを断る。
    """
    seen, dup = set(), []
    for line in text.split("\n"):
        m = re.match(r"^([A-Za-z_][\w-]*):", line)
        if not m:
            continue
        key = m.group(1)
        if key in seen and key not in dup:
            dup.append(key)
        seen.add(key)
    if dup:
        raise DuplicateKeyError(
            f"{name}: duplicate top-level key(s): {', '.join(dup)}. "
            "YAML keeps the last one, so the earlier content would be lost. "
            "Merge them before loading.")


def load_plan(path: Union[str, Path]) -> ExperimentPlan:
    """YAML の実験計画を読み込み :class:`ExperimentPlan` を返す。"""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    _check_duplicate_keys(text, path.name)
    data = yaml.load(text, Loader=_YamlLoader) or {}
    return ExperimentPlan.model_validate(data)


def save_plan(plan: ExperimentPlan, path: Union[str, Path]) -> None:
    """実験計画を YAML として書き出す(既定値/None 項目は省略して読みやすく)。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # exclude_defaults: 空の periods / days / mice や未設定項目を書かず簡潔に保つ。
    data = plan.model_dump(mode="python", exclude_defaults=True)
    # 日ごとの辞書は 1 行にまとめる。1 日 1 行だと 1 個体で数百行になり、
    # 実験内容より体重表のほうが長くなってしまうため(内容は一切変えない)。
    for trial in data.get("trials", []) or []:
        for mouse in (trial.get("mice") or []):
            for key in PERDAY_DICT_KEYS:
                v = mouse.get(key)
                if isinstance(v, dict) and v:
                    mouse[key] = _FlowMap(v)
            # custom は 1 段深い ({列キー: {day: 値}}) ので内側だけ 1 行にする。
            custom = mouse.get("custom")
            if isinstance(custom, dict):
                for col, days in list(custom.items()):
                    if isinstance(days, dict) and days:
                        custom[col] = _FlowMap(days)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(
            data,
            f,
            Dumper=_YamlDumper,
            allow_unicode=True,  # 日本語をそのまま出力
            sort_keys=False,     # モデル定義順を維持して可読性を保つ
            default_flow_style=False,
            width=100,           # フロー辞書が長くなったら折り返す
        )


def iter_plan_files(plan_dir: Union[str, Path]) -> List[Path]:
    """予定ディレクトリ内の計画 YAML を(名前順に)列挙する。存在しなければ空。

    拡張子は ``.yaml`` と ``.yml`` の両方を拾う(``.yml`` の計画が実在する)。

    同じフォルダには計画でない YAML も置いてある(``settings.yaml`` /
    ``experimental_slot.yaml`` などのマスタ)。**中身が計画でないファイルは
    そもそも返さない**。以前は拡張子だけで拾っていたため、CC の起動ログに
    ``experimental_slot.yaml`` の検証エラーが毎回出て、本当の問題を隠していた。

    拡張子の無いファイルは拾えないので、**計画に見えるのに拡張子が無い**
    ものは警告する。実例として ``prjDA11-1_RV_CSstop``(32 個体)が拡張子
    無しで置かれ、GUI からも CC からも見えないまま残っていた。
    """
    plan_dir = Path(plan_dir)
    if not plan_dir.is_dir():
        return []
    # サブフォルダ(plans/ など)も許容しつつ、隠し/テンポラリを避ける。
    files, hidden = [], []
    for p in sorted(plan_dir.rglob("*")):
        if not p.is_file() or p.name.startswith((".", "_")):
            continue
        if p.suffix.lower() in (".yaml", ".yml"):
            if _looks_like_plan(p):
                files.append(p)
        elif _looks_like_plan(p):
            hidden.append(p)
    for p in hidden:
        print(f"[ylabcommon.plan] looks like a plan but has no .yaml/.yml "
              f"extension, so it is not loaded: {p}")
    return files


def load_plans(plan_dir: Union[str, Path]) -> List[Tuple[Path, ExperimentPlan]]:
    """予定ディレクトリ内の全計画を読み込む。壊れたファイルは skip(警告出力)。"""
    result: List[Tuple[Path, ExperimentPlan]] = []
    for p in iter_plan_files(plan_dir):
        try:
            result.append((p, load_plan(p)))
        except Exception as e:  # noqa: BLE001 - 1 ファイルの破損で全体を止めない
            print(f"[ylabcommon.plan] failed to read plan file: {p}: {e}")
    return result


def _rel_labels(offset: int) -> Tuple[str, str]:
    """offset -> (rel_key, rel_label)。"""
    key = _REL_KEY.get(offset, f"{offset:+d}")
    label = _REL_LABEL.get(offset, f"{offset:+d}d")
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


def default_sessions(phases: List[str],
                     skips: Optional[List[bool]] = None) -> List[Optional[int]]:
    """phase 列 -> 各日の既定 session(同一 phase の出現順の累積)。

    phase が空の日は None。例: ``["1","1","2","1"]`` -> ``[1, 2, 1, 3]``。
    ``skips`` を渡すと ``True`` の日(:attr:`PlanDay.skip`)は数えず None を返すので、
    休みを挟んでも session 番号は順延する(``["1","1","1"], [False, True, False]``
    -> ``[1, None, 2]``)。
    """
    out: List[Optional[int]] = []
    counts: Dict[str, int] = {}
    for i, ph in enumerate(phases):
        if skips is not None and i < len(skips) and skips[i]:
            out.append(None)          # 休みの日は数えない -> 以降が順延する
            continue
        p = (ph or "").strip()
        if not p:
            out.append(None)
            continue
        counts[p] = counts.get(p, 0) + 1
        out.append(counts[p])
    return out


def resolve_day_date(
    period: "ExperimentTrial", day: PlanDay
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
        for period in plan.trials:
            days = plan.resolve_trial(period)     # program を実時間へ割り当てた結果
            for day in days:
                if day.skip:          # ステップを持たない日は実験しないので列挙しない
                    continue
                d = resolve_day_date(period, day)
                if d is None:
                    continue
                offset = (d - ref_date).days
                if abs(offset) > window_days:
                    continue
                rel_key, rel_label = _rel_labels(offset)
                session = day.session
                found.append(
                    ScheduledConfig(
                        offset=offset,
                        rel_key=rel_key,
                        rel_label=rel_label,
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
        for period in plan.trials:
            days = plan.resolve_trial(period)     # program を実時間へ割り当てた結果
            for day in days:
                if day.skip:          # ステップを持たない日は実験しないので列挙しない
                    continue
                d = resolve_day_date(period, day)
                if d is None:
                    continue
                offset = (d - ref_date).days
                if abs(offset) > window_days:
                    continue
                rel_key, rel_label = _rel_labels(offset)
                label = day.label
                session = day.session
                day_code = format_day_code(day.day, day.phase, session)
                for m in period.mice:
                    # 個体別の上書き -> その日の標準(ステップ)の順で解決する。
                    m_phase = (m.phase.get(label) if label else None) or day.phase
                    m_sess = (m.session.get(label) if label else None)
                    if m_sess is None:
                        m_sess = session
                    task = (m.task_param.get(label) if label else None) or day.task_param
                    photo = (
                        (m.photometry_param.get(label) if label else None)
                        or day.photometry_param
                    )
                    found.append(
                        ScheduledMouse(
                            offset=offset,
                            rel_key=rel_key,
                            rel_label=rel_label,
                            date=d,
                            day_label=label,
                            day_code=format_day_code(day.day, m_phase, m_sess),
                            phase=m_phase,
                            session=m_sess,
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
