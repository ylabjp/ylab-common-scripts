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
    #: ``-m``: re-create the results of these mice only (folder names). Empty = none named.
    overwrite_mice: frozenset[str] = Field(frozenset())
    #: ``--video``: also write the intermediate images of the analysis as videos.
    video: bool = Field(False)


def standard_arg_parser(
    gpu: bool = False,
    mouse: bool = False,
    video: bool = False,
    argv: list[str] | None = None,
) -> ArgModel:
    """Parse the standard command-line arguments.

    The optional arguments are off by default, so that a command which does not
    implement one keeps rejecting it:

    * ``gpu=True`` adds ``--gpu`` (CUDA device index).
    * ``mouse=True`` adds ``-m/--overwrite-mouse MOUSE...``: re-create the
      results of the named mice only. It cannot be combined with ``-o``. Whether
      a name matches a mouse is checked by the command, which knows the tree.
    * ``video=True`` adds ``--video``: write the intermediate images as videos.
      What it does on a day that is already analysed is decided by the command.

    ``argv`` defaults to ``sys.argv[1:]``.
    """
    parser = argparse.ArgumentParser(description='Standard parser')
    which = parser.add_mutually_exclusive_group()
    which.add_argument(
        '-o',
        '--overwrite',
        action='store_true',
        default=False,
        help='Overwrite existing analysis results'
    )
    if mouse:
        which.add_argument(
            "-m", "--overwrite-mouse",
            dest="overwrite_mice",
            nargs="+",
            metavar="MOUSE",
            default=[],
            help="Overwrite existing results only for these mice, named by their folder name "
                 "(e.g. -m ET484 ET485). Other mice are produced only if their output is missing",
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
    if video:
        parser.add_argument(
            "--video",
            action="store_true",
            default=False,
            help="Also write the intermediate images of the analysis as videos",
        )
    args = vars(parser.parse_args(argv))
    if "overwrite_mice" in args:
        args["overwrite_mice"] = frozenset(args["overwrite_mice"])
    return ArgModel(**args)
