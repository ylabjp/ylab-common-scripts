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

def standard_arg_parser() -> ArgModel:
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
    args = parser.parse_args()
    # print(vars(args))
    return ArgModel(**vars(args))
