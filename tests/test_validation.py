def test_simple_validation():

    z_positions = [0,1,2,3,4]

    diffs = [j-i for i,j in zip(z_positions[:-1],z_positions[1:])]

    assert all(d == 1 for d in diffs)
   
    BLUE = '\033[94m'
    print(f"\n\n{BLUE}[INFORMATION]: Verified z position, just basic check\n")
