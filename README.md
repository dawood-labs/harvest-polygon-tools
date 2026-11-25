# harvest-polygon-tools

Two small Python tools that turn a classified raster into harvest-monitoring polygons.
They split the work into **remaining cane (81)** and **harvested (82)**.

## What the two tools do

```
classified raster + field layer (L1)
              |
              v
   Tool 1: preprocess_classified.py
   (remaining-cane polygons, class 81)
              |
              v
   Check and edit in QGIS (by hand)
              |
              v
   Tool 2: postprocess_remaining.py
              |
              v
   Remaining cane 81  +  Harvested 82
```

- **Tool 1** reads a classified raster (1 = cane) and the field layer L1.
  It makes cane polygons with their area in acres.
- You check these polygons in QGIS and fix them by hand if needed.
- **Tool 2** takes your checked layer and L1. It writes the final layer with class 81 and class 82.
  It also checks that 81 + 82 equals the area of L1.

L1 means the area that had cane before this report.

## Why they exist

Analysts used two ArcMap Model Builder models for every report.
Together they had 41 tool boxes (13 + 28) that someone set up and ran by hand.
These tools do the same work with one command each. They are faster and give the same result on every machine.

## Install

1. Python 3.10 or newer.
2. One command:

```
pip install numpy rasterio shapely pyogrio pyproj
```

or `pip install -r requirements.txt`. Shapely must be 2.0 or newer.

3. Optional: `pip install psutil`. The tools then read free memory more exactly.
4. Download both `.py` files into the **same folder**.
   Tool 2 imports Tool 1 from its own folder.

Test the install. You must see a version number and no error:

```
python -c "import numpy, rasterio, shapely, pyogrio, pyproj; print(shapely.__version__)"
```

## Quick start

### Question mode (easiest)

Run a tool with no arguments. It asks its questions. Enter keeps the value in [brackets].

```
python preprocess_classified.py
```

Tool 1 asks, for example:

```
1) Classified raster (.tif):
2) L1 layer (.shp / .gpkg):
3) Output folder [...]:
4) Apply the majority filter? [Y/n]:
6) Remove slivers? [Y/n]:
Start? [Y/n]:
```

Read the summary (`Your answers:`) before you press Enter at `Start?`.
Then check the result in QGIS, save your edited layer, and run Tool 2:

```
python postprocess_remaining.py
```

Tool 2 asks, for example:

```
1) Checked remaining-cane layer (FINAL, .shp / .gpkg):
2) L1 layer (.shp / .gpkg):
3) Output folder [...]:
6) Classes to write: both, 81 (remaining) or 82 (harvested) [both]:
Start? [Y/n]:
```

Use the same L1 and the same UTM zone in both tools.

### Flag mode (no questions)

```
python preprocess_classified.py --raster classified.tif --l1 L1.shp --out remaining_cane.shp --min-acres 0.2 --utm 47N --neighbors 4
python postprocess_remaining.py --final checked.shp --l1 L1.shp --out final.shp --sliver-acres 0.2 --utm 47N
```

Run `python <tool>.py --help` to see every flag.

## Inputs and outputs

| Tool | Input | Output |
|---|---|---|
| Tool 1 | classified raster (cane value 1 by default, change with `--cane-value`), L1 layer (.shp or .gpkg) | cane polygons, class 81 |
| Tool 2 | your checked remaining-cane layer, the same L1 | polygons of class 81 and 82 |

- Output fields: `predicted` (class code) and `area_ac` (area in acres, 3 decimals).
- Output coordinate system: EPSG:4326 (WGS 84).
- Class codes: `81` = Remaining cane, `82` = Harvested.
- Output format: shapefile (.shp) or GeoPackage (.gpkg). A shapefile cannot be bigger than 2 GB, so use .gpkg for very big results.
- Tool 2 prints an area check. `OK` means 81 + 82 equals the area of L1.

## What happens when you run it again

The tool finds the earlier run and asks: `Continue the last run with these settings? [Y/n]:`

- **Continue** after a stop, a power cut or Ctrl+C: press Enter. Finished tiles are reused.
- **Change one late setting** (sliver size, projection, classes): answer `n`, change that one value.
  Only the last step runs again, in seconds.
- **New output name**: answer `n` and type the new name. The saved work of the earlier run is reused
  (`Reusing saved work from ...`).
- **Changed the filter** (Tool 1): the tiles must be computed again. The tool asks before it does.
- **Changed an input file**: the tool sees it and recomputes what it must.
- **Start fresh**: delete the `<output name>_work` folder, then run.

## How it works

- The raster is cut into tiles. Each tile is done on its own and saved (a checkpoint), so a stopped run continues.
- The tool reads your cores and free memory first.
  It plans how many processes and threads to use (processes x threads = tiles at the same time) and the tile size.
- Tool 2 cuts the L1 polygons into chunks in the same way.
- Area is calculated in one UTM zone that you choose (the tool suggests one). Use the same zone for every report.
- The result is the same on every machine and for every tile layout. A brute-force test checks this.
- The tool writes the output under a temporary name and swaps it in at the end.

## Verified results

Test area: a sugarcane area in Kenya. Raster 6,775 x 6,835 cells at 10 m. Field layer: 44,921 polygons, 40,345 acres.

- Tool 1 gives 28,246 polygons, the same as the existing step-by-step script. Exactly 1 polygon differs, by 0.007 acre.
  The cause is a raster-edge rule of the majority filter.
- Tool 2, run on a real final file of the old workflow, reproduces it: 36,088.453 vs 36,088.452 acres. Shapes are equal to about 0.01 acre.
- Remaining + harvested equals the L1 area (40,345.122 acres).
- Speed: the step-by-step script took 192.6 s. Tool 1 takes about 6 s on a 16-core server and 13.8 s on a 12-core laptop with under 3 GB of free memory.
  Tool 2 takes 31.4 s on that laptop (about 15 s on the server).
- 79 automated tests.

## Known limits

- The 8-neighbour majority filter and the raster-edge rule follow the Esri documentation.
  They were not compared with an ArcMap output.
- With very little free memory (under about 2 GB) the tools can use somewhat more than planned.
- The tools were checked on one test area.

## Running the tests

```
pip install pytest
python -m pytest -q tests/
```

The tests take 2 to 3 minutes.

## More help

Step-by-step guide for analysts, with every question and message: [docs/SOP.md](docs/SOP.md).

## License

MIT. See [LICENSE](LICENSE).
