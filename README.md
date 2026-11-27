# harvest-polygon-tools

Three small Python tools for harvest monitoring.
Tools 1 and 2 turn a classified raster into polygons of **remaining cane (81)** and **harvested (82)**.
Tool 3 turns one harvest report into an Excel workbook of statistics per boundary unit.

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
  It makes cane polygons with their area (acres by default; hectares, rai, square metres or square kilometres on request).
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
pip install numpy rasterio shapely pyogrio pyproj openpyxl
```

or `pip install -r requirements.txt`. Shapely must be 2.0 or newer.

3. Optional: `pip install psutil`. The tools then read free memory more exactly.
4. Download the `.py` files into the **same folder**.
   Tools 2 and 3 import Tool 1 from their own folder.

Test the install. You must see a version number and no error:

```
python -c "import numpy, rasterio, shapely, pyogrio, pyproj, openpyxl; print(shapely.__version__)"
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
2) L1 layer (.shp / .gpkg / .geojson):
3) Output folder [...]:
   Save as:  1) Shapefile (.shp)   2) GeoPackage (.gpkg)   3) GeoJSON (.geojson)  [1]:
4) Apply the majority filter? [Y/n]:
5) Projection used to calculate areas (detected from the data extent):
6) Unit for areas:
   1) Acres            2) Hectares         3) Rai (Thailand)
   4) Square metres    5) Square kilometres
   Choose a number or type the name [1]:
7) Remove slivers? [Y/n]:
   Minimum area in acres [0.2]:
Start? [Y/n]:
```

The unit answer can be the number, the name or a short form (`ha`, `rai`, `m2`, `sq km`).
A spelling mistake is confirmed first: `Did you mean Hectares? [Y/n]:`.
In another unit the default sliver size stays the same size as 0.2 acres: `Minimum area in rai [0.506 = 0.2 acres]:`.

Read the summary (`Your answers:`) before you press Enter at `Start?`.
Then check the result in QGIS, save your edited layer, and run Tool 2:

```
python postprocess_remaining.py
```

Tool 2 asks, for example:

```
1) Checked remaining-cane layer (FINAL, .shp / .gpkg / .geojson):
2) L1 layer (.shp / .gpkg / .geojson):
3) Output folder [...]:
4) Projection used to calculate areas (detected from the data extent):
5) Unit for areas:
6) Turn small harvested pieces that touch remaining cane into remaining cane? [Y/n]:
   Largest piece size in acres (pieces <= this size change) [0.2]:
Start? [Y/n]:
```

Tool 2 always writes both classes in question mode (`--classes 81` or `82` exists in flag mode).
Use the same L1, the same UTM zone and the same unit in both tools.

### Flag mode (no questions)

```
python preprocess_classified.py --raster classified.tif --l1 L1.shp --out remaining_cane.shp --min-acres 0.2 --utm 47N --neighbors 4
python postprocess_remaining.py --final checked.shp --l1 L1.shp --out final.shp --sliver-acres 0.2 --utm 47N
python postprocess_remaining.py --final checked.gpkg --l1 L1.gpkg --l1-layer fields --out final.geojson --unit hectares --utm 47N
```

`--min-acres` / `--sliver-acres` are always in acres (old scripts keep working).
`--min-area` / `--sliver-area` are in the unit of `--unit`; giving both forms is an error.
Exit codes: 0 = done, 2 = a wrong flag or file, 3 = the file was written but the geometry check found a problem.

Run `python <tool>.py --help` to see every flag.

## Inputs and outputs

| Tool | Input | Output |
|---|---|---|
| Tool 1 | classified raster (cane value 1 by default, change with `--cane-value`), L1 layer (.shp, .gpkg, .geojson or .json) | cane polygons, class 81 |
| Tool 2 | your checked remaining-cane layer, the same L1 (same formats) | polygons of class 81 and 82 |

- Output fields: `predicted` (class code) and ONE area field in the chosen unit:

| Unit | Field | Decimals | Size |
|---|---|---|---|
| acres (default) | `area_ac` | 3 | 4,046.85642 m2 |
| hectares | `area_ha` | 3 | 10,000 m2 |
| rai | `area_rai` | 3 | 1,600 m2 |
| square metres | `area_m2` | 1 | 1 m2 |
| square kilometres | `area_km2` | 6 | 1,000,000 m2 |

- With the default unit the files are byte for byte the same as before units existed.
- Output coordinate system: EPSG:4326 (WGS 84).
- Class codes: `81` = Remaining cane, `82` = Harvested.
- Output format: shapefile (.shp), GeoPackage (.gpkg) or GeoJSON (.geojson / .json). A shapefile cannot be bigger than 2 GB, so use .gpkg for very big results. GeoJSON is about 2.5 times larger than a shapefile; it follows RFC 7946 (no crs member = WGS 84, outer rings counter-clockwise) and keeps every coordinate exactly (no rounding).
- A GeoPackage with several layers: the tools use its only polygon layer; otherwise question mode lists the layers and asks, and flag mode needs `--l1-layer NAME` / `--final-layer NAME`. A GeoJSON file without a crs member is WGS 84.
- The RESULT block shows a `% of area` column (Tool 1: share of the cane inside L1; Tool 2: share of 81 + 82, and the changed small pieces as a share of L1).
- Tool 2 prints an area check. `OK` means 81 + 82 equals the area of L1.

### Geometry guarantee

Every output feature is one valid 2D Polygon: no MultiPolygons, lines, points, null or empty shapes, no self-intersections (GEOS rules), no repeated vertices and no overlapping polygons.
Just before writing, a quality gate repairs or drops what breaks these rules (same class overlaps are merged, between 81 and 82 the 81 wins).
After writing, the tool reads the saved file again and prints a `Geometry check` section; every row must say `OK`.
Dirty inputs are accepted: only their polygon parts are used, and a NOTE line says what was repaired or ignored, for example `NOTE: L1: 595 invalid polygons repaired.`

## What happens when you run it again

The tool finds the earlier run and asks: `Continue the last run with these settings? [Y/n]:`

The memory of the last run is kept in the folder of the script (in a hidden sub-folder `.harvest_tools`), not on
the computer. One folder per area, with its own copy of the two scripts, means its own memory: a copy never
shows or reuses the run of another folder. The first line of the screen says which folder is speaking.
If that folder cannot be written (a read-only share), the old place in your home folder is used and a NOTE says so.
The saved copy of the prepared L1 stays in your home folder and is shared by all folders.

- **Continue** after a stop, a power cut or Ctrl+C: press Enter. Finished tiles are reused.
- **Change one late setting** (sliver size, projection, unit, output format): answer `n`, change that one value.
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
- 100 automated tests.

## Known limits

- The 8-neighbour majority filter and the raster-edge rule follow the Esri documentation.
  They were not compared with an ArcMap output.
- With very little free memory (under about 2 GB) the tools can use somewhat more than planned.
- The tools were checked on one test area.

## Tool 3: harvest_stats_excel.py (statistics workbook)

It reads ONE harvest report (polygons with a class field: 81 remaining, 82 harvested, maybe other codes)
and a boundary layer. It writes an Excel workbook with the areas per boundary unit.
For the next report you give it the workbook of the report before. It carries the history forward
and never computes old reports again. Six reports in a season give six workbooks; each holds
everything up to its date.

```
python harvest_stats_excel.py
```

Questions (Enter keeps the value in [brackets]):

```
1) Boundary layer (.shp / .gpkg / .geojson):
   Fields of units.shp:
   1) name (26 values, e.g. A, B)
   2) name_2 (8 values, e.g. C, D)
2) How many levels do you want statistics for? [1]:
   Field for level 1 (the largest areas):
   Column title for this level [name_2]:
   Field for level 2 (inside level 1):
   Column title for this level [name]:
3) Harvest report layer of this date (.shp / .gpkg / .geojson):
   Class field: predicted
4) Report date (YYYY-MM-DD or DD-MM-YYYY) [2026-01-12]:
5) Workbook of the previous report (Enter = this is the first report):
6) Projection used to calculate areas (detected from the data extent):
   Unit for areas:
7) Output folder [...]:
   Output name (saved as .xlsx) [report_stats]:
Start? [Y/n]:
```

- Levels are the fields you choose, largest unit first. Each distinct combination of their values is one row.
  The field name is used in headers, sheet names and charts; a column title replaces it (Enter keeps the name).
- The class field is `predicted`; without it the tool lists the fields and asks.
- The date comes from the file name (YYYY-MM-DD) when it has one.
- With a previous workbook, the level fields, the unit and the projection must be the same as in it.

Flag mode:

```
python harvest_stats_excel.py --boundary units.shp --levels name_2,name --level-titles "Area,Unit" --report report_2026-01-12.shp --previous report_2025-12-23_stats.xlsx --out report_2026-01-12_stats.xlsx
```

Other flags: `--date`, `--class-field`, `--unit`, `--utm`, `--no-pivot`, `--boundary-layer`, `--report-layer`.

### Columns of the table

| Column | Meaning |
|---|---|
| one column per level | the unit (field values) |
| Total Crop Area | all codes together at the latest report |
| Crop Harvested on DD-MM-YYYY | one per report: harvested (82) in that period only = cumulative 82 at that date minus at the report before (first report: the cumulative 82) |
| `<code>` on DD-MM-YYYY | one per other code of a report (not 81, not 82), right after that report's harvested column; area with that code at that date. Rename the header in Excel if you like; the next workbook keeps your title |
| Total Harvested Crop (date) | cumulative 82 at the latest report |
| Total Remaining Crop (date) | total crop minus harvested: every code other than 82 counts as remaining |
| Total Harvested Crop % / Total Remaining Crop % (date) | share of the total crop; the two add to 100 |

The last row is `Totals` (the percentages are computed from the totals).

### Rules

- Reports must be **cumulative**: every report covers the whole crop area, so each row's total stays the same.
  A change of more than 0.01 % (and more than 0.01 of the unit) gives a WARNING with the rows.
  A report whose total looks like only the previous remaining area (an incremental file) is refused.
- A harvested area that goes down gives a negative period value: it is written as it is, highlighted, with a WARNING.
- Combinations without crop (zero-area slivers of a GIS intersection) are left out; the result says how many.
- Crop outside every boundary polygon is kept in a row `(outside the boundary)` (when more than 0.001 of the unit).
  The table total must equal the report total: the result shows `OK` or `CHECK`.
- Boundary polygons that overlap: the first polygon in the file keeps the overlap, so crop is counted once (NOTE).
- A crop polygon inside one boundary polygon is taken whole; only polygons crossing a boundary edge are cut.
  Dirty shapes are repaired first (NOTE with the counts), as in the other tools.
- The same date cannot be added twice; a date earlier than the previous report is refused.

### The workbook

| Sheet | What it holds |
|---|---|
| Dashboard | title band, five tiles with the key numbers, up to six charts (Excel charts, editable), a summary table of the first level |
| Stats | the table at the finest level, frozen header, filter, Totals row |
| By `<level>` | the same table summed to each coarser level |
| Data | one row per unit, report date and measure (Harvested in period, Remaining at date, Total harvested at date, one per other code) |
| Pivot | a real pivot table on the Stats table: one row per unit of the first level with its lower levels under it; columns = total crop, harvested in each period, total harvested, remaining. The same numbers stay as plain cells with `--no-pivot` |
| Chart data | the numbers the charts read |
| (very hidden) | a machine-readable copy of all numbers; the next run reads only this. Edit the visible sheets freely |

### Limits

- If Excel ever refuses the pivot table, run again with `--no-pivot`: the sheet keeps the same numbers as plain cells.
- Not tested in LibreOffice or with very large boundary layers (many thousands of units).

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
