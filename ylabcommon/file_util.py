import json
import os
import glob
import copy
import collections
import platform
import h5py
import pandas as pd
import shutil
import logging
import datetime
import numpy as np
import sys
from pathlib import Path
from packaging import version
import questionary
import yaml

def find_parents_for_dir(start_dir:Path,target:str)->Path:
    '''
    Traverses upwards from the given start_dir, looking in each parent directory
    for a file or directory named 'target'. If found, returns the full path to that target.
    Returns None if the target is not found up to the root of the filesystem.
    
    Args:
        start_dir (Path): The directory to start searching from.
        target (str): The name of the file or directory to find in the parent chain.

    Returns:
        Path: The path to the found target file/directory, or None if not found.
    '''
    cur_dir = start_dir
    while True:
        candidate = cur_dir / target
        if candidate.exists():
            return candidate
        parent = cur_dir.parent
        if parent == cur_dir:
            break
        cur_dir = parent
    return None

def init_base_drive(prefix: dict):
    '''
    prefix: {"Windows":"XXX","Linux":"XXX"}
    '''

    pf = platform.system()
    # pfn = platform.node()
    if pf not in prefix.keys():
        raise ValueError("Inappropriate prefix parameter")
    dirbase = prefix[pf]

    if not os.path.exists(dirbase):
        raise ValueError(
            "Cannot access to %s. Check drive and network status" % dirbase)
    return dirbase

def get_config_list(base_path:str,config_dir_name:str,file_type="yaml")->dict[str, Path]:

    config_list=list(
        find_parents_for_dir(Path(base_path),config_dir_name).glob("*."+file_type)
    )
    if len(config_list)==0:
        raise ValueError("Critical error: config dir not found.")
    config_base=config_list[0].parent

    config_basename_dict={}
    for c in config_list:
        if c.name[0]=="_":
            continue
        config_basename_dict[c.name]=c

    return config_basename_dict

def select_config(param_model,base_path:str,config_dir_name:str,file_type="yaml") -> list:
    config_basename_dict=get_config_list(base_path,config_dir_name,file_type)

    answers = questionary.checkbox(
        "Select config and <enter>",
        choices=config_basename_dict.keys(),
    ).ask()

    config_list = list(map(lambda x:config_basename_dict[x],answers))

    sap_list=[]
    
    for idx, t in enumerate(config_list):
        with open(t, 'r', encoding="utf-8") as f:
            analysis_json = yaml.safe_load(f)
            sap_list.append(
                param_model(**analysis_json)
            )

    return sap_list


def replace_yen_in_path(self, fname: str):
    '''
    For japanese win to linux
    '''
    return fname.replace("\\", "/")

def init_logger(self, log_path, log_id, log_category):
    '''
    log_id: project config name etc
    log_category: behavior, slice etc
    '''

    main_pyfile = os.path.basename(sys.argv[0]).split(".")[0]

    # clear blank
    prev_files = glob.glob(os.path.join(log_path, "*"))
    for prev_file in prev_files:
        if os.path.getsize(prev_file) == 0:
            try:
                os.remove(prev_file)
            except Exception:
                continue
    today_str = datetime.datetime.today().strftime('%Y-%m-%d')
    pfn = platform.node()

    formatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s')
    logger = logging.getLogger(log_id+today_str)
    logger.setLevel(logging.DEBUG)

    handlers = []
    for type in [["debug", logging.DEBUG], ["error", logging.ERROR]]:
        dir_name = os.path.join(
            log_path,
            log_category+"_"+type[0]
        )
        if not os.path.exists(dir_name):
            os.makedirs(dir_name)

        h = logging.FileHandler(
            os.path.join(
                dir_name,
                "%s_%s_%s_%s_%s.log" % (type[0],today_str, main_pyfile, log_id, pfn)
            ))
        h.setLevel(type[1])
        h.setFormatter(formatter)
        handlers.append(h)

    # std out handler
    s_h = logging.StreamHandler()
    s_h.setLevel(logging.INFO)
    s_h.setFormatter(formatter)
    handlers.append(s_h)

    for h in handlers:
        logger.addHandler(h)

    return logger
