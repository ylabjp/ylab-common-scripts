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
from packaging import version


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
