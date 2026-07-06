import numpy as np
import rasterio

from ingest.cli import main
from ingest.decoder.level3 import decode_file
from ingest.gridding.aeqd import grid_radial
from ingest.gridding.cog import write_cog

EXPECTED_NAME = {
    "AMX": "AMX_N0B_20260706_154517.tif",
    "JUA": "JUA_N0B_20260706_154347.tif",
}


def test_cog_roundtrip(site, sample_path, tmp_path):
    prod = decode_file(sample_path)
    grid = grid_radial(prod)
    out = write_cog(grid, prod, tmp_path / "out.tif")

    with rasterio.open(out) as ds:
        assert ds.width == ds.height == 3680
        assert ds.dtypes == ("uint8",)
        assert ds.nodata == 0.0

        # CRS AEQD centrada en el radar, geotransform con origen (-460km, +460km).
        proj4 = ds.crs.to_proj4()
        assert "+proj=aeqd" in proj4
        assert f"+lat_0={prod.lat}" in proj4
        assert f"+lon_0={prod.lon}" in proj4
        assert ds.transform.a == 250.0
        assert ds.transform.e == -250.0
        assert ds.transform.c == -460_000.0
        assert ds.transform.f == 460_000.0

        # Estructura COG: tiles + overviews internos.
        assert ds.block_shapes == [(512, 512)]
        assert ds.overviews(1) == [2, 4, 8]

        # Calibración embebida: físico = nivel·scale + offset.
        assert ds.scales == (prod.scale,)
        assert ds.offsets == (prod.offset,)

        tags = ds.tags()
        assert tags["SITE"] == site
        assert tags["PRODUCT"] == "N0B"
        assert tags["PRODUCT_CODE"] == "153"
        assert tags["UNIT"] == "dBZ"
        assert tags["VOL_TIME"] == prod.vol_time.isoformat()

        data = ds.read(1)
        assert np.array_equal(data, grid.data)


def test_cli_process(site, sample_path, tmp_path, capsys):
    assert main(["process", str(sample_path), "-o", str(tmp_path)]) == 0
    out = tmp_path / EXPECTED_NAME[site]
    assert out.exists()
    assert capsys.readouterr().out.strip() == str(out)
    with rasterio.open(out) as ds:
        assert ds.driver == "GTiff"
        assert ds.overviews(1) == [2, 4, 8]
