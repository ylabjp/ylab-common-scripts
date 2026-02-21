from typing import List, Optional, Dict, Union, Any
from typing import List, Literal
from pydantic import BaseModel, Field
from pathlib import Path
from ylabcommon.file_util import init_base_drive
import json
import os


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
    arena_mm_per_pix: Optional[float] = None
    arena_mm_per_pix_from_individual: Optional[bool] = False
    roi: Optional[Dict[str, List[List[int]]]] = {}
    start_frame: Optional[int] = None


DLC_MODEL_VERSIONs = [2020, 2025, 2026]
DLC_BODY_PARTS = {
    2020: [
        "left_ear",
        "right_ear",
        "snout",
        "centroid",
        "left_lateral",
        "right_lateral",
        "tail_base",
    ],
    2025: [
        "snout",
        "right_ear",
        "left_ear",
        "head_top",
        "tail_base",
        "rump_center",
        "centroid",
        "chest_center",
        "tail_end",
        "tail_dist",
        "tail_prox",
    ],
    2026: [
        "head_midpoint",
        "tail_end",
        "right_hip",
        "right_midside",
        "right_shoulder",
        "left_hip",
        "left_midside",
        "left_shoulder",
        "tail5",
        "tail4",
        "tail3",
        "tail2",
        "tail1",
        "tail_base",
        "mid_backend3",
        "mid_backend2",
        "mid_backend",
        "mouse_center",
        "mid_back",
        "neck",
        "right_ear_tip",
        "left_ear_tip",
        "right_ear",
        "left_ear",
        "right_eye",
        "left_eye",
        "nose",
    ],
}
DLC_HEAD_CENTER = {2020: [0, 1, 2], 2025: [0, 1, 2, 3], 2026: [0, 22, 23, 24, 25, 26]}
DLC_BODY_CENTER = {
    2020: [3, 4, 5, 6],
    2025: [4, 5, 6, 7],
    2026: [3, 4, 5, 6, 7, 8, 16, 17, 18],
}

def replace_yen_in_path_for_linux(fname: str):
    return fname.replace("\\", "/")

class DLCParam(BaseModel):

    bodyparts_version: Optional[int] = 2020 # custom configを追加してそこでmodel情報から取得するようにする
    is_dynamic:Optional[bool] = True
    dlc_median_filter_kernel_in_pixel:Optional[int] = 11

    def get_dlc_body_parts_all(self):
        return DLC_BODY_PARTS[self.bodyparts_version]

    def get_dlc_parts_for_head_center(self):
        v = self.bodyparts_version
        return [DLC_BODY_PARTS[v][i] for i in DLC_HEAD_CENTER[v]]

    def get_dlc_parts_for_body_center(self):
        v = self.bodyparts_version
        return [DLC_BODY_PARTS[v][i] for i in DLC_BODY_CENTER[v]]



class PreprocessingParam(BaseModel):
    time_bin_in_s_before_dlc: Optional[float] = None
    time_bin_in_s: Optional[float] = None
    model_targets: Optional[List[str]] = []
    def get_resample_str(self, is_before_dlc=False) -> str:
        if is_before_dlc:
            time_bin_in_s = self.time_bin_in_s_before_dlc
        else:
            time_bin_in_s = self.time_bin_in_s
        if time_bin_in_s < 1:
            resample_str = "%dms" % int(time_bin_in_s * 1000)
        else:
            resample_str = "%dS" % time_bin_in_s
        return resample_str


class GroupAnalysisItemParam(BaseModel):
    param: str
    cond_group: Optional[Dict[str, List[str]]] = None
    session_in_reverse_order: Optional[bool] = False
    trial_div: Optional[Dict[str, Any]] = None
    colors: Optional[Dict[str, Any]] = None


class AggregationParamItem(BaseModel):
    '''
    common to video and cc
    '''
    target: Optional[str] = ""      # TODO videoとccで異なる。統合する。
    targets:Optional[List[str]] = []
    task_type: Optional[str]=Field(default=None,alias="type")         # TODO videoとccで異なる。統合する。
    target_type: Optional[str]=""
    task_types: Optional[List[str]] = []
    range: Optional[List[int]] = None
    bin: Optional[int] = None

class EventFileterItem(BaseModel):
    target:str
    relation:str
class EventConfig(BaseModel):
    target: str
    type: str
    baseline_in_s: int = Field(gt=0)
    after_in_s: int = Field(gt=0)
    task_types: Optional[List[str]] = []
    task_type: Optional[str]=Field(default=None,alias="type")   
    scheduled_stimuli_keys: Optional[List[str]] = []
    response_targets: Optional[List[str]] = []
    is_exclusive:Optional[ bool  ] = True
    event_filter: Optional[List[EventFileterItem]] = None
    start_in_hm: Optional[str] = None
    end_in_hm: Optional[List[int]] = None
class PhotometryConfig(BaseModel):
    name: str
    target: str
    # 2つ目の要素にのみ存在するフィールドは Optional を使用
    ref: Optional[str] = None
    correction: Optional[str] = None

    is_autofluo_subtraction: Optional[bool] = False
    median_filter_in_s: float

    
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
    def __get_path(self,target):
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
        dirbase = self.__get_path("PRJ_ROOT")
        return dirbase / self.prj_dir
    def get_raw_drive(self)->Path:
        dirbase = self.__get_path("RAW_ROOT")
        return dirbase

    def get_raw_dir(self)->List[Path]:
        res=[]
        for r in self.raw_dir:
            res.append(self.get_raw_drive()/r)
        return res

    def generate_individual_param(self,path:Path):
        individual_param={}
        if path.exists():
            with open(path) as f:
                individual_param=json.load(f)
        return self.model_copy(update=individual_param,deep=True)

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
                },
            }, f, indent=4)

class VideoInfo(BaseModel):
    # 解析済みのビデオファイルパスのリスト
    raw_video_list: List[str]
    
    # 解析ステータス（"done" や "pending" など、特定の文字列のみ許可する場合）
    analysis_status: Optional[Literal["done", "pending", "error"]] = None
    
    # DLCの設定ファイルパス（実際にファイルが存在するかチェックしたい場合は FilePath 型も使えます）
    # dlc_param: Optional[str] = None

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
    class CONFIG:
        VIDEO_INFO_JSON = "video_info.json"
        PARAM_INDIVIDUAL_JSON = "param_individual.json"
    class REPORT:
        PREPROCESS_CC_REPORT_PDF = "df_preprocess_cc_monitor.pdf"
        PREPROCESS_VIDEO_REPORT_PDF = "df_preprocess_video_monitor.pdf"
        DLC_RAW_PDF = "dlc_raw_plot.pdf"


class DataKeys:
    DATA_KEY_TIMESERIES = "dataframe"
    DATA_KEY_AGGREGATION = "aggregation"
    DATA_KEY_EVENT_LIST = "event_list"
    DATA_KEY_TASK_SCHEDULED = "task_scheduled"
    DATA_KEY_BLOCK_TIME = "event_termination"
    PREPROCESS_CC_INDEX_COLUMNS = [
        "target",
        "signal_type",
        "task_type",
        "trial",
        "time",
        "rel_time",
    ]
    ANALYSIS_INDIVIDUAL_INDEX_COLUMS = [
        "cond",
        "mouse",
        "day",
        "within_factor",
    ] + PREPROCESS_CC_INDEX_COLUMNS
