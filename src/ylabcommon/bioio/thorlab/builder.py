from pathlib import Path

import warnings

from ylabcommon.utils.file_selection import collect_valid_tiffs
from ylabcommon.utils.utils import style_print
from ylabcommon.utils.perf import timed_step
from ylabcommon.bioio.core.bioio_writer import BioIOWriter
from ylabcommon.bioio.thorlab.thorlab_metadata_extractor import ThorlabMetadataExtractor
from ylabcommon.bioio.thorlab.thorlab_bioio_stack_builder import stack_thorlab_with_bioio_calibrated

from ylabcommon.bioio.thorlab.xml_parser import ExperimentXMLParser

## Main script for loading the files


def _check_size_t_tolerant(xml_size_t, image_size_t):
    """XML が宣言する SizeT と実データの T(時点数)を比較する。

    タイムラプス取得は途中で停止されることがあり、その場合 XML の指定より少ない時点数
    しか保存されない。この「短い T」は構造の破綻ではなく取得の打ち切りなので許容する
    (Warning を出して OK 扱い)。一方、実データの T が XML より多い場合は XML と構造が
    一致しない異常なので NG とする。

    Args:
        xml_size_t: XML(Experiment.xml)が示す時点数。None なら検証しない。
        image_size_t: 実際に読み込めた時点数。

    Returns:
        tuple[bool, str | None, str]:
            (ok, warning_message, detail)
            ok: 検証を通す(True)か否か。短いTなら True。
            warning_message: 短いTのとき出す警告文(なければ None)。
            detail: レポート用の内訳文字列。
    """
    if xml_size_t is None or image_size_t is None:
        return True, None, f"xml={xml_size_t} img={image_size_t} (skipped)"
    if image_size_t == xml_size_t:
        return True, None, f"xml={xml_size_t} img={image_size_t}"
    if image_size_t < xml_size_t:
        msg = (
            f"[thorlab] 取得された時点数 T={image_size_t} が XML 指定の SizeT={xml_size_t} より"
            f"少ないです。タイムラプス取得が途中で終了した可能性があります。"
            f"短い T を許容し、実データの T={image_size_t} で続行します。"
        )
        return True, msg, f"xml={xml_size_t} img={image_size_t} (short T tolerated)"
    # 実データの方が多い = XML と構造が一致しない
    return False, None, f"xml={xml_size_t} img={image_size_t} (more timepoints than XML)"


class ThorlabBioioBuilder:
    """
    Full reconstruction pipeline:

    TIFF discovery → lazy (dask) stacking → metadata from Experiment.xml →
    XML validation → Write OME

    画素の読み取りは書き出し (または解析側の compute) の1回だけで、この class 自体は
    1バイトも画素を読まない。入力側は Experiment.xml とファイル名、それに TIFF の
    ヘッダ1枚で完全に決まるため bioio は経由せず、bioio は出力 (OME) にだけ使う。
    """

    def __init__(
        self,
        tiff_dir: Path,
        *,
        compression: str = "zlib",
        compression_level: int = 6,
        validate_metadata: bool = True,
        dry_run: bool = False,
    ):

        self.tiff_dir = Path(tiff_dir)
        self.xml_file = self.tiff_dir/"Experiment.xml"
        self.dry_run = dry_run

        self.compression = compression
        self.compression_level = compression_level
        self.validate_metadata = validate_metadata

        self.stacked_data=None
        self.image_meta=None
        self._xml_cache=None
        self._params_cache=None

    # -------------------------------------------------
    # TIFF DISCOVERY + STACK
    # -------------------------------------------------

    def _get_xml(self) -> ExperimentXMLParser:
        """Experiment.xml を1回だけ開いて使い回す。

        以前は同じファイルを3回開いていた (params アダプタ / チャンネル名の取得 /
        検証用のパーサ)。取り出す値は大半が重複していたので、パーサを1つに集約した。
        """
        if self._xml_cache is None:
            self._xml_cache = ExperimentXMLParser(self.xml_file)
        return self._xml_cache

    def _get_params(self):
        if self._params_cache is None:
            self._params_cache = self._get_xml().as_params()
        return self._params_cache

    def _discover_and_stack(self):

        print("[Builder] Discovering valid TIFF files...")

        # ディレクトリ一覧の取得。ネットワークドライブが応答しないと、まだ1枚も
        # 開いていない段階でここが止まる。段階を分けておくと切り分けられる。
        with timed_step("thorlab.discover_tiffs", target=str(self.tiff_dir)):
            tiff_files = collect_valid_tiffs(self.tiff_dir)

        if not tiff_files:
            raise RuntimeError("No valid TIFF files found.")

        print(f"[Builder] Found {len(tiff_files)} usable TIFF files")
        print("[Builder] Ultra stacking images...")

        with timed_step("thorlab.parse_params", target=str(self.xml_file)):
            get_thorlabs_params = self._get_params()

        with timed_step("thorlab.stack", target=str(self.tiff_dir),
                        n_files=len(tiff_files)):
            stacked_data, tiff_files = stack_thorlab_with_bioio_calibrated(tiff_files, self.xml_file, get_thorlabs_params)

        # stacked_data is a lazy dask array (TCZYX). Derive depth from the XML
        # params + slice count, not from the pixels.
        nz = stacked_data.shape[2]
        dz = get_thorlabs_params.get("PixelSizeZ", 1.0)
        print(f"Total volume depth: {dz * max(nz - 1, 0)} microns")

        # Return the LAZY (dask-backed) stack. Pixels are read exactly once,
        # streamed to disk, at write time.
        return stacked_data, tiff_files

    # -------------------------------------------------
    # Metadata (XML only — no pixels, no BioImage)
    # -------------------------------------------------

    def _build_image_metadata(self, stacked_data):

        print("[Builder] Building image metadata from Experiment.xml...")

        # shape は自分たちで組み立てた遅延スタックが持っている。それ以外 (物理ピクセル
        # サイズ・チャンネル名・撮影日時・対物レンズ) は Experiment.xml が持っている。
        # どちらも画素を読まずに分かるので、ここでは1バイトも読まない。
        image_meta = ThorlabMetadataExtractor(stacked_data, self._get_params()).extract()

        print(f"Data shape: {image_meta.shape}")
        print(f"Dimension order: {image_meta.dimension_order}")
        print(f"Channel names: {image_meta.channel_names_index}")

        return image_meta

    # -------------------------------------------------
    # XML Validation
    # -------------------------------------------------

    def _validate_thorlab_stack(self, xml_meta, image_meta):
        """XML と実データの食い違いのうち、**まだ誰も報告していないもの** だけを返す。

        以前はここで 7 項目を表で出していたが、検証として働いていたのは 2 つだけだった。

        - ``PixelSizeX`` / ``PixelSizeZ`` は ``image_meta.pixel_size`` が XML の
          params からそのまま作られているため、XML を XML と比べていた。
          ``Δ=0.000000`` 以外になりようがなく、構造的に必ず PASS する。
        - ``SizeX`` / ``SizeY`` / ``SizeZ`` は本物の比較だが、食い違いは
          ``stack_thorlab_with_bioio_calibrated`` が「実データを採用した」と
          添えて既に警告している。ここで再掲しても情報は増えず、しかも
          「Final Status: NOT VALIDATED」という強い語だけが残る
          (Z スタック設定のまま T 連続撮影した取得では毎回そうなる)。

        残るのは、他のどこでも見ていない 2 つ。

        - チャンネル数: XML の Wavelength 数と実際に見つかったチャンネル数
        - 時点数が XML より **多い** 場合: 取得の打ち切りでは説明がつかない構造不一致
          (少ない場合は打ち切りとして許容する。:func:`_check_size_t_tolerant`)

        Returns:
            list[str]: 見つかった問題。空なら何も言うことは無い。
        """
        problems = []
        if not xml_meta:
            return problems

        ok_t, warn_t, detail_t = _check_size_t_tolerant(
            xml_meta.get("SizeT"), image_meta.size_t
        )
        if warn_t:
            warnings.warn(warn_t, stacklevel=2)
        if not ok_t:
            problems.append("SizeT: %s" % detail_t)

        xml_chan_count = len(xml_meta.get("Channels", []))
        if xml_chan_count != image_meta.size_c:
            problems.append(
                "Channels: the XML lists %d wavelength(s) but %d channel(s) were "
                "found in the file names"
                % (xml_chan_count, image_meta.size_c)
            )

        for p in problems:
            warnings.warn("[thorlab] %s" % p, stacklevel=2)
        return problems

    # -------------------------------------------------
    # WRITE OUTPUT
    # -------------------------------------------------

    def write(self,output_path:Path):

        print("[Builder] Writing OME output...")

        if self.stacked_data is None or self.image_meta is None:
            print("[Builder] Nothing to write (no stacked data; dry run?). Skipping.")
            return

        writer = BioIOWriter(
            output_path,
            compression=self.compression,
            compression_level=self.compression_level,
        )

        # Attach channel names when they line up with the image's C axis. If the
        # stack collapsed channels into another axis (len != size_c), write without
        # names rather than let the OME writer raise on the mismatch.
        channel_names = None
        names = getattr(self.image_meta, "channel_names_index", None)
        size_c = self.image_meta.size_c
        if names and size_c and len(names) == size_c:
            channel_names = list(names)
        elif names:
            print(f"[Builder] {len(names)} channel name(s) but image has C={size_c}; "
                  "writing without channel names.")

        # self.stacked_data is a lazy dask array (TCZYX). Hand it to the writer so
        # the pixels are read from disk exactly once and streamed straight into the
        # OME-TIFF (a single HDD->HDD pass). save_zarr=False: writing OME-Zarr too
        # would compute the stack a second time and re-read every source TIFF.
        writer.write(
            self.stacked_data,
            dim_order=self.image_meta.dim_order,
            channel_names=channel_names,
            #physical_pixel_sizes=phys_sizes,
            physical_pixel_sizes=self.image_meta.pixel_size,
            save_zarr=False,
        )

    # -------------------------------------------------
    # MAIN PIPELINE
    # -------------------------------------------------

    def build(self):
        print("=============================================================================")
        print("[Builder] Starting BioIO reconstruction pipeline")

        stacked_data, tiff_files = self._discover_and_stack()

        # 画素は読まずヘッダ/XML だけを見る工程。ここが長い場合は「読み込みが重い」の
        # ではなくメタデータ側の問題なので、画素の工程 (thorlab.stack) と分けて計る。
        with timed_step("thorlab.read_metadata", target=str(self.tiff_dir)):
            image_meta = self._build_image_metadata(stacked_data)

        xml_meta = None

        if self.validate_metadata and self.xml_file:
            # _get_xml() が返すのは _discover_and_stack で既に開いたパーサなので、
            # ここでファイルを読み直すことはない。
            with timed_step("thorlab.parse_xml", target=str(self.xml_file)):
                xml_meta = self._get_xml().extract_metadata()

        problems = self._validate_thorlab_stack(xml_meta, image_meta)

        if self.dry_run:
            style_print("[DRY RUN ENABLED]", "info")
            print("\n    EXECUTION SUMMARY    \n")
            print(f"Input TIFF count : {len(tiff_files)}")
            print(f"Stack shape      : {stacked_data.shape}")
            print(f"Pixel size (µm)  : {image_meta.pixel_size}")
            print(f"XML mismatches   : {'; '.join(problems) if problems else 'none'}")
            print("\nDry run completed successfully (nothing was written).\n")
            return

        self.stacked_data=stacked_data
        self.image_meta=image_meta

        print("[Builder] DONE.")
