import numpy as np


def test_numpy_stack():

    arrs = [np.zeros((10,10)) for _ in range(5)]

    stack = np.stack(arrs)

    assert stack.shape == (5,10,10)
    BLUE = '\033[94m'
    print(f"\n\n{BLUE}[INFORMATION]: Verified numpy stack, just basic check\n")
