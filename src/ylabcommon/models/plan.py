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

from datetime import date as DateType
from datetime import timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field

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
]

# behavior-config 直下の予定ディレクトリ名。CC controller / GUI 双方が参照する。
PLAN_DIR_NAME = "controller-plan"
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
    - ``photometry_param``: 既定の photometry パラメータファイル名。各 day で
      上書きできる。
    """

    config_dir: str = ""
    photometry_param: Optional[str] = None


class PlanDay(BaseModel):
    """スケジュール 1 日分。CC 参照の中核。

    Schedule は Period 非依存の上位概念(:class:`ExperimentPlan` の ``days``)。
    実施日は ``offset``(Period 開始日からの日数)で持ち、具体的な日付は
    Period.start + offset で算出する(:func:`resolve_day_date`)。

    - ``label``: day01 のような日ラベル。マウスの実験台割当のキーにもなる。
    - ``offset``: Period 開始日からの日数 (day01=0, day02=1, ...)。
    - ``phase``: exposure / conditioning / test など。
    - ``task_param``: この日の標準 task パラメータ名 (config_dir/param_files_task/ 以下)。
      個体ごとの上書きは :class:`PlanMouse` の ``task_param`` を参照。
    - ``photometry_param``: 既定 (:class:`CCConfig`) を上書きしたい場合のみ指定。
    """

    label: str = ""
    offset: int = 0
    phase: str = ""
    task_param: Optional[str] = None
    photometry_param: Optional[str] = None
    note: Optional[str] = None


class PlanMouse(BaseModel):
    """マウス 1 個体分の名簿と、日ごとの実験台(operant chamber)割当。

    ``bench`` は day ラベル -> チャンバー名 (例 ``{"day01": "B10"}``) の辞書。
    ``bw_before`` / ``bw_after`` は day ラベル -> 給水前 / 給水後の体重 g の辞書
    (例 ``{"day01": 23.4}``)。給水管理では bw_before を当日体重として用いる。
    標準体重 std_bw は保存せず日齢と settings.yaml から算出する。
    ``task_param`` は day ラベル -> その個体・その日に使う task パラメータ名の辞書。
    day の標準 (:class:`PlanDay` の ``task_param``) を上書きしたい日だけ入れる
    (標準と同じ日は入れない)。
    ``within_factor`` は day ラベル -> その個体・その日の within-subject 因子水準の
    辞書。取りうる値は :attr:`ExperimentPlan.within_factors`(Plan 直下の候補リスト)
    から選ぶ。標準は無く、指定した日だけ入れる。
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
    task_param: Dict[str, str] = Field(default_factory=dict)
    within_factor: Dict[str, str] = Field(default_factory=dict)
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

    ``days`` は全 Period 共通の Schedule(各日は ``offset`` を持つ)。``periods`` は
    それぞれ ``start`` と名簿を持ち、具体日付は start + offset で決まる。
    ``within_factors`` は within-subject 因子の候補リスト。Per-day で各個体・各日の
    :attr:`PlanMouse.within_factor` を選ぶときの選択肢になる。

    給水(絶水)管理:
    - ``water_restriction_ratio``: 目標体重の割合 (例 0.85 = 予測自由摂取体重の 85%)。
      YAML(=プロトコル)単位で決める。
    - ``daily_evaporation_ml``: 1 日あたりの水分蒸発量 (ml)。給水量の算出に加味する。
    予測自由摂取体重は settings.yaml の標準体重に対し、day-2 の実測体重
    (:attr:`PlanMouse.actual_bw_day_2` / :attr:`PlanMouse.age_day_2`) の比を掛けて
    求める(算出は GUI 側。標準体重データが behavior-config にあるため)。
    """

    protocol: str = ""
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
        """day 個別指定があればそれを、無ければ cc_config の既定値を返す。"""
        return day.photometry_param or self.cc_config.photometry_param


class ScheduledConfig(BaseModel):
    """特定の基準日から見た「予定された config」1 件。

    CC controller の config 選択ダイアログに 1 行として並ぶ。
    """

    offset: int  # 基準日からの日数 (-1=昨日, 0=今日, +1=明日)
    rel_key: str  # "yesterday" / "today" / "tomorrow" / "+N" など
    rel_label_ja: str  # 昨日 / 今日 / 明日 など
    date: DateType
    day_label: str = ""
    phase: str = ""
    protocol: str = ""
    plan_name: str = ""  # 由来した YAML ファイル名 (拡張子なし)
    period_name: str = ""  # 由来した Period 名 (複数 Period のとき)
    config_dir: str = ""
    task_param: Optional[str] = None
    photometry_param: Optional[str] = None

    def display_label(self) -> str:
        """選択ダイアログ 1 行分の日本語表示文字列。"""
        task = self.task_param or "(task未指定)"
        phase = ("[" + self.phase + "] ") if self.phase else ""
        origin = self.plan_name + (f":{self.period_name}" if self.period_name else "")
        return (
            f"【{self.rel_label_ja} {self.date.isoformat()}】 "
            f"{phase}{self.config_dir} / {task}  «{origin}»"
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

    戻り値は offset(昇順) -> protocol -> day_label の順にソートされる。
    """
    if ref_date is None:
        ref_date = DateType.today()

    found: List[ScheduledConfig] = []
    for path, plan in load_plans(plan_dir):
        plan_name = path.stem
        cc = plan.cc_config
        for period in plan.periods:
            for day in plan.days:
                d = resolve_day_date(period, day)
                if d is None:
                    continue
                offset = (d - ref_date).days
                if abs(offset) > window_days:
                    continue
                rel_key, rel_label_ja = _rel_labels(offset)
                found.append(
                    ScheduledConfig(
                        offset=offset,
                        rel_key=rel_key,
                        rel_label_ja=rel_label_ja,
                        date=d,
                        day_label=day.label,
                        phase=day.phase,
                        protocol=plan.protocol,
                        plan_name=plan_name,
                        period_name=period.name,
                        config_dir=cc.config_dir,
                        task_param=day.task_param,
                        photometry_param=day.photometry_param or cc.photometry_param,
                    )
                )

    found.sort(key=lambda s: (s.offset, s.protocol, s.period_name, s.day_label))
    return found
