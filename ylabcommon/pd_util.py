import pandas as pd
import os

pd.set_option('display.max_rows', 300)
pd.set_option('display.max_columns', 300)


def get_prj_name(fname: str) -> str:
    '''
    obtain prj and paradigm name from file path
    '''
    if os.path.basename(fname) == "df_individual_analyzed_merged.h5":
        return os.path.basename(os.path.dirname(os.path.dirname(fname)))+"_"+os.path.basename(os.path.dirname(fname))
    raise ValueError("file name is not correct")


def read_and_cache(
    fname_df: str,
        cache_path: str,
        cond_map: dict,
        df_target_key='aggregation/cc'
):
    """
    Reads a DataFrame from an HDF5 file, processes it, and caches the result.
    This function attempts to read a cached DataFrame from a specified cache path.
    If the cached file does not exist, it reads the DataFrame from the provided file path,
    processes it according to the given condition map, and then caches the processed DataFrame.
    Parameters:
    fname_df (str): The file path of the HDF5 file containing the DataFrame.
    cache_path (str): The directory path where the cached DataFrame will be stored.
    cond_map (dict): A dictionary mapping condition substrings to their replacements.
    df_target_key (str, optional): The key to access the target DataFrame within the HDF5 file. Default is 'aggregation/cc'.
    Returns:
    pandas.DataFrame: The processed DataFrame, either read from cache or newly processed.
    """

    project_name = os.path.join(
        cache_path,
        "_cache_"+get_prj_name(fname_df)+".h5"
    )

    if os.path.exists(project_name):
        d = pd.read_hdf(project_name, key="dataframe")
    else:
        d = pd.read_hdf(fname_df, key=df_target_key)
        d["cond_ori"] = d["cond"]

        def set_cond(x: str):
            for t in cond_map.keys():
                if x.find(t) > 0:
                    return t
            return x
            # raise ValueError("unknown condition: " + x)
        d["cond"] = d["cond"].apply(set_cond)
        d["phase_str"] = d["day"].apply(
            lambda x: x.split("phase")[1].split("S")[0])
        d["phase"] = d["phase_str"].apply(lambda x: int(x.split("-")[0]))
        d["phase_sub"] = d["phase_str"].apply(lambda x: int(
            0 if len(x.split("-")) == 1 else x.split("-")[1]))
        d["session"] = d["day"].apply(
            lambda x: int(x.split("phase")[1].split("S")[1]))
        d["phase"] = d["phase"]+d["phase_sub"]
        d.to_hdf(project_name, key="dataframe")
    return d
