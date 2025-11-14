import collections.abc


def deepupdate(dict_base: dict, other) -> dict:
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
