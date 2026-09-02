from typing import Any
import collections.abc
import json
import yaml
import os
import glob

def deepupdate(dict_base: dict, other: Any) -> dict:
    """
    Deepupdate dictionary
    """
    for k, v in other.items():
        if isinstance(
            v, collections.abc.Mapping
        ) and k in dict_base and isinstance(
            dict_base[k], collections.abc.Mapping
        ):
            deepupdate(dict_base[k], v)
        else:
            dict_base[k] = v
    return dict_base

class PromptColor:
    BLACK = "\033[30m"  # (文字)黒
    RED = "\033[31m"  # (文字)赤
    GREEN = "\033[32m"  # (文字)緑
    YELLOW = "\033[33m"  # (文字)黄
    BLUE = "\033[34m"  # (文字)青
    MAGENTA = "\033[35m"  # (文字)マゼンタ
    CYAN = "\033[36m"  # (文字)シアン
    WHITE = "\033[37m"  # (文字)白
    COLOR_DEFAULT = "\033[39m"  # 文字色をデフォルトに戻す
    BOLD = "\033[1m"  # 太字
    UNDERLINE = "\033[4m"  # 下線
    INVISIBLE = "\033[08m"  # 不可視
    REVERCE = "\033[07m"  # 文字色と背景色を反転
    BG_BLACK = "\033[40m"  # (背景)黒
    BG_RED = "\033[41m"  # (背景)赤
    BG_GREEN = "\033[42m"  # (背景)緑
    BG_YELLOW = "\033[43m"  # (背景)黄
    BG_BLUE = "\033[44m"  # (背景)青
    BG_MAGENTA = "\033[45m"  # (背景)マゼンタ
    BG_CYAN = "\033[46m"  # (背景)シアン0
    BG_WHITE = "\033[47m"  # (背景)白
    BG_DEFAULT = "\033[49m"  # 背景色をデフォルトに戻す
    RESET = "\033[0m"  # 全てリセット


def supports_color(stream: Any = None) -> bool:
    """この出力先に色を付けてよいか。

    付けてはいけない場合に付けると、``←[33m`` のような制御文字がそのまま見えて
    かえって読めなくなる。判定は3つ。

    - ``NO_COLOR`` が設定されていれば付けない (慣習)
    - 端末でない (ファイルへリダイレクト、パイプ) なら付けない
    - Windows のコンソールは既定で制御文字を解釈しないので、解釈を有効にできた
      ときだけ付ける
    """
    import sys

    if os.environ.get("NO_COLOR"):
        return False
    stream = stream if stream is not None else sys.stdout
    try:
        if not stream.isatty():
            return False
    except Exception:
        return False
    if os.name == "nt":
        return _enable_windows_vt(stream)
    return True


def _enable_windows_vt(stream: Any) -> bool:
    """Windows コンソールで ANSI 制御文字の解釈を有効にする。できなければ False。"""
    try:
        import ctypes

        # STD_OUTPUT_HANDLE (-11)
        handle = ctypes.windll.kernel32.GetStdHandle(-11)  # type: ignore[attr-defined]
        mode = ctypes.c_uint32()
        if not ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):  # type: ignore[attr-defined]
            return False
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        if mode.value & ENABLE_VIRTUAL_TERMINAL_PROCESSING:
            return True
        return bool(ctypes.windll.kernel32.SetConsoleMode(  # type: ignore[attr-defined]
            handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING))
    except Exception:
        return False


def colorize(text: str, color: str) -> str:
    """端末が対応しているときだけ色を付ける。対応していなければ素の文字列を返す。"""
    if not color or not supports_color():
        return text
    return f"{color}{text}{PromptColor.RESET}"



class MixedStyleDumper(yaml.Dumper):
    def represent_sequence(self, tag: Any, sequence: Any, flow_style: Any = None) -> Any:
        """
        Custom sequence representer.
        Forces flow style for lists that do not contain dictionaries.
        """
        # Check if any item in the sequence is a dictionary.
        if not any(isinstance(item, dict) for item in sequence):
            # If no dictionaries are found, force flow style (e.g., [item1, item2]).
            flow_style = True
        return super().represent_sequence(tag, sequence, flow_style)


def convert_json_to_yaml(json_fname: str) -> str:
    """
    Converts a JSON formatted string to a YAML formatted string.

    Args:
        json_string: A string containing data in JSON format.

    Returns:
        A string with the data converted to YAML format.
        Returns an error message string if the JSON is invalid.
    """
    try:
        # Step 1: Parse the JSON string into a Python dictionary
        with open(json_fname) as f:
            python_dict = json.load(f)

        # Step 2: Dump the Python dictionary into a YAML formatted string
        # `default_flow_style=False` makes it block style, which is more readable
        yaml_string = yaml.dump(
            python_dict,
            sort_keys=False,
            allow_unicode=True,
            width=4096,
            Dumper=MixedStyleDumper,
        )

        return yaml_string
    except json.JSONDecodeError:
        return "Error: Invalid JSON string provided."
    except Exception as e:
        return f"An unexpected error occurred: {e}"
