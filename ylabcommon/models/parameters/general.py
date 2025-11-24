import argparse
from pydantic import BaseModel, Field

class ArgModel(BaseModel):
    overwrite: bool = Field(False)

def standard_arg_parser() -> ArgModel:
    parser = argparse.ArgumentParser(description='Standard parser')
    parser.add_argument(
        '-o',
        '--overwrite',
        action='store_true',
        default=False,
        help='Overwrite existing analysis results'
    )
    args = parser.parse_args()
    # print(vars(args))
    return ArgModel(**vars(args))

