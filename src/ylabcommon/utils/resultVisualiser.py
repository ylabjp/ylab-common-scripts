import matplotlib.pyplot as plt
import numpy as np
import json
from pathlib import Path

class ResultVisualizer:
    def __init__(self, output_dir, experiment_name):
        self.output_dir = Path(output_dir)
        self.experiment_name = experiment_name

    def create_preview(self, img_obj, pixel_size_xy):
        """Generates a MIP with a 50µm scale bar."""
        # TCZYX access: Max project over Z (axis 2)
        # Using .compute() if it's a dask array from BioIO
        data = np.asarray(img_obj.data[0, 0, :, :, :])
        mip = np.max(data, axis=0)
        
        fig, ax = plt.subplots(figsize=(8, 8))
        # Use 'magma' for better dynamic range visibility than grayscale
        im = ax.imshow(mip, cmap='magma')
        
        # Scale bar calculation (50um)
        bar_um = 50
        bar_px = bar_um / pixel_size_xy
        
        # Positioning bar in bottom right
        pad = 20
        rect = plt.Rectangle((mip.shape[1] - bar_px - pad, mip.shape[0] - pad - 10), 
                             bar_px, 7, color='white')
        ax.add_patch(rect)
        ax.text(mip.shape[1] - (bar_px/2) - pad, mip.shape[0] - pad - 15, 
                f"{bar_um} µm", color='white', ha='center', weight='bold')
        
        ax.set_title(f"Preview: {self.experiment_name}")
        ax.axis('off')
        
        preview_path = self.output_dir / f"{self.experiment_name}_preview.png"
        plt.savefig(preview_path, bbox_inches='tight', dpi=150)
        plt.close()
        print(f"Preview saved: {preview_path}")
