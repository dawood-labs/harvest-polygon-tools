"""Harvest statistics workbook: one harvest report + a boundary layer -> an Excel workbook of areas per boundary unit.

Dependencies: the same as preprocess_classified.py (imported from the same folder) plus openpyxl:
    pip install numpy rasterio shapely pyogrio pyproj openpyxl          (optional: psutil)

Inputs
  BOUNDARY : polygons with one or more attribute fields that name the units (for example a larger unit
             and a smaller unit inside it). The user chooses 1 or more fields as LEVELS, from the largest
             unit inwards; every distinct combination of the chosen values is one row of the table.
  REPORT   : one harvest report (polygons with a class field, `predicted` by default): 81 = remaining
             cane, 82 = harvested, any other code is kept as its own column and counted as remaining.
             Reports are cumulative: every report covers the whole crop area.
  PREVIOUS : the workbook this tool wrote for the report before (optional). Its history is read from a
             very hidden sheet, so old reports are never recomputed.
Steps
  1. Both layers are read (dirty shapes are repaired, NOTE lines say what was done) and projected to the
     chosen projected CRS (default: UTM zone of the report). Boundary polygons that overlap: the first
     polygon in file order keeps the shared part (each piece of crop is counted once).
  2. A crop polygon lying inside one boundary polygon is assigned whole; a polygon crossing a boundary
     edge is intersected. Crop outside every boundary polygon is kept as "(outside the boundary)".
  3. Areas per row and class code, in the chosen unit, full precision; rounded only for display.
  4. The new report is added to the history of the previous workbook (checks: same unit, projection and
     level fields; the date is new and later; the total crop area per row stays the same; a file that
     looks incremental is refused) and the workbook is written: Dashboard, Stats, Pivot (a real
     pivot table on Stats), the hidden sheet with the numbers of the charts, hidden data sheet.
  5. Written under a temporary name and swapped in at the end.

Run without arguments (or with --ask) for questions; every answer is also a flag:
    python harvest_stats_excel.py --boundary units.shp --levels FIELD1,FIELD2 --report report.shp
        [--level-titles "Title 1,Title 2"] [--date 2026-01-12] [--previous last_stats.xlsx]
        [--class-field predicted] [--unit acres|hectares|rai|m2|km2] [--utm 47N | EPSG:32647]
        [--allow-non-utm] [--out stats.xlsx] [--no-pivot] [--boundary-layer NAME] [--report-layer NAME]
"""
import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
import sys
import threading
import time
import warnings
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import preprocess_classified as pc  # noqa: E402  (checks numpy, shapely, pyogrio, pyproj)
except ModuleNotFoundError as _e:
    if _e.name != "preprocess_classified":
        raise
    sys.exit("ERROR: preprocess_classified.py was not found. Put preprocess_classified.py in the same folder "
             "as this file.")
try:
    import openpyxl  # noqa: E402
except ImportError:
    sys.exit("ERROR: python package 'openpyxl' is missing. Install it with: pip install openpyxl   "
             "(or: conda install -c conda-forge openpyxl)")

import numpy as np  # noqa: E402
import pyproj  # noqa: E402
import shapely  # noqa: E402
from shapely import STRtree  # noqa: E402
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE  # noqa: E402
from openpyxl.chart import BarChart, DoughnutChart, LineChart, Reference  # noqa: E402
from openpyxl.chart.label import DataLabelList  # noqa: E402
from openpyxl.chart.shapes import GraphicalProperties  # noqa: E402
from openpyxl.chart.text import RichText  # noqa: E402
from openpyxl.drawing.line import LineProperties  # noqa: E402
from openpyxl.drawing.spreadsheet_drawing import AnchorMarker, OneCellAnchor  # noqa: E402
from openpyxl.drawing.text import CharacterProperties, Font as DFont, Paragraph, ParagraphProperties  # noqa: E402
from openpyxl.drawing.xdr import XDRPositiveSize2D  # noqa: E402
from openpyxl.formatting.rule import DataBarRule  # noqa: E402
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side  # noqa: E402
from openpyxl.utils import get_column_letter  # noqa: E402

UserError = pc.UserError
TOOL = "harvest_stats_excel"
FORMAT_VERSION = 1
DATA_SHEET = "_harvest_stats_data"
REMAIN, HARVEST = "81", "82"
OUTSIDE = "(outside the boundary)"
NO_VALUE = "(no value)"
OUTSIDE_KEY = "__outside__"           # key of the outside row in the hidden data
MIN_OUTSIDE = 0.001                    # outside crop above this (in the unit) gets its own row
TOTAL_TOL_REL, TOTAL_TOL_ABS = 1e-4, 0.01    # 0.01 % and 0.01 of the unit: change of a row's total crop
CHECK_TOL_REL = 1e-4                   # grand total of the table vs the report: OK within 0.01 %
HOME_DIR = ".harvest_stats_excel"
NOT_OURS = ("this workbook was not written by this tool or its hidden data sheet was removed")
# colours: official classes, then calm neutrals
C_REMAIN, C_HARVEST = "454B1B", "F89508"
C_OTHER = "8C8C8C"
C_INK, C_INK2, C_LINE = "1F2328", "5A6270", "D0D5DC"
C_HEAD, C_BAND, C_TOTAL, C_TILE = "2F3B2A", "F4F6F2", "E3E8DC", "F7F8F5"
C_NEG_FILL, C_NEG_INK = "FBE3E1", "9B1C1C"
FONT = "Arial"


def warn(msgs, text):
    print(f"WARNING: {text}")
    msgs.append(text)


# ---------------------------------------------------------------- values, names, dates
def norm_value(v):
    """A field value as it is shown and stored: int, float or text; missing / NaN -> None."""
    if v is None:
        return None
    if isinstance(v, (bool, np.bool_)):
        return str(bool(v))
    if isinstance(v, (int, np.integer)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        f = float(v)
        if not math.isfinite(f):
            return None
        return int(f) if f.is_integer() and abs(f) < 2 ** 53 else f
    if isinstance(v, bytes):
        v = v.decode("utf-8", "replace")
    s = str(v)
    return s


def code_text(v):
    """A class code as written in the file: 82, 82.0 -> '82'; text as it is; missing -> '(no value)'."""
    v = norm_value(v)
    if v is None or (isinstance(v, str) and not v.strip()):
        return NO_VALUE
    return str(v).strip() if isinstance(v, str) else str(v)


def clean_text(s, limit=None):
    """Text safe for a cell: no control characters, one line, at most `limit` characters."""
    s = ILLEGAL_CHARACTERS_RE.sub("", str(s))
    s = re.sub(r"\s+", " ", s).strip()
    if limit and len(s) > limit:
        s = s[:limit - 3].rstrip() + "..."
    return s


def shown(v):
    """A level value as shown in a cell."""
    if v is None:
        return NO_VALUE
    return clean_text(v) if isinstance(v, str) else v


def natural_key(v):
    """Sort key: numbers first by value, then text in natural order (Block 2 before Block 10)."""
    if v is None:
        return (2, ())
    if isinstance(v, (int, float)):
        return (0, (float(v),))
    parts = re.split(r"(\d+)", str(v).casefold())
    return (1, tuple((0, int(p)) if p.isdigit() else (1, p) for p in parts if p != ""))


def row_sort_key(key):
    return tuple(natural_key(v) for v in key)


SHEET_BAD = re.compile(r"[\[\]:*?/\\]")


def unique_headers(names):
    """Column headers unique without regard to case (an Excel table needs that): later ones get ' (2)'."""
    seen, out = set(), []
    for n in names:
        n = clean_text(n, 250) or "Column"
        cand, k = n, 2
        while cand.casefold() in seen:
            cand = f"{n} ({k})"
            k += 1
        seen.add(cand.casefold())
        out.append(cand)
    return out


DATE_ISO = re.compile(r"(?<!\d)(\d{4})[-_.](\d{2})[-_.](\d{2})(?!\d)")


def parse_date(text):
    """'2026-01-12' or '12-01-2026' (also with / or .) -> date; (None, hint) when it is not a date."""
    s = str(text).strip()
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
    else:
        m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", s)
        if not m:
            return None, "Type the date as YYYY-MM-DD or DD-MM-YYYY, e.g. 2026-01-12."
        d, mo, y = (int(x) for x in m.groups())
    try:
        return dt.date(y, mo, d), None
    except ValueError:
        return None, f"{s} is not a real date. Type it as YYYY-MM-DD or DD-MM-YYYY."


def dates_in_name(path):
    """Valid dates (YYYY-MM-DD) found in a file name, in order, without repeats."""
    out = []
    for m in DATE_ISO.finditer(Path(path).stem):
        try:
            d = dt.date(*(int(x) for x in m.groups()))
        except ValueError:
            continue
        if d not in out:
            out.append(d)
    return out


def dmy(d):
    """date or 'YYYY-MM-DD' -> 'DD-MM-YYYY'."""
    if isinstance(d, str):
        d = dt.date.fromisoformat(d)
    return d.strftime("%d-%m-%Y")


def file_sig(path):
    p = Path(path)
    try:
        st = p.stat()
        size = st.st_size
        if p.suffix.lower() == ".shp":
            size = sum(q.stat().st_size for q in pc.file_set(p, (".shp", ".shx", ".dbf")).values() if q.exists())
        return {"name": p.name, "size": size, "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))}
    except OSError:
        return {"name": p.name}


# ---------------------------------------------------------------- reading layers
def layer_fields(path):
    """[(field name, distinct values, two example values)] of a layer, in file order."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        info = pc.vector_info(path)
        names = [str(n) for n in info.get("fields", [])]
        if not names:
            return []
        meta, _, _, data = pc.vector_read(path, read_geometry=False, columns=names)
    got = [str(x) for x in meta["fields"]]
    out = []
    for n in names:                                  # in the order of the layer
        col = data[got.index(n)]
        vals = [norm_value(v) for v in np.asarray(col, dtype=object)]
        distinct = sorted({v for v in vals if v is not None}, key=natural_key)
        out.append((n, len(distinct) + (1 if any(v is None for v in vals) else 0), distinct[:2]))
    return out


def field_list_lines(fields):
    lines = []
    for i, (n, k, ex) in enumerate(fields, 1):
        e = ", ".join(clean_text(x, 30) for x in ex)
        lines.append(f"   {i}) {n} ({pc.plural(k, 'value')}" + (f", e.g. {e})" if e else ")"))
    return lines


def read_polygons(path, columns, area_crs, what, assume_crs=None, workers=1, notes=None):
    """(polygons in area_crs, row of each polygon, column arrays, layer crs) of a vector layer; dirty shapes
    repaired (pc.clean_polygons) and one NOTE with the counts."""
    try:
        meta, _, wkb, data = pc.vector_read(path, read_geometry=True, columns=list(columns))
    except UserError:
        raise
    except Exception as e:
        raise UserError(f"cannot read the {what} {Path(path).name} ({str(e).splitlines()[0][:200]}). {pc.FORMAT_HINT}")
    got = [str(x) for x in meta.get("fields", [])]
    data = [data[got.index(c)] for c in columns]
    g, bad = pc.from_wkb_safe(wkb)
    lcrs = meta.get("crs") or ("EPSG:4326" if pc.is_geojson(path) else None) or assume_crs
    if lcrs is None:
        raise UserError(f"the {what} {Path(path).name} has no coordinate system (.prj missing)")
    rep = {"unreadable": bad} if bad else {}
    keep = ~shapely.is_missing(g)
    keep[keep] = ~shapely.is_empty(g[keep])
    if keep.any() and pyproj.CRS(lcrs) != area_crs:
        g = g.copy()
        g[keep] = pc.project_geoms(g[keep], lcrs, area_crs, workers)
    parts, prow = pc.clean_polygons(g, workers, rep)
    note = pc.input_note(what, rep)
    if note and notes is not None:
        print(f"NOTE: {note}")
        notes.append(note)
    return parts, prow, data, lcrs


def layer_crs(path):
    return pc.vector_crs(path)


def layer_bounds(path):
    """Total bounds of a layer in its own CRS (None when it has no features)."""
    info = pc.vector_info(path)
    b = info.get("total_bounds")
    if b is None or not np.all(np.isfinite(b)):
        return None
    return tuple(float(v) for v in b)


def boundary_units(path, fields, area_crs, workers=1, notes=None, assume_crs=None):
    """Boundary polygons ready for assignment: (polygons, unit index of each, unit keys, overlap m2, n features).
    Overlaps between features: the first feature in file order keeps the shared part."""
    parts, prow, data, _ = read_polygons(path, fields, area_crs, "boundary", assume_crs, workers, notes)
    n = len(data[0]) if len(data) else 0
    cols = [np.asarray(c, dtype=object) for c in data]
    keys = [tuple(norm_value(c[i]) for c in cols) for i in range(n)]
    uniq = {}
    unit_of_row = np.array([uniq.setdefault(k, len(uniq)) for k in keys], dtype=np.int64)
    unit_keys = list(uniq)
    overlap = 0.0
    if len(parts) > 1:
        pairs = pc._real_overlaps(parts, pc.NOISE_M2, workers=workers)
        if len(pairs):
            earlier = {}
            for a, b in pairs:            # a < b: a comes first in file order (parts are in row order)
                if prow[a] != prow[b]:
                    earlier.setdefault(int(b), []).append(int(a))
            parts = parts.copy()
            for j in sorted(earlier):
                before = parts[j].area
                cut = shapely.union_all(parts[earlier[j]])
                parts[j] = shapely.difference(parts[j], cut)
                overlap += before - parts[j].area
            ok = ~shapely.is_empty(parts)
            p2, i2 = shapely.get_parts(parts[ok], return_index=True)
            p3, i3 = shapely.get_parts(p2, return_index=True)
            sel = (shapely.get_type_id(p3) == 3) & ~shapely.is_empty(p3)
            sel[sel] = shapely.area(p3[sel]) > 0
            parts, prow = p3[sel], prow[ok][i2][i3][sel]
    return parts, unit_of_row[prow], unit_keys, overlap, n


def assign_areas(crop, codes, bparts, bunit, n_units, n_codes, workers=1):
    """Area (m2) of every code in every unit: (inside[n_units, n_codes], outside[n_codes]). A crop polygon
    covered by one boundary polygon is taken whole; a polygon touching several is intersected."""
    inside = np.zeros((n_units, n_codes))
    outside = np.zeros(n_codes)
    if len(crop) == 0:
        return inside, outside
    area = shapely.area(crop)
    if len(bparts) == 0:
        np.add.at(outside, codes, area)
        return inside, outside
    local = threading.local()
    bwkb = shapely.to_wkb(bparts)

    def own():
        """Every thread works on its own copy of the boundary polygons and tree: GEOS prepared geometries
        build their index lazily and must not be shared between threads."""
        if not hasattr(local, "tree"):
            local.parts = shapely.from_wkb(bwkb)
            shapely.prepare(local.parts)
            local.tree = STRtree(local.parts)
        return local.parts, local.tree

    def chunk(ix):
        bparts, tree = own()
        ins = np.zeros((n_units, n_codes))
        g = crop[ix]
        a, b = tree.query(g, predicate="intersects")
        hits = np.bincount(a, minlength=len(ix))
        got = np.zeros(len(ix))
        one = hits[a] == 1
        if one.any():
            cov = shapely.covers(bparts[b[one]], g[a[one]])
            aw, bw = a[one][cov], b[one][cov]
            np.add.at(ins, (bunit[bw], codes[ix[aw]]), area[ix[aw]])
            got[aw] = area[ix[aw]]
            rest = np.ones(len(a), dtype=bool)
            rest[np.flatnonzero(one)[cov]] = False
            a, b = a[rest], b[rest]
        if len(a):
            ia = shapely.area(shapely.intersection(g[a], bparts[b]))
            np.add.at(ins, (bunit[b], codes[ix[a]]), ia)
            np.add.at(got, a, ia)
        out = np.zeros(n_codes)
        np.add.at(out, codes[ix], area[ix] - got)
        return ins, out

    workers = max(1, min(workers, len(crop) // 20_000 + 1))
    with ThreadPoolExecutor(workers) as ex:
        for ins, out in ex.map(chunk, pc._chunks(len(crop), workers, 4)):
            inside += ins
            outside += out
    return inside, outside


def compute_report(boundary, fields, report, class_field, area_crs, unit, workers=1, notes=None, assume=None):
    """Areas of the report per unit and code, in the unit. Returns dict with values {key: {code: area}},
    outside {code: area}, totals {code: area}, codes (as found in the file), counts."""
    pc.tick("reading the boundary layer")
    bparts, bunit, keys, overlap_m2, nfeat = boundary_units(boundary, fields, area_crs, workers, notes,
                                                            assume.get("boundary") if assume else None)
    pc.tick(f"reading the report ({Path(report).name})")
    crop, crow, data, _ = read_polygons(report, [class_field], area_crs, "report", assume.get("report") if assume else None,
                                        workers, notes)
    raw = np.asarray(data[0], dtype=object) if len(data) else np.array([], dtype=object)
    row_codes = [code_text(v) for v in raw]
    found = sorted(set(row_codes), key=natural_key)
    cidx = {c: i for i, c in enumerate(found)}
    codes = np.array([cidx[row_codes[r]] for r in crow], dtype=np.int64) if len(crow) else np.zeros(0, np.int64)
    pc.tick(f"{pc.plural(len(crop), 'crop polygon')} and {pc.plural(len(bparts), 'boundary polygon')}: "
            f"assigning areas")
    inside, outside = assign_areas(crop, codes, bparts, bunit, len(keys), len(found), workers)
    f = 1.0 / unit.m2
    values = {}
    for u, k in enumerate(keys):
        if inside[u].any():
            values[k] = {c: float(inside[u, i]) * f for i, c in enumerate(found) if inside[u, i] != 0}
    totals = np.zeros(len(found))
    if len(crop):
        np.add.at(totals, codes, shapely.area(crop))
    return {"values": values, "outside": {c: float(outside[i]) * f for i, c in enumerate(found) if outside[i] != 0},
            "totals": {c: float(totals[i]) * f for i, c in enumerate(found)}, "codes": found,
            "n_crop": int(len(crop)), "n_features": nfeat, "n_keys": len(keys), "all_keys": keys,
            "overlap": overlap_m2 * f}


# ---------------------------------------------------------------- the hidden data sheet
def _data_lines(values):
    """Sorted text lines of the per-unit numbers (the checksum is computed on them)."""
    lines = []
    for key, by_date in values.items():
        kj = json.dumps(OUTSIDE_KEY if key == OUTSIDE_KEY else list(key), ensure_ascii=True)
        for d, by_code in by_date.items():
            for c, a in by_code.items():
                lines.append((kj, d, c, repr(float(a))))
    lines.sort()
    return lines


def checksum(lines):
    h = hashlib.sha1()
    for t in lines:
        h.update(("\t".join(t) + "\n").encode("utf-8"))
    return h.hexdigest()


def write_data_sheet(wb, meta, values):
    ws = wb.create_sheet(DATA_SHEET)
    ws.sheet_state = "veryHidden"
    lines = _data_lines(values)
    meta = dict(meta, rows=len(lines), checksum=checksum(lines))
    ws.append([TOOL, str(FORMAT_VERSION), "machine-readable copy for the next run; do not edit"])
    for k in sorted(meta):
        ws.append(["meta", k, json.dumps(meta[k], ensure_ascii=True, sort_keys=True)])
    ws.append(["data", "key", "date", "code", "area"])
    for t in lines:              # text only (keys are JSON, areas repr): exact, never a formula
        ws.append(["v", *t])
    return ws


def read_previous(path):
    """The history stored in a workbook of this tool: (meta, values {key: {date: {code: area}}}, labels)."""
    p = Path(path)
    if not p.is_file():
        raise UserError(f"previous workbook not found: {p}")
    if p.suffix.lower() not in (".xlsx", ".xlsm"):
        raise UserError(f"the previous workbook must be an .xlsx file written by this tool: {p.name}")
    try:
        wb = openpyxl.load_workbook(str(p), read_only=True, data_only=True)
    except Exception as e:
        raise UserError(f"cannot open the previous workbook {p.name} ({str(e).splitlines()[0][:150]})")
    try:
        if DATA_SHEET not in wb.sheetnames:
            raise UserError(f"{p.name}: {NOT_OURS}. Give the workbook that this tool wrote for the previous report.")
        rows = wb[DATA_SHEET].iter_rows(values_only=True)
        try:
            head = next(rows)
            if head[0] != TOOL:
                raise ValueError("unknown first row")
            version = int(head[1])
            meta, lines, data = {}, [], False
            for r in rows:
                r = tuple(r) + (None,) * 5
                if r[0] == "meta" and not data:
                    meta[str(r[1])] = json.loads(r[2])
                elif r[0] == "data":
                    data = True
                elif r[0] == "v" and data:
                    lines.append((str(r[1]), str(r[2]), str(r[3]), str(r[4])))
                elif r[0] is None and r[1] is None:
                    continue
                else:
                    raise ValueError("unexpected row")
            if version > FORMAT_VERSION:
                raise UserError(f"{p.name} was written by a newer version of this tool (data format {version}); "
                                f"use the newer tool")
            if meta.get("tool") != TOOL or meta.get("rows") != len(lines):
                raise ValueError("row count")
            if checksum(sorted(lines)) != meta.get("checksum"):
                raise ValueError("checksum")
            values = {}
            for kj, d, c, a in lines:
                k = json.loads(kj)
                key = OUTSIDE_KEY if k == OUTSIDE_KEY else tuple(k)
                dt.date.fromisoformat(d)
                values.setdefault(key, {}).setdefault(d, {})[c] = float(a)
            for k in ("unit", "crs", "levels", "reports"):
                if k not in meta:
                    raise ValueError(f"missing {k}")
        except UserError:
            raise
        except Exception as e:
            raise UserError(f"{p.name}: {NOT_OURS} (the hidden data sheet is damaged: {e}). Give the workbook that "
                            f"this tool wrote for the previous report.")
        labels = read_labels(wb, meta)
    finally:
        wb.close()
    return meta, values, labels


def read_labels(wb, meta):
    """Headers of code columns the user renamed in the visible Stats sheet: {(code, date): text}. A cell is
    taken only when it is at the place this tool wrote it, not empty and not one of the tool's own headers
    (then the columns were moved and nothing is taken)."""
    out = {}
    spec = meta.get("code_columns") or {}
    sheet, row, cols = spec.get("sheet"), spec.get("row"), spec.get("columns") or []
    if not cols or sheet not in wb.sheetnames:
        return out
    try:
        cells = next(wb[sheet].iter_rows(min_row=row, max_row=row, values_only=True))
    except Exception:
        return out
    ours = set(spec.get("all_headers") or [])
    for c in cols:
        i = c["col"] - 1
        v = cells[i] if i < len(cells) else None
        if isinstance(v, str) and v.strip() and v.strip() != c["header"] and v.strip() not in ours:
            out[(c["code"], c["date"])] = clean_text(v, 250)
    return out


def merge_labels(old, new):
    """Labels of earlier workbooks plus the renames read now (a rename wins)."""
    out = {(x["code"], x["date"]): x["text"] for x in (old or [])}
    out.update(new or {})
    return out


def code_label(code, labels):
    """The name the user gave a code (the last renamed header ending in ' on DD-MM-YYYY'), else None."""
    best = None
    for (c, d), text in sorted(labels.items(), key=lambda kv: kv[0][1]):
        tail = f" on {dmy(d)}"
        if c == code and text.endswith(tail) and text[:-len(tail)].strip():
            best = text[:-len(tail)].strip()
    return best


def _dbf_distinct(path, field):
    """Distinct values of one attribute of a shapefile, read straight from the .dbf column (no geometry, no
    record objects: a few hundredths of a second for a million rows). None when the file is not as expected."""
    p = Path(path)
    dbf = next((q for q in (p.with_suffix(".dbf"), p.with_suffix(".DBF")) if q.is_file()), None)
    if dbf is None:
        return None
    with open(dbf, "rb") as f:
        head = f.read(32)
        if len(head) < 32:
            return None
        nrec = int.from_bytes(head[4:8], "little")
        hlen = int.from_bytes(head[8:10], "little")
        rlen = int.from_bytes(head[10:12], "little")
        offset, found = 1, None
        while True:
            d = f.read(32)
            if len(d) < 32 or d[0] == 0x0D:
                break
            name = d[:11].split(b"\0")[0].decode("ascii", "ignore")
            if name == field:
                found = (offset, d[16], chr(d[11]))
                break
            offset += d[16]
    if found is None or rlen <= 0 or dbf.stat().st_size < hlen + nrec * rlen:
        return None
    if nrec == 0:
        return []
    off, length, kind = found
    rows = np.memmap(dbf, dtype=np.uint8, mode="r", offset=hlen, shape=(nrec, rlen))
    col = np.ascontiguousarray(rows[:, off:off + length]).view(f"S{length}").ravel()
    vals = np.unique(col[rows[:, 0] != 0x2A])                     # 0x2A marks a deleted record
    del rows
    out = []
    for v in vals:
        if any(c > 127 for c in v):
            return None                                           # text in a code page: let the reader decode it
        t = v.decode("ascii").strip()
        if kind in "NF":
            if t in ("", "*" * len(t)):
                out.append(None)
            else:
                x = float(t)
                out.append(int(x) if x == int(x) else x)
        elif kind == "C":
            out.append(t or None)
        else:
            return None
    return out


def distinct_values(path, field):
    """Distinct values of one attribute of a layer, the fastest way the format allows."""
    p = Path(path)
    if p.suffix.lower() == ".shp":
        try:
            got = _dbf_distinct(p, field)
            if got is not None:
                return got
        except Exception:
            pass
    return list(pc.vector_read(p, read_geometry=False, columns=[field])[3][0])


def other_codes(report, class_field):
    """The class codes of the report other than 81 and 82, as texts, in natural order (attributes only)."""
    try:
        found = {code_text(v) for v in distinct_values(report, class_field)}
    except Exception:
        return []
    return sorted((c for c in found if c not in (REMAIN, HARVEST)), key=natural_key)


def class_name_check(a):
    """A name for a class code: any short text."""
    name = clean_text(a, 60)
    return (name, None) if name else (None, "Type a name, or press Enter to keep the code.")


def parse_class_names(text):
    """'282=Cloud,232=Smoke' -> {'282': 'Cloud', '232': 'Smoke'}."""
    out = {}
    for part in (split_list(text) if text else []):
        if not part:
            continue
        code, sep, name = part.partition("=")
        name = clean_text(name, 60)
        if not sep or not code.strip() or not name:
            raise UserError(f"--class-names: write CODE=NAME, comma separated (got '{part}')")
        out[code.strip()] = name
    return out


# ---------------------------------------------------------------- the table
class Table3:
    """The finest-level rows and every derived number, from the history (values in the unit)."""

    def __init__(self, values, reports, levels, labels, unit, all_keys=()):
        self.reports = sorted(reports, key=lambda r: r["date"])
        self.dates = [r["date"] for r in self.reports]
        self.levels, self.labels, self.unit = levels, labels, unit
        self.nl = len(levels)
        eps = 0.5 * 10 ** -unit.decimals
        keys = [k for k in values if k != OUTSIDE_KEY]
        self.left_out, self.left_out_area = [], 0.0
        rows = []
        for k in keys:
            tot = max((sum(values[k].get(d, {}).values()) for d in self.dates), default=0.0)
            if tot < eps:
                self.left_out.append(k)
                self.left_out_area += sum(values[k].get(self.dates[-1], {}).values())
            else:
                rows.append(k)
        rows.sort(key=row_sort_key)
        self.left_out += [k for k in dict.fromkeys(all_keys) if k not in values]   # never any crop
        self.outside = None
        if OUTSIDE_KEY in values:
            mx = max((sum(values[OUTSIDE_KEY].get(d, {}).values()) for d in self.dates), default=0.0)
            if mx > MIN_OUTSIDE:
                self.outside = OUTSIDE_KEY
            else:
                self.left_out_area += sum(values[OUTSIDE_KEY].get(self.dates[-1], {}).values())
        self.keys = rows + ([OUTSIDE_KEY] if self.outside else [])
        self.values = values
        self.other = {r["date"]: [c for c in r["codes"] if c not in (REMAIN, HARVEST)] for r in self.reports}

    def level_cells(self, key, n=None):
        n = self.nl if n is None else n
        if key == OUTSIDE_KEY:
            return [OUTSIDE] + [None] * (n - 1)
        return [shown(v) for v in key[:n]]

    def at(self, key, d, code=None):
        by = self.values.get(key, {}).get(d, {})
        return by.get(code, 0.0) if code is not None else sum(by.values())

    def numbers(self, key):
        """Every number of one row: total, harvested per period, other codes, cumulative harvested."""
        last = self.dates[-1]
        total = self.at(key, last)
        cum = [self.at(key, d, HARVEST) for d in self.dates]
        period = [c - (cum[i - 1] if i else 0.0) for i, c in enumerate(cum)]
        other = {(c, d): self.at(key, d, c) for d in self.dates for c in self.other[d]}
        return {"total": total, "cum": cum, "period": period, "other": other, "harvested": cum[-1],
                "remaining": total - cum[-1]}

    def header_texts(self, titles):
        last = dmy(self.dates[-1])
        cols = list(titles) + ["Total Crop Area"]
        code_cols = []
        for d in self.dates:
            cols.append(f"Crop Harvested on {dmy(d)}")
            for c in self.other[d]:
                default = f"{c} on {dmy(d)}"
                text = self.labels.get((c, d))
                if text is None:
                    lab = code_label(c, self.labels)
                    text = f"{lab} on {dmy(d)}" if lab else default
                code_cols.append({"code": c, "date": d, "col": len(cols) + 1, "header": text})
                cols.append(text)
        cols += [f"Total Harvested Crop ({last})", f"Total Remaining Crop ({last})",
                 f"Total Harvested Crop % ({last})", f"Total Remaining Crop % ({last})"]
        cols = unique_headers(cols)
        for c in code_cols:
            c["header"] = cols[c["col"] - 1]
        return cols, code_cols

    def row_values(self, nums):
        """The number cells of a row (after the level cells), in column order; % as fractions."""
        out = [nums["total"]]
        for i, d in enumerate(self.dates):
            out.append(nums["period"][i])
            out += [nums["other"][(c, d)] for c in self.other[d]]
        t = nums["total"]
        hp = nums["harvested"] / t if t > 0 else None
        rp = nums["remaining"] / t if t > 0 else None
        return out + [nums["harvested"], nums["remaining"], hp, rp]

    def grouped(self, n):
        """Rows summed to the first n levels: [(level cells, numbers)] in sorted order (outside last)."""
        groups = {}
        for k in self.keys:
            g = OUTSIDE_KEY if k == OUTSIDE_KEY else k[:n]
            nums = self.numbers(k)
            if g not in groups:
                groups[g] = nums
            else:
                a = groups[g]
                groups[g] = {"total": a["total"] + nums["total"],
                             "cum": [x + y for x, y in zip(a["cum"], nums["cum"])],
                             "period": [x + y for x, y in zip(a["period"], nums["period"])],
                             "other": {c: a["other"][c] + nums["other"][c] for c in a["other"]},
                             "harvested": a["harvested"] + nums["harvested"],
                             "remaining": a["remaining"] + nums["remaining"]}
        order = sorted((g for g in groups if g != OUTSIDE_KEY), key=row_sort_key)
        if OUTSIDE_KEY in groups:
            order.append(OUTSIDE_KEY)
        return [(g, groups[g]) for g in order]

    def totals(self):
        z = {"total": 0.0, "cum": [0.0] * len(self.dates), "period": [0.0] * len(self.dates),
             "other": {(c, d): 0.0 for d in self.dates for c in self.other[d]}, "harvested": 0.0, "remaining": 0.0}
        for k in self.keys:
            n = self.numbers(k)
            z["total"] += n["total"]
            z["harvested"] += n["harvested"]
            z["remaining"] += n["remaining"]
            z["cum"] = [x + y for x, y in zip(z["cum"], n["cum"])]
            z["period"] = [x + y for x, y in zip(z["period"], n["period"])]
            for c in z["other"]:
                z["other"][c] += n["other"][c]
        return z


# ---------------------------------------------------------------- workbook helpers
def put(ws, r, c, v, **style):
    """Write one cell; text is always text (a value starting with '=' never becomes a formula)."""
    if isinstance(v, str):
        v = ILLEGAL_CHARACTERS_RE.sub("", v)
    cell = ws.cell(row=r, column=c, value=v)
    if isinstance(v, str):
        cell.data_type = "s"
    for k, val in style.items():
        setattr(cell, k, val)
    return cell


def font(size=10, bold=False, color=C_INK, italic=False):
    return Font(name=FONT, size=size, bold=bold, color=color, italic=italic)


def fill(color):
    return PatternFill("solid", start_color=color, end_color=color)


THIN = Side(style="thin", color=C_LINE)
MED = Side(style="medium", color=C_HEAD)


def area_format(unit):
    d = unit.decimals
    f = "#,##0" + ("." + "0" * d if d else "")
    return f"{f};-{f};{f}"


PCT = "0.0%"


def write_table(ws, start_row, header, rows, totals, n_levels, unit, title_line, neg_cols=(), pct_cols=()):
    """A styled table: a line above the header, the header, the rows, a Totals row. Returns
    (header row, last row, negative cells written)."""
    put(ws, start_row, 1, title_line, font=font(10, italic=True, color=C_INK2))
    hr = start_row + 1
    for c, h in enumerate(header, 1):
        put(ws, hr, c, h, font=font(10, True, "FFFFFF"), fill=fill(C_HEAD),
            alignment=Alignment(horizontal="left" if c <= n_levels else "center", vertical="center", wrap_text=True),
            border=Border(bottom=MED))
    ws.row_dimensions[hr].height = 45
    af = area_format(unit)
    negs = 0
    r = hr
    for i, cells in enumerate(rows):
        r += 1
        band = fill(C_BAND) if i % 2 else None
        for c, v in enumerate(cells, 1):
            cell = put(ws, r, c, v, font=font(10), border=Border(bottom=THIN))
            if band is not None:
                cell.fill = band
            if c > n_levels:
                cell.number_format = PCT if c in pct_cols else af
                if c in neg_cols and isinstance(v, float) and round(v, unit.decimals) < 0:
                    cell.fill = fill(C_NEG_FILL)
                    cell.font = font(10, True, C_NEG_INK)
                    negs += 1
    r += 1
    for c, v in enumerate(totals, 1):
        cell = put(ws, r, c, v, font=font(10, True), fill=fill(C_TOTAL), border=Border(top=MED, bottom=MED))
        if c > n_levels:
            cell.number_format = PCT if c in pct_cols else af
            if c in neg_cols and isinstance(v, float) and round(v, unit.decimals) < 0:
                cell.fill = fill(C_NEG_FILL)
                cell.font = font(10, True, C_NEG_INK)
    widths = []
    for c, h in enumerate(header, 1):
        if c <= n_levels:
            longest = max([len(str(x[c - 1])) for x in rows if x[c - 1] is not None] + [len("Totals"), 8])
            w = min(max(longest + 2, min(len(h), 18) + 2), 45)
        else:
            w = 16 if c in pct_cols else 17
        widths.append(w)
        ws.column_dimensions[get_column_letter(c)].width = w
    ws.freeze_panes = ws.cell(row=hr + 1, column=n_levels + 1)
    ws.auto_filter.ref = f"A{hr}:{get_column_letter(len(header))}{r - 1}"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = f"{hr}:{hr}"
    ws.sheet_view.showGridLines = False
    return hr, r, negs


def stats_rows(tab, n, titles):
    """(rows, totals row) of a table summed to the first n levels."""
    rows = []
    for g, nums in tab.grouped(n):
        rows.append(tab.level_cells(g, n) + tab.row_values(nums))
    tot = tab.totals()
    trow = ["Totals"] + [None] * (n - 1) + tab.row_values(tot)
    return rows, trow


def column_roles(tab, n):
    """(1-based) columns holding the harvested-in-period values and the two % columns, for n level columns."""
    neg, c = [], n + 2
    for d in tab.dates:
        neg.append(c)
        c += 1 + len(tab.other[d])
    return set(neg), {c + 2, c + 3}


# ---------------------------------------------------------------- charts
def chart_text(size=900, bold=False, color=C_INK2):
    cp = CharacterProperties(latin=DFont(typeface=FONT), sz=size, b=bold, solidFill=color)
    return RichText(p=[Paragraph(pPr=ParagraphProperties(defRPr=cp), endParaRPr=cp)])


def style_chart(ch, title, x_title=None, y_title=None):
    ch.title = title
    ch.roundedCorners = False
    ch.style = 2
    ch.title.overlay = False
    ch.title.tx.rich.p[0].pPr = ParagraphProperties(defRPr=CharacterProperties(
        latin=DFont(typeface=FONT), sz=1100, b=True, solidFill=C_INK))
    ch.graphical_properties = GraphicalProperties(ln=LineProperties(solidFill=C_LINE))
    if ch.legend is not None:
        ch.legend.overlay = False
        ch.legend.position = "b"
        ch.legend.txPr = chart_text()
    if hasattr(ch, "x_axis") and not isinstance(ch, DoughnutChart):
        ch.x_axis.delete = False
        ch.y_axis.delete = False
        ch.x_axis.txPr = chart_text()
        ch.y_axis.txPr = chart_text()
        if ch.y_axis.majorGridlines is not None:
            ch.y_axis.majorGridlines.spPr = GraphicalProperties(ln=LineProperties(solidFill="E6E9ED"))
        if x_title:
            ch.x_axis.title = x_title
            ch.x_axis.title.overlay = False
            ch.x_axis.title.tx.rich.p[0].pPr = ParagraphProperties(defRPr=CharacterProperties(
                latin=DFont(typeface=FONT), sz=900, b=False, solidFill=C_INK2))
        if y_title:
            ch.y_axis.title = y_title
            ch.y_axis.title.overlay = False
            ch.y_axis.title.tx.rich.p[0].pPr = ParagraphProperties(defRPr=CharacterProperties(
                latin=DFont(typeface=FONT), sz=900, b=False, solidFill=C_INK2))


def color_series(s, color, line=False):
    if line:
        s.graphicalProperties.line.solidFill = color
        s.graphicalProperties.line.width = 28575
        s.marker.symbol = "circle"
        s.marker.size = 8
        s.marker.graphicalProperties = GraphicalProperties(solidFill=color, ln=LineProperties(solidFill="FFFFFF"))
        s.smooth = False
    else:
        s.graphicalProperties = GraphicalProperties(solidFill=color, ln=LineProperties(solidFill="FFFFFF", w=12700))


def data_labels(fmt, pos=None, pct=False, color=C_INK, bold=False):
    lab = DataLabelList()
    lab.showVal = not pct
    lab.showPercent = pct
    for k in ("showSerName", "showCatName", "showLegendKey", "showLeaderLines"):
        setattr(lab, k, False)
    lab.numFmt = fmt
    if pos:
        lab.position = pos
    lab.txPr = chart_text(800, bold, color)
    return lab


EMU_PX = 9525


def col_px(width):
    """Pixel width of a column of `width` characters (default font, as Excel draws it)."""
    return int(width * 7 + 5)


def place(ws, chart, x_px, row, w_px, h_px, widths):
    """Anchor a chart at x pixels from the left edge of column A and at the top of `row` (1-based)."""
    col, left = 0, 0
    while left + col_px(widths[col]) <= x_px:
        left += col_px(widths[col])
        col += 1
    anchor = OneCellAnchor(_from=AnchorMarker(col=col, colOff=(x_px - left) * EMU_PX, row=row - 1, rowOff=0),
                           ext=XDRPositiveSize2D(w_px * EMU_PX, h_px * EMU_PX))
    chart.anchor = anchor
    chart.width, chart.height = w_px / 37.8, h_px / 37.8
    ws.add_chart(chart)


# ---------------------------------------------------------------- the workbook
def build_workbook(tab, levels, unit, area_crs, meta, report_info, previous_name, pivot=True):
    """The whole workbook (openpyxl Workbook) and facts for the result block."""
    wb = openpyxl.Workbook()
    titles = [lv["title"] for lv in levels]
    last = tab.dates[-1]
    nl = tab.nl
    uname = unit.text
    facts = {}

    # ---- Stats
    dash = wb.active
    dash.title = "Dashboard"
    st = wb.create_sheet("Stats")
    header, code_cols = tab.header_texts(titles)
    rows, trow = stats_rows(tab, nl, titles)
    neg, pct = column_roles(tab, nl)
    line = f"All areas in {uname}. Report date {dmy(last)}."
    hr, _, negs = write_table(st, 1, header, rows, trow, nl, unit, line, neg, pct)
    facts["negatives"] = negs
    facts["rows"] = len(rows)
    meta["code_columns"] = {"sheet": "Stats", "row": hr, "columns": code_cols, "all_headers": header}

    # ---- Pivot
    pv = wb.create_sheet("Pivot")
    pv.sheet_view.showGridLines = False
    pivot_ref = write_pivot_cells(pv, tab, unit, header)
    if pivot:
        add_pivot(pv, st, tab, header, rows, hr, pivot_ref, wb)
    facts["pivot"] = bool(pivot)

    # ---- Chart data + Dashboard
    cd = wb.create_sheet("Chart data")
    cd.sheet_state = "hidden"            # the charts need their numbers in cells; nobody has to look at them
    write_dashboard(dash, cd, dashboard_results(tab, titles, unit, area_crs, report_info, previous_name))

    write_data_sheet(wb, meta, tab.values)
    wb.properties.title = f"Harvest statistics {dmy(last)}"
    wb.properties.subject = f"Harvest statistics per boundary unit, report date {dmy(last)}, areas in {uname}"
    wb.properties.creator = None
    wb.properties.lastModifiedBy = None
    wb.active = 0
    for ws in wb.worksheets:
        ws.sheet_view.tabSelected = ws.title == "Dashboard"
    return wb, facts


PIVOT_TOP = 7                 # sheet row of the pivot's header (rows above it: title and how to read it)
BLANK = "(blank)"             # what Excel shows for an empty level cell


def pivot_tree(tab):
    """Rows of the pivot in compact form, parents first: (depth, level value as in the Stats cell, numbers).
    The value is None where the Stats cell is empty (lower levels of the outside row)."""
    nl = tab.nl
    lookup = [dict(tab.grouped(n)) for n in range(1, nl + 1)]

    def order(g, depth):
        return (g == OUTSIDE_KEY, natural_key(shown(g[depth])) if g != OUTSIDE_KEY else ())

    def walk(prefix, depth, out):
        kids = [g for g in lookup[depth] if depth == 0 or ((g == OUTSIDE_KEY) == (prefix == OUTSIDE_KEY)
                                                           and (g == OUTSIDE_KEY or g[:depth] == prefix))]
        for g in sorted(kids, key=lambda g: order(g, depth)):
            value = (OUTSIDE if depth == 0 else None) if g == OUTSIDE_KEY else shown(g[depth])
            out.append((depth, value, lookup[depth][g]))
            if depth + 1 < nl:
                walk(g, depth + 1, out)
        return out
    return walk((), 0, [])


def pivot_columns(tab, header):
    """(titles of the value columns shown in the pivot, their captions): 'Total Crop Area' and every per-date
    column of the Stats sheet. The four total columns at the end (total harvested, total remaining and their %)
    are fields of the pivot too, but not ticked: the user adds them from the field list when wanted."""
    cols = header[tab.nl:len(header) - 4]
    return cols, [f"Sum of {h}" for h in cols]


def write_pivot_cells(pv, tab, unit, header):
    """The Pivot sheet: a title, how to read it, and the cells as Excel shows the pivot (so the sheet reads
    right before Excel refreshes it, and also without the pivot). One row per level-1 unit with its lower
    levels under it; one column per value column of the Stats sheet. Returns (first row, last row, last col)."""
    af = area_format(unit)
    nl = tab.nl
    titles = tab.levels_titles
    cols, captions = pivot_columns(tab, header)
    last = dmy(tab.dates[-1])
    put(pv, 1, 1, "Harvest by " + " and ".join(titles), font=font(14, True))
    lines = [f"Each row is one {titles[0]}"
             + (f"; the rows under it are its {titles[1]}" if nl > 1 else "")
             + f". All areas in {unit.text}. The last row is the whole area.",
             "Columns: total crop area, then the area harvested in each period (from the report before to that "
             "date).",
             "This is a pivot table on the sheet 'Stats'. The field list on the right opens when a cell of the "
             "table is selected: tick a field to add its column,",
             f"for example the total harvested or the remaining crop on {last}. Click a - or + beside a name to "
             "close or open it."]
    for i, text in enumerate(lines):
        put(pv, 2 + i, 1, text, font=font(10, color=C_INK2))
    top = PIVOT_TOP
    caption = " / ".join(str(t) for t in titles)
    put(pv, top, 1, caption, font=font(10, True, "FFFFFF"), fill=fill(C_HEAD),
        alignment=Alignment(vertical="center", wrap_text=True))
    for j, h in enumerate(captions):
        put(pv, top, 2 + j, h, font=font(10, True, "FFFFFF"), fill=fill(C_HEAD),
            alignment=Alignment(horizontal="center", vertical="center", wrap_text=True))
    pv.row_dimensions[top].height = 48
    nv = len(cols)
    r = top
    tree = pivot_tree(tab)
    for depth, value, nums in tree:
        r += 1
        parent = depth < nl - 1
        cell = put(pv, r, 1, BLANK if value is None else value, font=font(10, parent),
                   alignment=Alignment(indent=depth, horizontal="left"))
        if parent and nl > 1:
            cell.fill = fill(C_BAND)
        for j, v in enumerate(tab.row_values(nums)[:nv]):
            c2 = put(pv, r, 2 + j, v, font=font(10, parent), number_format=af)
            if parent and nl > 1:
                c2.fill = fill(C_BAND)
    r += 1
    put(pv, r, 1, "Grand Total", font=font(10, True), fill=fill(C_TOTAL), border=Border(top=MED))
    for j, v in enumerate(tab.row_values(tab.totals())[:nv]):
        put(pv, r, 2 + j, v, font=font(10, True), fill=fill(C_TOTAL), border=Border(top=MED), number_format=af)
    longest = max([len(str(t[1])) + 2 * t[0] for t in tree] + [len(caption), 14])
    pv.column_dimensions["A"].width = min(longest + 3, 50)
    for j in range(nv):
        pv.column_dimensions[get_column_letter(2 + j)].width = 17
    pv.freeze_panes = pv.cell(row=top + 1, column=2)
    inside = f"B{top + 1}"                       # a selected cell inside the pivot: Excel then shows the field list
    for sel in pv.sheet_view.selection:
        if sel.pane in (None, "bottomRight"):
            sel.activeCell, sel.sqref = inside, inside
    return top, r, 1 + nv


def add_pivot(pv, st, tab, header, rows, hr, pivot_ref, wb):
    """A real pivot table (openpyxl pivot classes) on the table of the sheet `st` ('Stats', header in row `hr`,
    `rows` = its rows without the Totals row): row fields = the level columns (compact form), values = the sum
    of every value column (see pivot_columns), laid out side by side. The cache holds the records and is
    refreshed when Excel opens the file."""
    from openpyxl.pivot.cache import CacheDefinition, CacheField, CacheSource, SharedItems, WorksheetSource
    from openpyxl.pivot.fields import Index, Missing, Number, Text
    from openpyxl.pivot.record import Record, RecordList
    from openpyxl.pivot.table import (DataField, FieldItem, Location, PivotField, PivotTableStyle,
                                      RowColField, RowColItem, TableDefinition)
    nl = tab.nl
    cols, captions = pivot_columns(tab, header)
    nv = len(cols)                                        # value columns shown
    nf = len(header)                                      # every column of the table is a field
    fields, idx, as_text = [], [], []
    for j in range(nf):
        vals = [rv[j] for rv in rows]
        if j >= nl:                                       # a value column: numbers, no items
            nums = [v for v in vals if v is not None] or [0.0]
            fields.append(CacheField(name=header[j], numFmtId=0, sharedItems=SharedItems(
                containsSemiMixedTypes=False, containsString=False, containsNumber=True,
                containsBlank=(len(nums) < len(vals)) or None,
                minValue=float(min(nums)), maxValue=float(max(nums)))))
            idx.append(None)
            as_text.append(False)
            continue
        real = [v for v in vals if v is not None]
        blank = len(real) < len(vals)
        text = not all(isinstance(v, int) and not isinstance(v, bool) for v in real)
        if text:
            items = sorted({str(v) for v in real}, key=lambda v: (v == OUTSIDE, natural_key(v)))
            si = SharedItems(_fields=[Text(v=v) for v in items] + ([Missing()] if blank else []),
                             containsBlank=blank or None)
        else:
            items = sorted(set(real))
            si = SharedItems(_fields=[Number(v=v) for v in items] + ([Missing()] if blank else []),
                             containsSemiMixedTypes=False, containsString=False, containsNumber=True,
                             containsInteger=True, containsBlank=blank or None,
                             minValue=float(min(items)), maxValue=float(max(items)))
        lookup = {v: i for i, v in enumerate(items)}
        if blank:
            lookup[None] = len(items)
        fields.append(CacheField(name=header[j], numFmtId=0, sharedItems=si))
        idx.append(lookup)
        as_text.append(text)

    def item_of(j, v):
        return idx[j][str(v) if (as_text[j] and v is not None) else v]

    def value(j, v):
        if j < nl:
            return Index(v=item_of(j, v))
        return Missing() if v is None else Number(v=float(v))
    recs = [Record(_fields=[value(j, rv[j]) for j in range(nf)]) for rv in rows]
    src_ref = f"A{hr}:{get_column_letter(nf)}{hr + len(rows)}"
    source = CacheSource(type="worksheet", worksheetSource=WorksheetSource(ref=src_ref, sheet=st.title))
    cache = CacheDefinition(refreshOnLoad=True, createdVersion=6, refreshedVersion=6, minRefreshableVersion=3,
                            recordCount=len(recs), cacheSource=source, cacheFields=fields)
    cache.records = RecordList(r=recs)

    pfields = []
    for j in range(nf):
        if j >= nl + nv:
            pfields.append(PivotField(showAll=False))             # in the field list, not ticked
        elif j >= nl:
            pfields.append(PivotField(dataField=True, showAll=False))
        else:
            items = [FieldItem(x=i) for i in range(len(idx[j]))] + [FieldItem(t="default")]
            pfields.append(PivotField(axis="axisRow", showAll=False, items=items))
    # row items (compact form, subtotals on top): one <i> per pivot row, r = fields shared with the row above
    row_items = [RowColItem(t="data", r=depth, x=[Index(v=item_of(depth, value))])
                 for depth, value, _ in pivot_tree(tab)]
    row_items.append(RowColItem(t="grand", x=[Index(v=0)]))
    col_items = [RowColItem(t="data", i=k, x=[Index(v=k)]) for k in range(nv)]
    first, last_r, last_c = pivot_ref
    loc = Location(ref=f"A{first}:{get_column_letter(last_c)}{last_r}", firstHeaderRow=0, firstDataRow=1,
                   firstDataCol=1)
    fmt = area_format(tab.unit)
    fmt_id = 164 + wb._number_formats.index(fmt) if fmt in wb._number_formats else None
    pt = TableDefinition(name="HarvestPivot", cacheId=1, dataCaption="Values", updatedVersion=6,
                         minRefreshableVersion=3, createdVersion=6, useAutoFormatting=True, itemPrintTitles=True,
                         indent=0, outline=True, outlineData=True, multipleFieldFilters=False,
                         applyNumberFormats=False, applyBorderFormats=False, applyFontFormats=False,
                         applyPatternFormats=False, applyAlignmentFormats=False, applyWidthHeightFormats=True,
                         rowHeaderCaption=" / ".join(str(t) for t in tab.levels_titles),
                         location=loc, pivotFields=pfields,
                         rowFields=[RowColField(x=j) for j in range(nl)], rowItems=row_items,
                         colFields=[RowColField(x=-2)] if nv > 1 else [], colItems=col_items,
                         dataFields=[DataField(name=captions[k], fld=nl + k, baseField=0, baseItem=0,
                                               numFmtId=fmt_id) for k in range(nv)],
                         pivotTableStyleInfo=PivotTableStyle(name="PivotStyleMedium4", showRowHeaders=True,
                                                             showColHeaders=True, showRowStripes=False,
                                                             showColStripes=False, showLastColumn=True))
    pt.cache = cache

    def write_rels(archive, manifest, _orig=pt._write_rels):
        _orig(archive, manifest)
        pt.id = None             # no r:id on the root element: the schema does not allow it, Excel never writes it
    pt._write_rels = write_rels
    pv._pivots.append(pt)


def dashboard_results(tab, titles, unit, area_crs, report_info, previous_name):
    """The plain results structure the Dashboard is drawn from (numbers in the unit, full precision; no
    geometry, no openpyxl objects). Keys:
      unit            {"text", "header", "decimals", "format"}   (format = Excel number format of areas)
      level_titles    titles of the levels, largest first
      dates           report dates 'DD-MM-YYYY', oldest first
      total, harvested, remaining   whole crop area at the latest date; harvested = cumulative 82
      period          area harvested in each period, one per date
      days            length of each period in days (None for the first report: its start is not known)
      pace            area harvested per day in each period (None where days is None)
      cum_harvested   cumulative harvested at each date;   cum_pct   the same as a fraction of the crop
      by_level1       [(name, harvested, remaining, total)], most remaining crop first; at most 20 rows, the
                      rest summed in one 'Others (n)' row; the outside row, when there is one, is the last
      top_level2      [(label 'level-2 (level-1)', remaining)] the 10 largest remaining, [] with one level
      finest          [(harvested, total)] of every unit of the last level that holds crop
      other           area of the codes other than 81 / 82 at the latest date (it is part of `remaining`)
      other_codes     their texts, e.g. ['282']
      report, boundary, previous, projection    texts for the notes
    """
    tot = tab.totals()
    totals_at = [sum(tab.at(k, d) for k in tab.keys) for d in tab.dates]

    def row(name, nums):
        return (name, sum(n["harvested"] for n in nums), sum(n["remaining"] for n in nums),
                sum(n["total"] for n in nums))
    g1 = tab.grouped(1)
    inside = sorted((x for x in g1 if x[0] != OUTSIDE_KEY), key=lambda x: -x[1]["remaining"])
    by1 = [row(str(shown(g[0])), [n]) for g, n in inside[:20]]
    if len(inside) > 20:
        by1.append(row(f"Others ({len(inside) - 20})", [n for _, n in inside[20:]]))
    by1 += [row(OUTSIDE, [n]) for g, n in g1 if g == OUTSIDE_KEY]
    top2 = []
    if tab.nl >= 2:
        g2 = [(g, n) for g, n in tab.grouped(2) if g != OUTSIDE_KEY and n["remaining"] > 0]
        g2.sort(key=lambda x: -x[1]["remaining"])
        top2 = [(f"{shown(g[1])} ({shown(g[0])})", n["remaining"]) for g, n in g2[:10]]
    finest = [(n["harvested"], n["total"]) for g, n in tab.grouped(tab.nl) if g != OUTSIDE_KEY and n["total"] > 0]
    iso = [dt.date.fromisoformat(d) for d in tab.dates]
    days = [None] + [(y - x).days for x, y in zip(iso, iso[1:])]
    pace = [None if not n else v / n for v, n in zip(tot["period"], days)]
    last = tab.dates[-1]
    others = {c: tot["other"][(c, last)] for c in tab.other[last]}
    return {"other": sum(others.values()),
            "other_codes": [code_label(c, tab.labels) or c for c, v in others.items() if v > 0],
            "unit": {"text": unit.text, "header": unit.header, "decimals": unit.decimals, "format": area_format(unit)},
            "level_titles": list(titles), "dates": [dmy(d) for d in tab.dates],
            "total": tot["total"], "harvested": tot["harvested"], "remaining": tot["remaining"],
            "period": list(tot["period"]), "days": days, "pace": pace, "cum_harvested": list(tot["cum"]),
            "cum_pct": [c / t if t > 0 else 0.0 for c, t in zip(tot["cum"], totals_at)],
            "by_level1": by1, "top_level2": top2, "finest": finest, "report": report_info["report"],
            "boundary": report_info["boundary"], "previous": previous_name, "projection": pc.crs_label(area_crs)}


def forecast(res):
    """(days still needed, finish date 'DD-MM-YYYY') at the pace of the latest period, or None when it cannot
    be said (one report, no harvest in the latest period, nothing left)."""
    pace = res["pace"][-1]
    eps = 0.5 * 10 ** -res["unit"]["decimals"]
    if pace is None or pace <= 0 or res["remaining"] < eps:
        return None
    need = math.ceil(res["remaining"] / pace)
    d, m, y = (int(x) for x in res["dates"][-1].split("-"))
    return need, dmy((dt.date(y, m, d) + dt.timedelta(days=need)).isoformat())


def key_findings(res):
    """A few plain sentences that say what the numbers mean (drawn on the Dashboard under the tiles)."""
    u = res["unit"]["text"]
    dec = res["unit"]["decimals"]
    T, H, R = res["total"], res["harvested"], res["remaining"]
    dates, nd = res["dates"], len(res["dates"])
    lvl1 = res["level_titles"][0]

    def ar(v):
        return f"{v:,.{dec}f}"

    def pc1(x):
        return f"{x * 100:.1f} %"

    def names(items):
        items = [str(x) if len(str(x)) <= 28 else str(x)[:26] + ".." for x in items]
        return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]
    out = []
    if T <= 0:
        return ["The report holds no crop inside the boundary."]
    if R < 0.5 * 10 ** -dec:
        out.append(f"Harvest is complete: all {ar(T)} {u} of the crop are harvested on {dates[-1]}.")
    else:
        out.append(f"{pc1(H / T)} of the crop is harvested ({ar(H)} of {ar(T)} {u}); {ar(R)} {u} remain "
                   f"on {dates[-1]}.")
    if nd >= 2 and res["pace"][-1] is not None:
        p, n = res["pace"][-1], res["days"][-1]
        text = (f"Latest period ({dates[-2]} to {dates[-1]}, {pc.plural(n, 'day')}): {ar(res['period'][-1])} {u} "
                f"harvested, {p:,.1f} {u} per day")
        prev = res["pace"][-2] if nd >= 3 else None
        if prev and prev > 0:
            chg = p / prev - 1
            text += (f", about the same pace as the period before ({prev:,.1f})" if abs(chg) < 0.02 else
                     f", {abs(chg) * 100:.0f} % {'faster' if chg > 0 else 'slower'} than the period before "
                     f"({prev:,.1f})")
        out.append(text + ".")
        fc = forecast(res)
        if fc:
            out.append(f"At the pace of the latest period the remaining crop would be harvested in about "
                       f"{pc.plural(fc[0], 'day')}, around {fc[1]} (an estimate, not a plan).")
        elif R >= 0.5 * 10 ** -dec:
            out.append("No harvest was measured in the latest period, so no finish date can be estimated.")
    units = [r for r in res["by_level1"] if r[0] != OUTSIDE and not r[0].startswith("Others (") and r[3] > 0]
    if len(units) >= 2 and R > 0:
        top = units[:min(3, len(units) - 1)]                       # by_level1 is sorted by remaining crop
        share = sum(r[2] for r in top) / R
        out.append(f"Where the work is left: {names([r[0] for r in top])} "
                   f"hold{'s' if len(top) == 1 else ''} {share * 100:.0f} % of the remaining crop "
                   f"({ar(sum(r[2] for r in top))} {u}).")
        big = [r for r in units if r[3] >= 0.01 * T]
        if len(big) >= 2:
            best = max(big, key=lambda r: r[1] / r[3])
            worst = min(big, key=lambda r: r[1] / r[3])
            if best[0] != worst[0]:
                out.append(f"Most advanced {lvl1}: {names([best[0]])} ({pc1(best[1] / best[3])} harvested). "
                           f"Least advanced: {names([worst[0]])} ({pc1(worst[1] / worst[3])}).")
    if len(res["level_titles"]) >= 2 and res["finest"]:
        shares = [h / t for h, t in res["finest"]]
        done = sum(x >= 0.95 for x in shares)
        low = sum(x < 0.5 for x in shares)
        out.append(f"Of the {len(shares)} {res['level_titles'][-1]} units with crop, {done} "
                   f"{'is' if done == 1 else 'are'} at least 95 % harvested and {low} "
                   f"{'is' if low == 1 else 'are'} below 50 %.")
    if res["other"] > 0:
        out.append(f"The remaining crop includes {ar(res['other'])} {u} of class {', '.join(res['other_codes'][:3])} "
                   f"(counted as not harvested).")
    neg = sum(1 for v in res["period"] if round(v, dec) < 0)
    if neg:
        out.append(f"Check: the harvested area went down in {pc.plural(neg, 'period')} (red cells on 'Stats'); "
                   f"harvested crop cannot come back.")
    return out


def write_chart_data(cd, res):
    """Write the numbers behind the charts on the sheet `cd` ('Chart data') and return the row ranges.

    Layout (1-based rows; one empty row between blocks; the header of a block is the row above its first row):
      row 1      a note line
      block "a"  level-1 title | Harvested | Remaining | Total crop | Harvested % | Share of the remaining crop
                 one row per entry of res['by_level1'] (most remaining crop first)
      block "b"  Report date | Harvested so far % | Harvested in the period | Harvested so far | Days in the
                 period | Harvested per day; one row per date (the last two are empty for the first report)
      block "e"  (2+ levels only) 'level-2 (level-1)' | Remaining: res['top_level2']
    Returns {"a": (first, last), "b": ..., "e": ... or None}."""
    af = res["unit"]["format"]
    put(cd, 1, 1, f"Numbers behind the Dashboard charts (areas in {res['unit']['text']}). Written by the tool; "
                  f"edit the Stats sheet, not this one.", font=font(10, italic=True, color=C_INK2))
    blocks = {}
    r = 3
    lvl1 = res["level_titles"][0]
    R = res["remaining"]
    put(cd, r, 1, f"Harvested and remaining by {lvl1}", font=font(10, True))
    heads = [lvl1, "Harvested", "Remaining", "Total crop", "Harvested %", "Share of the remaining crop"]
    for j, h in enumerate(heads):
        put(cd, r + 1, 1 + j, h, font=font(10, True))
    for i, (name, h, rem, t) in enumerate(res["by_level1"]):
        put(cd, r + 2 + i, 1, name, font=font(10), number_format="@")
        for j, v in enumerate((h, rem, t)):
            put(cd, r + 2 + i, 2 + j, v, font=font(10), number_format=af)
        put(cd, r + 2 + i, 5, h / t if t > 0 else 0.0, font=font(10), number_format=PCT)
        put(cd, r + 2 + i, 6, rem / R if R > 0 else 0.0, font=font(10), number_format=PCT)
    blocks["a"] = (r + 2, r + 1 + len(res["by_level1"]))
    r = blocks["a"][1] + 2
    nd = len(res["dates"])
    put(cd, r, 1, "Per report date", font=font(10, True))
    heads = ["Report date", "Harvested so far %", "Harvested in the period", "Harvested so far",
             "Days in the period", "Harvested per day"]
    for j, h in enumerate(heads):
        put(cd, r + 1, 1 + j, h, font=font(10, True))
    for i in range(nd):
        put(cd, r + 2 + i, 1, res["dates"][i], font=font(10), number_format="@")
        put(cd, r + 2 + i, 2, res["cum_pct"][i], font=font(10), number_format=PCT)
        put(cd, r + 2 + i, 3, res["period"][i], font=font(10), number_format=af)
        put(cd, r + 2 + i, 4, res["cum_harvested"][i], font=font(10), number_format=af)
        if res["days"][i]:
            put(cd, r + 2 + i, 5, res["days"][i], font=font(10), number_format="0")
            put(cd, r + 2 + i, 6, res["pace"][i], font=font(10), number_format="#,##0.0")
    blocks["b"] = (r + 2, r + 1 + nd)
    r = blocks["b"][1] + 2
    blocks["e"] = None
    if len(res["level_titles"]) >= 2 and res["top_level2"]:
        t2 = res["level_titles"][1]
        put(cd, r, 1, f"Top {len(res['top_level2'])} {t2} by remaining crop", font=font(10, True))
        put(cd, r + 1, 1, f"{t2} ({lvl1})", font=font(10, True))
        put(cd, r + 1, 2, "Remaining", font=font(10, True))
        for i, (label, v) in enumerate(res["top_level2"]):
            put(cd, r + 2 + i, 1, label, font=font(10), number_format="@")
            put(cd, r + 2 + i, 2, v, font=font(10), number_format=af)
        blocks["e"] = (r + 2, r + 1 + len(res["top_level2"]))
    cd.column_dimensions["A"].width = 40
    for col in "BCDEF":
        cd.column_dimensions[col].width = 24
    cd.sheet_view.showGridLines = False
    return blocks


def text_categories(chart, ref):
    """Category labels read as text (strRef), as Excel writes them for text cells."""
    from openpyxl.chart.data_source import AxDataSource, StrRef
    for s in chart.series:
        s.cat = AxDataSource(strRef=StrRef(f=ref))


def cat_ref(cd, first, last, col=1):
    return f"{quote_sheet(cd.title)}!${get_column_letter(col)}${first}:${get_column_letter(col)}${last}"


def quote_sheet(name):
    return "'" + name.replace("'", "''") + "'"


DASH_WIDTHS = [2, 29, 2, 29, 2, 29, 2, 29, 2, 29, 2]      # A..K: five wide columns with narrow gaps


def _bar(cd, first, last, cols, title, value_title, colors, horizontal=True, stacked=False):
    """A bar / column chart of the columns `cols` (header in the row above `first`), categories in column A."""
    ch = BarChart()
    ch.type = "bar" if horizontal else "col"
    ch.gapWidth = 55 if horizontal else 80
    if stacked:
        ch.grouping, ch.overlap = "stacked", 100
    for col in cols:
        ch.add_data(Reference(cd, min_col=col, min_row=first - 1, max_row=last), titles_from_data=True)
    text_categories(ch, cat_ref(cd, first, last))
    style_chart(ch, title, None, value_title)
    for s, colr in zip(ch.series, colors):
        color_series(s, colr)
    if horizontal:
        ch.x_axis.scaling.orientation = "maxMin"        # first row at the top
        ch.y_axis.crosses = "max"                       # value axis stays at the bottom
    if len(cols) == 1:
        ch.legend = None
    return ch


def _x_title(ch, text):
    ch.x_axis.title = text
    ch.x_axis.title.overlay = False
    ch.x_axis.title.tx.rich.p[0].pPr = ParagraphProperties(defRPr=CharacterProperties(
        latin=DFont(typeface=FONT), sz=900, b=False, solidFill=C_INK2))


def write_dashboard(dash, cd, res):
    """Draw the Dashboard sheet `dash` from the plain results `res` (see dashboard_results). Top to bottom:
      a title band; five tiles (crop, harvested, remaining, pace, estimated finish); 'Key findings' in plain
      sentences (key_findings); the charts, two per row; a table per level-1 unit; notes.
    Each chart answers one question (native, editable; numbers on the sheet `cd`, see write_chart_data):
      progress   how far is the harvest?      line, share of the crop harvested    block b, column B  (2+ reports)
      pace       is it speeding up?           columns, area harvested per day      block b, column F  (2+ reports)
      area       where is the crop left?      stacked bars per level-1 unit        block a, columns B:C
      top        which level-2 units first?   bars, most remaining crop            block e, column B  (2+ levels)
    No computation of areas happens here: every number comes from `res`."""
    blocks = write_chart_data(cd, res)
    af = res["unit"]["format"]
    utext = res["unit"]["text"]
    dates = res["dates"]
    nd, last = len(dates), dates[-1]
    T, H, R = res["total"], res["harvested"], res["remaining"]
    lvl1 = res["level_titles"][0]
    widths = DASH_WIDTHS
    ncol = len(widths)
    wide = [2, 4, 6, 8, 10]                                 # the five wide columns (B, D, F, H, J)

    dash.sheet_view.showGridLines = False
    for i, w in enumerate(widths, 1):
        dash.column_dimensions[get_column_letter(i)].width = w

    # ---- title band
    for r, h in ((1, 8), (2, 32), (3, 18), (4, 8)):
        dash.row_dimensions[r].height = h
        for c in range(1, ncol + 1):
            dash.cell(row=r, column=c).fill = fill(C_HEAD)
    put(dash, 2, 2, "Harvest monitoring dashboard", font=font(20, True, "FFFFFF"), fill=fill(C_HEAD),
        alignment=Alignment(vertical="center"))
    span = (f"Report {nd} of the season ({dates[0]} to {last})" if nd > 1 else "First report of the season")
    put(dash, 3, 2, f"Report date {last}   |   Areas in {utext}   |   {span}", font=font(10, color="DDE3D5"),
        fill=fill(C_HEAD), alignment=Alignment(vertical="center"))
    dash.row_dimensions[5].height = 10

    # ---- tiles
    hp = H / T if T > 0 else 0.0
    rp = R / T if T > 0 else 0.0
    fc = forecast(res)
    done = R < 0.5 * 10 ** -res["unit"]["decimals"]
    tiles = [("TOTAL CROP AREA", T, af, utext, C_INK2),
             ("HARVESTED SO FAR", H, af, f"{hp * 100:.1f} % of the crop", C_HARVEST),
             ("REMAINING", R, af, f"{rp * 100:.1f} % of the crop", C_REMAIN)]
    if nd > 1 and res["pace"][-1] is not None:
        tiles.append(("HARVEST PACE, LATEST PERIOD", res["pace"][-1], "#,##0.0",
                      f"{utext} per day over {pc.plural(res['days'][-1], 'day')}", C_HARVEST))
    else:
        tiles.append(("HARVEST PACE", "-", "@", "known from the second report", C_INK2))
    if done:
        tiles.append(("ESTIMATED FINISH", "Complete", "@", "nothing left to harvest", C_REMAIN))
    elif fc:
        tiles.append(("ESTIMATED FINISH", fc[1], "@", f"about {pc.plural(fc[0], 'day')} at the latest pace",
                      C_REMAIN))
    else:
        tiles.append(("ESTIMATED FINISH", "-", "@", "known from the second report" if nd < 2
                      else "no harvest in the latest period", C_INK2))
    for (label, val, fmt, sub, accent), c in zip(tiles, wide):      # shrink_to_fit: no text leaves its tile
        put(dash, 6, c, label, font=font(8, True, C_INK2), fill=fill(C_TILE),
            border=Border(top=Side(style="thick", color=accent)),
            alignment=Alignment(horizontal="left", vertical="center", shrink_to_fit=True))
        put(dash, 7, c, val, font=font(18, True, C_INK), fill=fill(C_TILE), number_format=fmt,
            alignment=Alignment(horizontal="left", vertical="center", shrink_to_fit=True))
        put(dash, 8, c, sub, font=font(9, True, accent), fill=fill(C_TILE),
            border=Border(bottom=THIN), alignment=Alignment(horizontal="left", vertical="center", shrink_to_fit=True))
    dash.row_dimensions[6].height = 20
    dash.row_dimensions[7].height = 32
    dash.row_dimensions[8].height = 20
    dash.row_dimensions[9].height = 12

    # ---- key findings
    row = 10
    put(dash, row, 2, "Key findings", font=font(12, True, C_INK))
    for text in key_findings(res):
        row += 1
        put(dash, row, 2, "-  " + text, font=font(10, color=C_INK))
        dash.row_dimensions[row].height = 17
    row += 2

    # ---- charts
    a0, a1 = blocks["a"]
    b0, b1 = blocks["b"]
    if a1 > a0 and res["by_level1"][-1][0] == OUTSIDE and res["by_level1"][-1][3] < 0.001 * T:
        a1 -= 1                 # a trace of crop outside the boundary: in the table below, not in the charts
    n1 = a1 - a0 + 1
    h_std = 310
    charts = []
    if nd >= 2:
        ln = LineChart()
        ln.add_data(Reference(cd, min_col=2, min_row=b0 - 1, max_row=b1), titles_from_data=True)
        text_categories(ln, cat_ref(cd, b0, b1))
        style_chart(ln, "How far is the harvest? Share of the crop harvested so far", "Report date", None)
        color_series(ln.series[0], C_HARVEST, line=True)
        ln.legend = None
        ln.y_axis.number_format = "0%"
        ln.y_axis.scaling.min = 0
        ln.y_axis.scaling.max = 1
        ln.y_axis.majorUnit = 0.2
        if nd <= 12:
            ln.series[0].dLbls = data_labels("0.0%", "t", bold=True)
        charts.append((ln, h_std))
        cc = _bar(cd, b0 + 1, b1, [6], f"Is it speeding up? {utext.capitalize()} harvested per day in each period",
                  None, [C_HARVEST], horizontal=False)
        cc.series[0].tx = None                               # one series: the title says what it is
        _x_title(cc, "Period ending on")
        cc.y_axis.number_format = "#,##0"
        if nd <= 13:
            cc.series[0].dLbls = data_labels("#,##0.0", "outEnd", bold=True)
        charts.append((cc, h_std))
    h_low = max(h_std, 110 + 26 * n1)
    if blocks["e"]:
        e0, e1 = blocks["e"]
        n_e = e1 - e0 + 1
        h_low = max(h_low, 110 + 26 * n_e)
    ar = _bar(cd, a0, a1, [2, 3], f"Where is the crop left? Harvested and remaining by {lvl1} ({utext})", None,
              [C_HARVEST, C_REMAIN], stacked=True)
    ar.y_axis.number_format = "#,##0"
    charts.append((ar, h_low))
    if blocks["e"]:
        tb = _bar(cd, e0, e1, [2], f"Which {res['level_titles'][1]} hold the most remaining crop? ({utext})",
                  None, [C_REMAIN])
        tb.y_axis.number_format = "#,##0"
        tb.series[0].dLbls = data_labels("#,##0", "outEnd", bold=True)
        charts.append((tb, h_low))

    left_x, gap = col_px(widths[0]), 20
    full = sum(col_px(w) for w in widths[1:10])             # columns B..J
    cw = (full - gap) // 2
    for i in range(0, len(charts), 2):
        pair = charts[i:i + 2]
        h = max(x[1] for x in pair)
        for j, (ch, _) in enumerate(pair):
            place(dash, ch, left_x + j * (cw + gap), row, cw if len(pair) == 2 else full, h, widths)
        row += h // 20 + 2
    if nd < 2:
        put(dash, row, 2, "The progress line and the harvest pace appear from the second report on.",
            font=font(9, italic=True, color=C_INK2))
        row += 2

    # ---- table per level-1 unit
    put(dash, row, 2, f"Progress by {lvl1} (most remaining crop first)", font=font(12, True, C_INK))
    row += 1
    heads = [lvl1, f"Total crop ({utext})", f"Remaining ({utext})", "Harvested %", "Share of the remaining crop"]
    for c in range(2, 11):
        dash.cell(row=row, column=c).fill = fill(C_HEAD)
    for h, c in zip(heads, wide):
        put(dash, row, c, h, font=font(10, True, "FFFFFF"), fill=fill(C_HEAD),
            alignment=Alignment(horizontal="left" if c == 2 else "right", vertical="center", shrink_to_fit=True))
    dash.row_dimensions[row].height = 22
    first = row + 1
    lines = [(n, t, h, rem) for n, h, rem, t in res["by_level1"]] + [("Totals", T, H, R)]
    for i, (name, t, h, rem) in enumerate(lines):
        row += 1
        is_tot = i == len(lines) - 1
        bg = fill(C_TOTAL) if is_tot else (fill(C_BAND) if i % 2 else None)
        edge = Border(top=MED, bottom=MED) if is_tot else Border(bottom=THIN)
        for c in range(2, 11):
            cell = dash.cell(row=row, column=c)
            cell.border = edge
            if bg is not None:
                cell.fill = bg
        put(dash, row, 2, name, font=font(10, is_tot))
        put(dash, row, 4, t, font=font(10, is_tot), number_format=af)
        put(dash, row, 6, rem, font=font(10, is_tot), number_format=af)
        put(dash, row, 8, h / t if t > 0 else 0.0, font=font(10, True), number_format=PCT)
        put(dash, row, 10, rem / R if R > 0 else 0.0, font=font(10, True), number_format=PCT)
    dash.conditional_formatting.add(
        f"H{first}:H{row}", DataBarRule(start_type="num", start_value=0, end_type="num", end_value=1,
                                        color=C_HARVEST, showValue=True))
    if row - 1 >= first:
        dash.conditional_formatting.add(
            f"J{first}:J{row - 1}", DataBarRule(start_type="num", start_value=0, end_type="num", end_value=1,
                                                color="8A9440", showValue=True))
    row += 2

    # ---- notes
    notes = [f"Report: {res['report']}   |   Boundary: {res['boundary']}"
             + (f"   |   Previous workbook: {res['previous']}" if res["previous"] else ""),
             f"Projection for areas: {res['projection']}   |   Levels: " + " > ".join(res["level_titles"]),
             "Remaining = every class that is not harvested. Pace = area harvested in a period / its days. The "
             "finish date assumes the pace of the latest period.",
             "The charts read their numbers from the hidden sheet 'Chart data'; the full table is on 'Stats'."]
    for text in notes:
        put(dash, row, 2, text, font=font(9, italic=True, color=C_INK2))
        row += 1
    dash.print_area = f"A1:{get_column_letter(ncol)}{row}"
    dash.page_setup.orientation = "portrait"
    dash.page_setup.fitToWidth = 1
    dash.page_setup.fitToHeight = 0
    dash.sheet_properties.pageSetUpPr.fitToPage = True
    return blocks


def workbook_from_saved(src, pivot=True):
    """Rebuild the whole workbook from the hidden data of a workbook this tool wrote (no overlay, seconds).
    Returns (workbook, facts). For quick work on the layout: save the result under another name."""
    meta, values, read_lab = read_previous(src)
    unit = pc.get_unit(meta["unit"])
    labels = merge_labels(meta.get("labels"), read_lab)
    tab = Table3(values, meta["reports"], meta["levels"], labels, unit)
    tab.levels_titles = [lv["title"] for lv in meta["levels"]]
    last = max(meta["reports"], key=lambda r: r["date"])
    info = {"report": (last.get("file") or {}).get("name", "?"), "boundary": (meta.get("boundary") or {}).get("name", "?")}
    meta = dict(meta)
    for k in ("rows", "checksum"):
        meta.pop(k, None)
    return build_workbook(tab, meta["levels"], unit, pyproj.CRS(meta["crs"]), meta, info, None, pivot)


# ---------------------------------------------------------------- output
def excel_lock(out):
    """A reason why the output cannot be written now (open in Excel), else None."""
    out = Path(out)
    owner = out.with_name("~$" + out.name)
    if owner.exists():
        return f"{owner.name} exists next to it, so Excel has it open"
    if out.exists():
        try:
            with open(out, "r+b"):
                pass
        except OSError as e:
            return str(e.strerror or e)
    return None


def save_atomic(wb, out):
    """Save under a temporary name in the same folder, then replace the target. If the target cannot be
    replaced (open in Excel), the new file is kept under another name and that name is returned with a note."""
    out = Path(out)
    tmp = out.with_name(f".{out.stem}.tmp{os.getpid()}.xlsx")
    try:
        wb.save(str(tmp))
        with zipfile.ZipFile(tmp) as z:                  # the saved file is a complete package
            bad = z.testzip()
            if bad:
                raise UserError(f"the saved workbook is damaged ({bad}); nothing was replaced")
        try:
            os.replace(tmp, out)
            return out, None
        except OSError as e:
            alt = out.with_name(out.stem + "_new_" + time.strftime("%Y%m%d_%H%M%S") + out.suffix)
            os.replace(tmp, alt)
            return alt, (f"could not replace {out.name} ({e.strerror or e}); is it open in Excel? The old file is "
                         f"unchanged and the new workbook is saved as {alt}")
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def made_by_this_tool(p):
    """True when an existing .xlsx holds the hidden data sheet of this tool."""
    try:
        with zipfile.ZipFile(p) as z:
            return DATA_SHEET in z.read("xl/workbook.xml").decode("utf-8", "replace")
    except Exception:
        return False


# ---------------------------------------------------------------- last-run record
def save_last_run(info):
    pc.save_record(__file__, "stats", HOME_DIR, info)


def load_last_run():
    d = pc.load_record(__file__, "stats", HOME_DIR)
    return d if d and d.get("boundary") and d.get("report") and d.get("out") else None


# ---------------------------------------------------------------- run
def resolve_class_field(report, class_field=None):
    names = [str(n) for n in pc.vector_info(report).get("fields", [])]
    if class_field:
        if class_field in names:
            return class_field
        low = [n for n in names if n.casefold() == class_field.casefold()]
        if len(low) == 1:
            return low[0]
        raise UserError(f"the report {Path(report).name} has no field '{class_field}'; its fields: "
                        + (", ".join(names) or "none"))
    low = [n for n in names if n.casefold() == "predicted"]
    if len(low) == 1:
        return low[0]
    raise UserError(f"the report {Path(report).name} has no field 'predicted'; choose the class field with "
                    f"--class-field (its fields: {', '.join(names) or 'none'})")


def check_levels(levels, boundary):
    names = [str(n) for n in pc.vector_info(boundary).get("fields", [])]
    if not levels:
        raise UserError("choose at least one level field")
    seen = set()
    for lv in levels:
        f = lv["field"]
        if f not in names:
            raise UserError(f"the boundary layer {Path(boundary).name} has no field '{f}'; its fields: "
                            + ", ".join(names))
        if f in seen:
            raise UserError(f"the field '{f}' is chosen twice; every level needs its own field")
        seen.add(f)
    return levels


def crs_text(crs):
    return f"EPSG:{crs.to_epsg()}" if crs.to_epsg() else crs.to_wkt()


def check_previous(meta, levels, unit, area_crs, date, boundary):
    """The previous workbook must match: the same level fields, unit and projection; a new, later date."""
    notes = []
    pf = [x["field"] for x in meta["levels"]]
    nf = [x["field"] for x in levels]
    if pf != nf:
        raise UserError(f"the previous workbook has the level fields {', '.join(pf)} but this run uses "
                        f"{', '.join(nf)}. The levels must stay the same for the whole season (same fields, same "
                        f"order); choose those fields or start a new season without a previous workbook.")
    pu = pc.get_unit(meta["unit"])
    if pu.key != unit.key:
        raise UserError(f"the previous workbook is in {pu.text}, this run in {unit.text}. The unit must stay the "
                        f"same for the whole season, because the history is not recomputed; use {pu.text}.")
    pcrs = pyproj.CRS(meta["crs"])
    if pcrs != area_crs:
        raise UserError(f"the previous workbook used {pc.crs_label(pcrs)} for areas, this run {pc.crs_label(area_crs)}. "
                        f"The projection must stay the same for the whole season, because the history is not "
                        f"recomputed; use {pc.crs_label(pcrs)}.")
    dates = [r["date"] for r in meta["reports"]]
    if date in dates:
        k = dates.index(date)
        before = (f"the workbook of the report of {dmy(dates[k - 1])}" if k > 0
                  else "no previous workbook (this date was the first report)")
        raise UserError(f"the previous workbook already contains the report of {dmy(date)}. To make this report's "
                        f"workbook again, give {before}.")
    if date < max(dates):
        raise UserError(f"the report date {dmy(date)} is earlier than the last report in the previous workbook "
                        f"({dmy(max(dates))}). Reports must be added in date order.")
    pb = (meta.get("boundary") or {}).get("name")
    if pb and pb != Path(boundary).name:
        notes.append(f"the previous workbook was made with the boundary file {pb}; this run uses "
                     f"{Path(boundary).name} (the level fields are the same).")
    return notes


def run(boundary, levels, report, date, out, previous=None, utm=None, unit=None, class_field=None,
        allow_non_utm=False, pivot=True, boundary_layer=None, report_layer=None, workers=None,
        class_names=None):
    """levels = [{'field': name, 'title': title}] from the largest unit inwards; date = 'YYYY-MM-DD';
    class_names = {code: name} for the codes other than 81 / 82 (a name replaces the code in the headers)."""
    pc.start_clock()
    t0 = time.time()
    unit = pc.get_unit(unit)
    boundary, report, out = Path(boundary).resolve(), Path(report).resolve(), Path(out).resolve()
    previous = Path(previous).resolve() if previous else None
    workers = max(1, min(workers or pc.n_cpus(), pc.n_cpus()))
    if out.suffix.lower() != ".xlsx":
        raise UserError("the output must end with .xlsx")
    if not out.parent.is_dir():
        raise UserError(f"output folder does not exist: {out.parent}")
    if not pc.folder_writable(out.parent):
        raise UserError(f"cannot write in the output folder {out.parent}")
    lock = excel_lock(out)
    if lock:
        raise UserError(f"the output {out.name} is open in another program ({lock}). Close it in Excel and run "
                        f"again, or choose another output name.")
    for p, what, lay, flag in ((boundary, "boundary", boundary_layer, "--boundary-layer"),
                               (report, "report", report_layer, "--report-layer")):
        if not p.is_file():
            raise UserError(f"{what} file not found: {p}")
        if p.suffix.lower() not in pc.VECTOR_EXTS:
            raise UserError(f"the {what} layer must be a vector file. {pc.FORMAT_HINT}")
        pc.choose_layer(p, lay, what, flag)
    levels = check_levels([dict(field=lv["field"], title=clean_text(lv.get("title") or lv["field"], 250)
                                or lv["field"]) for lv in levels], boundary)
    class_field = resolve_class_field(report, class_field)
    date = dt.date.fromisoformat(str(date)).isoformat()
    notes, warns = [], []
    rcrs, bcrs = layer_crs(report), layer_crs(boundary)
    assume = {}
    if not rcrs and not bcrs:
        raise UserError("neither the report nor the boundary layer has a coordinate system (.prj missing)")
    if not bcrs:
        assume["boundary"] = rcrs
        notes.append(f"the boundary layer has no coordinate system; it is taken to be {pc.crs_label(rcrs)} like the report.")
    if not rcrs:
        assume["report"] = bcrs
        notes.append(f"the report has no coordinate system; it is taken to be {pc.crs_label(bcrs)} like the boundary.")
    for n in notes:
        print(f"NOTE: {n}")
    bounds = layer_bounds(report) or layer_bounds(boundary)
    if bounds is None:
        raise UserError("the report and the boundary layer have no features")
    area_crs = pc.resolve_area_crs(utm, rcrs or bcrs, bounds, allow_non_utm)

    prev_meta, prev_values, labels = None, {}, {}
    if previous:
        if previous == out:
            notes.append(f"the new workbook replaces the previous one ({out.name}); its history is carried over.")
            print(f"NOTE: {notes[-1]}")
        prev_meta, prev_values, read_lab = read_previous(previous)
        for n in check_previous(prev_meta, levels, unit, area_crs, date, boundary):
            print(f"NOTE: {n}")
            notes.append(n)
        labels = merge_labels(prev_meta.get("labels"), read_lab)
        for (c, d), text in sorted(read_lab.items()):
            old = {(x["code"], x["date"]): x["text"] for x in prev_meta.get("labels") or []}
            if old.get((c, d)) != text:
                n = f"your column title '{text}' for code {c} on {dmy(d)} is kept."
                print(f"NOTE: {n}")
                notes.append(n)

    if class_names:
        present = set(other_codes(report, class_field))
        for c, name in class_names.items():
            c = str(c)
            if c in present:
                labels[(c, date)] = f"{name} on {dmy(date)}"       # carried to later reports (code_label)
            else:
                n = f"the name '{name}' was given for class {c}, but the report holds no such class; it is ignored."
                print(f"NOTE: {n}")
                notes.append(n)

    info = {"boundary": str(boundary), "report": str(report), "out": str(out), "date": date,
            "previous": str(previous) if previous else None, "utm": crs_text(area_crs), "unit": unit.key,
            "levels": levels, "class_field": class_field, "pivot": bool(pivot),
            "boundary_layer": pc.layer_kw(boundary).get("layer"), "report_layer": pc.layer_kw(report).get("layer"),
            "allow_non_utm": bool(allow_non_utm), "started": time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "running", "pid": os.getpid()}
    save_last_run(info)
    try:
        res = _run(boundary, levels, report, date, out, previous, area_crs, unit, class_field, pivot, workers,
                   prev_meta, prev_values, labels, notes, warns, assume, t0)
    except KeyboardInterrupt:
        save_last_run(dict(info, status="interrupted"))
        raise
    except BaseException as e:
        save_last_run(dict(info, status="failed", progress=f"{type(e).__name__}: {e}"[:300]))
        raise
    save_last_run(dict(info, status="finished", out=str(res["written"])))
    return res


def _run(boundary, levels, report, date, out, previous, area_crs, unit, class_field, pivot, workers,
         prev_meta, prev_values, labels, notes, warns, assume, t0):
    fields = [lv["field"] for lv in levels]
    rep = compute_report(boundary, fields, report, class_field, area_crs, unit, workers, notes, assume)
    if rep["overlap"] > 0:
        n = (f"boundary polygons overlap each other ({pc.area_text(rep['overlap'], unit)} {unit.text} in all); "
             f"crop there is counted once, for the polygon that comes first in the boundary file.")
        print(f"NOTE: {n}")
        notes.append(n)
    file_total = sum(rep["totals"].values())
    eps = 0.5 * 10 ** -unit.decimals

    # ---- checks against the previous workbook
    values = {k: dict(v) for k, v in prev_values.items()}
    reports = list(prev_meta["reports"]) if prev_meta else []
    if prev_meta:
        pd_ = max(r["date"] for r in reports)
        prev_tot = {k: sum(v.get(pd_, {}).values()) for k, v in prev_values.items()}
        prev_h = sum(v.get(pd_, {}).get(HARVEST, 0.0) for v in prev_values.values())
        prev_T = sum(prev_tot.values())
        prev_R = prev_T - prev_h
        d_tot, d_rem = abs(file_total - prev_T), abs(file_total - prev_R)
        if prev_h > 0 and d_tot > 0.01 * prev_T and d_rem < d_tot:
            raise UserError(
                f"this report looks INCREMENTAL, not cumulative: its total area {pc.area_text(file_total, unit)} "
                f"{unit.text} is close to the area that was still remaining on {dmy(pd_)} "
                f"({pc.area_text(prev_R, unit)}) and not to the total crop area ({pc.area_text(prev_T, unit)}). "
                f"This tool needs cumulative reports, which cover the whole crop area every time (81 + 82 = the "
                f"whole crop). Use the cumulative report of {dmy(date)}.")
        now_tot = {k: sum(v.values()) for k, v in rep["values"].items()}
        if sum(rep["outside"].values()):
            now_tot[OUTSIDE_KEY] = sum(rep["outside"].values())
        changed = []
        for k in set(prev_tot) | set(now_tot):
            a, b = prev_tot.get(k, 0.0), now_tot.get(k, 0.0)
            if abs(b - a) > TOTAL_TOL_ABS and abs(b - a) > TOTAL_TOL_REL * max(abs(a), abs(b)):
                changed.append((k, a, b))
        gone = [k for k in prev_tot if prev_tot[k] >= eps and now_tot.get(k, 0.0) < eps]
        new = [k for k in now_tot if now_tot[k] >= eps and prev_tot.get(k, 0.0) < eps]
        if gone:
            warn(warns, f"{pc.plural(len(gone), 'row')} of the previous workbook {'has' if len(gone) == 1 else 'have'} "
                        f"no crop in this report (kept with zeros): " + names_text(gone, levels))
        if new:
            warn(warns, f"{pc.plural(len(new), 'row')} {'is' if len(new) == 1 else 'are'} new in this report "
                        f"(zeros for the earlier reports): " + names_text(new, levels))
        changed = [c for c in changed if c[0] not in gone and c[0] not in new]
        if changed:
            changed.sort(key=lambda c: -abs(c[2] - c[1]))
            items = [f"{key_name(k, levels)} {pc.area_text(a, unit)} -> {pc.area_text(b, unit)} "
                     f"({'+' if b >= a else ''}{pc.area_text(b - a, unit)})" for k, a, b in changed[:10]]
            more = f" and {len(changed) - 10} more" if len(changed) > 10 else ""
            warn(warns, f"the total crop area changed since {dmy(pd_)} (more than 0.01 %) in "
                        f"{pc.plural(len(changed), 'row')}: " + "; ".join(items) + more
                 + ". Cumulative reports should keep each row's total; check the report.")

    # ---- add this report
    for k, v in rep["values"].items():
        values.setdefault(k, {})[date] = dict(v)
    if rep["outside"]:
        values.setdefault(OUTSIDE_KEY, {})[date] = dict(rep["outside"])
    reports.append({"date": date, "codes": rep["codes"], "file": file_sig(report), "class_field": class_field,
                    "total": file_total})
    reports.sort(key=lambda r: r["date"])
    tab = Table3(values, reports, levels, labels, unit, rep["all_keys"])
    tab.levels_titles = [lv["title"] for lv in levels]
    outside_now = sum(rep["outside"].values())
    if tab.outside:
        n = (f"{pc.area_text(outside_now, unit)} {unit.text} of crop lie outside every boundary polygon in this "
             f"report; they are in the row '{OUTSIDE}'.")
        print(f"NOTE: {n}")
        notes.append(n)

    # ---- negative periods
    neg = []
    for k in tab.keys:
        nums = tab.numbers(k)
        for i, d in enumerate(tab.dates):
            if round(nums["period"][i], unit.decimals) < 0:
                neg.append((k, d, nums["period"][i]))
    if neg:
        items = [f"{key_name(k, levels)} on {dmy(d)}: {pc.area_text(v, unit)}" for k, d, v in neg[:10]]
        warn(warns, f"the harvested area went DOWN (negative harvest in the period) in {pc.plural(len(neg), 'case')}: "
                    + "; ".join(items) + (f" and {len(neg) - 10} more" if len(neg) > 10 else "")
                    + ". The values are written as they are and highlighted.")

    # ---- write
    pc.tick("writing the workbook")
    meta = {"tool": TOOL, "format_version": FORMAT_VERSION, "unit": unit.key, "crs": crs_text(area_crs),
            "levels": levels, "boundary": file_sig(boundary), "reports": reports,
            "labels": [{"code": c, "date": d, "text": t} for (c, d), t in sorted(labels.items())],
            "written": time.strftime("%Y-%m-%d %H:%M:%S")}
    wb, facts = build_workbook(tab, levels, unit, area_crs, meta,
                               {"report": report.name, "boundary": boundary.name},
                               previous.name if previous else None, pivot)
    written, note = save_atomic(wb, out)
    if note:
        warn(warns, note)
    pc.tick("done")

    # ---- result
    tot = tab.totals()
    table_total = tot["total"]
    diff = table_total - file_total
    ok = abs(diff) <= max(CHECK_TOL_REL * abs(file_total), 10 ** -unit.decimals)
    res = {"written": str(written), "out": str(out), "totals": tot, "file_total": file_total, "table_total": table_total,
           "check_ok": ok, "rows": len(tab.keys), "left_out": len(tab.left_out), "left_out_area": tab.left_out_area,
           "outside": outside_now, "warnings": warns, "notes": notes, "dates": tab.dates, "facts": facts,
           "values": values, "codes": rep["codes"], "unit": unit.key}
    print_result(tab, res, unit, area_crs, written, previous, levels, time.time() - t0, rep)
    return res


def key_name(k, levels):
    if k == OUTSIDE_KEY:
        return OUTSIDE
    return " / ".join(str(shown(v)) for v in k)


def names_text(keys, levels, limit=10):
    keys = sorted(keys, key=lambda k: (k == OUTSIDE_KEY, row_sort_key(k) if k != OUTSIDE_KEY else ()))
    t = ", ".join(key_name(k, levels) for k in keys[:limit])
    return t + (f" and {len(keys) - limit} more" if len(keys) > limit else "")


def print_result(tab, res, unit, area_crs, written, previous, levels, seconds, rep):
    tot = res["totals"]
    T, H, R = tot["total"], tot["harvested"], tot["remaining"]
    a = lambda v: pc.area_text(v, unit)          # noqa: E731
    p = lambda v: pc.pct_text(v, T).rjust(9)     # noqa: E731
    last = dmy(tab.dates[-1])
    period = (f"  Harvested in this period ({dmy(tab.dates[-2])} to {last})" if len(tab.dates) > 1
              else f"  Harvested in this period (first report, {last})")
    table = [(f"Report of {last}", unit.header, "% of crop"),
             ("  Total crop area", a(T), p(T)),
             ("  Harvested so far (82)", a(H), p(H)),
             ("  Remaining (all codes but 82)", a(R), p(R)),
             (period, a(tot["period"][-1]), p(tot["period"][-1]))]
    for c in tab.other[tab.dates[-1]]:
        v = tot["other"][(c, tab.dates[-1])]
        table.append((f"    of which code {c} (counted as remaining)", a(v), p(v)))
    diff = res["table_total"] - res["file_total"]
    pct = 100 * diff / res["file_total"] if res["file_total"] else 0.0
    check = [("  Report total (all polygons)", a(res["file_total"])),
             ("  Table total (all rows)", a(res["table_total"])),
             ("  Difference", a(diff), f"({abs(pct):.4f} %)  {'OK' if res['check_ok'] else 'CHECK'}")]
    if res["outside"] > 0:
        check.extend(pc.wrap_rows(f"Crop outside the boundary: {a(res['outside'])} {unit.text}"
                                  + (f" (row '{OUTSIDE}')." if tab.outside else " (too small for a row).")))
    if not res["check_ok"]:
        check.extend(pc.wrap_rows(f"WARNING: the table total differs from the report total by {pct:.4f} % "
                                  f"(more than 0.01 %)."))
    rows = [("  Rows written (combinations of the levels)", f"{len(tab.keys) - (1 if tab.outside else 0):,}"),
            ("  Left out: combinations without crop", f"{len(tab.left_out):,}")]
    if tab.left_out_area > 0:
        small = 10 ** -unit.decimals
        amount = a(tab.left_out_area) if round(tab.left_out_area, unit.decimals) > 0 else f"less than {small:g}"
        rows.extend(pc.wrap_rows(f"The left-out combinations hold {amount} {unit.text} of crop in this report "
                                 f"(each rounds to zero)."))
    rows.append(("  Boundary features / distinct combinations", f"{rep['n_features']:,} / {rep['n_keys']:,}"))
    rows.append(("  Crop polygons in the report", f"{rep['n_crop']:,}"))
    w = res["warnings"]
    warn_rows = [(f"  {len(w)} warning{'s' if len(w) != 1 else ''} above" if w else "  none",)]
    lv_text = " > ".join(lv["field"] + (f" ('{lv['title']}')" if lv["title"] != lv["field"] else "") for lv in levels)
    settings = [(f"  Levels      : {lv_text}",),
                (f"  Projection  : {pc.crs_label(area_crs)}",),
                (f"  Unit        : {unit.text}",),
                (f"  Reports     : {len(tab.dates)} ({', '.join(dmy(d) for d in tab.dates)})",)]
    files = [(f"  Workbook : {written}",)]
    if previous:
        files.append((f"  Previous : {previous}",))
    if not res["facts"].get("pivot"):
        files.append(("  Pivot    : left out (--no-pivot); the plain cross-table is on the Pivot sheet",))
    sections = [("", table), ("Area check (the table must hold the whole report)", check),
                ("Rows", rows), ("Warnings", warn_rows), ("Settings used", settings), ("Files", files),
                ("", [(f"Time     : {seconds:.1f} s",)])]
    pc.print_result_block("RESULT", sections)


# ---------------------------------------------------------------- question mode
def _xlsx_out_check(raw):
    p = Path(pc.clean_path(raw))
    stem = None
    if p.suffix.lower() in (".xlsx", ".xls", ".xlsm", ".csv"):
        stem, p = p.stem, p.parent
    r, hint = pc._folder_check(str(p))
    if hint:
        return None, hint
    return (r[0], stem), None


def _xlsx_name_check(a):
    a = a.strip()
    if Path(a).suffix.lower() in (".xlsx", ".xls", ".xlsm", ".csv"):
        a = a[:-len(Path(a).suffix)].rstrip()
    return pc.output_name_check(a)


def ask_out(default_dir, default_stem, previous=None, label="7) Output folder"):
    folder, stem = pc.ask_text(label, str(default_dir), _xlsx_out_check)
    stem_def = stem or default_stem
    while True:
        stem = pc.ask_text("   Output name (saved as .xlsx)", stem_def, _xlsx_name_check)
        out = folder / (stem + ".xlsx")
        stem_def = stem
        lock = excel_lock(out)
        if lock:
            print(f"   {out.name} is open in another program ({lock}). Close it in Excel or choose another name.")
            continue
        if out.exists():
            if previous and out.resolve() == Path(previous).resolve():
                if not pc.ask_yes_no(f"   {out.name} is the workbook of the previous report. Replace it with the "
                                     f"new one (its history is carried over)?", False):
                    continue
            elif made_by_this_tool(out):
                if not pc.ask_yes_no(f"   {out.name} was made by an earlier run of this tool. Replace it?", True):
                    continue
            elif not pc.ask_yes_no(f"   {out.name} already exists. Overwrite?", False):
                continue
        return out


def ask_levels(fields, defaults):
    """'How many levels ...' then the field of every level (number or name, never twice) and its title."""
    names = [f[0] for f in fields]
    old = defaults.get("levels") or []
    old_ok = old if all(lv.get("field") in names for lv in old) else []
    n_def = str(len(old_ok) or 1)

    def n_check(a):
        if a.strip().isdigit() and 1 <= int(a) <= len(names):
            return int(a), None
        return None, f"Type a number from 1 to {len(names)}."
    n = pc.ask_text("2) How many levels do you want statistics for?", n_def, n_check)
    levels = []
    for i in range(n):
        used = [lv["field"] for lv in levels]

        def f_check(a, used=used):
            a = a.strip()
            if a.isdigit() and 1 <= int(a) <= len(names):
                name = names[int(a) - 1]
            elif a in names:
                name = a
            else:
                low = [x for x in names if x.casefold() == a.casefold()]
                if len(low) != 1:
                    return None, f"Type a number from the list (1 to {len(names)}) or a field name."
                name = low[0]
            if name in used:
                return None, f"{name} is already level {used.index(name) + 1}. Choose another field."
            return name, None
        dflt = None
        if i < len(old_ok) and old_ok[i]["field"] not in used:
            dflt = str(names.index(old_ok[i]["field"]) + 1)
        q = (f"   Field for level {i + 1} (the largest areas)" if i == 0
             else f"   Field for level {i + 1} (inside level {i})")
        name = pc.ask_text(q, dflt, f_check)
        t_def = name
        if i < len(old_ok) and old_ok[i]["field"] == name and old_ok[i].get("title"):
            t_def = old_ok[i]["title"]

        def t_check(a):
            t = clean_text(a, 250)
            return (t, None) if t else (None, "Type a title, or press Enter to keep the field name.")
        title = pc.ask_text("   Column title for this level", t_def, t_check)
        levels.append({"field": name, "title": title})
    return levels


def _prev_check(levels, boundary_fields):
    def check(a):
        if a.strip() in ("0", "-"):
            return "", None
        p = Path(pc.clean_path(a))
        try:
            meta, _, _ = read_previous(p)
        except UserError as e:
            return None, str(e)[0].upper() + str(e)[1:]
        return (p, meta), None
    return check


def interactive(defaults):
    print("Harvest statistics workbook - answer the questions, Enter keeps the [default].\n")
    exts = pc.VECTOR_EXTS
    boundary = pc.ask_text("1) Boundary layer (.shp / .gpkg / .geojson)", defaults.get("boundary"),
                           pc._file_check(exts, "boundary layer"))
    b_layer = pc.ask_layer(boundary, "boundary layer")
    fields = layer_fields(boundary)
    if not fields:
        raise UserError(f"the boundary layer {boundary.name} has no attribute fields to group by")
    print(f"   Fields of {boundary.name}:")
    for line in field_list_lines(fields):
        print(line)
    levels = ask_levels(fields, defaults)

    report = pc.ask_text("3) Harvest report layer of this date (.shp / .gpkg / .geojson)", defaults.get("report"),
                         pc._file_check(exts, "report layer"))
    r_layer = pc.ask_layer(report, "report layer")
    rnames = [str(n) for n in pc.vector_info(report).get("fields", [])]
    pred = [n for n in rnames if n.casefold() == "predicted"]
    if len(pred) == 1:
        class_field = pred[0]
        print(f"   Class field: {class_field}")
    else:
        if not rnames:
            raise UserError(f"the report {report.name} has no attribute fields (a class field is needed)")
        print("   The report has no field 'predicted'. Its fields:")
        for i, n in enumerate(rnames, 1):
            print(f"   {i}) {n}")

        def c_check(a):
            a = a.strip()
            if a.isdigit() and 1 <= int(a) <= len(rnames):
                return rnames[int(a) - 1], None
            return (a, None) if a in rnames else (None, f"Type a number from 1 to {len(rnames)} or a field name.")
        cd = defaults.get("class_field")
        class_field = pc.ask_text("   Class field", str(rnames.index(cd) + 1) if cd in rnames else None, c_check)

    found = dates_in_name(report)
    if len(found) > 1:
        print(f"   The file name holds {len(found)} dates: {', '.join(d.isoformat() for d in found)}.")
    ddef = found[-1].isoformat() if found else None
    edit = bool(defaults.get("edit"))                     # a second round after 'e' at Start?: the answers given
    if edit and defaults.get("date"):
        ddef = defaults["date"]
    date = pc.ask_text("4) Report date (YYYY-MM-DD or DD-MM-YYYY)", ddef, parse_date).isoformat()

    last_out = defaults.get("out")
    prev_def = last_out if last_out and Path(last_out).is_file() and made_by_this_tool(last_out) else None
    if edit:
        prev_def = defaults.get("previous")
    q = ("5) Workbook of the previous report (type 0 if this is the first report)" if prev_def
         else "5) Workbook of the previous report (Enter = this is the first report)")
    previous, prev_meta = None, None
    while True:
        if prev_def:
            ans = pc.ask_text(q, prev_def, _prev_check(levels, fields))
        else:
            raw = pc._input(f"{q}: ").strip()
            ans = "" if raw == "" else None
            if ans is None:
                v, hint = _prev_check(levels, fields)(raw)
                if hint:
                    print(f"   {hint}")
                    continue
                ans = v
        if ans == "":
            break
        previous, prev_meta = ans
        pf = [x["field"] for x in prev_meta["levels"]]
        if pf != [lv["field"] for lv in levels]:
            print(f"   That workbook has the levels {', '.join(pf)}; you chose "
                  f"{', '.join(lv['field'] for lv in levels)}. The levels must stay the same for the whole season.")
            names = [f[0] for f in fields]
            if all(f in names for f in pf) and pc.ask_yes_no("   Use its levels?", True):
                levels = [dict(x) for x in prev_meta["levels"]]
            else:
                previous, prev_meta = None, None
                continue
        dates = [r["date"] for r in prev_meta["reports"]]
        if date in dates or date < max(dates):
            why = ("already contains the report of " + dmy(date)) if date in dates else (
                f"ends on {dmy(max(dates))}, after this report ({dmy(date)})")
            print(f"   That workbook {why}. Give the workbook of the report just before {dmy(date)}.")
            previous, prev_meta = None, None
            continue
        old_t = [x.get("title") or x["field"] for x in prev_meta["levels"]]
        if [lv["title"] for lv in levels] != old_t:
            print(f"   The previous workbook uses the column titles: {', '.join(old_t)}.")
            if pc.ask_yes_no("   Use the same titles?", True):
                for lv, t in zip(levels, old_t):
                    lv["title"] = t
        break

    class_names = ask_class_names(report, class_field, previous, defaults.get("class_names") if edit else None)

    rcrs = layer_crs(report) or layer_crs(boundary)
    if not rcrs:
        raise SystemExit("Stopped: neither the report nor the boundary layer has a coordinate system (.prj missing).")
    bounds = layer_bounds(report) or layer_bounds(boundary)
    if bounds is None:
        raise UserError("the report and the boundary layer have no features")
    crs = pyproj.CRS(rcrs)
    opts = pc.utm_options(crs, bounds)
    north = pc.data_centre(crs, bounds)[1] >= 0
    fixed_crs = pyproj.CRS(prev_meta["crs"]) if prev_meta else None
    fixed_unit = pc.get_unit(prev_meta["unit"]) if prev_meta else None
    if opts:
        print("6) Projection used to calculate areas (detected from the data extent):")
        for i, (c, label, note) in enumerate(opts, 1):
            print(f"   {i}) {label}  EPSG:{c.to_epsg()}  ({note})")
    else:
        print("6) The data cross the 180 degree meridian: no UTM zone can be suggested.")
    if fixed_crs is not None:
        dflt = crs_text(fixed_crs)
        print(f"   The previous workbook used {pc.crs_label(fixed_crs)}; Enter keeps it (it must stay the same).")
    else:
        dflt = pc.default_area_crs(defaults.get("utm"), crs, bounds, opts)
        pc.say_earlier_choice(dflt)
    allow = False
    while True:
        utm = pc.ask_text("   Choose number, zone (e.g. 47N) or EPSG code" if opts else
                          "   Type a zone (e.g. 1S) or EPSG code", dflt, lambda a: pc.parse_crs_msg(a, north, opts))
        if fixed_crs is not None and utm != fixed_crs:
            print(f"   The previous workbook used {pc.crs_label(fixed_crs)}. The projection must stay the same for "
                  f"the whole season, because the history is not recomputed.")
            continue
        is_utm, far = pc.area_crs_notes(utm, crs, bounds)
        if not is_utm:
            print(f"   {pc.NON_UTM_NOTE}")
            if not pc.ask_yes_no(f"   Use {utm.name} anyway?", False):
                continue
            allow = True
        if far:
            print(f"   {far}")
            if not pc.ask_yes_no("   Use it anyway?", False):
                continue
        break
    while True:
        unit = pc.ask_unit("  ", fixed_unit or defaults.get("unit"))
        if fixed_unit is not None and unit.key != fixed_unit.key:
            print(f"   The previous workbook is in {fixed_unit.text}. The unit must stay the same for the whole "
                  f"season, because the history is not recomputed.")
            continue
        break
    if edit and defaults.get("out_edit"):
        out = ask_out(Path(defaults["out_edit"]).parent, Path(defaults["out_edit"]).stem, previous)
    else:
        out = ask_out(report.parent, report.stem + "_stats", previous)
    print("\nYour answers:")
    bl = f" (layer {b_layer})" if pc.layer_kw(boundary) else ""
    rl = f" (layer {r_layer})" if pc.layer_kw(report) else ""
    print(f"  boundary    : {boundary}{bl}")
    for i, lv in enumerate(levels, 1):
        t = f" (column title '{lv['title']}')" if lv["title"] != lv["field"] else ""
        print(f"  level {i}     : {lv['field']}{t}")
    print(f"  report      : {report}{rl}\n  class field : {class_field}\n  report date : {dmy(date)}")
    for c, name in class_names.items():
        print(f"  class {c:<6}: " + (f"named '{name}'" if name != c else "keeps its code as the name"))
    print(f"  previous    : {previous if previous else 'none (first report)'}")
    print(f"  projection  : {utm.name} (EPSG:{utm.to_epsg()})")
    print(f"  unit        : {unit.text}")
    print(f"  output      : {out}")
    choice = pc.ask_start()
    if choice == "cancel":
        print("Cancelled.")
        return None
    if choice == "edit":
        raise pc.EditAnswers(dict(edit=True, boundary=str(boundary), report=str(report), levels=levels,
                                  utm=crs_text(utm), unit=unit.key, class_field=class_field, date=date,
                                  previous=str(previous) if previous else None, class_names=class_names,
                                  out_edit=str(out)))
    return dict(boundary=boundary, levels=levels, report=report, date=date, out=out, previous=previous, utm=utm,
                unit=unit, class_field=class_field, allow_non_utm=allow, boundary_layer=b_layer, report_layer=r_layer,
                class_names=class_names)


def ask_class_names(report, class_field, previous, given=None):
    """Ask a name for every class code other than 81 / 82 (Enter keeps the name typed just before, else the
    name used in the previous workbook, else the code)."""
    names = {}
    others = other_codes(report, class_field)
    if not others:
        return names
    known = {}
    if previous:
        try:
            pm, _, renamed = read_previous(previous)
            known = merge_labels(pm.get("labels"), renamed)
        except Exception:
            known = {}
    print(f"   The report holds {pc.plural(len(others), 'class', 'classes')} other than 81 and 82: "
          f"{', '.join(others)}.")
    print("   Such a class gets its own column and is counted as remaining crop. Give it a name, or press "
          "Enter to keep what is in [ ].")
    for c in others:
        names[c] = pc.ask_text(f"   Name for class {c}", (given or {}).get(c) or code_label(c, known) or c,
                               class_name_check)
    return names


def next_report(last):
    """The short way for the second and later reports of a season: this folder's last run finished and its
    workbook is still there, so only the new report is asked; boundary, levels, titles, projection and unit
    come from that workbook. Returns the answers for run(), or None to go through all the questions."""
    try:
        prev = Path(last["out"])
        boundary = Path(last["boundary"])
        if last.get("status") != "finished" or not prev.is_file() or not boundary.is_file() \
                or not made_by_this_tool(prev):
            return None
        meta, _, _ = read_previous(prev)
        levels = [dict(x) for x in meta["levels"]]
        dates = sorted(r["date"] for r in meta["reports"])
        unit = pc.get_unit(meta["unit"])
        crs = pyproj.CRS(meta["crs"])
        names = [f[0] for f in layer_fields(boundary)]
        if any(lv["field"] not in names for lv in levels):
            return None
    except Exception:
        return None
    lv_text = " > ".join(lv.get("title") or lv["field"] for lv in levels)
    print("This folder already holds a season:")
    print(f"  workbook    : {prev}")
    print(f"  reports     : {len(dates)} ({', '.join(dmy(d) for d in dates[-6:])})")
    print(f"  boundary    : {boundary}")
    print(f"  levels      : {lv_text}")
    print(f"  projection  : {pc.crs_label(crs)}")
    print(f"  unit        : {unit.text}")
    if not pc.ask_yes_no("Add the next report to this season? (n = answer all the questions again)", True):
        print()
        return None
    given = {}
    while True:
        while True:
            report = pc.ask_text("1) Harvest report layer of the new date (.shp / .gpkg / .geojson)",
                                 given.get("report"), pc._file_check(pc.VECTOR_EXTS, "report layer"))
            sig = file_sig(report)
            if any(isinstance(r.get("file"), dict) and r["file"].get("name") == sig.get("name")
                   and r["file"].get("size") == sig.get("size") for r in meta["reports"]):
                print("   That report is already in the workbook. Give the report of the new date.")
                given.pop("report", None)
                continue
            break
        r_layer = pc.ask_layer(report, "report layer")
        rnames = [str(n) for n in pc.vector_info(report).get("fields", [])]
        wanted = ([n for n in rnames if n == last.get("class_field")]
                  or [n for n in rnames if n.casefold() == "predicted"])
        if not wanted:
            print(f"   The report has no class field '{last.get('class_field') or 'predicted'}': all the questions "
                  "are asked.\n")
            return None
        class_field = wanted[0]
        found = dates_in_name(report)
        ddef = given.get("date") or (found[-1].isoformat() if found else None)

        def d_check(a):
            d, hint = parse_date(a)
            if hint:
                return None, hint
            if d.isoformat() <= dates[-1]:
                return None, f"The workbook already ends on {dmy(dates[-1])}; the new report must be later."
            return d, None
        if ddef and ddef <= dates[-1]:
            ddef = None
        date = pc.ask_text("2) Report date (YYYY-MM-DD or DD-MM-YYYY)", ddef, d_check).isoformat()
        class_names = ask_class_names(report, class_field, prev, given.get("class_names"))
        if given.get("out"):
            out = ask_out(Path(given["out"]).parent, Path(given["out"]).stem, prev, label="3) Output folder")
        else:
            out = ask_out(report.parent, report.stem + "_stats", prev, label="3) Output folder")
        print("\nYour answers:")
        print(f"  report      : {report}\n  class field : {class_field}\n  report date : {dmy(date)}")
        for c, name in class_names.items():
            print(f"  class {c:<6}: " + (f"named '{name}'" if name != c else "keeps its code as the name"))
        print(f"  previous    : {prev}")
        print(f"  output      : {out}")
        print(f"  from the previous workbook: boundary {boundary.name}, levels {lv_text}, {unit.text}, "
              f"{pc.crs_label(crs)}")
        choice = pc.ask_start()
        if choice == "cancel":
            print("Cancelled.")
            return {}
        if choice == "start":
            break
        given = dict(report=str(report), date=date, class_names=class_names, out=str(out))
        print(pc.EDIT_NOTE)
    return dict(boundary=boundary, levels=levels, report=report, date=date, out=out, previous=prev, utm=crs,
                unit=unit, class_field=class_field, allow_non_utm=True, boundary_layer=last.get("boundary_layer"),
                report_layer=r_layer, class_names=class_names)


def describe_last(last):
    state = last.get("status", "?")
    if state == "running" and pc._pid_alive(last.get("pid")) is False:
        state = "interrupted"
    print("A previous run was found:")
    print(f"  (remembered for this folder: {pc.last_run_home(__file__)})")
    print(f"  boundary    : {last['boundary']}")
    for i, lv in enumerate(last.get("levels") or [], 1):
        t = f" (column title '{lv.get('title')}')" if lv.get("title") and lv.get("title") != lv.get("field") else ""
        print(f"  level {i}     : {lv.get('field')}{t}")
    print(f"  report      : {last['report']}  ({dmy(last['date']) if last.get('date') else '?'})")
    print(f"  previous    : {last.get('previous') or 'none (first report)'}")
    print(f"  output      : {last['out']}")
    print(f"  status      : {state}, started {last.get('started', '?')}")
    print("Its answers are the defaults below; for the next report give its workbook as the previous one.\n")


def _validate_cli(a, ap):
    for name in ("boundary", "report"):
        p = Path(pc.clean_path(getattr(a, name)))
        if not p.is_file():
            ap.error(f"--{name}: file not found: {p}")
        if p.suffix.lower() not in pc.VECTOR_EXTS:
            ap.error(f"--{name}: {p.name} is not a vector file. {pc.FORMAT_HINT}")
        try:
            pc.choose_layer(p, getattr(a, name + "_layer"), name, f"--{name}-layer")
        except UserError as e:
            ap.error(f"--{name}: {e}")
    if a.previous:
        p = Path(pc.clean_path(a.previous))
        if not p.is_file():
            ap.error(f"--previous: file not found: {p}")


def split_list(text):
    return [x.strip() for x in str(text).split(",")] if text is not None else []


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--boundary", help="boundary layer (.shp, .gpkg, .geojson or .json)")
    ap.add_argument("--boundary-layer", default=None, help="layer of the boundary file when it has several")
    ap.add_argument("--levels", help="level fields from the largest unit inwards, comma separated: FIELD1,FIELD2")
    ap.add_argument("--level-titles", default=None,
                    help="column titles of the levels, comma separated, in the same order (default: the field names)")
    ap.add_argument("--report", help="harvest report of this date (.shp, .gpkg, .geojson or .json)")
    ap.add_argument("--report-layer", default=None, help="layer of the report file when it has several")
    ap.add_argument("--class-field", default=None, help="class field of the report (default: predicted)")
    ap.add_argument("--class-names", default=None, help="names for class codes other than 81 and 82, used in the "
                    "column headers: 282=Cloud,232=Smoke (default: the code itself)")
    ap.add_argument("--date", default=None, help="report date YYYY-MM-DD or DD-MM-YYYY (default: from the file name)")
    ap.add_argument("--previous", default=None, help="workbook this tool wrote for the previous report")
    ap.add_argument("--unit", default=None,
                    help="acres (default), hectares, rai, m2 or km2; with --previous: the previous workbook's unit")
    ap.add_argument("--utm", default=None, help="projected CRS for areas: 47N, 32647 or EPSG:32647 (default: "
                                                "the UTM zone of the data; with --previous: the previous workbook's)")
    ap.add_argument("--allow-non-utm", action="store_true", help="accept a projected CRS that is not a UTM zone")
    ap.add_argument("--out", default=None, help="output .xlsx (default: <report name>_stats.xlsx next to the report)")
    ap.add_argument("--no-pivot", action="store_true", help="leave the real pivot table out (the plain cross-table stays)")
    ap.add_argument("--workers", type=int, default=None, help="threads (default: all cores)")
    ap.add_argument("--ask", action="store_true", help="question-and-answer mode")
    a = ap.parse_args(argv)
    ask = a.ask or len(sys.argv if argv is None else argv) <= (1 if argv is None else 0)
    if ask:
        try:
            last = load_last_run()
            defaults = {}
            ans = next_report(last) if last else None
            if ans is None and last:
                describe_last(last)
                defaults = dict(boundary=pc._existing(last.get("boundary")), report=pc._existing(last.get("report")),
                                levels=last.get("levels"), utm=last.get("utm"), unit=last.get("unit"),
                                out=last.get("out"), class_field=last.get("class_field"),
                                prev_out=last.get("previous"))
            if ans is None:
                ans = pc.ask_all(interactive, defaults)
            if ans:
                run(**ans, pivot=not a.no_pivot, workers=a.workers)
        except KeyboardInterrupt:
            print("\nStopped.")
        except SystemExit as e:
            print(f"\n{e}" if e.code not in (None, 0) else "\nStopped.")
        except UserError as e:
            print(f"\nERROR: {e}")
        except Exception as e:  # keep the window readable, no traceback for the analyst
            print(f"\nERROR: {type(e).__name__}: {e}")
        try:
            input("\nPress Enter to close")
        except EOFError:
            pass
        return
    if not (a.boundary and a.levels and a.report):
        ap.error("--boundary, --levels and --report are required (or run without arguments for questions)")
    _validate_cli(a, ap)
    fields = split_list(a.levels)
    titles = split_list(a.level_titles) if a.level_titles is not None else []
    if a.level_titles is not None and len(titles) != len(fields):
        ap.error(f"--level-titles has {len(titles)} titles for {len(fields)} levels; give one title per level")
    report = Path(pc.clean_path(a.report))
    if a.date:
        d, hint = parse_date(a.date)
        if d is None:
            ap.error(f"--date: {hint}")
    else:
        found = dates_in_name(report)
        if not found:
            ap.error("--date is needed: the report file name holds no date (YYYY-MM-DD)")
        if len(found) > 1:
            ap.error(f"--date is needed: the report file name holds {len(found)} dates "
                     f"({', '.join(x.isoformat() for x in found)})")
        d = found[0]
    out = Path(pc.clean_path(a.out)) if a.out else report.with_name(report.stem + "_stats.xlsx")
    if out.suffix.lower() != ".xlsx":
        ap.error("--out must end with .xlsx")
    if not out.parent.is_dir():
        ap.error(f"--out: folder does not exist: {out.parent}")
    unit, utm = a.unit, a.utm
    try:
        if a.previous:
            meta, _, _ = read_previous(Path(pc.clean_path(a.previous)))
            unit = unit if unit is not None else meta["unit"]
            utm = utm if utm is not None else meta["crs"]
            if not titles:
                old = {x["field"]: x.get("title") for x in meta["levels"]}
                titles = [old.get(f) or f for f in fields]
        if unit is not None:
            unit = pc.cli_unit(unit, ap)
        if not titles:
            titles = fields
        levels = [{"field": f, "title": t or f} for f, t in zip(fields, titles)]
        res = run(Path(pc.clean_path(a.boundary)), levels, report, d.isoformat(), out,
                  Path(pc.clean_path(a.previous)) if a.previous else None, utm, unit, a.class_field,
                  a.allow_non_utm, not a.no_pivot, a.boundary_layer, a.report_layer, a.workers,
                  parse_class_names(a.class_names))
        if res and not res["check_ok"]:
            sys.exit(3)
    except UserError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
