from typing import List, Dict, Optional, Any
from pydantic import BaseModel, Field


class DLCConfig(BaseModel):
    config_path: str
    bodyparts_version: int
    is_dynamic: bool


class PreprocessingConfig(BaseModel):
    time_bin_in_s_before_dlc: float
    time_bin_in_s: float
    model_targets: List[str]


class AggregationVideoItem(BaseModel):
    target: str
    type: str


class AggregationCCItem(BaseModel):
    target_type: str
    targets: List[str]
    task_types: Optional[List[str]] = None


class AggregationCalcItem(BaseModel):
    x: List[str]
    y: List[str]
    calc: str


class EventFilter(BaseModel):
    target: str
    relation: str  # "include" / "exclude" など


class EventTargetItem(BaseModel):
    target: str           # "cc" / "task" など
    type: str             # "response_onset" / "task" など
    # response_onset系のみ
    response_targets: Optional[List[str]] = None
    # task系のみ
    task_types: Optional[List[str]] = None
    scheduled_stimuli_keys: Optional[List[str]] = None

    baseline_in_s: float
    after_in_s: float

    event_filter: Optional[List[EventFilter]] = None


class VideoParam(BaseModel):
    arena_mm_per_pix: float
    start_frame: int
    arena_box: List[List[int]]
    roi: Dict[str, Any]


class TrialDiv(BaseModel):
    div_num: List[int]
    task_types: List[str]


class GroupAnalysisConfig(BaseModel):
    param: str
    cond_group: Optional[Dict[str, List[str]]] = None
    trial_div: Optional[TrialDiv] = None
    target_key: Optional[str] = None
    session_in_reverse_order: Optional[bool] = None


class BehaviorParamConfig(BaseModel):
    prj_dir: str
    raw_dir: List[str]

    dlc: DLCConfig = Field(alias="DLC")
    photometry: List[Any]

    preprocessing: PreprocessingConfig

    aggregation_video: Dict[str, AggregationVideoItem]
    aggregation_cc: Dict[str, AggregationCCItem]
    aggregation_calc: Dict[str, AggregationCalcItem]

    event_target: Dict[str, EventTargetItem]

    video_param: VideoParam

    group_analyses: Dict[str, GroupAnalysisConfig]
    group_analyses_temp: Dict[str, GroupAnalysisConfig]


