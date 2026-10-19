import argparse
from pydantic import BaseModel, Field
from datetime import datetime

class TRANSFER_STATUS:
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"

class TransferLog(BaseModel):
    transfer:str = Field(default=TRANSFER_STATUS.COMPLETE)    
    transfer_timestamp:datetime = Field(default=datetime.now())
    

class ArgModel(BaseModel):
    overwrite: bool = Field(False)
    gpu: int | None = Field(None)

def standard_arg_parser(gpu: bool = False) -> ArgModel:
    """Parse the standard command-line arguments.

    ``gpu=True`` adds ``--gpu`` (CUDA device index). It is off by default so
    that commands which do not use a GPU keep rejecting ``--gpu``.
    """
    parser = argparse.ArgumentParser(description='Standard parser')
    parser.add_argument(
        '-o',
        '--overwrite',
        action='store_true',
        default=False,
        help='Overwrite existing analysis results'
    )
    parser.add_argument(
        "-s", "--subfolder", 
        type=str,
        help="config subfolder",
        default=""
    )
    if gpu:
        parser.add_argument(
            "--gpu",
            type=int,
            default=None,
            help="CUDA device index to use (cupy / CUDA numbering)"
        )
    args = parser.parse_args()
    # print(vars(args))
    return ArgModel(**vars(args))
