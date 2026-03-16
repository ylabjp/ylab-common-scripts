import sys
import platform
import psutil # Optional: to track memory usage during the run
from bioio import __version__ as bioio_ver
import numpy as np
import hashlib

def get_enhanced_metadata(img_obj, tiff_files):
    """
    Extracts hardware, software, and deep image specifics.
    img_obj: A BioImage object from the first file in your list.
    """
    env_info = {
        "python_version": sys.version.split()[0],
        "bioio_version": bioio_ver,
        "os": platform.system(),
        "machine": platform.node()
    }
    '''
    if hasattr(img_obj, "data"):
        dtype = img_obj.data.dtype
    else:
        dtype = "unknown"
    '''
    
    execution = {
        "total_files_found": len(tiff_files),
    }

    return {
        "environment": env_info,
        "execution_details": execution,
        "physical_units": "micrometer" # Standard for Thorlabs/BioIO
    }

def generate_file_sha256(file_path):
    """Generates a SHA-256 hash for a file to ensure data integrity."""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        # Read in chunks to avoid memory issues with large TIFFs
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()
