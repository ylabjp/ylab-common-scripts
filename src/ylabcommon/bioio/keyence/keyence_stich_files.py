def stitch_tiles(tile_arrays):

    aligned_tiles = []

    for tile in tile_arrays:

        tx = tile.attrs["X"]
        ty = tile.attrs["Y"]

        tile = tile.assign_coords({
            "X": tile.coords["X"] + tx,
            "Y": tile.coords["Y"] + ty
        })

        aligned_tiles.append(tile)

    stitched = xr.combine_by_coords(aligned_tiles)

    return stitched

