from pathlib import Path

import json
from datetime import datetime, UTC, timezone

from ylabcommon.utils.file_selection import collect_valid_tiffs
from ylabcommon.utils.outfile_name import extract_dimensions
from ylabcommon.utils.summary_metadata_helper import get_enhanced_metadata, generate_file_sha256
from ylabcommon.utils.utils import hybrid, style_print
from ylabcommon.bioio.core.bioio_reader import BioIOReader
from ylabcommon.bioio.core.bioio_writer import BioIOWriter
from ylabcommon.bioio.thorlab.thorlab_metadata_extractor import ThorlabMetadataExtractor
from ylabcommon.bioio.thorlab.thorlab_params_adapter import ThorlabParamsAdapter
from ylabcommon.bioio.thorlab.thorlab_bioio_stack_builder import stack_thorlab_with_bioio_calibrated, get_channel_names_index

from ylabcommon.bioio.thorlab.xml_parser import ExperimentXMLParser

## Main script for loading the files


class ThorlabBioioBuilder:
    """
    Full reconstruction pipeline:

    TIFF discovery → Ultra stacking → BioIOReader →
    Metadata extraction → XML validation → Output naming → Write OME
    """

    def __init__(
        self,
        tiff_dir: Path,
        *,
        compression: str = "zlib",
        compression_level: int = 6,
        validate_metadata: bool = True,
        dry_run: str = False,
    ):

        self.tiff_dir = Path(tiff_dir)
        self.xml_file = self.tiff_dir/"Experiment.xml"
        self.dry_run = dry_run

        self.compression = compression
        self.compression_level = compression_level
        self.validate_metadata = validate_metadata

        self.stacked_data=None
        self.image_meta=None
        self._params_cache=None

    # -------------------------------------------------
    # TIFF DISCOVERY + STACK
    # -------------------------------------------------

    def _get_params(self):
        # Parse Experiment.xml once and reuse; the adapter was previously invoked
        # separately in _discover_and_stack and _load_with_bioio, re-parsing the
        # same file each time.
        if self._params_cache is None:
            params_adapter = ThorlabParamsAdapter(self.xml_file)
            self._params_cache = params_adapter.extract()
        return self._params_cache

    def _discover_and_stack(self):

        print("[Builder] Discovering valid TIFF files...")

        tiff_files = collect_valid_tiffs(self.tiff_dir)

        if not tiff_files:
            raise RuntimeError("No valid TIFF files found.")

        print(f"[Builder] Found {len(tiff_files)} usable TIFF files")
        print("[Builder] Ultra stacking images...")
        
        get_thorlabs_params = self._get_params()
        stacked_data, tiff_files = stack_thorlab_with_bioio_calibrated(tiff_files, self.xml_file, get_thorlabs_params)

        total_depth_um = stacked_data.Z.max().values
        print(f"Total volume depth: {total_depth_um} microns")

        # Return the LAZY (dask-backed) stack. Do NOT call .data here — that would
        # materialize the entire volume in RAM. Pixels are read exactly once,
        # streamed to disk, at write time.
        return stacked_data, tiff_files

    # -------------------------------------------------
    # BioIO Processing Reader
    # -------------------------------------------------

    def _load_with_bioio(self, stacked_data):

        print("[Builder] Loading stacked data via BioIOReader...")

        reader = BioIOReader(stacked_data)

        # Metadata only — do NOT call reader.read()/.data here; that would decode
        # the whole volume. All values below come from headers/params.
        params = self._get_params()
        #params = get_thorlabs_params(self.xml_file)
        dx = params.get("PixelSizeX", 1.0)
        dy = params.get("PixelSizeY", dx)
        dz = params.get("PixelSizeZ", 1.0)
        channel_names_str = params.get("ChannelNames")
        current_pixel_size = (dz, dy, dx) # The (Z, Y, X) tuple
        channel_names_index = get_channel_names_index(self.xml_file)

        extractor = ThorlabMetadataExtractor(
            reader, 
            pixel_size_tuple = current_pixel_size,
            channel_names_index = channel_names_index
        )
        hybrid_channel_name = hybrid(channel_names_index, channel_names_str)
        image_meta = extractor.extract()
        image_meta.dim_order = "TCZYX"

        print(f"Data shape: {reader.get_shape()}")
        print(f"Dimension order from reader: {reader.get_dim_order()}")
        print(f"Shape from image meta: {image_meta.shape}")

        # Pass the lazy stack straight through — still unread.
        return stacked_data, image_meta, hybrid_channel_name

    # -------------------------------------------------
    # XML Validation
    # -------------------------------------------------

    def _validate(self, xml_meta, image_meta):

        print("[Builder] Validating XML <-> BioIO metadata...")

        report = {"status": "PASS", "checks": []}

        def record(name, ok, detail):
            report["checks"].append(
                {"check": name, "ok": ok, "detail": detail}
            )
            if not ok:
                report["status"] = "NOT VALIDATED"

        if xml_meta:

            # spatial size validation
            record("SizeX",
                   xml_meta["SizeX"] == image_meta.shape[-1],
                   f"xml={xml_meta['SizeX']} image={image_meta.shape[-1]}")

            record("SizeY",
                   xml_meta["SizeY"] == image_meta.shape[-2],
                   f"xml={xml_meta['SizeY']} image={image_meta.shape[-2]}")

            # Z depth
            record("SizeZ",
                   xml_meta["SizeZ"] == image_meta.shape[2],
                   f"xml={xml_meta['SizeZ']} image={image_meta.shape[2]}")
        #Pixel calibratio
        if xml_meta["PixelSizeX"] and image_meta.pixel_size:

            diff = abs(xml_meta["PixelSizeX"] - image_meta.pixel_size[2])

            record("PixelSizeX",
                   diff < 1e-3,
                   f"Δ={diff}")
        #Channel count
        record("Channels",
               len(xml_meta["Channels"]) == image_meta.shape[1],
               "channel count")

        print(f"[Builder] Validation status: {report['status']}")
        return report

    def _validate_thorlab_stack(self, xml_meta, image_meta):
        """
        image_meta: The BioImage object (or a custom wrapper) of the stacked data
        xml_meta: Dictionary parsed from Experiment.xml
        """
        report = {"status": "VALIDATED", "checks": []}

        def record(name, ok, msg):
            report["checks"].append({"name": name, "ok": ok, "msg": msg})
            if not ok:
                report["status"] = "NOT VALIDATED"

        if xml_meta:
            #spatial size validation - use size_x, size_y, size_z
            record("SizeX",
               xml_meta["SizeX"] == image_meta.size_x,
               f"xml={xml_meta['SizeX']} image={image_meta.size_x}")

            record("SizeY",
               xml_meta["SizeY"] == image_meta.size_y,
               f"xml={xml_meta['SizeY']} image={image_meta.size_y}")

            #Z depth
            record("SizeZ",
               xml_meta["SizeZ"] == image_meta.size_z,
               f"xml={xml_meta['SizeZ']} image={image_meta.size_z}")

            # Pixel calibration
            if xml_meta.get("pixel_size") and image_meta.pixel_size:
                # index 2 is X in your (Z, Y, X) tuple
                diff = abs(xml_meta["PixelSizeX"] - image_meta.pixel_size[2])
                record("PixelSizeX", diff_x < 1e-4, f"Δ={diff_x:.6f}")

                #Volume/Time Depth Validation
                if xml_meta.get("ZStackEnabled") and xml_meta.get("PixelSizeZ"):
                    diff_z = abs(xml_meta["PixelSizeZ"] - image_meta.pixel_size[0])
                    record("PixelSizeZ", diff_z < 1e-4, f"Δ={diff_z:.6f}")
            else:
                record("SizeT", 
                xml_meta["SizeT"] == image_meta.size_t, 
                f"xml={xml_meta['SizeT']} img={image_meta.size_t}")

            #Channel Count
            xml_chan_count = len(xml_meta.get("Channels", []))
            record("Channels", 
                xml_chan_count == image_meta.size_c, 
                f"xml={xml_chan_count} img={image_meta.size_c}"), 

        style_print("\n========== Validation Results ================", "header")

        for check in report["checks"]:
            status = "PASS" if check["ok"] else "Some Parameters Not validated"
            print(f"[{status}] {check['name']}: {check['msg']}")
        print(f"Final Status: {report['status']}")
        print("=============================================\n")
        return report

    # -------------------------------------------------
    # WRITE OUTPUT
    # ---------------------------------------   ----------
    
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

        # self.stacked_data is a lazy, dask-backed xarray. Hand the underlying
        # dask array to the writer so the pixels are read from disk exactly once
        # and streamed straight into the OME-TIFF (a single HDD->HDD pass).
        # save_zarr=False: writing OME-Zarr too would compute the stack a second
        # time and re-read every source TIFF.
        writer.write(
            self.stacked_data.data,
            dim_order=self.image_meta.dim_order,
            channel_names=None,
            #physical_pixel_sizes=phys_sizes,
            physical_pixel_sizes=self.image_meta.pixel_size,
            save_zarr=False,
        )
    # -------------------------------------------------
    # Validation report
    # -------------------------------------------------

    def _write_report(self, report, image_meta, output_path, hybrid_channel_name, tiff_files):

        report_path = output_path.with_suffix(".validation.json")
        extra_meta_summary = get_enhanced_metadata(image_meta, tiff_files)

        payload = {
            #"timestamp": datetime.datetime.now(datetime.UTC),
            #"timestamp": datetime.utcnow().isoformat(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **extra_meta_summary,
            "source_tiff_dir": str(self.tiff_dir),
            "experiment_xml": str(self.xml_file) if self.xml_file else None,
            "Channel_name_hybrid_index_str": hybrid_channel_name, 
            "image_metadata": image_meta.to_dict(),
            "validation": report,
            "software": "ylabcommon + thorlab_loader + bioio backend",
        }

        #payload.update(extra_meta_summary)

        for i in (tiff_files): 
            if i == 0:  # Only do this for the first file to save time
                first_file_hash = generate_file_sha256(output_path)
                payload["integrity_check"] = {"first_file_sha256": first_file_hash}

        with open(report_path, "w") as f:
            json.dump(payload, f, indent=2)

        print(f"[Builder] Validation report → {report_path}")

    # -------------------------------------------------
    # MAIN PIPELINE
    # -------------------------------------------------

    def build(self):
        print("=============================================================================")
        print("[Builder] Starting BioIO reconstruction pipeline")

        stacked_data, tiff_files = self._discover_and_stack()

        data, image_meta, hybrid_channel_name  = self._load_with_bioio(stacked_data)

        xml_meta = None

        if self.validate_metadata and self.xml_file:
            xml = ExperimentXMLParser(self.xml_file)
            xml_meta = xml.extract_metadata()

        report = self._validate_thorlab_stack(xml_meta, image_meta)
        
        image_name, dims = extract_dimensions(tiff_files)
        
        if self.dry_run:
            style_print("[DRY RUN ENABLED]", "info")
            print("[Validating] TIFF discovery successful")
            print("[Validating] BioIO stacking successful")
            print("[Validating]  Metadata extraction successful")
            print(f"[Validating] Validation status: {report['status']}")
            print("[Skipping] file writing")
            print("[Skipping] summary JSON writing")
            print("\n    EXECUTION SUMMARY    \n")
            print(f"Input TIFF count : {len(tiff_files)}")
            print(f"Stack shape      : {data.shape}")
            print(f"Pixel size (µm)  : {image_meta.pixel_size}")

            print("\nDry run completed successfully.\n")
            return

        self.stacked_data=stacked_data
        self.image_meta=image_meta

        #===============================================================
        #Write summary report 
        #===============================================================

        # summary_report = ReportBuilder()

        # # dataset information
        # summary_report.collect_dataset(
        #     str(self.tiff_dir),
        #     "Thorlab",
        #     len(tiff_files)
        # )

        # # experiment XML
        # summary_report.add_section(
        #     "experiment_files",
        #     {
        #         "experiment_xml": str(self.xml_file)
        #      }
        # )

        # # hybrid channel names
        # summary_report.add_section(
        #     "thorlab_channels",
        #     {
        #          "Channel_name_hybrid_index_str": hybrid_channel_name
        #     }
        # )

        # # metadata from stacked TIFF
        # summary_report.add_section(
        #     "image_metadata",
        #     image_meta
        # )

        # # dimensions detected from filenames
        # summary_report.set_dimensions(dims)

        # # stack metadata (shape, dtype, pixel sizes etc.)
        # summary_report.collect_metadata(image_meta, stacked_data)

        # # output information
        # summary_report.set_output(self.output_dir, output_filename)

        # # validation
        # summary_report.finalize_validation()

        # # write report
        # summary_report.write(self.output_dir, output_filename)

        print("[Builder] DONE.")
