import collections.abc
import json
import yaml
import os
import glob

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


class MixedStyleDumper(yaml.Dumper):
    def represent_sequence(self, tag, sequence, flow_style=None):
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
