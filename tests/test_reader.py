import numpy as np
from ylabcommon.bioio.core.bioio_reader import BioIOReader

def test_reader_loads_file():

    data = np.zeros((1,1,5,64,64), dtype=np.uint16)

    reader = BioIOReader(data)

    result = reader.read()

    assert result is not None
  
    BLUE = '\033[94m'
    print(f"\n\n{BLUE}[INFORMATION]: Verified reader\n")

def test_reader_shape():

    data = np.zeros((1,1,5,64,64), dtype=np.uint16)

    reader = BioIOReader(data)

    result = reader.read()

    assert result.shape == (1,1,5,64,64)

    BLUE = '\033[94m'
    print(f"\n\n{BLUE}[INFORMATION]: Verified shape from reader\n")

def test_reader_dimensions():

    data = np.zeros((1,1,5,64,64), dtype=np.uint16)

    reader = BioIOReader(data)

    result = reader.read()

    assert len(result.shape) == 5

    BLUE = '\033[94m'
    print(f"\n\n{BLUE}[INFORMATION]: Verified dimension from reader\n")

