import json
import platform
import sys
from datetime import datetime
from pathlib import Path
import numpy as np


class ReportBuilder:

    def __init__(self):

        self.report = {
            "timestamp": datetime.utcnow().isoformat(),
            "dataset": {},
            "detected_dimensions": {},
            "physical_metadata": {},
            "stack_statistics": {},
            "output": {},
            "validation": {
                "status": "UNKNOWN",
                "checks": []
            },
            "environment": {
                "python_version": sys.version.split()[0],
                "os": platform.system(),
                "machine": platform.node()
            }
        }

    def collect_dataset(self, input_dir, microscope, total_files):

        self.report["dataset"] = {
            "source_tiff_dir": str(input_dir),
            "microscope": microscope,
            "total_files_found": total_files
        }
    
    def add_section(self, name, data):
        self.report[name] = data

    def collect_metadata(self, image_meta, stacked_data):

        pixel_sizes = getattr(image_meta, "pixel_sizes", None)

        if pixel_sizes:
            self.report["physical_metadata"]["pixel_size_xy_um"] = pixel_sizes[1]

        z_positions = getattr(image_meta, "z_positions", None)

        if z_positions and len(z_positions) > 1:

            diffs = np.diff(z_positions)
            z_spacing = np.median(diffs) / 1000.0

            self.report["physical_metadata"]["z_spacing_um"] = z_spacing

        if hasattr(image_meta, "lens"):
            self.report["physical_metadata"]["objective"] = image_meta.lens

        if hasattr(stacked_data, "shape"):
            self.report["stack_statistics"]["stack_shape"] = tuple(stacked_data.shape)

        if hasattr(stacked_data, "dtype"):
            self.report["stack_statistics"]["dtype"] = str(stacked_data.dtype)

        if hasattr(stacked_data, "nbytes"):
            self.report["stack_statistics"]["size_MB"] = round(stacked_data.nbytes / (1024**2), 2)


    def compress_dims(self, dims):

        clean = {}
        for k, v in dims.items():
            v = sorted(v)
            if len(v) == 1:
                clean[k] = [v[0]]
            elif len(v) == 2:
                clean[k] = [v[0], v[-1]]
            else:
                clean[f"{k}_range"] = [v[0], v[-1]]
                clean[f"{k}_count"] = len(v)
        return clean

    def format_dims(self, dims):
        
        clean = {}
        for k, v in dims.items():
            v = sorted(v)
            if k == "Z":
                clean["Z_range"] = [v[0], v[-1]]
                clean["Z_count"] = len(v)
            elif len(v) == 1:
                clean[k] = [v[0]]
            elif len(v) == 2:
                clean[k] = [v[0], v[-1]]
            else:
                clean[f"{k}_range"] = [v[0], v[-1]]
                clean[f"{k}_count"] = len(v)

        return clean

    def set_dimensions(self, dims):

        self.report["detected_dimensions"] = self.format_dims(dims)

    def add_validation(self, name, ok, value):

        self.report["validation"]["checks"].append({
            "name": name,
            "ok": ok,
            "value": value
        })

    def finalize_validation(self):

        ok = all(c["ok"] for c in self.report["validation"]["checks"])
        self.report["validation"]["status"] = "VALIDATED" if ok else "NOT VALIDATED"

    def set_output(self, output_dir, output_file):

        self.report["output"] = {
            "output_directory": str(output_dir),
            "output_file": output_file
        }

    def _json_converter(self, obj):

        if isinstance(obj, np.integer):
            return int(obj)

        if isinstance(obj, np.floating):
            return float(obj)

        if isinstance(obj, np.ndarray):
            return obj.tolist()

        if isinstance(obj, set):
            return list(obj)

        if isinstance(obj, Path):
            return str(obj)

        return str(obj)

    def compact_small_lists(self, json_text):
        pattern = r"\[\s+(\d+),\s+(\d+)\s+\]"
        return re.sub(pattern, r"[\1, \2]", json_text)

    def write(self, output_dir, output_filename):

        output_dir = Path(output_dir)
        json_file = output_filename.with_suffix(".validation.json")
        txt_file =  output_filename.with_suffix(".report.txt")
       
        json_str = self.report
        with open(json_file, "w") as f:
            json.dump(json_str, f, indent=2, default=self._json_converter)

        with open(txt_file, "w") as f:

            for section, content in self.report.items():

                f.write(f"{section.upper()}\n")
                f.write("\n")
               
                if isinstance(content, dict):
                    for k, v in content.items():
                        f.write(f"{k}: {v}\n")

                elif isinstance(content, list):
                    for item in content:
                        f.write(f"- {item}\n")
                else:
                     f.write(str(content) + "\n")

                f.write("\n")
