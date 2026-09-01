from typing import List, Optional, Dict, Union, Any
from typing import List, Literal
from pydantic import BaseModel, Field,ConfigDict, field_validator, model_validator
from pathlib import Path
from datetime import datetime
import json
import os
import platform
import pandas as pd

RoiItem = Union[list[int], dict[str, str]]

class VideoParam(BaseModel):
    '''
    # "video_param": {
    #         "arena_mm_per_pix": 0.5,
    #         "start_frame": 0,
    #         "arena_box": [
    #             [
    #                 350,
    #                 135
    #             ],
    #             [
    #                 350,
    #                 300
    #             ]
    #         ],
    #         "roi": {
    #             "NearSide": [
    #                 [
    #                     570,
    #                     75
    #                 ],
    #                 [
    #                     130,
    #                     420
    #                 ]
    #             ],
    '''
    arena_box: Optional[List[List[int]]] = Field(default=[[0,0],[100,100]])
    arena_mm_per_pix: Optional[float] = 0.0
    # arena_mm_per_pix_from_individual: Optional[bool] = False
    roi: Optional[Dict[str, List[RoiItem]]] = {}
    start_frame: Optional[int] = 0
    # Recording fps used ONLY to synthesise an evenly-spaced time axis when NO
    # *timestamp.npy exists at all (the fix_prj_video-timestamp.py salvage path). When
    # timestamps exist they are the source of truth, and cv2's reported fps is unreliable
    # for the custom recorder.
    #
    # This is NOT a project-wide constant: the recording fps can differ between
    # experiments (and problem data with inconsistencies is exactly where salvage runs),
    # so set it PER INDIVIDUAL in the day's param_individual.json (video_param.salvage_fps)
    # rather than in the shared project config. Default None -> the salvage script falls
    # back to its documented default with a warning.
    salvage_fps: Optional[float] = None


DLC_MODEL_VERSIONs = [2020, 2025, 2026]

# head_center / body_center を計算するときに、どの body part をどちらの重心に
# 平均するかを示す role タグ。
# - HEAD: head_center の平均に含める
# - BODY: body_center の平均に含める
# - None: どちらの重心にも使わない (tail など独立に動く点)
# NOTE: 新しいモデル(2026)には "centroid" が存在しない。centroid の有無に依存せず、
#       role タグに従って計算するので、centroid が無いモデルでも問題なく動く。
HEAD = "head"
BODY = "body"

# 各バージョンの body parts を (part_name, role) で定義する。
# part_name の並び順は DLC モデルの出力順に一致させること
# (detect_dlc_bodyparts_version は順序に依存しないが、可読性のため合わせておく)。
DLC_BODY_PARTS = {
    2020: [
        ("left_ear", HEAD),
        ("right_ear", HEAD),
        ("snout", HEAD),
        ("centroid", BODY),
        ("left_lateral", BODY),
        ("right_lateral", BODY),
        ("tail_base", BODY),
    ],
    2025: [
        ("snout", HEAD),
        ("right_ear", HEAD),
        ("left_ear", HEAD),
        ("head_top", HEAD),
        ("tail_base", BODY),
        ("rump_center", BODY),
        ("centroid", BODY),
        ("chest_center", BODY),
        ("tail_end", None),
        ("tail_dist", None),
        ("tail_prox", None),
    ],
    2026: [
        ("head_midpoint", HEAD),
        ("tail_end", None),
        ("right_hip", BODY),
        ("right_midside", BODY),
        ("right_shoulder", BODY),
        ("left_hip", BODY),
        ("left_midside", BODY),
        ("left_shoulder", BODY),
        ("tail5", None),
        ("tail4", None),
        ("tail3", None),
        ("tail2", None),
        ("tail1", None),
        ("tail_base", None),
        ("mid_backend3", None),
        ("mid_backend2", None),
        ("mid_backend", BODY),
        ("mouse_center", BODY),
        ("mid_back", BODY),
        ("neck", None),
        ("right_ear_tip", None),
        ("left_ear_tip", None),
        ("right_ear", HEAD),
        ("left_ear", HEAD),
        ("right_eye", HEAD),
        ("left_eye", HEAD),
        ("nose", HEAD),
    ],
}


def get_dlc_body_part_names(version: int) -> List[str]:
    """指定バージョンの全 body part 名を出力順で返す。"""
    return [name for name, _role in DLC_BODY_PARTS[version]]


def get_dlc_parts_for_region(version: int, region: str) -> List[str]:
    """指定バージョンで、指定 region(HEAD/BODY) に属する body part 名を返す。"""
    return [name for name, role in DLC_BODY_PARTS[version] if role == region]


def detect_dlc_bodyparts_version(body_parts) -> int:
    """
    DLC 出力に含まれる body part の集合から、どの DLC モデルバージョンかを判定する。

    body_parts: DLC の h5 に含まれる body part 名の iterable
    return: 一致した DLC_BODY_PARTS のバージョン
    raise: ValueError どのバージョンにも一致しない場合
    """
    observed = set(body_parts)
    for version, parts in DLC_BODY_PARTS.items():
        if set(name for name, _role in parts) == observed:
            return version
    raise ValueError(
        "DLC body parts が既知のどのモデルバージョンにも一致しません。\n"
        f"  検出された parts ({len(observed)}個): {sorted(observed)}\n"
        f"  対応バージョン: {list(DLC_BODY_PARTS.keys())}\n"
        "ylabcommon の DLC_BODY_PARTS にモデル定義を追加するか、"
        "使用した DLC モデルを確認してください。"
    )

def replace_yen_in_path_for_linux(fname: str):
    return fname.replace("\\", "/")

class DLCParam(BaseModel):
    config_path:str=""
    # bodyparts_version は DLC 出力の body parts から自動判定するのが基本
    # (detect_dlc_bodyparts_version)。ここでの値はフォールバック/明示指定用。
    bodyparts_version: Optional[int] = 2020
    is_dynamic:Optional[bool] = True
    dlc_median_filter_kernel_in_pixel:Optional[int] = 11

    def get_dlc_body_parts_all(self, version: Optional[int] = None):
        return get_dlc_body_part_names(version if version is not None else self.bodyparts_version)

    def get_dlc_parts_for_head_center(self, version: Optional[int] = None):
        return get_dlc_parts_for_region(
            version if version is not None else self.bodyparts_version, HEAD
        )

    def get_dlc_parts_for_body_center(self, version: Optional[int] = None):
        return get_dlc_parts_for_region(
            version if version is not None else self.bodyparts_version, BODY
        )



class PreprocessingParam(BaseModel):
    # onset 等の情報を取得する video 系の内部処理で使う細かい resample 幅(s)。
    # 最終出力(cc/video)の resample は time_bin_in_s、内部処理用の細かい bin は
    # time_bin_in_s_for_video_processing を get_resample(for_video_processing=True) で参照する。
    time_bin_in_s_for_video_processing: Optional[float] = None
    time_bin_in_s: Optional[float] = None
    model_targets: Optional[List[str]] = []
    def get_resample_str(self, for_video_processing=False) -> str:
        if for_video_processing:
            time_bin_in_s = self.time_bin_in_s_for_video_processing
        else:
            time_bin_in_s = self.time_bin_in_s
        if time_bin_in_s < 1:
            resample_str = "%dms" % int(time_bin_in_s * 1000)
        else:
            resample_str = "%ds" % time_bin_in_s
        return resample_str

    def get_resample(self, for_video_processing=False) -> pd.Timedelta:
        if for_video_processing:
            time_bin_in_s = self.time_bin_in_s_for_video_processing
        else:
            time_bin_in_s = self.time_bin_in_s
        return pd.Timedelta(seconds=time_bin_in_s)

class TrialDiv(BaseModel):
    """
    1 day / 1 session の trial をビン分割して tdiv 列を作り、day/session をさらに
    tdiv 単位に分けて時系列表示するための設定 (models/aggregation.py::apply_trial_div)。

    div_num:
        int      -> 各 day/session の観測 trial 範囲を div_num 等分する (例: 3 で前半/中盤/後半)。
        list[int]-> 明示的な trial 番号の境界 (bin edges)。例 [10, 13, 16, 19] は
                    trials 10-13 / 13-16 / 16-19 の3ビン。境界の外側の trial は除外される。
    """
    div_num: Union[int, List[int]]
    # DEPRECATED (2026-07): task_type単位の絞り込みは Event 指定時 (EventConfig.task_types) で
    # 行う設計に統一した。集計時フィルタ (apply_trial_div の task_types 分岐) は削除済みのため、
    # この項目はもう読まれない。後方互換で既存configの `task_types: []` を受理するために残すだけ。
    # 新規configには書かないこと。
    task_types: Optional[List[str]] = None


class GroupAnalysisItemParam(BaseModel):
    '''
    Behavioral paramの中で指定されるItem
    '''
    group_analysis_param: str
    cond_group: Optional[Dict[str, List[str]]] = None
    session_in_reverse_order: Optional[bool] = False
    trial_div: Optional[TrialDiv] = None
    # day単位/phase・session単位の集計切り分けを明示指定する。
    # Noneなら従来通りday文字列にphase情報が含まれるかで自動判定する。
    is_phase: Optional[bool] = None
    # PSTH(連続値グラフ)の時間ビンを t=0 を境界として bin_merge 個ずつまとめ、各群の平均で
    # 粗く(なめらかに)表示する調整用パラメータ。t=0 は必ず境界(onset後の先頭)になり、
    # onset前/後のビンは同じ群に混ざらない。個体ごと(群平均の前)にまとめるため mean/sem/count は
    # 統計的に正しく計算される。None または 1 で無効(従来通り)。2 以上で有効。
    bin_merge: Optional[int] = None
    # discrete(イベント)チャネルについて、既定の per-trial stat(PSTH の窓内積分 = 1トライアル
    # あたり応答数)に加えて「PSTH 非依存の窓内総イベント数」(トライアル数で割らない生カウント和)を
    # stat に追加出力する。(event, "target_count"/"baseline_count"/"subtract_count") として出力され、
    # plot_selection_stat の key では [[event, target_count], [discrete, ...]] のように選べる。
    # div は per-trial div と同値(比でトライアル数が相殺)なので出さない。総数はトライアル数に比例する
    # ため per-trial 指標とは一致しない(意図的)。既定 False。
    include_event_totals: Optional[bool] = False
    # cond をフォルダ構造(prj/cond*/mouse/day)から切り離し、**個体単位**で組み直す。
    # {新ラベル: [mouse_id, ...]} の形式で、cond_group の3効果(再ラベル / 未記載の除外 /
    # キー順の描画順固定)をそのまま単位だけ mouse に置き換えたもの。
    #   * どのグループにも書かれていない個体は**除外**される(cond_group と同じ規則)。
    #     「書けば in、書かなければ out」なので個体の出し入れがこれだけで書ける。
    #   * mouse_id はフォルダ名と完全一致(性別サフィックス -m/-f 込み)。
    #   * cond_group と同時には指定できない(どちらも「その行の cond は何か」を決めるため)。
    # 例: {"Responder": ["OATC001-m", "OATC005-m"], "NonResponder": ["OATC002-m"]}
    cond_by_mouse: Optional[Dict[str, List[str]]] = None
    # cond の構成はそのままに、指定した個体だけを全集計から外す。cond_group とも
    # cond_by_mouse とも併用でき、他のどの処理よりも先に適用される。
    exclude_mice: Optional[List[str]] = None

    @model_validator(mode="after")
    def _check_cond_selection(self):
        """cond_by_mouse / exclude_mice の静的に決まる矛盾を config 読み込み時に弾く。

        いずれも「どちらの意味に取るべきか決められない」書き方なので、黙って一方を採用すると
        n が意図せず減った図が正常に見えてしまう。データを見なくても判定できるものはここで
        落とし、データ依存の検査(存在しない mouse_id / どの群にも入らない個体)は
        behavior_analysis 側の resolve_cond_selection が警告する。
        """
        if self.cond_by_mouse:
            if self.cond_group:
                raise ValueError(
                    "cond_group and cond_by_mouse cannot be used together: both decide "
                    "the cond of a row. Use cond_group to regroup folder conds, or "
                    "cond_by_mouse to rebuild conds from individual mice."
                )
            empty = [label for label, mice in self.cond_by_mouse.items() if not mice]
            if empty:
                raise ValueError(
                    f"cond_by_mouse has group(s) with no mouse: {empty}. "
                    "Remove the group or list its mice."
                )
            seen: Dict[str, str] = {}
            for label, mice in self.cond_by_mouse.items():
                for m in mice:
                    if m in seen:
                        raise ValueError(
                            f"cond_by_mouse lists mouse '{m}' in both '{seen[m]}' and "
                            f"'{label}'. A mouse can belong to only one group."
                        )
                    seen[m] = label
            both = sorted(set(self.exclude_mice or []) & set(seen))
            if both:
                raise ValueError(
                    f"mouse(s) {both} appear in both cond_by_mouse and exclude_mice. "
                    "Drop them from one of the two."
                )
        return self


class AggregationParamItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)    # typeの読み込みに必要。いずれtypeを書き換える
    '''
    common to video and cc
    '''
    target: Optional[str] = ""      # TODO videoとccで異なる。統合する。
    targets:Optional[List[str]] = []
    # DEPRECATED (2026-07): 旧 aggregation_cc 用。現在どのコードからも参照されない。
    task_type: Optional[str]=Field(default=None,alias="type")         # TODO videoとccで異なる。統合する。
    target_type: Optional[str]=""
    # DEPRECATED (2026-07): 旧 aggregation_cc[name].task_types。task_type単位のイベント絞り込みは
    # Event 指定 (event_target[name].task_types) へ移行済み。generate_cc_timeseries の
    # aggregation_cc ループは targets/target_type のみを参照し、この項目は読まれない。
    # 詳細と移行例は behavior-analysis/docs/analysis-spec.md「task_type の指定」節を参照。
    task_types: Optional[List[str]] = []
    range: Optional[List[int]] = None
    bin: Optional[int] = None

class EventFileterItem(BaseModel):
    target:str
    relation: Literal["include", "exclude"]
class EventConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)    # typeの読み込みに必要。いずれtypeを書き換える
    target: str
    baseline_in_s: int = Field(gt=0)
    after_in_s: int = Field(gt=0)
    event_use_on_collision: Optional[Literal["first", "last", "none"]] = "last"
    # 同一event_targetの複数repのbaseline/after windowが時間的に重なった場合の解決方法。
    # first: 先に発生したrepを優先し、重複区間は後発repで上書きしない
    # last : 後に発生したrepを優先し、重複区間は後発repで上書きする(旧is_exclusiveなし相当の挙動)
    # none : 重複区間はどちらのrepにも属さないものとして両方とも破棄する
    rep_targets: Optional[List[int]] = []   # どのrepを対象にするか。空の場合は全てのrepが対象
    task_types: Optional[List[str]] = []    # task table上のtypeを指定。空の場合は全てのtypeが対象
    task_type: Optional[str]=Field(default=None,alias="type") # ccのeventにおいて"type": "response_onset"などを指定する。紛らわしい。
    scheduled_stimuli_keys: Optional[List[str]] = []
    response_targets: Optional[List[str]] = []
    event_filter: Optional[List[EventFileterItem]] = None
    start_in_hm: Optional[str] = None
    end_in_hm: Optional[List[int]] = None
    duration_in_hm: Optional[List[int]] = None
    # `type: "time_periods"` 専用。[hours, minutes]で1回あたりの期間の長さを指定する(例: [12, 0]で12時間)。
    # (行頭を `# type:` にすると PEP 484 の型コメントと解釈され、mypy がこのファイルを
    #  構文エラーにする。ここを import する全モジュールの型チェックが止まるので崩さないこと)
    # after_in_sの代わりにこの値が秒に変換されてイベントwindowの幅として使われる(baseline_in_sは通常通り有効)。
class PhotometryConfig(BaseModel):
    name: str
    target: str
    # 2つ目の要素にのみ存在するフィールドは Optional を使用
    ref: Optional[str] = None
    correction: Optional[str] = None

    is_autofluo_subtraction: Optional[bool] = False
    median_filter_in_s: float

class AggregationCalcConfig(BaseModel):
    """各プロット・計算の具体的な設定"""
    # TODO 不要か
    x: List[str]
    y: List[str]
    calc: Literal["x-y", "x/y"]


class PreprocessingScope(BaseModel):
    """このプロジェクトで実際に実行する preprocessing ステージの宣言(`st` の進捗判定専用)。

    3キーとも明示必須。キー -> analysis_meta.yaml のステージ名 (実行する CLI):
        cc                   -> preprocess_cc                   (`pc`)
        dlc                  -> preprocess_dlc_merge            (`pv` 前半)
        detection_difference -> preprocess_detection_difference (`pvdd`)

    `preprocess_video` (`pv` 後半) は宣言キーではない。dlc / detection_difference の
    マージ結果なので (dlc or detection_difference) から自動導出する。両方 false だと
    DataModelVideo.generate_from_preprocessed_files が空を返して出力自体が作られない。

    【重要】この項目は進捗レポートの集計対象を決めるだけで、解析結果にも
    analysis_meta.yaml の config_hash にも一切影響しない。どの DataModel の
    CONFIG_FIELDS にも入れてはならない(入れると既存出力が一斉に STALE(config) になる)。
    """

    # `detection_diff` 等のキー綴り間違いを黙って無視して既定値のまま集計しないため。
    model_config = ConfigDict(extra="forbid")

    cc: bool
    dlc: bool
    detection_difference: bool

    @model_validator(mode="after")
    def _at_least_one_stage(self):
        if not (self.cc or self.dlc or self.detection_difference):
            raise ValueError(
                "preprocessing_scope: cc / dlc / detection_difference が全て false です。"
                "少なくとも1つを true にしてください"
                "(全 false では判定対象ステージが無くなり、全 day が NOT STARTED になります)。"
            )
        return self


class BehaviorParam(BaseModel):
    """
    Root model
    """

    prj_dir: Path
    raw_dir: List[Path]
    DLC: Optional[DLCParam] = Field(default_factory=DLCParam)
    preprocessing: Optional[PreprocessingParam] = Field(
        default_factory=PreprocessingParam
    )
    # このプロジェクトで実際に実行する preprocessing ステージの宣言(`st` の進捗集計対象)。
    # None = 未宣言 -> status 側の既定を適用し、レポート冒頭に「未宣言」の警告を出す。
    # 【重要】PreprocessingParam の中に入れたり、どこかの CONFIG_FIELDS に足したりしないこと。
    # top-level なら全ステージの config_hash が不変。preprocessing 配下に入れると
    # preprocess_video と individual_analysis のハッシュが変わり既存出力が STALE(config) になる。
    preprocessing_scope: Optional[PreprocessingScope] = None
    video_param: Optional[VideoParam] = Field(default_factory=VideoParam)

    group_analyses: Optional[Dict[str, GroupAnalysisItemParam]] = Field(
        default_factory=dict
    )
    # Other optional fields mentioned in Analysis_Param.py validation list

    event_target: Optional[Dict[str, EventConfig]] = Field(default_factory=dict)
    aggregation_video: Optional[Dict[str, AggregationParamItem]] = Field(
        default_factory=dict
    )
    aggregation_cc: Optional[Dict[str, AggregationParamItem]] = Field(
        default_factory=dict
    )
    response_aggregation: Optional[Dict[str, Any]] = Field(default_factory=dict)
    photometry: Optional[List[PhotometryConfig]] = Field(default_factory=list)
    # aggregation_calc:  Optional[Dict[str, AggregationCalcConfig]]= Field(default_factory=dict)

    @staticmethod
    def get_path(target):
        base_val = os.getenv(target)
        if not base_val:
            raise EnvironmentError(
                f"{target} is not set. "
                "Please create a .env.windows or .env.linux file."
            )
        dirbase = Path(base_val)
        if not dirbase.exists():
            raise ValueError(
                str(dirbase)
                + " was not found. Please check the network status."
            )
        return dirbase
    def get_prj_dir(self)->Path:
        return self.get_prj_drive() / self.prj_dir

    def get_prj_drive(self)->Path:
        dirbase = self.get_path("PRJ_ROOT")
        return dirbase

    def get_raw_drive(self)->Path:
        dirbase = self.get_path("RAW_ROOT")
        return dirbase


    def get_raw_dir(self)->List[Path]:
        res=[]
        for r in self.raw_dir:
            res.append(self.get_raw_drive()/r)
        return res


    def generate_individual_param(self,path:Path):
        
        if not path.exists():
            return self

        with open(path) as f:
            individual_param=json.load(f)
        
        if "video_param" not in individual_param.keys():
            return self

        # overwriteするscopeは決めておく
        base=self.model_dump()
        for k in ["arena_box","roi","start_frame","salvage_fps"]:
            if k in individual_param["video_param"].keys():
                base["video_param"][k]=individual_param["video_param"][k]

        return self.model_validate(base)

    def save_individual_param(self,path:Path):
        '''
        Define the scope of the parameters to be saved for individual dataset
        Currently, the scope is confined to video_param
        IMPORTANT: if the scope is extended, the the corresponding code should use "generate_individual_param" 
        '''
        with open(path,"w") as f:
            json.dump({
                "video_param":{
                    "arena_box": self.video_param.arena_box,
                    "roi": self.video_param.roi,
                    "start_frame": self.video_param.start_frame,
                    "salvage_fps": self.video_param.salvage_fps,
                },
            }, f, indent=4)

#: "analyzing" のまま何時間経ったら「放置された残骸」と見なすか。
#: DLC の1フォルダ(= 1 day 分の動画)は長くても数時間で終わるので、これを超えて
#: 出力も status も動かないなら、掴んだPCが落ちた/中断されたと判断してよい。
#: 短くしすぎると走行中の解析を別PCが奪って二重解析になる。
ANALYZING_STALE_HOURS = 12.0


def _hostname() -> str:
    """実行PC名。betterstack_log の ``host`` と同じ出典(platform.node())にそろえる。"""
    try:
        return platform.node() or "unknown"
    except Exception:
        return "unknown"


class VideoInfo(BaseModel):
    # 解析済みのビデオファイルパスのリスト
    raw_video_list: List[str]
    
    # 解析ステータス（"done" や "pending" など、特定の文字列のみ許可する）
    # 未設定は "pending" に正規化する。実データには "" / null の未設定表現が混在しており
    # (旧既定値が "" だったため。raw2prj.py が明示的に "pending" を渡す回避策を入れているのも同じ理由)、
    # そのまま読むと Literal に無い "" で ValidationError になっていた。
    # 受理はしつつ、以降のコードが分岐する値を1つに揃えるため読込時に pending へ寄せる。
    analysis_status: Optional[Literal["done", "pending", "error", "analyzing", "fail"]] = "pending"

    @field_validator("analysis_status", mode="before")
    @classmethod
    def _unset_to_pending(cls, v):
        """未設定 ("" / null) を "pending" に正規化する。"""
        if v is None or v == "":
            return "pending"
        return v


    # DLCの設定ファイルパス（実際にファイルが存在するかチェックしたい場合は FilePath 型も使えます）
    dlc_param: Optional[str] = ""

    # 解析を開始したPC名と開始時刻(ISO8601, ローカルtz付き)。
    # DLCは複数のワークステーションが同じ raw_video を取り合うので、"analyzing" のまま
    # 固着したフォルダを見たとき「どのPCがいつ掴んだか」が分からないと、生きている解析なのか
    # 落ちた残骸なのか判断できない。mark_analyzing() で status と同時に書く。
    # 旧データには存在しないので Optional(未記録は None)。done/fail になっても消さない
    # (「誰がいつ回したか」の記録として残す)。
    analysis_host: Optional[str] = None
    analysis_started_at: Optional[str] = None

    def mark_analyzing(self) -> None:
        """status を "analyzing" にし、実行PC名と開始時刻を同時に記録する。

        PC名は betterstack_log が全ログに付ける ``host`` と同じ ``platform.node()``
        を使う。video_info.json とログを同じ名前で突き合わせられるようにするため。
        """
        self.analysis_status = "analyzing"
        self.analysis_host = _hostname()
        self.analysis_started_at = datetime.now().astimezone().isoformat(timespec="seconds")

    def analysis_age_in_hours(self, fallback_mtime: Optional[float] = None) -> Optional[float]:
        """"analyzing" を書いてからの経過時間。分からなければ None。

        fallback_mtime: analysis_started_at が無い(この項目より前に書かれた)場合に使う
            video_info.json の st_mtime。status を変えた時にファイルを書いているので、
            旧データでも「いつ analyzing になったか」の近似になる。
        """
        started = None
        if self.analysis_started_at:
            try:
                started = datetime.fromisoformat(self.analysis_started_at)
            except ValueError:
                started = None
        if started is None and fallback_mtime is not None:
            started = datetime.fromtimestamp(fallback_mtime).astimezone()
        if started is None:
            return None
        now = datetime.now(started.tzinfo) if started.tzinfo else datetime.now()
        return (now - started).total_seconds() / 3600.0

    def analysis_is_stale(
        self, fallback_mtime: Optional[float] = None,
        hours: float = ANALYZING_STALE_HOURS,
    ) -> bool:
        """"analyzing" の主張を放置された残骸と見なしてよいか。

        経過時間が分からない場合は **False**(= 掴んだままとして扱う)。二重解析は
        GPU を無駄にするうえ出力が競合するので、判断材料が無いときは踏み込まない。
        呼び出し側は video_info.json の st_mtime を fallback_mtime に渡せるので、
        実運用でこの分岐に落ちることはほぼ無い。
        """
        age = self.analysis_age_in_hours(fallback_mtime)
        return age is not None and age >= hours

    def analysis_owner_label(self) -> str:
        """「どのPCがいつ掴んだか」を1行で返す。未記録なら "unknown host"。

        経過時間も添える(固着の判断に要るのは絶対時刻より経過時間のため)。
        開始時刻が壊れている場合は時刻だけそのまま出す。
        """
        host = self.analysis_host or "unknown host"
        if not self.analysis_started_at:
            return host
        try:
            started = datetime.fromisoformat(self.analysis_started_at)
        except ValueError:
            return f"{host} since {self.analysis_started_at}"
        now = datetime.now(started.tzinfo) if started.tzinfo else datetime.now()
        hours = (now - started).total_seconds() / 3600.0
        return f"{host} since {self.analysis_started_at} ({hours:.1f}h ago)"

    def get_video_full_path_list(self, basedir:Path) -> list:
        '''
        basedir: raw drive path. typically "bparam.get_raw_drive()"
        return: list of video full path
        '''
        video_list = []
        for i in range(len(self.raw_video_list)):
            video_list.append(basedir/ replace_yen_in_path_for_linux(self.raw_video_list[i]))
        return video_list


class FileNames:
    class STORE:
        PREPROCCESSED_VIDEO = "df_preprocess_video.h5"  #
        PREPROCCESSED_DIFFERENCE_DETECTION = "df_difference_detection_raw.h5"
        PREPROCCESSED_CC = "df_preprocess_cc.h5"
        ANALYZED_INDIVIDUAL_H5 = "df_individual_analyzed.h5"
        ANALYZED_MERGED_H5 = "df_individual_analyzed_merged.h5"
        OUTPUT_FILE_DLC_RAW_H5 = "dlc_raw.h5"

    class PersistencePrefix:
        PREPROCCESSED_VIDEO = "dfpv_"  #
        PREPROCCESSED_DIFFERENCE_DETECTION = "dfdd_"
        PREPROCCESSED_CC = "dfpc_"
        ANALYZED_INDIVIDUAL_H5 = "dfi_"
        ANALYZED_MERGED_H5 = "dfm_"
        OUTPUT_FILE_DLC_RAW_H5 = "dlcraw_"

    class CONFIG:
        VIDEO_INFO_JSON = "video_info.json"
        PARAM_INDIVIDUAL_JSON = "param_individual.json"
    class REPORT:
        PREPROCESS_CC_REPORT_PDF = "df_preprocess_cc_monitor.pdf"
        PREPROCESS_VIDEO_REPORT_PDF = "df_preprocess_video_monitor.pdf"
        DLC_RAW_PDF = "dlc_raw_plot.pdf"

def get_persistence_name(base:str,key:str,temp:bool=False)->str:
    """
    base: prefix of the file name
    key: key of the file name
    return: file name
    """
    if temp:
        return base+key+"_temp.parquet"
    return base+key+".parquet"


class DataKeys:
    # do not use "_" in the key name. use "-" instead.
    # TODO delete 
    DATA_KEY_TIMESERIES = "dataframe"
    DATA_KEY_AGGREGATION = "aggregation"
    DATA_KEY_AGGREGATION_CC = "aggregation-cc"
    DATA_KEY_AGGREGATION_VIDEO = "aggregation-video"
    DATA_KEY_EVENT_LIST = "event-list"
    DATA_KEY_TASK_SCHEDULED = "task-scheduled"
    DATA_KEY_BLOCK_TIME = "event-termination"

    TIMESERIES = "timeseries"
    AGGREGATION = "aggregation"
    AGGREGATION_CC = "aggregation-cc"
    AGGREGATION_VIDEO = "aggregation-video"
    EVENT_LIST = "event-list"
    EVENT_LIST_TASK = "event-list-task"
    EVENT_LIST_CC = "event-list-cc"
    EVENT_LIST_VIDEO = "event-list-video"
    TASK_SCHEDULED = "task-scheduled"
    BLOCK_TIME = "event-termination"
    HISTOGRAM = "histogram"
    FRAME2FILE ="frame2file"


    class Index:
        PREPROCESS_CC = [
        "target",
        "signal_type",
        "task_type",
        "trial",
        "time",
        "rel_time_in_s",
    ]

    class TaskSchedule:
        BLOCK_TERMINATION_IN_MS = "block_termination_in_ms"
        BLOCK_TERMINATION_ORIGIN = "block_termination_origin"
        TRIAL_START = "trial_start"
        DEFAULT_BLOCK_LENGTH_IN_MS = "default_block_length_in_ms"
        ACTUAL_BLOCK_LENGTH_IN_MS = "actual_block_length_in_ms"
        @classmethod
        def get_index_columns(cls):
            return [
                cls.BLOCK_TERMINATION_IN_MS,
                cls.BLOCK_TERMINATION_ORIGIN,
                cls.TRIAL_START,
                cls.DEFAULT_BLOCK_LENGTH_IN_MS,
                cls.ACTUAL_BLOCK_LENGTH_IN_MS,
            ]
