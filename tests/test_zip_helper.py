import zipfile


def test_zip_creation(tmp_path):

    zip_file = tmp_path / "test.zip"

    with zipfile.ZipFile(zip_file,"w") as z:

        file = tmp_path / "file.txt"
        file.write_text("test")

        z.write(file,"file.txt")

    assert zip_file.exists()
  
    BLUE = '\033[94m'
    print(f"\n\n{BLUE}[INFORMATION]: Verified zip writer, just basic check\n")
