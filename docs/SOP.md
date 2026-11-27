# SOP: Harvest monitoring tools

## 1. In short
1. Tool 1 (`preprocess_classified.py`) reads a classified raster (1 = cane) and L1. It makes cane polygons (81).
2. You check and edit these polygons in QGIS.
3. Tool 2 (`postprocess_remaining.py`) makes the final layer: 81 Remaining cane, 82 Harvested. It checks 81 + 82 = L1 area.

Order: raster + L1 -> TOOL 1 -> check in QGIS -> TOOL 2 -> 81 and 82 polygons
4. Tool 3 (`harvest_stats_excel.py`) makes an Excel workbook of areas per boundary unit from one report (part 11).

## 2. Set up once
1. Install Python 3.10 or newer from python.org. Tick "Add Python to PATH".
2. Open "Command Prompt". Run this one command:
```
pip install numpy rasterio shapely pyogrio pyproj openpyxl
```
3. Optional: `pip install psutil`.
4. Test it (you must see a version number, no error):
```
python -c "import numpy, rasterio, shapely, pyogrio, pyproj, openpyxl; print(shapely.__version__)"
```
5. Put ALL .py files in the SAME folder, for example `C:\tools`. Tools 2 and 3 need Tool 1 next to them.
## 3. Before every run
- [ ] Close heavy programs (QGIS projects, many browser tabs). The tool uses almost all free memory.
- [ ] Do not start other heavy work during the run. Charger on. Sleep off.
- [ ] Free disk space on the output drive.
- [ ] The output file is not open in QGIS or ArcMap.

## 4. Tool 1 - make remaining cane
```
cd C:\tools
python preprocess_classified.py
```

Enter keeps the value in [brackets]. Paste paths in any style.
| Question as printed | What you type | Enter gives |
|---|---|---|
| `1) Classified raster (.tif):` | `C:\work\report1\classified.tif` | nothing (first run) |
| `2) L1 layer (.shp / .gpkg / .geojson):` | `D:\data\L1.shp` | nothing (first run) |
| `   Which layer is the L1 [1]:` (only for a .gpkg with several layers) | a number from the list | the first polygon layer |
| `3) Output folder [C:\work\report1]:` | a folder, or a full file path | the raster folder |
| `   Output name [classified_remaining_cane]:` | a name, no extension | raster name + `_remaining_cane` |
| `   Save as:  1) Shapefile (.shp)   2) GeoPackage (.gpkg)   3) GeoJSON (.geojson)  [1]:` | `2` for a very big result | 1 (shapefile) |
| `4) Apply the majority filter? [Y/n]:` | `n` = no filter | yes |
| `   Number of neighbours (4 or 8) [4]:` | `8` | 4 |
| `   Choose number, zone (e.g. 47N) or EPSG code [1]:` | a number, `47N` or `32647` | the suggested zone |
| `6) Unit for areas:` ... `Choose a number or type the name [1]:` | `2`, `hectares`, `ha`, `rai`, `m2`, `km2` | 1 (acres) |
| `7) Remove slivers? [Y/n]:` | `n` = keep all | yes |
| `   Minimum area in acres [0.2]:` | e.g. `0.5` (a big value asks `Use it? [y/N]`) | 0.2 acres |
| `Start? [Y/n]:` | `n` = cancel (`Cancelled.`) | start |

Notes:
- A full file path at the folder question works.
- A bad name asks again: `A file name cannot contain : ? (Windows rule). Spaces, - _ ( ) [ ] are fine.`
- Majority filter = smooths the raster. Slivers = tiny polygons (noise).
- Unit: the list is `1) Acres  2) Hectares  3) Rai (Thailand)  4) Square metres  5) Square kilometres`.
  A spelling mistake asks first: `Did you mean Hectares? [Y/n]:`. Other units show the same size as 0.2 acres:
  `   Minimum area in rai [0.506 = 0.2 acres]:`. The number you type is in your unit.
- Projection: the tool lists every UTM zone the raster touches. Number 1 = centre zone. Areas use the ONE zone you pick.
  On a later run you may see `Enter keeps your earlier choice: UTM 47N (EPSG:32647)`. Enter keeps that zone.
- A non-UTM choice (e.g. 3857) asks `Use WGS 84 / Pseudo-Mercator anyway? [y/N]:`. Press Enter (no).
- A far zone asks `UTM zone 36 is 11 zones away from the data centre (zone 47); areas get less accurate far from the zone centre.` Press Enter (no).
- If the output exists: `report2.shp was made by an earlier run of this tool. Replace it? [Y/n]:`.
Read the summary (`Your answers:`) before you press Enter at `Start?`.

While it runs you see `Machine: ...` and `Plan: ...` lines (processes x threads = tiles done at the same time;
`kept free` = memory left for Windows; `memory mode: desktop` = normal PC).

What you get at the end (shortened, real run):
```
==================================== RESULT ====================================
Remaining cane polygons written :     28,246
Total area                      : 28,731.028 acres

How the result was reached          Polygons       Acres  % of area
  Cane pieces inside L1               42,656  30,069.735      100.0
  Removed: slivers below 0.2 acres    14,364   1,338.707        4.5
  Removed: numerical noise                46       0.000        0.0
  Written to the output               28,246  28,731.028       95.5

Geometry check (the saved file was read back, 0.6 s)
  Single polygons                     28,246          OK
  Invalid (GEOS rules)                     0          OK
  Overlapping pairs                        0          OK
  Repaired before saving                   0
```

- `Written to the output` = what is in your file. Write these numbers in your report.
- `% of area` = share of the first row. Removed + written = 100 % (rounding can give 99.9 or 100.1).
- Big results may add: `Note: adding up the area_ac column of the file gives 28,731.001 acres (each polygon is rounded to 3 decimals).` This is rounding.
- Geometry check: the tool reads the saved file again. Every row must say `OK`. `CHECK` comes with a `WARNING:` line. Send a screenshot.
- Fields: `predicted` (81) and ONE area field: `area_ac` (acres), `area_ha`, `area_rai`, `area_m2` or `area_km2`. Coordinate system: WGS 84.
## 5. Check in QGIS
1. Open the Tool 1 file in QGIS with the raster or an image.
2. Edit by hand if needed. Save the layer as a file. Close it in QGIS.

## 6. Tool 2 - remaining cane and harvested
```
python postprocess_remaining.py
```

| Question as printed | What you type | Enter gives |
|---|---|---|
| `1) Checked remaining-cane layer (FINAL, .shp / .gpkg / .geojson):` | your checked file | nothing (first run) |
| `2) L1 layer (.shp / .gpkg / .geojson):` | the same L1 as in Tool 1 | nothing (first run) |
| `3) Output folder [...]:` | a folder, or a full file path | the checked file folder |
| `   Output name [checked_harvest_final]:` | a name, no extension | checked name + `_harvest_final` |
| `   Save as: ... 3) GeoJSON (.geojson)  [1]:` | `2` = GeoPackage, `3` = GeoJSON | 1 (shapefile) |
| `4) Projection used to calculate areas ...` | as in Tool 1 | the suggested zone |
| `5) Unit for areas:` ... `Choose a number or type the name [1]:` | as in Tool 1 | 1 (acres) |
| `6) Turn small harvested pieces that touch remaining cane into remaining cane? [Y/n]:` | `n` = keep all as 82 | yes |
| `   Largest piece size in hectares (pieces <= this size change) [0.081 = 0.2 acres]:` | e.g. `0.2` | 0.2 acres |
| `Start? [Y/n]:` | `n` = cancel | start |

Tool 2 always writes both classes (81 and 82).

Use the same L1, zone and unit as in Tool 1. The end of the run (shortened, real run):

```
==================================== RESULT ====================================
Class                        Polygons       Acres  % of area
  81  Remaining cane           28,208  31,260.373       77.5
  82  Harvested                19,073   9,084.749       22.5
  Total (81 + 82)              47,281  40,345.122      100.0

Area check (remaining + harvested must equal L1)
  L1 area                              40,345.122
  Difference                                0.000  (0.0000 %)  OK

Small harvested pieces (rule: 0.2 acres or smaller AND touching remaining cane)
  Changed to remaining cane   175,644   2,529.345  (6.3 % of L1)
```

- Area check must say `OK`. If it says `CHECK`, a `WARNING: 81 + 82 differs from the area of L1 ...` line follows. Send a screenshot.
- Small harvested pieces: small 82 pieces that touch 81 become 81. The row shows how many, and their share of L1.
- The `Geometry check` section is the same as in Tool 1.

## 7. Run again
Start the same tool. You see `A previous run was found:` and the old answers.
The first line says which folder the memory belongs to. Each folder with its own copy of the scripts has its own memory.
Then: `Continue the last run with these settings? [Y/n]:`
| You want to ... | Do this | What happens |
|---|---|---|
| Continue after a stop, power cut or Ctrl+C | Enter at `Continue ...` | Finished work is reused: `resuming: 722 of 2209 tiles already done` |
| Same settings, run was finished | Enter at `Continue ...` | `The output ... was already up to date for these settings; nothing was written again.` |
| Change only sliver size, projection, unit or format | `n`, then Enter on all, change one value | Only the last step runs, in seconds: `Setting changed: unit (acres -> hectares). Only the last step runs again.` |
| Change only the output name | `n`, then type the new name | `Reusing saved work from <folder>` |
| Change the filter (Tool 1) | `n`, then change the filter answer | `Setting changed: number of neighbours (4 -> 8). Tiles must be recomputed.` then `Discard them and recompute? [Y/n]:` Enter = yes |
| Raster, L1 or checked file changed | Run again | `The file ... was modified since the last run (...). Tiles must be recomputed.` (Tool 2: `Inputs changed since the saved overlay: ...`) |
| Tool file is newer | Run again | `This work folder was made by an older version of the tool; its saved tiles cannot be reused.` |
| Start completely fresh | Delete the `<output name>_work` folder, then run | Everything is computed again |
After `n`, every question shows your old answer in [brackets].

## 8. Messages
| Message (short) | Meaning | Do this |
|---|---|---|
| `File not found: nofile.tif. Paste the full path of the raster.` | Wrong path | Paste the right path |
| `A file name cannot contain ...` | Bad character in the name | Type the name again |
| `The L1 layer has no coordinate system (.prj missing).` | L1 has no .prj | Fix it in QGIS. Answer `y` only if sure. Enter stops the run |
| `Please answer y or n.` / `Not understood. Type a list number, a zone like 47N, or an EPSG code like 32647.` / `Type a number, e.g. 0.2.` / `Type a number from 1 to 5 or a unit name, e.g. acres, hectares, rai.` | Wrong answer | Answer again |
| `The L1 layer must be a vector file. Use a Shapefile (.shp), a GeoPackage (.gpkg) or a GeoJSON file (.geojson / .json).` | Wrong file type | Paste a .shp, .gpkg or .geojson file |
| `NOTE: L1: 595 invalid polygons repaired.` | The layer had broken shapes; the tool fixed or skipped them | Nothing. Fix the layer in QGIS if the counts are big |
| `WARNING: the saved file has geometry problems the tool could not repair: ...` | Rare. The file is saved anyway | Send a screenshot |
| `ERROR: preprocess_classified.py was not found. Put preprocess_classified.py in the same folder as this file.` | Tool 2 is alone in its folder | Put both .py files together |
| `ERROR: python package 'rasterio' is missing. Install it with: pip install rasterio ...` | Package missing | Run the pip command |
| `ERROR: the output X is open in another program (...)` | File open in QGIS/ArcMap | Close it, or use another name |
| `not enough disk space next to the output: ...` | Disk is full | Press Enter at `Choose another output path?` and pick another drive |
| `tile ... does not fit in memory even when split; ...` | Not enough memory | Close programs. Run again |

## 9. Good to know
- Saved work: the folder `<output name>_work` sits next to each output (size is in the RESULT). Delete it when no run is active; `--clean` does it for you.
- Working on many areas? Make one folder per area and copy both .py files into each. Each folder remembers its own last run.
- The last-run memory is in the hidden folder `.harvest_tools` next to the scripts. If that folder cannot be written, your home folder is used and a NOTE tells you.
- Tool 1 saves a copy of the prepared L1 in your home folder (shared by all areas): `.preprocess_classified\l1_prepared` (max 2 GB, safe to delete).
- Areas are calculated in ONE UTM zone, the one you choose. Use the same zone for every report.
- Units: 1 acre = 4,046.85642 m2, 1 hectare = 10,000 m2, 1 rai = 1,600 m2. Pick one unit and keep it for every report.
- Formats: in and out, .shp, .gpkg and .geojson all work. A .geojson is about 2.5 times bigger than a .shp. A shapefile cannot be bigger than 2 GB: use GeoPackage for very big results.
- To copy the old workflow: answer `n` to the filter and `n` to slivers in Tool 1, then use the 0.2 acre rule in Tool 2 (the default).
  Areas the analyst added by hand come only from your QGIS check.
- The result does not depend on the computer: every machine gives the same polygons and acres.
- Limits: the 8-neighbour filter and the outer row of the raster are not checked against ArcMap. With under about 2 GB free memory, the tool may use a little more than planned.
- If something goes wrong, send a screenshot of the whole window.
## 10. Without questions
```
python preprocess_classified.py --raster C:\work\report1\classified.tif --l1 D:\data\L1.shp --out C:\work\report1\remaining_cane.shp --min-acres 0.2 --utm 47N --neighbors 4
python postprocess_remaining.py --final C:\work\report1\checked.shp --l1 D:\data\L1.shp --out C:\work\report1\final.shp --sliver-acres 0.2 --utm 47N
python postprocess_remaining.py --final C:\work\report1\checked.gpkg --l1 D:\data\L1.gpkg --l1-layer fields --out C:\work\report1\final.geojson --unit rai --utm 47N
```
`--min-acres` / `--sliver-acres` are always acres. `--min-area` / `--sliver-area` are in the `--unit`. Exit code 3 = the geometry check found a problem.

## 11. Tool 3 - statistics workbook
```
python harvest_stats_excel.py
```

| Question as printed | What you type | Enter gives |
|---|---|---|
| `1) Boundary layer (.shp / .gpkg / .geojson):` | your boundary file | the last one |
| (the tool lists the fields: `1) name_2 (8 values, e.g. ..., ...)`) | - | - |
| `2) How many levels do you want statistics for? [1]:` | e.g. `2` | 1 |
| `   Field for level 1 (the largest areas):` | a list number or a field name | the last one |
| `   Column title for this level [name_2]:` | a nicer title, e.g. `Area` | the field name |
| `   Field for level 2 (inside level 1):` | a list number or a field name | the last one |
| `3) Harvest report layer of this date (.shp / .gpkg / .geojson):` | the report | the last one |
| `   Class field:` (only when there is no `predicted`) | a list number or a field name | - |
| `4) Report date (YYYY-MM-DD or DD-MM-YYYY) [2026-01-12]:` | Enter, or the date | the date in the file name |
| `5) Workbook of the previous report (Enter = this is the first report):` | the workbook of the report before | first report |
| `5) Workbook of the previous report (type 0 if this is the first report) [...]:` | Enter = the last workbook; `0` = first report | the last workbook |
| `6) Projection ...` and `Unit for areas:` | as in Tools 1 and 2 | the previous workbook's (must stay the same) |
| `7) Output folder [...]:` / `   Output name (saved as .xlsx) [...]:` | a folder / a name | report folder / report name + `_stats` |
| `Start? [Y/n]:` | `n` = cancel | start |

Next report: run Tool 3 again with the new report and give the workbook of the report before at question 5.
The new workbook holds all reports so far. Keep the same boundary fields, unit and projection all season.

What the workbook holds:

| Sheet | What it holds |
|---|---|
| Dashboard | key numbers in tiles (with harvest pace and an estimated finish date), key findings as text, charts, a progress table |
| Stats | one row per unit; `Crop Harvested on DD-MM-YYYY` = harvested in that period only; `Totals` row |
| Pivot | pivot table: one row per unit, columns = total crop and harvested in each period (tick more fields in the list on the right) |
| Chart data (hidden) | the numbers of the charts |

- Reports must be cumulative (each covers the whole crop area). 81 + other codes = remaining, 82 = harvested.
- A code other than 81 and 82 gets its own column, e.g. `282 on 12-01-2026`. You may rename it; the next workbook keeps your title.
- You may change titles, widths and notes in the visible sheets. Do not delete sheets.

Messages:

| Message (short) | Meaning | Do this |
|---|---|---|
| `WARNING: the total crop area changed since ...` | a unit's total is different from the last report | check the report |
| `WARNING: the harvested area went DOWN ...` | a period is negative (highlighted in the sheet) | check the report |
| `WARNING: ... row(s) ... no crop in this report` / `... new in this report` | a unit disappeared or is new | check the boundary and report |
| `NOTE: boundary polygons overlap each other ...` | overlapping units; crop is counted once | fix the boundary if it matters |
| `NOTE: ... of crop lie outside every boundary polygon ...` | crop outside the units, row `(outside the boundary)` | check the boundary |
| `ERROR: this report looks INCREMENTAL, not cumulative ...` | the file holds only the cane still standing | use the cumulative report |
| `ERROR: the previous workbook already contains the report of ...` | this date is in it already | give the workbook of the report before |
| `ERROR: the report date ... is earlier than the last report ...` | wrong order | add reports in date order |
| `ERROR: ... the unit must stay the same` / `projection must stay the same` / `levels must stay the same` | a setting differs from the previous workbook | use the same setting |
| `ERROR: ... this workbook was not written by this tool or its hidden data sheet was removed` | wrong file, or its hidden sheet is gone | give the workbook Tool 3 wrote |
| `ERROR: the output ... is open in another program` | the workbook is open in Excel | close it, or use another name |

Result block: `Area check` must say `OK` (the table holds the whole report). `Rows written` = units in the table; `Left out` = combinations without crop (slivers).

Flag mode:
```
python harvest_stats_excel.py --boundary D:\data\units.shp --levels name_2,name --level-titles "Area,Unit" --report C:\work\report2.shp --previous C:\work\report1_stats.xlsx
```
`--no-pivot` leaves the pivot table out (if Excel refuses it).
