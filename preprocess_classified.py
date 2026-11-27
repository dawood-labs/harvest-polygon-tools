"""Fast classified raster -> remaining-cane polygons (one command, tiled, resumable).

Dependencies (one command):
    pip install numpy rasterio shapely pyogrio pyproj          (optional: psutil)
    (or: conda install -c conda-forge numpy rasterio shapely pyogrio pyproj psutil)
Needs shapely >= 2.0.

Steps
  1. Optional majority filter, one pass over the original values (Esri rules).
     Counts: MAJORITY = 3 of 4 (FOUR) or 5 of 8 (EIGHT) neighbours with the same value;
     HALF = 2 of 4 or 4 of 8. The agreeing neighbours must be contiguous: EIGHT = one
     unbroken run around the ring of 8; FOUR = neighbours next to each other in the ring
     N-E-S-W (so N+S or E+W alone never count).
     Raster edge cells: FOUR needs two such adjacent equal neighbours; EIGHT needs three
     contiguous equal neighbours including one along the edge (corner: all three).
     Note on FOUR edge cells, two possible readings of the Esri text:
       a) two equal neighbours that touch each other (used here), or
       b) any two equal neighbours (W+E on a top edge would also count).
     Only the outermost row/column of the raster can differ; not verified against ArcMap.
     HALF tie (two values qualify): the centre is kept. Nodata centres stay nodata and
     nodata never votes; cells outside the raster never match.
  2. cane = (value == --cane-value); other classes are dropped.
  3. Polygonise the cane class (exact cell edges, 4-connectivity, no simplification), in
     blocks of at most BLOCK x BLOCK cells so that cane with many holes stays fast.
  4. Keep the part inside the L1 layer, everything dissolved, split to single parts.
     Result = explode(union(cane cells) intersected with union(L1)). Each block piece is
     clipped to L1 at once; pieces of neighbouring blocks/tiles that share a boundary
     segment (not just a corner) are unioned afterwards. Vertices that the block seams add
     where an L1 edge crosses a seam are removed again, so the result equals a run
     without blocks or tiles.
  5. Area in the chosen projected CRS (default: UTM zone of the data centre); the written
     geometry is never round-tripped through that CRS. Pieces below 0.01 m2 (numerical noise)
     are always removed.
  6. Drop slivers smaller than --min-acres (acres) or --min-area (in --unit); default 0.2 acres,
     the same physical size in every unit. Output: EPSG:4326, fields predicted (81) and ONE area
     field in the --unit: area_ac (acres = m2 / 4046.85642, 3 decimals, default), area_ha
     (hectares, 3), area_rai (rai = 1,600 m2, 3), area_m2 (1) or area_km2 (6).
  7. Quality gate before writing (the features as they are written, in EPSG:4326): every feature
     is one valid 2D Polygon (GEOS rules; repaired with make_valid, multi-parts split, points,
     lines, null, empty and zero-area shapes dropped, repeated consecutive vertices removed) and
     no two polygons share area (they would be dissolved into one). The saved file is then read
     back and checked again; the RESULT block shows the counts (exit code 3 when a problem is left).
Inputs and output: Shapefile (.shp), GeoPackage (.gpkg) or GeoJSON (.geojson / .json). A GeoJSON
file without a crs member is EPSG:4326 (RFC 7946). A GeoPackage with several layers: the only
polygon layer is used, else --l1-layer NAME (question mode lists the layers). Dirty L1 polygons
(null, lines, points, Z values, self-intersections, multi-parts) are accepted: the polygon parts are
used and a NOTE counts what was repaired or ignored. GeoJSON output: exterior rings counter-
clockwise (RFC 7946 order), coordinates written with 17 significant digits (not rounded).

How it scales: the machine (cores, free memory) is read first. Memory budget = available memory
minus a buffer of 20 % of it (at least 0.5 GB, at most 1 GB); --max-memory-gb / --memory-buffer-gb
override. Memory mode (printed in the plan line): "container" when a cgroup memory limit or
MEM_GUARANTEE is set - strict planning (2 x the measured worst case per cell), new tiles wait while
free memory is below the buffer, because going over the limit kills the run; "desktop" (Windows,
macOS, plain Linux) - the measured memory plus a modest margin, new tiles wait only below 0.3 GB free
and never long, because a shortage there means paging; a rare heavy tile is split in 4 and retried.
Layout: P worker processes x T threads (P x T = parallel tiles, all cores by default). Every process
costs about 200 MB of its own, so as many processes as the budget comfortably holds are used and the
remaining cores run as threads (down to 1 process x N threads = threads inside this program, nothing
spawned, on small budgets). Every thread opens the raster and its part of L1 itself. --workers sets
the number of parallel tiles; --processes / --threads fix the layout. The raster is processed tile by
tile (window reads only, one-cell halo for the filter). Finished tiles are saved in
<output stem>_work/ so an interrupted run resumes where it stopped; a run with a new output
name reads the saved tiles of the previous run in place when its inputs and tile settings match.
Each stage deletes its inputs only after its own result is saved, and the output is written under a
temporary name and swapped in at the end. The prepared L1 (reprojected, repaired) is kept in a cache folder in the user's home
(.preprocess_classified/l1_prepared, at most 2 GB, least recently used first out; only files this
tool created are deleted) so the next report date with the same L1 skips that step. Shapefile output
is limited to 2 GB by the format; use .gpkg for very large results. The work folder holds a marker
file; the script only ever deletes its own files. The end of a run prints a RESULT block; add
--summary-line for the old one-line "SUMMARY key=value" output for scripts.

Run without arguments (or with --ask) for a question-and-answer mode; all choices are also
available as flags for unattended runs:
    python preprocess_classified.py --raster in.tif --l1 l1.shp --out out.shp [--min-acres 0.2]
        [--unit acres|hectares|rai|m2|km2] [--min-area A] [--l1-layer NAME]
        [--utm 47N | EPSG:32647] [--allow-non-utm] [--assume-l1-crs] [--cane-value 1]
        [--workers N] [--processes P] [--threads T] [--memory-mode auto|desktop|container]
        [--keep-slivers] [--no-filter] [--neighbors 4|8] [--majority-threshold majority|half]
        [--max-memory-gb G] [--memory-buffer-gb G] [--tile-size T] [--clean] [--summary-line]
In question mode the output is asked as folder, name and format (1 shapefile, 2 GeoPackage,
3 GeoJSON); the unit is asked after the projection. Changing only the unit, the sliver size, the
projection, the output name or format reruns only the last step.
"""
import argparse
import hashlib
import importlib
import json
import math
import multiprocessing
import os
import pickle
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
import warnings
from collections import deque
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, wait
from pathlib import Path
from urllib.parse import unquote, urlparse

_PIP = {"numpy": "numpy", "rasterio": "rasterio", "shapely": "shapely",
        "pyogrio": "pyogrio", "pyproj": "pyproj"}
for _mod, _pkg in _PIP.items():
    try:
        importlib.import_module(_mod)
    except ImportError:
        sys.exit(f"ERROR: python package '{_pkg}' is missing. Install it with: "
                 f"pip install {_pkg}   (or: conda install -c conda-forge {_pkg})")

import numpy as np  # noqa: E402
import pyogrio  # noqa: E402
import pyogrio.raw  # noqa: E402
import pyproj  # noqa: E402
import rasterio  # noqa: E402
import shapely  # noqa: E402
from rasterio import features  # noqa: E402
from rasterio.windows import Window  # noqa: E402
from shapely import STRtree  # noqa: E402

if int(shapely.__version__.split(".")[0]) < 2:
    sys.exit("ERROR: shapely >= 2.0 is required (pip install -U shapely)")
# polygonising an in-memory mask has no georeference by design (global filter: thread-safe)
warnings.filterwarnings("ignore", category=rasterio.errors.NotGeoreferencedWarning)

CACHE_VERSION = 6                     # bump whenever saved tiles/merge results change meaning
ACRE_M2 = 4046.85642
PREDICTED = 81
FINAL_GROUP_BYTES = 32 * 1024 ** 2   # final stage works on groups of about this much WKB (main process memory)
SHP_WARN_BYTES = int(1.5 * 1024 ** 3)  # estimated result size above which a shapefile output gets a NOTE
NOISE_M2 = 0.01                       # pieces below this area are numerical noise, always dropped
BLOCK = int(os.environ.get("PREPROCESS_TEST_BLOCK", 512))   # largest block polygonised in one call (cells)
BASE_FALLBACK_BYTES = (320 if os.name == "nt" else 220) * 1024 ** 2   # when the own memory cannot be read


def process_rss():
    """Resident memory of this process in bytes (psutil, else the Windows API, else /proc), or None."""
    try:
        import psutil
        return int(psutil.Process().memory_info().rss)
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]
            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            k = ctypes.windll.kernel32
            k.GetCurrentProcess.restype = wintypes.HANDLE
            if ctypes.windll.psapi.GetProcessMemoryInfo(k.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
                return int(pmc.WorkingSetSize)
        except Exception:
            pass
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except Exception:
        pass
    return None


def _measured_base():
    """Memory of one worker process = this process right after loading the libraries (every spawned
    worker loads the same ones), +15 %. Resident memory counts the shared library pages in full for
    every process: on Linux some of them are really shared, on Windows hardly any, so this errs on
    the safe side."""
    m = process_rss()
    return int(max(m * 1.15, 150 * 1024 ** 2)) if m else BASE_FALLBACK_BYTES


WORKER_BASE_BYTES = _measured_base()   # per worker process: interpreter + numpy, GDAL, GEOS, PROJ (measured)
TASK_BASE_BYTES = 32 * 1024 ** 2      # per tile task besides its cells: L1 part, trees, buffers
DESKTOP_HOLD_BYTES = int(0.3 * 1024 ** 3)   # desktop mode: hold new tasks only below this much free memory
DESKTOP_HOLD_MAX_S = 10.0             # ... and never longer than this per task
MARKER = "preprocess_classified.workfolder"
LOW_MEMORY_NOTE = ("Free memory is low: closing other programs (QGIS, browser tabs) lets the tool use more "
                   "cores at full speed.")


def memory_note(explicit_budget, procs):
    """NOTE when the planner used fewer worker processes than parallel tasks because of memory."""
    if explicit_budget:
        return (f"The memory budget holds {plural(procs, 'worker process', 'worker processes')}; the other "
                f"tasks run as threads (a larger --max-memory-gb allows more processes).")
    return LOW_MEMORY_NOTE


LOCK = "run.lock"
SHP_SIDECARS = (".shp", ".shx", ".dbf", ".prj", ".cpg", ".sbn", ".sbx", ".qix", ".fbn", ".fbx",
                ".ain", ".aih", ".atx", ".ixs", ".mxs", ".shp.xml")
GPKG_SIDECARS = (".gpkg", ".gpkg-wal", ".gpkg-shm", ".gpkg-journal")
L1_SIG = {".shp": (".shp", ".shx", ".dbf", ".prj", ".cpg"), ".gpkg": (".gpkg", ".gpkg-wal", ".gpkg-shm"),
          ".geojson": (".geojson",), ".json": (".json",)}
NON_UTM_NOTE = ("Areas are exact only in an equal-area or local UTM projection; in other projected "
                "systems (e.g. Web Mercator EPSG:3857) acres can be badly wrong.")
_T0 = [time.time()]
_TILE_RE = re.compile(r"^\d+_\d+_\d+_\d+\.(edge|fin)(\.tmp)?$")


class UserError(Exception):
    """A problem the user can fix (shown without a traceback)."""


class DiskSpaceError(UserError):
    pass


class TileStopped(Exception):
    """Raised inside a tile task when the run is being stopped (Ctrl+C)."""


_STOP = threading.Event()   # set when the run stops: tile threads of this process end at the next block


_START = [time.time()]   # start of the run clock (moved forward by the time spent on questions)


def start_clock():
    _T0[0] = _START[0] = time.time()


def pause_clock(seconds):
    """Leave `seconds` (time spent waiting for an answer) out of the stage times and the total."""
    _T0[0] += seconds
    _START[0] += seconds


def run_seconds():
    return time.time() - _START[0]


def asked(fn, *args):
    """Call a question function; the time the user needs to answer is not counted as run time."""
    t = time.time()
    try:
        return fn(*args)
    finally:
        pause_clock(time.time() - t)


def tick(msg):
    """Print a progress line with the seconds since the previous one; returns those seconds."""
    now = time.time()
    dt = now - _T0[0]
    print(f"[{dt:7.1f}s] {msg}", flush=True)
    _T0[0] = now
    return dt


def acres_text(v):
    """Acres with 3 decimals and thousands separators; never a negative zero."""
    return f"{round(float(v), 3) + 0.0:,.3f}"


# ---------------------------------------------------------------- units of area
class Unit:
    """One unit of area. key: stored in records and cache keys; name: shown in the list; text: used in
    sentences; short: short code; field: output attribute; m2: square metres per unit; decimals: of the
    output values; header: column header of the result tables; forms: extra accepted spellings."""

    def __init__(self, key, name, text, short, field, m2, decimals, header, forms=()):
        self.key, self.name, self.text, self.short, self.field = key, name, text, short, field
        self.m2, self.decimals, self.header, self.forms = float(m2), int(decimals), header, tuple(forms)

    @property
    def word(self):
        """The unit inside a table row: the text when short, else the short code ('m2', 'km2')."""
        return self.text if len(self.text) <= 8 else self.short

    def __repr__(self):
        return f"Unit({self.key})"


#            key         list name            sentence text        short  field       m2 per unit  dec  column header
UNITS = (Unit("acres", "Acres", "acres", "ac", "area_ac", ACRE_M2, 3, "Acres", ("acre",)),
         Unit("hectares", "Hectares", "hectares", "ha", "area_ha", 10_000, 3, "Hectares", ("hectare", "hectar")),
         Unit("rai", "Rai (Thailand)", "rai", "rai", "area_rai", 1_600, 3, "Rai", ("rais",)),
         Unit("m2", "Square metres", "square metres", "m2", "area_m2", 1, 1, "Square metres",
              ("sqm", "square meter", "square meters", "square metre", "meters", "metres", "meter", "metre")),
         Unit("km2", "Square kilometres", "square kilometres", "km2", "area_km2", 1_000_000, 6, "Sq km",
              ("sqkm", "square kilometer", "square kilometers", "square kilometre")))
UNIT_BY_KEY = {u.key: u for u in UNITS}
ACRES = UNIT_BY_KEY["acres"]
UNIT_HINT = (f"Type a number from 1 to {len(UNITS)} or a unit name, e.g. "
             + ", ".join(u.text for u in UNITS[:3]) + ".")


def _unit_norm(text):
    """Lower case, no punctuation or spaces; a superscript two counts as 2."""
    t = str(text).strip().lower().replace("\u00b2", "2")
    return re.sub(r"[\s.\-_()]+", "", t)


def _unit_sound(t):
    """A rough sound-alike form for spelling mistakes: k/q -> c, y -> i, no doubled letters."""
    t = t.replace("ck", "c").replace("k", "c").replace("q", "c").replace("y", "i").replace("ph", "f")
    return re.sub(r"(.)\1+", r"\1", t)


def _unit_forms():
    forms = {}
    for i, u in enumerate(UNITS, 1):
        for f in (str(i), u.key, u.name, u.text, u.short, u.header, u.name.split(" (")[0]) + u.forms:
            forms.setdefault(_unit_norm(f), u)
    return forms


def parse_unit(text):
    """Read a unit answer: (unit, None, None) for a list number or a known spelling (any case, spaces
    and dots ignored); (None, unit, None) when the text only looks like a unit name (a spelling
    mistake: ask before using it); (None, None, hint) when nothing is close."""
    if isinstance(text, Unit):
        return text, None, None
    forms = _unit_forms()
    t = _unit_norm(text or "")
    if t in forms:
        return forms[t], None, None
    if not t or t.isdigit() or not re.search(r"[a-z]", t):
        return None, None, UNIT_HINT
    import difflib
    best, score = None, 0.0
    for f, u in forms.items():
        if f.isdigit():
            continue
        s = max(difflib.SequenceMatcher(None, t, f).ratio(),
                difflib.SequenceMatcher(None, _unit_sound(t), _unit_sound(f)).ratio())
        if s > score:
            best, score = u, s
    if best is not None and score >= 0.6:
        return None, best, None
    return None, None, UNIT_HINT


def get_unit(u):
    """A Unit from a Unit, a key or any accepted spelling; None / '' = acres. Raises UserError otherwise."""
    if u is None or (isinstance(u, str) and not u.strip()):
        return ACRES
    unit, guess, hint = parse_unit(u)
    if unit is not None:
        return unit
    if guess is not None:
        raise UserError(f"unit '{u}' is not known. Did you mean {guess.text}? ({UNIT_HINT})")
    raise UserError(f"unit '{u}' is not known. {UNIT_HINT}")


def unit_of_record(d):
    """The unit stored in a last-run record or cache key; records written before units existed = acres."""
    try:
        return get_unit((d or {}).get("unit"))
    except UserError:
        return ACRES


def from_acres(v, unit):
    """Acres -> the unit (acres are returned unchanged, bit for bit)."""
    return float(v) if unit.key == "acres" else float(v) * ACRE_M2 / unit.m2


def to_acres(v, unit):
    """The unit -> acres (acres are returned unchanged, bit for bit)."""
    return float(v) if unit.key == "acres" else float(v) * unit.m2 / ACRE_M2


def area_text(v, unit):
    """A total in the unit with its decimals and thousands separators; never a negative zero."""
    return f"{round(float(v), unit.decimals) + 0.0:,.{unit.decimals}f}"


def size_text(v, unit):
    """A size setting in the unit, short: rounded to the unit's decimals, trailing zeros removed
    (0.2, 0.081, 809.4); a small value that would round to 0 keeps 3 significant digits."""
    v = float(v)
    r = round(v, unit.decimals)
    if r == 0 and v > 0:
        return f"{v:.3g}"
    s = f"{r:.{unit.decimals}f}".rstrip("0").rstrip(".") if unit.decimals else f"{r:.0f}"
    return s or "0"


def size_number(acres, unit):
    """A size setting held in acres as a number in the unit. Acres keep their usual text (0.2, 7.0)
    unless they have more than 3 decimals."""
    if unit.key == "acres" and round(float(acres), 3) == float(acres):
        return str(acres)
    return size_text(from_acres(acres, unit), unit)


def size_in_unit(acres, unit):
    """A size setting held in acres, shown in the unit: '0.2 acres', '0.081 hectares'."""
    return f"{size_number(acres, unit)} {unit.text}"


def size_default_text(acres, unit):
    """Default of a size question: '0.2' in acres; '0.081 = 0.2 acres' in other units."""
    if unit.key == "acres":
        return size_number(acres, unit)
    return f"{size_text(from_acres(acres, unit), unit)} = {size_number(acres, ACRES)} acres"


def pct_text(part, whole):
    """Share in % with one decimal ('100.0', '4.5'); never '-0.0'; '-' when the whole is 0."""
    if not whole or whole <= 0:
        return "-"
    return f"{max(round(100.0 * float(part) / float(whole), 1), 0.0) + 0.0:.1f}"


def unit_list_lines():
    """The numbered list of units, three per line."""
    cells = [f"{i}) {u.name}" for i, u in enumerate(UNITS, 1)]
    return ["   " + "".join(c.ljust(20) for c in cells[k:k + 3]).rstrip() for k in range(0, len(cells), 3)]


def ask_unit(label, default=None):
    """'N) Unit for areas:' with the list; any accepted spelling; a likely spelling mistake is confirmed
    ('Did you mean Hectares? [Y/n]'); anything else is asked again with a hint. Returns a Unit."""
    default = unit_of_record({"unit": default.key if isinstance(default, Unit) else default})
    print(f"{label} Unit for areas:")
    for line in unit_list_lines():
        print(line)

    def check(a):
        unit, guess, hint = parse_unit(a)
        if unit is not None:
            return unit, None
        if guess is not None:
            if ask_yes_no(f"   Did you mean {guess.name}?", True):
                return guess, None
            return None, UNIT_HINT
        return None, hint
    return ask_text("   Choose a number or type the name", str(UNITS.index(default) + 1), check)


def area_check(unit, warning):
    """A size question in the unit: (size in acres, None) or (None, hint). Above LARGE_ACRES (the same
    physical size in every unit) warning(text) is asked first; text = the typed size with the unit."""
    def check(a):
        v, hint = parse_acres(a)
        if hint is not None:
            return None, hint
        ac = to_acres(v, unit)
        if ac > LARGE_ACRES and not ask_yes_no(warning(f"{v:g} {unit.text}"), False):
            return None, "Type a smaller size."
        return ac, None
    return check


def ask_size(question, unit, default_acres, warning, tail=""):
    """Ask a size in the unit; Enter keeps default_acres exactly (the same physical size). Returns acres."""
    dtext = size_default_text(default_acres, unit)
    check = area_check(unit, warning)

    def chk(a):
        if a.strip() == dtext:
            return float(default_acres), None
        return check(a)
    return ask_text(f"{question} in {unit.text}{tail}", dtext, chk)


def plural(n, one, many=None):
    """'1 piece' / '3 pieces' (n printed with thousands separators)."""
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


def crs_label(crs):
    """'WGS 84 / UTM zone 36N (EPSG:32636)' (the name alone when there is no EPSG code)."""
    try:
        c = pyproj.CRS(crs)
    except Exception:
        return str(crs)
    return c.name + (f" (EPSG:{c.to_epsg()})" if c.to_epsg() else "")


def status_text(state, progress):
    """'finished' / 'interrupted, 3 of 42 tiles done' - the status word is never repeated."""
    progress = (progress or "").strip()
    if not progress or progress == state:
        return state
    if progress.startswith(state):
        return progress
    return f"{state}, {progress}"


RESULT_WIDTH = 80


def _words(text):
    return len(text.split())


def wrap_line(line, width=RESULT_WIDTH):
    """One logical line wrapped once to `width`.

    Breaks fall at spaces or path separators ("/", "\\"); a line with " | " items (the Time line)
    breaks only after a "|". The next lines are indented to the text after 'Label : ' (or two more
    than the line's own indent; a line that is itself indented by 4 or more keeps that indent). A
    last line of one or two words takes words from the line above when that line keeps at least
    three. Short lines are returned unchanged."""
    if len(line) <= width:
        return [line]
    lead = len(line) - len(line.lstrip(" "))
    m = re.match(r"^(\s*\S[^:]{0,20}: )", line)
    ind = " " * (len(m.group(1)) if m else (lead if lead >= 4 else lead + 2))
    if " | " in line:
        tokens = re.findall(r".*?\| |.+$", line)
    else:
        tokens = re.findall(r"[^ /\\]*[ /\\]|[^ /\\]+$", line)
    lines = [[]]
    for t in tokens:
        cur = lines[-1]
        text = ("" if len(lines) == 1 else ind) + "".join(cur) + t.rstrip()
        if cur and "".join(cur).strip() and len(text) > width:
            lines.append([t.lstrip(" ")])
        else:
            cur.append(t)

    def render(k, toks):
        return (("" if k == 0 else ind) + "".join(toks)).rstrip()
    while len(lines) >= 2 and _words(render(len(lines) - 1, lines[-1])) < 3 and len(lines[-2]) > 1 \
            and lines[-2][-1].endswith(" "):          # whole words or items only, never a piece of a path
        moved = [lines[-2][-1].lstrip(" ")] + lines[-1]
        if len(render(len(lines) - 1, moved)) > width or _words(render(len(lines) - 2, lines[-2][:-1])) < 3:
            break
        lines[-2], lines[-1] = lines[-2][:-1], moved
    return [render(k, toks) for k, toks in enumerate(lines)]


def print_result_block(title, sections):
    """Print a titled block (ASCII, aligned). sections = [(heading, rows)]; a row is a tuple of strings.
    Rows with 2+ cells are aligned in columns (first cell left, the next two right-aligned, any further
    cell left as free text); a row with one cell is printed as it is. An empty heading prints no
    heading line. Shared look for the end-of-run summary of the tools in this folder."""
    wid = [0, 0, 0]
    for _, rows in sections:
        for r in rows:
            if len(r) > 1:
                for i in range(min(len(r), 3)):
                    wid[i] = max(wid[i], len(r[i]))
    lines = []
    for heading, rows in sections:
        if lines:
            lines.append("")
        if heading:
            lines.extend(wrap_line(heading, RESULT_WIDTH))
        for r in rows:
            if len(r) == 1:
                lines.extend(wrap_line(r[0], RESULT_WIDTH))
                continue
            cells = [r[0].ljust(wid[0])] + [c.rjust(wid[i]) for i, c in enumerate(r[1:3], 1)]
            line = "  ".join(cells) + "".join("  " + c for c in r[3:])
            lines.append(line.rstrip())
    width = RESULT_WIDTH
    head = f" {title} "
    print("=" * ((width - len(head)) // 2) + head + "=" * (width - len(head) - (width - len(head)) // 2))
    print("\n".join(lines))
    print("=" * width, flush=True)


def wrap_rows(text, width=None):
    """A sentence as one result-block row (two spaces indent); the block wraps it once, with the next
    lines indented by four (or to the text after 'Label: ')."""
    return [("  " + text,)]


def cache_rows(label, folder, size, sentence):
    """'  Cache  : <folder> (<size>)' and the sentence as its own paragraph below; when the size does
    not fit next to the folder it starts the sentence's paragraph instead."""
    first = f"  {label} : {folder} ({size})"
    if len(first) <= RESULT_WIDTH:
        return [(first,), ("           " + sentence,)]
    return [(f"  {label} : {folder}",), (f"           ({size}) " + sentence,)]


def human_size(n):
    """1,234 B / 12.3 KB / 72.5 MB / 1.2 GB"""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024


# ---------------------------------------------------------------- machine
def n_cpus():
    """Usable cores (affinity where available, cgroup quota if present, else cpu_count)."""
    try:
        n = len(os.sched_getaffinity(0))
    except AttributeError:
        n = os.cpu_count() or 1
    try:
        q, p = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if q != "max":
            n = min(n, max(1, int(int(q) / int(p))))
    except Exception:
        pass
    if os.name == "nt":
        n = min(n, 61)   # ProcessPoolExecutor limit on Windows
    return max(1, n)


def _read_int(path):
    try:
        t = Path(path).read_text().strip()
        return None if t == "max" else int(t)
    except Exception:
        return None


def _cgroup_stat(path, key):
    try:
        for line in Path(path).read_text().splitlines():
            k, v = line.split()
            if k == key:
                return int(v)
    except Exception:
        pass
    return None


def memory_status():
    """(available, total) memory in bytes.

    available = the smallest of: MemAvailable (psutil / OS call), cgroup memory limit minus the
    cgroup's non-reclaimable (anonymous) usage, and MEM_GUARANTEE (bytes, if set) minus that usage.
    """
    avail = total = None
    try:
        import psutil
        vm = psutil.virtual_memory()
        avail, total = int(vm.available), int(vm.total)
    except Exception:
        pass
    if avail is None and os.name == "nt":
        try:
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
            ms = MS()
            ms.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
            avail, total = int(ms.ullAvailPhys), int(ms.ullTotalPhys)
        except Exception:
            pass
    if avail is None:
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemAvailable:"):
                    avail = int(line.split()[1]) * 1024
                elif line.startswith("MemTotal:"):
                    total = int(line.split()[1]) * 1024
        except Exception:
            pass
    caps = []
    cg2 = Path("/sys/fs/cgroup")
    used = _cgroup_stat(cg2 / "memory.stat", "anon")
    for name in ("memory.max", "memory.high"):
        lim = _read_int(cg2 / name)
        if lim is not None and used is not None and lim < 1 << 50:
            caps.append(lim - used)
    lim1 = _read_int("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    used1 = _cgroup_stat("/sys/fs/cgroup/memory/memory.stat", "total_rss")
    if lim1 is not None and used1 is not None and lim1 < 1 << 50:
        caps.append(lim1 - used1)
        used = used1 if used is None else used
    try:
        g = int(os.environ["MEM_GUARANTEE"])
        caps.append(g - (used if used is not None else max(0, (total or g) - (avail or 0))))
    except (KeyError, ValueError):
        pass
    cands = [c for c in ([avail] + caps) if c is not None]
    return max(0, min(cands)) if cands else 2 * 1024 ** 3, total or 0


def available_memory():
    return memory_status()[0]


def memory_buffer_bytes(total, buffer_gb=None, available=None):
    """Memory kept free for other programs: 20 % of the available memory, at least 0.5 GB and at
    most 1 GB (2.5 GB free -> 0.5, 12 GB free -> 1.0). `total` is kept for callers of the old rule."""
    if buffer_gb is not None:
        return int(buffer_gb * 1024 ** 3)
    if available is None:
        available = available_memory()
    return int(min(max(0.2 * available, 0.5 * 1024 ** 3), 1.0 * 1024 ** 3))


def memory_mode():
    """'container' when a cgroup memory limit or MEM_GUARANTEE is set (going over it gets the run
    killed: strict planning), else 'desktop' (Windows, macOS, plain Linux: a shortage means paging,
    so planning is more lenient). PREPROCESS_MEMORY_MODE=desktop|container overrides."""
    forced = os.environ.get("PREPROCESS_MEMORY_MODE", "").strip().lower()
    if forced in ("desktop", "container"):
        return forced
    if os.environ.get("MEM_GUARANTEE", "").strip():
        return "container"
    if os.name == "nt" or sys.platform == "darwin":
        return "desktop"
    for p in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.high",
              "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        v = _read_int(p)
        if v is not None and v < 1 << 50:
            return "container"
    return "desktop"


def _pid_alive(pid):
    """True / False if known, None if it cannot be checked."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid == os.getpid():
        return True
    try:
        import psutil
        return psutil.pid_exists(pid)
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            code = ctypes.c_ulong()
            k.GetExitCodeProcess(h, ctypes.byref(code))
            k.CloseHandle(h)
            return code.value == 259                 # STILL_ACTIVE
        except Exception:
            return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None


# ---------------------------------------------------------------- majority filter
# ring order of the 8 neighbours (row offset, col offset): N NE E SE S SW W NW
_RING = [(-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1)]


def _decide(val_of, ok_of, ring, k, centre_is_cane, cane, allowed=None):
    """Cane mask after one filter pass, independent of the number of classes.

    A value qualifies when k ring-consecutive neighbours are valid and equal to each other
    (run starting at ring position s; allowed(s) restricts the start positions on raster edges).
    One qualifying value replaces the centre; none or two different ones keep the centre."""
    n = len(ring)
    V = [val_of(j) for j in ring]
    OK = [ok_of(j) for j in ring]
    E = [(V[i] == V[(i + 1) % n]) & OK[i] & OK[(i + 1) % n] for i in range(n)]
    has1 = np.zeros(centre_is_cane.shape, dtype=bool)
    has2 = np.zeros(centre_is_cane.shape, dtype=bool)
    w1 = np.array(V[0], copy=True)
    for s in range(n):
        run = OK[s].copy() if k == 1 else E[s].copy()
        for i in range(1, k - 1):
            run &= E[(s + i) % n]
        if allowed is not None:
            run &= allowed(s)
        vs = V[s]
        has2 |= run & has1 & (vs != w1)
        new = run & ~has1
        w1 = np.where(new, vs, w1)
        has1 |= new
    return np.where(has1 & ~has2, w1 == cane, centre_is_cane)


def _filter_rows(P, Q, data, r0, r1, cane, nb, majority):
    cols = data.shape[1]

    def sl(j):
        dr, dc = _RING[j]
        return (slice(r0 + 1 + dr, r1 + 1 + dr), slice(1 + dc, 1 + dc + cols))

    ring = [0, 2, 4, 6] if nb == 4 else list(range(8))
    k = (nb // 2 + 1) if majority else (nb // 2)
    res = _decide(lambda j: P[sl(j)], lambda j: Q[sl(j)], ring, k, data[r0:r1] == cane, cane)
    return res & Q[r0 + 1:r1 + 1, 1:cols + 1]


def _filter_frame(P, Q, data, cane, nb, sides):
    """Cells on a true raster edge (Esri rule): FOUR needs two adjacent equal neighbours;
    EIGHT needs three contiguous equal neighbours including one along the edge.
    sides = (top, bottom, left, right): which array edges are true raster edges."""
    rows, cols = data.shape
    top, bottom, left, right = sides
    fr = np.zeros(data.shape, dtype=bool)
    if top:
        fr[0, :] = True
    if bottom:
        fr[-1, :] = True
    if left:
        fr[:, 0] = True
    if right:
        fr[:, -1] = True
    rr, cc = np.nonzero(fr)
    if len(rr) == 0:
        return rr, cc, np.zeros(0, dtype=bool)

    def ix(j):
        dr, dc = _RING[j]
        return rr + 1 + dr, cc + 1 + dc

    allowed = None
    if nb == 8:
        row_edge = ((rr == 0) & top) | ((rr == rows - 1) & bottom)
        col_edge = ((cc == 0) & left) | ((cc == cols - 1) & right)
        along = [(row_edge & (j in (2, 6))) | (col_edge & (j in (0, 4))) for j in range(8)]
        allowed = lambda s: along[s] | along[(s + 1) % 8] | along[(s + 2) % 8]  # noqa: E731
    ring = [0, 2, 4, 6] if nb == 4 else list(range(8))
    res = _decide(lambda j: P[ix(j)], lambda j: Q[ix(j)], ring, 2 if nb == 4 else 3,
                  data[rr, cc] == cane, cane, allowed)
    return rr, cc, res & Q[rr + 1, cc + 1]


def filter_array(data, nodata, cane, neighbors=4, threshold="majority",
                 sides=(True, True, True, True), workers=1):
    """Return (filtered == cane) as a bool array. sides: which array edges are true raster edges
    (the other edges carry a halo, whose own ring of cells is not reliable and must be cropped)."""
    valid = np.ones(data.shape, dtype=bool) if nodata is None else (data != nodata)
    P = np.pad(data, 1)
    Q = np.pad(valid, 1)  # padding and nodata never vote
    rows = data.shape[0]
    maj = threshold == "majority"
    if workers <= 1:
        res = _filter_rows(P, Q, data, 0, rows, cane, neighbors, maj)
    else:
        step = max(64, -(-rows // (workers * 4)))
        spans = [(r, min(r + step, rows)) for r in range(0, rows, step)]
        res = np.empty(data.shape, dtype=bool)

        def run_span(sp):
            res[sp[0]:sp[1]] = _filter_rows(P, Q, data, sp[0], sp[1], cane, neighbors, maj)

        with ThreadPoolExecutor(workers) as ex:
            list(ex.map(run_span, spans))
    rr, cc, fres = _filter_frame(P, Q, data, cane, neighbors, sides)
    res[rr, cc] = fres
    return res


def majority_cane(data, nodata, cane, workers, neighbors=4, threshold="majority"):
    """Whole-array filter (all four edges are raster edges), optionally threaded."""
    return filter_array(data, nodata, cane, neighbors, threshold, workers=workers)


# ---------------------------------------------------------------- CRS helpers
def zone_crs(zone, north):
    return pyproj.CRS.from_epsg((32600 if north else 32700) + int(zone))


def _zone_of_lon(lon):
    return int(min(60, max(1, (lon + 180) // 6 + 1)))


def data_centre(crs, bounds):
    """(lon, lat) of the raster centre and the lon/lat of 84 edge samples."""
    t = pyproj.Transformer.from_crs(crs, 4326, always_xy=True)
    xs = np.linspace(bounds[0], bounds[2], 21)
    ys = np.linspace(bounds[1], bounds[3], 21)
    px = np.concatenate([xs, xs, np.full(21, bounds[0]), np.full(21, bounds[2])])
    py = np.concatenate([np.full(21, bounds[1]), np.full(21, bounds[3]), ys, ys])
    lon, lat = t.transform(px, py)
    clon, clat = t.transform((bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2)
    return float(clon), float(clat), np.asarray(lon, float), np.asarray(lat, float)


def crosses_antimeridian(lon):
    lon = lon[np.isfinite(lon)]
    return bool(len(lon) and np.nanmax(lon) - np.nanmin(lon) > 180)


def utm_options(crs, bounds):
    """UTM zones covering the raster extent: list of (CRS, label, note); first = suggested.
    Empty list when the data cross the 180 degree meridian (an explicit EPSG is needed)."""
    clon, clat, lon, lat = data_centre(crs, bounds)
    if crosses_antimeridian(lon):
        return []
    zc = _zone_of_lon(clon)
    zones = sorted(set(_zone_of_lon(v) for v in lon[np.isfinite(lon)]) | {zc})
    zones = list(range(zones[0], zones[-1] + 1))
    zones.sort(key=lambda z: (z != zc, abs(z - zc)))
    north_c = clat >= 0
    spans_eq = bool(np.nanmin(lat) < 0 < np.nanmax(lat))
    opts = []
    for z in zones:
        for nth in ([north_c] + ([not north_c] if spans_eq else [])):
            c = zone_crs(z, nth)
            note = ("suggested: covers the centre of the data" if (z == zc and nth == north_c)
                    else "covers part of the data")
            opts.append((c, f"UTM {z}{'N' if nth else 'S'}", note))
    return opts


def utm_zone_of(crs):
    """UTM zone number of a CRS, or None when it is not a UTM projection."""
    try:
        name = crs.coordinate_operation.name if crs.coordinate_operation else ""
    except Exception:
        name = ""
    m = re.match(r"UTM zone (\d{1,2})([NS])", name or "")
    return int(m.group(1)) if m else None


def parse_crs_msg(text, default_north=True, options=None):
    """Parse '1' (list number), '42N', 'utm 47 n', '32642', 'EPSG:32642' -> (CRS or None, hint).

    A bare number is a list number when the list has that many entries; otherwise (with a list
    shown) the user must type the hemisphere, e.g. 1N. Without a list a bare 1-60 is a zone."""
    s = text.strip().lower().replace("_", " ")
    if not s:
        return None, "Type a list number, a zone like 47N, or an EPSG code like 32647."
    try:
        if re.fullmatch(r"\d+", s) and options:
            n = int(s)
            if 1 <= n <= len(options):
                return options[n - 1][0], None
            if n <= 60:
                return None, (f"{n} is not a list number. For UTM zone {n} type {n}N or {n}S, "
                              f"or type an EPSG code.")
        m = re.fullmatch(r"(?:utm)?\s*(\d{1,2})\s*([ns])?", s)
        if m and 1 <= int(m.group(1)) <= 60:
            north = default_north if m.group(2) is None else m.group(2) == "n"
            crs = zone_crs(m.group(1), north)
        else:
            m = re.fullmatch(r"(?:epsg\s*[: ]?\s*)?(\d{4,6})", s)
            if not m:
                return None, "Not understood. Type a list number, a zone like 47N, or an EPSG code like 32647."
            crs = pyproj.CRS.from_epsg(int(m.group(1)))
    except Exception:
        return None, "Unknown coordinate system. Type a zone like 47N or an EPSG code like 32647."
    if not crs.is_projected:
        return None, f"EPSG:{crs.to_epsg()} is not projected (degrees). Choose a UTM zone, e.g. 47N."
    if any(abs(ax.unit_conversion_factor - 1.0) > 1e-9 for ax in crs.axis_info):
        return None, f"EPSG:{crs.to_epsg()} is not in metres. Choose a UTM zone, e.g. 47N."
    return crs, None


def parse_crs(text, default_north=True, options=None):
    return parse_crs_msg(text, default_north, options)[0]


def area_crs_notes(crs, raster_crs, bounds):
    """(is_utm, far_note or None) for a chosen area CRS."""
    z = utm_zone_of(crs)
    if z is None:
        return False, None
    clon, _, lon, _ = data_centre(raster_crs, bounds)
    if crosses_antimeridian(lon):
        return True, None
    zc = _zone_of_lon(clon)
    d = min(abs(z - zc), 60 - abs(z - zc))
    if d > 1:
        return True, (f"UTM zone {z} is {d} zones away from the data centre (zone {zc}); "
                      f"areas get less accurate far from the zone centre.")
    return True, None


# ---------------------------------------------------------------- path helper
def clean_path(text):
    """Normalise a pasted path: quotes, leading '&' / r-prefix, file:// URL, ~, %VAR%, $VAR, backslashes.

    Variables are expanded only when they exist and the path as typed does not exist.
    Returns a string with forward slashes (valid on Windows and Linux).
    """
    s = text.strip()
    s = re.sub(r"^&\s*", "", s)
    s = re.sub(r"^[rR](?=[\"'\u201c\u2018])", "", s)
    quotes = "\"'\u201c\u201d\u2018\u2019"
    for _ in range(3):
        s = s.strip()
        if len(s) >= 2 and s[0] in quotes and s[-1] in quotes:
            s = s[1:-1]
        else:
            break
    s = s.strip()
    if s.lower().startswith("file:"):
        u = urlparse(s)
        p = unquote(u.path)
        if u.netloc and u.netloc != "localhost":
            p = "//" + u.netloc + p
        elif re.match(r"^/[A-Za-z]:", p):
            p = p[1:]
        s = p
    raw = s.replace("\\", "/")
    try:
        raw_exists = os.path.exists(raw)
    except (OSError, ValueError):
        raw_exists = False
    if not raw_exists:
        s = re.sub(r"%([^%\\/]+)%", lambda m: os.environ.get(m.group(1), m.group(0)), s)
        s = re.sub(r"\$\{(\w+)\}|\$(\w+)",
                   lambda m: os.environ.get(m.group(1) or m.group(2), m.group(0)), s)
    s = s.replace("\\", "/")
    if s.startswith("~"):
        s = os.path.expanduser(s).replace("\\", "/")
    return s


def file_set(path, exts):
    """Explicit sidecar paths of a file (no glob patterns, safe with [ ] in names)."""
    p = Path(path)
    base = str(p.with_suffix(""))
    return {e: Path(base + e) for e in exts}


def out_exts(out):
    s = Path(out).suffix
    return SHP_SIDECARS if s.lower() == ".shp" else GPKG_SIDECARS if s.lower() == ".gpkg" else (s,)


def folder_writable(folder):
    try:
        fd, p = tempfile.mkstemp(prefix=".pc_write_test_", dir=str(folder))
        os.close(fd)
        os.remove(p)
        return True
    except OSError:
        return False


def locked_outputs(out):
    """Existing output files that cannot be opened for writing (open in ArcMap/QGIS on Windows)."""
    bad = []
    for p in file_set(out, out_exts(out)).values():
        if p.exists():
            try:
                with open(p, "r+b"):
                    pass
            except OSError:
                bad.append(p.name)
    return bad


# ---------------------------------------------------------------- vector formats and layers
VECTOR_EXTS = (".shp", ".gpkg", ".geojson", ".json")
FORMAT_HINT = "Use a Shapefile (.shp), a GeoPackage (.gpkg) or a GeoJSON file (.geojson / .json)."
GEOJSON_BYTES_RATIO = 2.5      # GeoJSON text / shapefile size (.shp+.shx+.dbf), measured on two samples: 2.40, 2.43
GEOJSON_NOTE_BYTES = 1024 ** 3  # estimated GeoJSON size above which a NOTE suggests a GeoPackage
_LAYERS = {}                    # input file (resolved path) -> chosen layer of a file with several layers


def is_geojson(path):
    return Path(path).suffix.lower() in (".geojson", ".json")


def driver_of(path):
    s = Path(path).suffix.lower()
    return "GPKG" if s == ".gpkg" else "GeoJSON" if s in (".geojson", ".json") else "ESRI Shapefile"


def _layer_key(path):
    try:
        return str(Path(path).resolve())
    except (OSError, ValueError):
        return str(path)


def layer_kw(path):
    """{'layer': name} when a layer was chosen for this file (a GeoPackage with several layers), else {}."""
    name = _LAYERS.get(_layer_key(path))
    return {"layer": name} if name else {}


def list_layers(path):
    """[(name, geometry type, features)] of a vector file; UserError when it cannot be read."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ls = pyogrio.list_layers(str(path))
            return [(str(n), str(t), int(pyogrio.read_info(str(path), layer=str(n)).get("features", 0)))
                    for n, t in ls]
    except Exception as e:
        raise UserError(f"cannot read {Path(path).name} as a vector layer ({str(e).splitlines()[0][:200]}). "
                        f"{FORMAT_HINT}")


def polygon_layers(layers):
    """Layers that can hold polygons (type Polygon / MultiPolygon, or unknown / mixed types)."""
    return [x for x in layers if "polygon" in x[1].lower() or x[1].lower() in ("none", "unknown", "geometry",
                                                                               "geometrycollection")]


def choose_layer(path, layer=None, what="L1", flag="--l1-layer"):
    """Pick the layer of an input file and remember it for every later read. One layer: that one.
    Several: the given name; else the only polygon layer; else UserError listing the layers."""
    layers = list_layers(path)
    key = _layer_key(path)
    names = [n for n, _, _ in layers]
    if not layers:
        raise UserError(f"the {what} file {Path(path).name} has no layers. {FORMAT_HINT}")
    if layer:
        if layer not in names:
            raise UserError(f"the {what} file {Path(path).name} has no layer '{layer}'; its layers: "
                            + ", ".join(names))
        if len(names) > 1:
            _LAYERS[key] = layer
        else:
            _LAYERS.pop(key, None)
        return layer
    if len(layers) == 1:
        _LAYERS.pop(key, None)
        return names[0]
    polys = polygon_layers(layers)
    if len(polys) == 1:
        _LAYERS[key] = polys[0][0]
        return polys[0][0]
    raise UserError(f"the {what} file {Path(path).name} has {len(layers)} layers: "
                    + ", ".join(f"{n} ({t}, {c:,} features)" for n, t, c in layers)
                    + f". Choose one with {flag} NAME")


def ask_layer(path, what="L1"):
    """Question mode: a file with several layers lists them and asks which one (default: the first
    polygon layer). Returns the layer name."""
    layers = list_layers(path)
    key = _layer_key(path)
    if len(layers) <= 1:
        _LAYERS.pop(key, None)
        return layers[0][0] if layers else None
    polys = polygon_layers(layers)
    if len(polys) == 1:
        print(f"   {Path(path).name} has {len(layers)} layers; its only polygon layer '{polys[0][0]}' is used.")
        _LAYERS[key] = polys[0][0]
        return polys[0][0]
    print(f"   {Path(path).name} has {len(layers)} layers:")
    for i, (n, t, c) in enumerate(layers, 1):
        print(f"   {i}) {n}  ({t}, {c:,} features)")
    first = layers.index(polys[0]) + 1 if polys else 1

    def check(a):
        a = a.strip()
        if a.isdigit() and 1 <= int(a) <= len(layers):
            return layers[int(a) - 1][0], None
        if a in [n for n, _, _ in layers]:
            return a, None
        return None, f"Type a number from 1 to {len(layers)} or a layer name."
    name = ask_text(f"   Which layer is the {what}", str(first), check)
    _LAYERS[key] = name
    return name


def vector_info(path):
    """pyogrio.read_info of the chosen layer (no 'several layers' warning)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return pyogrio.read_info(str(path), **layer_kw(path))


def vector_crs(path):
    """CRS of an input layer; a GeoJSON file without a crs member is EPSG:4326 (RFC 7946)."""
    crs = vector_info(path).get("crs")
    if not crs and is_geojson(path):
        return "EPSG:4326"
    return crs


def vector_bounds(path):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return pyogrio.read_bounds(str(path), **layer_kw(path))


def vector_read(path, **kw):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return pyogrio.raw.read(str(path), **layer_kw(path), **kw)


# ---------------------------------------------------------------- geometry quality (inputs and outputs)
_GEOM_NAMES = {0: "points", 1: "lines", 2: "lines", 4: "points", 5: "lines"}


def from_wkb_safe(wkb):
    """WKB -> geometries; rows GEOS cannot read (unclosed rings, too few points) are fixed when this
    shapely can (>= 2.1) or become missing. Returns (geoms, number of such rows)."""
    if len(wkb) == 0:
        return np.array([], dtype=object), 0
    try:
        return shapely.from_wkb(wkb), 0
    except Exception:
        pass
    out = np.empty(len(wkb), dtype=object)
    bad = 0
    for i, w in enumerate(wkb):
        try:
            out[i] = shapely.from_wkb(w) if w is not None else None
        except Exception:
            bad += 1
            try:
                out[i] = shapely.from_wkb(w, on_invalid="fix")
            except Exception:
                out[i] = None
    return out, bad


def clean_polygons(g, workers=1, report=None):
    """Polygon parts of any geometries, in row order: missing / empty rows, points and lines are left
    out, Z / M values dropped, invalid shapes repaired (GEOS make_valid), multi-part shapes and
    collections split into single polygons, zero-area polygons left out. Returns (polygons, row of each
    polygon). report (dict) counts what was done. Rows that are already valid 2D single polygons come
    back as the same objects (so nothing about them changes)."""
    rep = report if report is not None else {}
    n = len(g)
    if n == 0:
        return np.array([], dtype=object), np.zeros(0, dtype=np.int64)
    orig = np.asarray(g, dtype=object)
    g = orig.copy()
    miss = shapely.is_missing(g)
    emp = np.zeros(n, dtype=bool)
    emp[~miss] = shapely.is_empty(g[~miss])
    ok = ~miss & ~emp
    tid = np.full(n, -1)
    tid[ok] = shapely.get_type_id(g[ok])
    counts = {"null": int(miss.sum()), "empty": int(emp.sum())}
    for t, name in _GEOM_NAMES.items():
        counts[name] = counts.get(name, 0) + int((tid == t).sum())
    ok &= np.isin(tid, (3, 6, 7))
    z = np.zeros(n, dtype=bool)
    z[ok] = shapely.has_z(g[ok])
    if z.any():
        g[z] = shapely.force_2d(g[z])
    counts["z"] = int(z.sum())
    area0 = np.zeros(n, dtype=bool)                      # zero area as drawn (a bow-tie also has 0)
    area0[ok] = shapely.area(g[ok]) <= 0
    idx = np.flatnonzero(ok)
    valid = np.zeros(n, dtype=bool)
    if len(idx):
        with ThreadPoolExecutor(max(1, workers)) as ex:
            valid[idx] = np.concatenate(list(ex.map(lambda i: shapely.is_valid(g[idx[i]]),
                                                    _chunks(len(idx), workers))))
            bad = idx[~valid[idx]]
            if len(bad):
                fixed = ex.map(lambda i: shapely.make_valid(g[bad[i]]), _chunks(len(bad), workers, 16))
                g[bad] = np.concatenate(list(fixed))
        counts["invalid"] = int(len(idx) - valid[idx].sum())
    simple = ok & valid & (tid == 3) & ~z
    if simple[ok].all():
        parts, prow = orig[ok], idx
    else:
        rows = np.flatnonzero(ok)
        p1, i1 = shapely.get_parts(g[rows], return_index=True)
        p2, i2 = shapely.get_parts(p1, return_index=True)
        t2 = shapely.get_type_id(p2)
        sel = (t2 == 3) & ~shapely.is_empty(p2)
        sel[sel] = shapely.area(p2[sel]) > 0
        in_coll = (tid == 7)[rows[i1[i2]]]               # points and lines inside a collection as drawn
        for t, name in _GEOM_NAMES.items():
            counts[name] += int((in_coll & (t2 == t)).sum())
        parts, prow = p2[sel], rows[i1[i2][sel]]
        keep = simple[prow]
        parts[keep] = orig[prow[keep]]                   # untouched rows: the very same objects
        cnt = np.bincount(prow, minlength=n)
        counts["split"] = int((ok & np.isin(tid, (6, 7)) & (cnt >= 1)).sum())
        counts["zero_area"] = int((ok & (cnt == 0) & area0).sum())
        counts["collapsed"] = int((ok & (cnt == 0) & ~area0).sum())
        if counts["zero_area"]:                          # a zero-area shape is not counted as repaired
            counts["invalid"] = counts.get("invalid", 0) - int((ok & (cnt == 0) & area0 & ~valid).sum())
    for k, v in counts.items():
        if v:
            rep[k] = rep.get(k, 0) + v
    return parts, prow


_REPORT_TEXT = (("null", "null geometry", "null geometries", "ignored"),
                ("empty", "empty geometry", "empty geometries", "ignored"),
                ("unreadable", "unreadable geometry", "unreadable geometries", "fixed or ignored"),
                ("points", "point", "points", "ignored"), ("lines", "line", "lines", "ignored"),
                ("zero_area", "zero-area polygon", "zero-area polygons", "ignored"),
                ("z", "geometry with Z/M values", "geometries with Z/M values", "made 2D"),
                ("invalid", "invalid polygon", "invalid polygons", "repaired"),
                ("split", "multi-part feature", "multi-part features", "split into single polygons"),
                ("collapsed", "polygon without area after repair", "polygons without area after repair", "ignored"),
                ("repeated", "polygon with repeated vertices", "polygons with repeated vertices", "cleaned"),
                ("overlaps", "overlap between polygons", "overlaps between polygons", "resolved"))


def report_text(rep):
    """'3 null geometries ignored, 12 invalid polygons repaired' ('' when nothing was done)."""
    out = []
    for k, one, many, what in _REPORT_TEXT:
        v = int((rep or {}).get(k, 0))
        if v:
            out.append(f"{v:,} {one if v == 1 else many} {what}")
    return ", ".join(out)


def input_note(what, rep):
    """'L1: 3 null geometries ignored, ...' or None when the layer was clean."""
    t = report_text(rep)
    return f"{what}: {t}." if t else None


def _real_overlaps(g, min_area, chunk=200_000, progress=None, workers=None):
    """Pairs (i < j) of polygons whose interiors share more than min_area (units of g): bounding-box
    pairs from an STRtree, then the DE-9IM matrix (interior/interior of dimension 2) and the area of
    the shared part, in chunks of query geometries and in parallel threads."""
    n = len(g)
    if n < 2:
        return np.zeros((0, 2), dtype=np.int64)
    tree = STRtree(g)
    workers = workers or n_cpus()
    res = []
    with ThreadPoolExecutor(max(1, workers)) as ex:
        for k in range(0, n, chunk):
            a, b = tree.query(g[k:k + chunk])
            a = a + k
            m = a < b
            a, b = a[m], b[m]
            if len(a):
                rel = np.concatenate(list(ex.map(lambda i: shapely.relate(g[a[i]], g[b[i]]),
                                                 _chunks(len(a), workers))))
                hit = np.char.startswith(rel.astype(str), "2")
                a, b = a[hit], b[hit]
                if len(a):
                    inter = shapely.area(shapely.intersection(g[a], g[b]))
                    keep = inter > min_area
                    res.append(np.stack([a[keep], b[keep]], 1))
            if progress and n > chunk:
                progress(min(k + chunk, n), n)
    return np.concatenate(res) if res else np.zeros((0, 2), dtype=np.int64)


def min_area_deg(g):
    """0.01 m2 (the noise size) in square degrees at the latitude of the data (EPSG:4326 geometries)."""
    if len(g) == 0:
        return 0.0
    b = shapely.total_bounds(g)
    lat = math.radians(max(min((b[1] + b[3]) / 2, 89.0), -89.0))
    return NOISE_M2 / (111_320.0 ** 2 * max(math.cos(lat), 0.01))


def gate(g, cls=None, rep=None):
    """Quality gate on the features about to be written (EPSG:4326, in the form they are written).

    Guarantees: single 2D polygons, valid by the GEOS rules (self-intersections, ring self-touches,
    spikes, holes outside the shell, rings of fewer than 4 points are repaired with make_valid), no null,
    empty or zero-area shapes, no repeated consecutive vertices, and no two polygons that share area
    (more than 0.01 m2): the same class is dissolved into the first polygon, between classes the lower
    code wins (81 over 82). Returns (geoms, row of each geometry in the input, changed flags); rows that
    need nothing come back as the same objects, so their written bytes do not change."""
    rep = rep if rep is not None else {}
    n = len(g)
    if n == 0:
        return g, np.zeros(0, dtype=np.int64), np.zeros(0, dtype=bool)
    parts, rows = clean_polygons(g, n_cpus(), rep)
    g = np.asarray(g, dtype=object)
    same = np.fromiter((parts[i] is g[rows[i]] for i in range(len(parts))), dtype=bool, count=len(parts))
    changed = ~(same & (np.bincount(rows, minlength=n)[rows] == 1))
    nc = shapely.get_num_coordinates(parts)
    rr = shapely.remove_repeated_points(parts, 0.0)
    rep_v = shapely.get_num_coordinates(rr) < nc
    if rep_v.any():
        parts = parts.copy()
        parts[rep_v] = rr[rep_v]
        changed |= rep_v
        rep["repeated"] = rep.get("repeated", 0) + int(rep_v.sum())
    pairs = _real_overlaps(parts, min_area_deg(parts))
    if len(pairs):
        parts = parts.copy()
        c = np.zeros(len(parts), dtype=np.int64) if cls is None else np.asarray(cls)[rows]
        drop = np.zeros(len(parts), dtype=bool)
        for i, j in pairs:
            if drop[i] or drop[j]:
                continue
            if c[i] == c[j]:                            # same class: dissolve into the first one
                pp = polys_only(np.array([shapely.make_valid(shapely.union(parts[i], parts[j]))], dtype=object))
                if len(pp):
                    parts[i] = pp[np.argmax(shapely.area(pp))]
                drop[j] = True
                changed[i] = True
            else:                                       # the lower class code wins (81 over 82)
                w, lo = (i, j) if c[i] < c[j] else (j, i)
                pp = polys_only(np.array([shapely.make_valid(shapely.difference(parts[lo], parts[w]))],
                                         dtype=object))
                if len(pp):
                    parts[lo] = pp[np.argmax(shapely.area(pp))]
                else:
                    drop[lo] = True
                changed[lo] = True
        rep["overlaps"] = rep.get("overlaps", 0) + len(pairs)
        parts, rows, changed = parts[~drop], rows[~drop], changed[~drop]
    return parts, rows, changed


def geometry_check(path, progress=True):
    """Read a written file back and check what is on disk: declared layer type, geometry types, null /
    empty, GEOS validity (with reasons), repeated consecutive vertices, Z / M values, zero area and
    overlapping polygons (more than 0.01 m2 shared). Returns a dict of counts."""
    t = time.time()
    g = _read_own_geojson(path) if is_geojson(path) else None
    if g is not None:                    # GeoJSON has no declared layer type: Polygon when every feature is one
        unreadable = 0
        types = set(shapely.get_type_id(g[~shapely.is_missing(g)]).tolist())
        layer_type = "Polygon" if types <= {3} else "Unknown"
    else:
        info = vector_info(path)
        _, _, wkb, _ = pyogrio.raw.read(str(path), columns=[])
        g, unreadable = from_wkb_safe(wkb)
        layer_type = str(info.get("geometry_type"))
    res = {"features": len(g), "layer_type": layer_type, "unreadable": unreadable}
    miss = shapely.is_missing(g)
    emp = np.zeros(len(g), dtype=bool)
    emp[~miss] = shapely.is_empty(g[~miss])
    res["null_empty"] = int(miss.sum() + emp.sum())
    ok = ~miss & ~emp
    tid = np.full(len(g), -1)
    tid[ok] = shapely.get_type_id(g[ok])
    res["polygons"] = int((tid == 3).sum())
    res["other_types"] = int((ok & (tid != 3)).sum())
    poly = g[tid == 3]
    cores = n_cpus()
    with ThreadPoolExecutor(cores) as ex:
        valid = np.concatenate(list(ex.map(lambda i: shapely.is_valid(poly[i]), _chunks(len(poly), cores)))) \
            if len(poly) else np.zeros(0, dtype=bool)
    res["invalid"] = int((~valid).sum())
    res["invalid_reasons"] = sorted({r.split("[")[0] for r in shapely.is_valid_reason(poly[~valid][:50])})
    res["zero_area"] = int((shapely.area(poly) <= 0).sum())
    res["repeated"] = int((shapely.get_num_coordinates(shapely.remove_repeated_points(poly, 0.0))
                           < shapely.get_num_coordinates(poly)).sum())
    res["z"] = int(shapely.has_z(g[ok]).sum())
    show = (lambda k, n: print(f"  geometry check: overlaps tested for {k:,} of {n:,} polygons", flush=True)) \
        if progress and len(poly) > 1_000_000 else None
    res["overlaps"] = int(len(_real_overlaps(poly[valid], min_area_deg(poly), progress=show)))
    res["seconds"] = time.time() - t
    return res


def geometry_ok(c):
    return (c["features"] == c["polygons"] and not c["null_empty"] and not c["invalid"] and not c["repeated"]
            and not c["overlaps"] and not c["z"] and not c["zero_area"] and c["layer_type"] == "Polygon")


def geometry_rows(c, rep):
    """Result-block rows of the geometry check (aligned with the tables above it)."""
    def ok(bad):
        return "CHECK" if bad else "OK"
    lt = c["layer_type"]
    rows = [("  Single polygons", f"{c['polygons']:,}", ok(c["polygons"] != c["features"])),
            ("  Other geometry types", f"{c['other_types']:,}", ok(c["other_types"])),
            ("  Null or empty", f"{c['null_empty']:,}", ok(c["null_empty"])),
            ("  Invalid (GEOS rules)", f"{c['invalid'] + c['zero_area']:,}", ok(c["invalid"] or c["zero_area"])),
            ("  Repeated vertices", f"{c['repeated']:,}", ok(c["repeated"])),
            ("  Overlapping pairs", f"{c['overlaps']:,}", ok(c["overlaps"])),
            ("  Layer type", lt + (" Z" if c["z"] else ""), ok(lt != "Polygon" or c["z"])),
            ("  Repaired before saving", f"{sum(int(v) for v in (rep or {}).values()):,}")]
    done = report_text(rep)
    if done:
        rows += wrap_rows(f"Before saving: {done}.")
    if not geometry_ok(c):
        what = []
        if c["invalid"]:
            what.append(f"{c['invalid']:,} invalid ({', '.join(c['invalid_reasons'])})")
        for k, label in (("other_types", "not single polygons"), ("null_empty", "null or empty"),
                         ("repeated", "with repeated vertices"), ("overlaps", "overlapping pairs"),
                         ("z", "with Z/M values"), ("zero_area", "without area")):
            if c[k]:
                what.append(f"{c[k]:,} {label}")
        if lt != "Polygon":
            what.append(f"layer type {lt}")
        rows += wrap_rows("WARNING: the saved file has geometry problems the tool could not repair: "
                          + "; ".join(what) + ". Check it in QGIS before you use it.")
    return rows


def orient_ccw(g):
    """Exterior rings counter-clockwise, holes clockwise (shapely >= 2.1 vectorised, else per polygon)."""
    if hasattr(shapely, "orient_polygons"):
        return shapely.orient_polygons(g)
    from shapely.geometry.polygon import orient
    return np.array([orient(x, 1.0) for x in g], dtype=object)


GEOJSON_HEAD = '{"type":"FeatureCollection","name":%s,"features":[\n'


def _json_number(v):
    """A JSON number: integers as integers, floats in the shortest form that reads back exactly."""
    if isinstance(v, (int, np.integer)):
        return str(int(v))
    return repr(float(v))


def write_vector(path, wkb, field_data, fields, driver, layer, append):
    """Write polygons (EPSG:4326, layer type Polygon). GeoJSON is written by this tool, one feature per
    line (RFC 7946: no crs member = EPSG:4326, exterior rings counter-clockwise, holes clockwise),
    with every coordinate in the shortest form that reads back to the same number (GEOS writer, in
    parallel threads) - GDAL's GeoJSON writer was 20 times slower and its RFC7946=YES option rounds
    coordinates to 7 decimals, which made polygons invalid in a test. finish_vector closes the file."""
    if driver != "GeoJSON":
        pyogrio.raw.write(str(path), geometry=wkb, field_data=field_data, fields=fields, geometry_type="Polygon",
                          crs="EPSG:4326", driver=driver, append=append, layer=layer)
        return
    big = len(wkb) and sum(map(len, wkb)) > PARALLEL_TEXT_BYTES // 3
    geo = np.concatenate(_in_processes(_to_geojson_wkb, _split_list(wkb, n_cpus() * 2) if big else [wkb])) \
        if len(wkb) else np.array([], dtype=object)
    cols = [[_json_number(v) for v in np.asarray(col).tolist()] for col in field_data]
    names = [json.dumps(f) for f in fields]
    with open(path, "a" if append else "w", encoding="ascii", newline="\n") as fh:
        if not append:
            fh.write(GEOJSON_HEAD % json.dumps(layer or Path(path).stem))
        lines = []
        for i, gj in enumerate(geo):
            props = ",".join(f"{n}:{c[i]}" for n, c in zip(names, cols))
            lead = "," if (append or i) else ""
            lines.append(f'{lead}{{"type":"Feature","properties":{{{props}}},"geometry":{gj}}}\n')
        fh.write("".join(lines))


def finish_vector(path, driver):
    """Close a GeoJSON file written by write_vector (nothing to do for the other formats)."""
    if driver == "GeoJSON":
        with open(path, "a", encoding="ascii", newline="\n") as fh:
            fh.write("]}\n")


_POLY_HEAD, _POLY_TAIL = '{"type":"Polygon","coordinates":[[[', ']]]}'


def _parse_own_lines(feats):
    """Feature lines of write_vector -> WKB of their polygons (runs in worker processes): the numbers
    are read with numpy (correctly rounded: the same numbers that were written), rings and polygons
    are built with GEOS. None when a line does not have the expected layout."""
    key = ',"geometry":'
    rings, ring_poly = [], []
    for k, x in enumerate(feats):
        i = x.rfind(key)
        if not x.startswith('{"type":"Feature"') or i < 0 or not x.endswith("}"):
            return None
        gj = x[i + len(key):-1]
        if not (gj.startswith(_POLY_HEAD) and gj.endswith(_POLY_TAIL)):
            return None
        for r in gj[len(_POLY_HEAD):-len(_POLY_TAIL)].split("]],[["):
            rings.append(r)
            ring_poly.append(k)
    if not feats:
        return np.array([], dtype=object)
    try:
        npts = np.array([r.count("],[") + 1 for r in rings])
        nums = np.fromstring(",".join(r.replace("],[", ",") for r in rings), sep=",")
        if len(nums) != 2 * npts.sum():
            return None
        lr = shapely.linearrings(nums.reshape(-1, 2), indices=np.repeat(np.arange(len(rings)), npts))
        return to_wkb(shapely.polygons(lr, indices=np.array(ring_poly)))
    except Exception:
        return None


def _to_geojson_wkb(wkb):
    """WKB -> GeoJSON geometry texts, exterior rings counter-clockwise (runs in worker processes)."""
    return shapely.to_geojson(orient_ccw(shapely.from_wkb(wkb)))


PARALLEL_TEXT_BYTES = 20 * 1024 ** 2    # GeoJSON text above this is written / read in worker processes


def _in_processes(fn, parts, workers=None):
    """[fn(p) for p in parts] in spawned worker processes (the text work of GeoJSON holds the GIL);
    serial when there is only one part or the processes cannot start."""
    workers = min(workers or n_cpus(), len(parts))
    if workers < 2:
        return [fn(p) for p in parts]
    try:
        with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn")) as ex:
            return list(ex.map(fn, parts))
    except (OSError, RuntimeError, ImportError, AttributeError, pickle.PicklingError):
        return [fn(p) for p in parts]


def _split_list(items, n):
    k = max(1, math.ceil(len(items) / max(1, n)))
    return [items[i:i + k] for i in range(0, len(items), k)]


def _parse_own_range(task):
    """Worker: parse the feature lines between two byte offsets of a GeoJSON file of this tool."""
    path, start, end = task
    with open(path, "rb") as fh:
        fh.seek(start)
        text = fh.read(end - start).decode("ascii")
    feats = [x[1:] if x.startswith(",") else x for x in text.split("\n") if x]
    return _parse_own_lines(feats)


def _read_own_geojson(path):
    """Geometries of a GeoJSON file written by write_vector, parsed from the bytes on disk; big files
    in worker processes that each read their own byte range. None when the file has another layout."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            head = fh.readline()
            body0 = fh.tell()
            fh.seek(max(size - 4, 0))
            tail = fh.read()
    except OSError:
        return None
    if not head.startswith(b'{"type":"FeatureCollection"') or not tail.endswith(b"]}\n"):
        return None
    body1 = size - 3
    n = n_cpus() * 2 if body1 - body0 > PARALLEL_TEXT_BYTES else 1
    cuts = [body0]
    with open(path, "rb") as fh:
        for k in range(1, n):
            fh.seek(max(body0 + (body1 - body0) * k // n, cuts[-1]))
            fh.readline()                                   # to the start of the next line
            cuts.append(min(fh.tell(), body1))
    cuts.append(body1)
    tasks = [(str(path), a_, b_) for a_, b_ in zip(cuts[:-1], cuts[1:]) if b_ > a_]
    if not tasks:
        return np.array([], dtype=object)
    res = _in_processes(_parse_own_range, tasks)
    if any(r is None for r in res):
        return None
    return shapely.from_wkb(np.concatenate(res)) if res else np.array([], dtype=object)


def feature_count(path):
    """Number of features in a written file (for the GeoJSON files of this tool: their lines)."""
    if is_geojson(path):
        try:
            with open(path, "rb") as fh:
                data = fh.read()
            if data.startswith(b'{"type":"FeatureCollection"') and data.endswith(b"\n]}\n"):
                return data.count(b"\n") - 2
        except OSError:
            pass
    return pyogrio.read_info(str(path))["features"]


# ---------------------------------------------------------------- geometry helpers
def polys_only(geoms):
    """Flatten geometries to non-empty polygons."""
    parts = shapely.get_parts(shapely.get_parts(geoms))
    return parts[(shapely.get_type_id(parts) == 3) & ~shapely.is_empty(parts)]


def _chunks(n, workers, per=4):
    return np.array_split(np.arange(n), max(1, min(n, workers * per)))


def project_geoms(geoms, src_crs, dst_crs, workers=1):
    """Transform geometries (chunked, vectorised over all coordinates of a chunk)."""
    if len(geoms) == 0:
        return geoms
    workers = min(workers, max(1, len(geoms) // 5000))   # thread start-up is not free

    def run_chunk(ix):
        tr = pyproj.Transformer.from_crs(src_crs, dst_crs, always_xy=True)
        g = geoms[ix]
        xy = shapely.get_coordinates(g)
        x, y = tr.transform(xy[:, 0], xy[:, 1])
        return shapely.set_coordinates(g.copy(), np.column_stack((x, y)))

    if workers <= 1:
        return run_chunk(np.arange(len(geoms)))
    with ThreadPoolExecutor(workers) as ex:
        return np.concatenate(list(ex.map(run_chunk, _chunks(len(geoms), workers))))


def area_m2(geoms, crs, utm_crs, workers=1):
    proj = geoms if pyproj.CRS(crs) == utm_crs else project_geoms(geoms, crs, utm_crs, workers)
    return shapely.area(proj)


def acres_of(geoms, crs, utm_crs, workers=1):
    return area_m2(geoms, crs, utm_crs, workers) / ACRE_M2


def to_wkb(geoms):
    return shapely.to_wkb(geoms) if len(geoms) else np.array([], dtype=object)


def l1_info(path):
    """(crs or None, fids, bounds[n,4]) of the L1 layer without reading geometries (a GeoJSON file
    without a crs member is EPSG:4326)."""
    fids, b = vector_bounds(path)
    return vector_crs(path), fids, np.asarray(b).T


def _read_geoms(path, bbox=None):
    """(geometries, layer crs, report) of an input layer; unreadable rows are counted in the report."""
    kw = {"bbox": tuple(float(v) for v in bbox)} if bbox is not None else {}
    try:
        meta, _, wkb, _ = vector_read(path, read_geometry=True, columns=[], **kw)
    except UserError:
        raise
    except Exception as e:
        raise UserError(f"cannot read {Path(path).name} ({str(e).splitlines()[0][:200]}). {FORMAT_HINT}")
    g, bad = from_wkb_safe(wkb)
    crs = meta.get("crs") or ("EPSG:4326" if is_geojson(path) else None)
    return g, crs, ({"unreadable": bad} if bad else {}), wkb


def load_l1(path, crs, bbox=None, workers=1, trusted=False, report=None):
    """Read an input polygon layer (optionally only a bbox in its CRS), reproject to `crs` (a layer
    without CRS is taken as being in `crs`) and keep the polygon parts only: null / empty rows, points
    and lines are ignored, Z values dropped, invalid shapes repaired, multi-parts split (clean_polygons).
    report (dict) receives the counts. trusted = a layer this tool prepared itself (no checks)."""
    g, lcrs, rep, _ = _read_geoms(path, bbox)
    if report is not None:
        report.update(rep)
    if trusted:
        g = g[~shapely.is_missing(g)]
        return polys_only(g) if len(g) else g
    keep = ~shapely.is_missing(g)
    keep[keep] = ~shapely.is_empty(g[keep])
    if lcrs and keep.any() and pyproj.CRS(lcrs) != pyproj.CRS(crs):
        g = g.copy()
        g[keep] = project_geoms(g[keep], lcrs, crs, workers)
    return clean_polygons(g, workers, report)[0]


def _valid_l1_wkb(l1_path, crs, workers=1, report=None):
    """WKB of load_l1(l1_path, crs) - the same polygons in the same order. Rows that are already valid
    2D single polygons keep their source WKB when no reprojection is needed."""
    g, lcrs, rep, wkb = _read_geoms(l1_path)
    if report is not None:
        report.update(rep)
    if len(g) == 0:
        return np.array([], dtype=object)
    if lcrs and pyproj.CRS(lcrs) != pyproj.CRS(crs):
        return to_wkb(load_l1(l1_path, crs, workers=workers, report=report))
    parts, rows = clean_polygons(g, workers, report)
    same = np.fromiter((parts[i] is g[rows[i]] for i in range(len(parts))), dtype=bool, count=len(parts))
    out = np.empty(len(parts), dtype=object)
    out[same] = wkb[rows[same]]
    if (~same).any():
        out[~same] = to_wkb(parts[~same])
    return out


L1_CACHE_DIR = Path.home() / ".preprocess_classified" / "l1_prepared"
L1_CACHE_MARKER = "preprocess_classified.l1cache"
L1_CACHE_INDEX = "index.json"
L1_CACHE_RE = re.compile(r"^l1v_[0-9a-f]{16}(\.\d+\.tmp)?\.gpkg(-wal|-shm|-journal)?$")


def l1_cache_cap_bytes():
    try:
        return int(float(os.environ.get("PREPROCESS_L1_CACHE_GB", "2")) * 1024 ** 3)
    except ValueError:
        return 2 * 1024 ** 3


def l1_cache_key(l1_path, raster_crs):
    return _hash({"v": CACHE_VERSION, "kind": "l1_prepared", "l1": _file_sig(l1_path, "l1"),
                  "crs": pyproj.CRS(raster_crs).to_wkt(), "clean": 2})


def _l1_cache_dir():
    """The cache folder (created with a marker); None when it cannot be used."""
    d = L1_CACHE_DIR
    try:
        d.mkdir(parents=True, exist_ok=True)
        m = d / L1_CACHE_MARKER
        if not m.exists():
            if any(True for _ in d.iterdir()):
                return None   # a folder of that name that this tool did not make: leave it alone
            m.write_text("prepared L1 layers of preprocess_classified.py; safe to delete when no run is active\n")
        return d if folder_writable(d) else None
    except OSError:
        return None


def _l1_cache_index(d):
    try:
        return json.loads((d / L1_CACHE_INDEX).read_text()).get("files", {})
    except Exception:
        return {}


def _l1_cache_evict(d, index, keep_name):
    """Delete least recently used prepared layers above the size cap; only files this tool
    created (listed in its index, matching its own name pattern) are ever deleted."""
    cap = l1_cache_cap_bytes()
    names = [n for n in index if L1_CACHE_RE.match(n) and (d / n).exists()]
    total = sum((d / n).stat().st_size for n in names)
    for n in sorted(names, key=lambda n: index[n].get("used", 0)):
        if total <= cap:
            break
        if n == keep_name or time.time() - index[n].get("used", 0) < 3600:
            continue   # in use now, or used within the last hour (perhaps by another run)
        try:
            size = (d / n).stat().st_size
            (d / n).unlink()
            total -= size
            index.pop(n, None)
        except OSError:
            pass       # open in another run (Windows): try again next time
    for n in list(index):
        if not (d / n).exists():
            index.pop(n, None)


def cached_l1_report(l1_path, raster_crs):
    """What was repaired or ignored when this L1 was prepared (from the cache index; {} when unknown)."""
    try:
        d = _l1_cache_dir()
        if d is None:
            return {}
        return _l1_cache_index(d).get(f"l1v_{l1_cache_key(l1_path, raster_crs)}.gpkg", {}).get("report") or {}
    except Exception:
        return {}


def prepare_l1(l1_path, raster_crs, work, workers, cache=True, report=None):
    """Read L1 once, reproject to the raster CRS, make it valid (parallel) and save it spatially
    indexed so that tiles read only their own part. The prepared layer is cached between runs in
    L1_CACHE_DIR (key: L1 file signature + raster CRS), so later report dates with the same L1
    skip this step; without a usable cache folder it goes to <work>/l1_valid.gpkg as before. report
    (dict) receives what was repaired or ignored in L1 (kept in the cache index for later runs)."""
    report = report if report is not None else {}
    dst = Path(work) / "l1_valid.gpkg"
    if dst.exists():
        return dst
    d = _l1_cache_dir() if cache else None
    name = f"l1v_{l1_cache_key(l1_path, raster_crs)}.gpkg" if d else None
    if d is not None and (d / name).exists():
        try:
            index = _l1_cache_index(d)
            index.setdefault(name, {"size": (d / name).stat().st_size, "l1": str(Path(l1_path).resolve())})
            index[name]["used"] = time.time()
            report.update(index[name].get("report") or {})
            _write_json(d / L1_CACHE_INDEX, {"files": index})
        except OSError:
            pass
        return d / name
    w = _valid_l1_wkb(l1_path, raster_crs, workers, report)
    if len(w) == 0:
        raise UserError(f"the L1 layer {Path(l1_path).name} has no usable polygons"
                        + (f" ({report_text(report)})" if report_text(report) else ""))
    if d is not None:
        tmp = d / f"{name[:-5]}.{os.getpid()}.tmp.gpkg"
        try:
            if tmp.exists():
                tmp.unlink()
            pyogrio.raw.write(str(tmp), geometry=w, field_data=[], fields=[], geometry_type="Polygon",
                              crs=pyproj.CRS(raster_crs).to_wkt(), driver="GPKG", layer="l1")
            os.replace(tmp, d / name)
            index = _l1_cache_index(d)
            index[name] = {"size": (d / name).stat().st_size, "used": time.time(), "l1": str(Path(l1_path).resolve()),
                           "report": report}
            _l1_cache_evict(d, index, name)
            _write_json(d / L1_CACHE_INDEX, {"files": index})
            return d / name
        except OSError:
            for q in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
                try:
                    if q.exists():
                        q.unlink()
                except OSError:
                    pass
    tmp = Path(work) / "l1_valid.tmp.gpkg"
    if tmp.exists():
        tmp.unlink()
    pyogrio.raw.write(str(tmp), geometry=w, field_data=[], fields=[], geometry_type="Polygon",
                      crs=pyproj.CRS(raster_crs).to_wkt(), driver="GPKG", layer="l1")
    os.replace(tmp, dst)
    return dst


def clip_pieces(comps, l1, tree):
    """explode(comp intersected with the union of its L1 candidates) for each component.
    l1 must be prepared; tree = STRtree(l1)."""
    if len(comps) == 0 or len(l1) == 0:
        return np.array([], dtype=object)
    ci, li = tree.query(comps, predicate="intersects")
    if len(ci) == 0:
        return np.array([], dtype=object)
    inside = shapely.contains(l1[li], comps[ci])
    done = np.zeros(len(comps), dtype=bool)
    done[ci[inside]] = True
    out = [comps[done]]
    order = np.argsort(ci, kind="stable")
    ci, li = ci[order], li[order]
    cuts, starts = np.unique(ci, return_index=True)
    ends = np.append(starts[1:], len(ci))
    keep = ~done[cuts]
    cuts, starts, ends = cuts[keep], starts[keep], ends[keep]
    if len(cuts):
        out.append(polys_only(_clip_batch(comps, l1, li, cuts, starts, ends)))
    return np.concatenate(out)


def _clip_batch(comps, l1, li, cuts, starts, ends):
    """intersection(comp, union of its L1 candidates) for many components in two array calls
    (fewer interpreter round trips: threads scale better). Same GEOS operations, same results."""
    n = ends - starts
    U = np.empty(len(cuts), dtype=object)
    one = n == 1
    U[one] = l1[li[starts[one]]]
    multi = np.flatnonzero(~one)
    try:
        if len(multi):
            m = int(n[multi].max())
            A = np.full((len(multi), m), None, dtype=object)
            for k, j in enumerate(multi):
                A[k, :n[j]] = l1[li[starts[j]:ends[j]]]
            U[multi] = shapely.union_all(A, axis=1)
        return shapely.intersection(comps[cuts], U)
    except shapely.errors.GEOSException:
        pass
    res = np.empty(len(cuts), dtype=object)
    for k, (c, s, e) in enumerate(zip(cuts, starts, ends)):   # one by one, repairing what GEOS rejects
        cand = l1[li[s:e]]
        try:
            u = cand[0] if len(cand) == 1 else shapely.union_all(cand)
            res[k] = shapely.intersection(comps[c], u)
        except shapely.errors.GEOSException:
            u = shapely.union_all(shapely.make_valid(cand))
            res[k] = shapely.intersection(shapely.make_valid(comps[c]), u)
    return res


def clip_to_l1(comps, l1, workers=1):
    """explode(comps intersected with union(l1)) for disjoint, edge-free components."""
    if len(comps) == 0 or len(l1) == 0:
        return np.array([], dtype=object)
    shapely.prepare(l1)
    tree = STRtree(l1)
    if workers <= 1:
        return clip_pieces(comps, l1, tree)
    order = np.argsort(-shapely.get_num_coordinates(comps))
    comps = comps[order]
    nch = max(1, min(len(comps), workers * 16))
    chunks = [np.arange(i, len(comps), nch) for i in range(nch)]
    with ThreadPoolExecutor(workers) as ex:
        parts = [p for p in ex.map(lambda ix: clip_pieces(comps[ix], l1, tree), chunks) if len(p)]
    return np.concatenate(parts) if parts else np.array([], dtype=object)


# ---------------------------------------------------------------- seam merging
def _on_grid(v, step, origin):
    k = np.rint((v - origin) / step)
    return k * step + origin == v


def grid_protected(l1, tf):
    """L1 vertices with a coordinate on a cell-edge line (never removed as seam vertices)."""
    if len(l1) == 0:
        return np.zeros((0, 2))
    xy = shapely.get_coordinates(l1)
    gx, gy = _on_grid(xy[:, 0], tf.a, tf.c), _on_grid(xy[:, 1], tf.e, tf.f)
    return np.unique(xy[gx | gy], axis=0)


def node_cells(src, p, rows, cols):
    """Filtered cane value of the 4 cells around each grid node (row, col): arrays NW, NE, SW, SE.
    Cells outside the raster are not cane. Each node is read as a tiny window, filtered exactly as
    its tile (one-cell halo), so the answer does not depend on tiles or blocks."""
    n = len(rows)
    out = np.zeros((4, n), dtype=bool)
    H, W = src.height, src.width
    for k, (r, c) in enumerate(zip(np.asarray(rows).tolist(), np.asarray(cols).tolist())):
        r0, c0, r1, c1 = max(r - 1, 0), max(c - 1, 0), min(r + 1, H), min(c + 1, W)
        if r0 >= r1 or c0 >= c1:
            continue
        m = tile_cane(src, (r0, c0, r1, c1), p)
        box = np.zeros((2, 2), dtype=bool)
        box[r0 - (r - 1):r1 - (r - 1), c0 - (c - 1):c1 - (c - 1)] = m
        out[:, k] = box[0, 0], box[0, 1], box[1, 0], box[1, 1]
    return out


def make_cane_at(raster, p):
    """node_cells for one raster, opened once per call (thread-safe: nothing is shared)."""
    def cane_at(rows, cols):
        with rasterio.open(str(raster)) as src:
            return node_cells(src, p, rows, cols)
    return cane_at


def _clean_crossings(polys, tf, protected, cane_at=None):
    """Remove vertices that only exist because pieces of two blocks/tiles were unioned.

    1. Where an L1 edge crosses a seam: such a vertex lies on a cell-edge line in one coordinate
       only and the boundary crosses that line there (neither neighbour lies on it). Genuine
       vertices on a cell-edge line (where the boundary turns onto a cane edge) have a neighbour on
       the line.
    2. (with cane_at) A vertex on a grid node in the middle of a straight horizontal or vertical
       edge (both neighbours on the same line): the union of the pieces on both sides of a seam
       leaves it there. It is removed unless the cane cells around the node turn there (then a run
       without seams keeps it too). Such vertices change no shape, but they change the area after
       projection (a straight lon/lat edge is not straight in UTM), so the result would depend on
       the tile size.
    L1 vertices are always kept. Vectorised over all rings; only polygons that lose a vertex are
    rebuilt."""
    polys = np.asarray(polys, dtype=object)
    if len(polys) == 0:
        return polys
    rings, pidx = shapely.get_rings(polys, return_index=True)
    xy, ridx = shapely.get_coordinates(rings, return_index=True)
    closing = np.r_[ridx[1:] != ridx[:-1], True]          # last (repeated) point of each ring
    xy, ridx = xy[~closing], ridx[~closing]
    n = len(xy)
    if n == 0:
        return polys
    start = np.r_[True, ridx[1:] != ridx[:-1]]
    end = np.r_[ridx[1:] != ridx[:-1], True]
    first = np.maximum.accumulate(np.where(start, np.arange(n), 0))
    last = np.minimum.accumulate(np.where(end, np.arange(n), n)[::-1])[::-1]
    prev = np.where(start, last, np.arange(n) - 1)
    nxt = np.where(end, first, np.arange(n) + 1)
    x, y = xy[:, 0], xy[:, 1]
    gx, gy = _on_grid(x, tf.a, tf.c), _on_grid(y, tf.e, tf.f)
    rm = (gx & ~gy & (x[prev] != x) & (x[nxt] != x)) | (gy & ~gx & (y[prev] != y) & (y[nxt] != y))
    if cane_at is not None:
        vert = gx & gy & (x[prev] == x) & (x[nxt] == x)
        horz = gx & gy & (y[prev] == y) & (y[nxt] == y)
        cand = np.flatnonzero(vert | horz)
        if protected and len(cand):
            cand = cand[np.array([(a, b) not in protected for a, b in xy[cand]], dtype=bool)]
        if len(cand):
            cc = np.rint((x[cand] - tf.c) / tf.a).astype(np.int64)
            rr = np.rint((y[cand] - tf.f) / tf.e).astype(np.int64)
            nw, ne, sw, se = cane_at(rr, cc)
            straight = np.where(vert[cand], (nw == sw) & (ne == se), (nw == ne) & (sw == se))
            rm[cand[straight]] = True
    if rm.any() and protected:
        k = np.flatnonzero(rm)
        rm[k] = np.array([(a, b) not in protected for a, b in xy[k]], dtype=bool)
    if not rm.any():
        return polys
    # rebuild all affected polygons in one vectorised call (a ring keeps all its vertices when
    # fewer than 3 would remain); linear in the number of vertices, also for polygons with many holes
    nring = len(rings)
    ring_ok = np.bincount(ridx[~rm], minlength=nring) >= 3
    aff = np.unique(pidx[ridx[rm]])
    ring_aff = np.zeros(nring, dtype=bool)
    ring_aff[np.isin(pidx, aff)] = True
    sel = ring_aff[ridx] & (~rm | ~ring_ok[ridx])
    rid = ridx[sel]
    uring, rnew = np.unique(rid, return_inverse=True)
    lr = shapely.linearrings(xy[sel], indices=rnew)
    _, pnew = np.unique(pidx[uring], return_inverse=True)
    out = polys.copy()
    out[aff] = shapely.polygons(lr, indices=pnew)
    return out


def _spatial_order(geoms):
    b = shapely.bounds(geoms)
    cx, cy = (b[:, 0] + b[:, 2]) / 2, (b[:, 1] + b[:, 3]) / 2
    n = max(1, int(math.sqrt(len(geoms))))
    span = (np.nanmax(cy) - np.nanmin(cy)) or 1.0
    band = np.floor((cy - np.nanmin(cy)) / span * n)
    return np.lexsort((cx, band))


def merge_pieces(geoms, tf, protected=None, workers=1, budget=None, progress=False, cane_at=None):
    """Union pieces that share a boundary segment of positive length (a shared corner does not
    join them), clean seam-crossing vertices, explode. Returns (pieces, number of merged groups)."""
    n = len(geoms)
    if n < 2:
        return geoms, 0
    protected = set(map(tuple, protected)) if protected is not None and len(protected) else set()
    tree = STRtree(geoms)
    a, b = tree.query(geoms, predicate="intersects")
    m = a < b
    a, b = a[m], b[m]
    if len(a):
        if workers > 1 and len(a) > 20000:
            parts = np.array_split(np.arange(len(a)), workers * 4)
            with ThreadPoolExecutor(workers) as ex:
                share = np.concatenate(list(ex.map(
                    lambda ix: shapely.relate_pattern(geoms[a[ix]], geoms[b[ix]], "****1****"), parts)))
        else:
            share = shapely.relate_pattern(geoms[a], geoms[b], "****1****")
        a, b = a[share], b[share]
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for x, y in zip(a.tolist(), b.tolist()):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx
    roots = np.fromiter((find(i) for i in range(n)), dtype=np.int64, count=n)
    order = np.argsort(roots, kind="stable")
    groups = np.split(order, np.flatnonzero(np.diff(roots[order])) + 1)
    single = [g[0] for g in groups if len(g) == 1]
    multi = sorted((g for g in groups if len(g) > 1), key=len, reverse=True)
    out = [geoms[np.array(single, dtype=np.int64)]]
    if not multi:
        return np.concatenate(out), 0
    total, done, last = len(multi), [0], [time.time()]

    def report():
        if progress and time.time() - last[0] > 10:
            print(f"  merge: {done[0]:,} of {total:,} groups unioned", flush=True)
            last[0] = time.time()

    def union_small(gs):
        res = []
        for g in gs:
            res.append(shapely.union_all(geoms[g]))
            done[0] += 1
        return res

    big = [g for g in multi if len(g) > 256]
    small = [g for g in multi if len(g) <= 256]
    res = []
    for g in big:   # one very large output polygon: parallel tree reduction
        nv = int(shapely.get_num_coordinates(geoms[g]).sum())
        if budget and nv * 16 * 8 > budget:
            raise UserError(f"one output polygon is too large for the memory budget ({nv:,} vertices, "
                            f"about {nv * 128 / 1024 ** 3:.1f} GB needed); raise --max-memory-gb")
        cur = geoms[g][_spatial_order(geoms[g])]
        with ThreadPoolExecutor(max(1, workers)) as ex:
            while len(cur) > 1:
                cur = np.array(list(ex.map(shapely.union_all, [cur[i:i + 16] for i in range(0, len(cur), 16)])),
                               dtype=object)
                report()
        res.append(cur[0])
        done[0] += 1
    if small:
        k = max(1, workers * 8)
        if workers > 1 and len(small) > k:
            with ThreadPoolExecutor(workers) as ex:
                futs = [ex.submit(union_small, small[i::k]) for i in range(k)]
                while True:
                    dn, nd = wait(futs, timeout=2)
                    report()
                    if not nd:
                        break
                for f in futs:
                    res.extend(f.result())
        else:
            res.extend(union_small(small))
    merged = polys_only(np.array(res, dtype=object))
    out.append(_clean_crossings(merged, tf, protected, cane_at))
    return np.concatenate(out), total


# ---------------------------------------------------------------- tile work (runs in workers)
def split4(win):
    r0, c0, r1, c1 = win
    rm, cm = (r0 + r1) // 2, (c0 + c1) // 2
    return [(r0, c0, rm, cm), (r0, cm, rm, c1), (rm, c0, r1, cm), (rm, cm, r1, c1)]


def win_name(win):
    return "{}_{}_{}_{}".format(*win)


def _save(obj, path):
    tmp = str(path) + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump(obj, f, protocol=4)
    os.replace(tmp, path)


def _load(path):
    with open(path, "rb") as f:
        return pickle.load(f)


def pix_to_world(geoms, tf):
    """Apply the (north-up) raster transform to geometries given in global pixel coordinates."""
    if len(geoms) == 0:
        return geoms
    a, c, e, f = tf.a, tf.c, tf.e, tf.f
    return shapely.transform(geoms, lambda xy: np.column_stack((xy[:, 0] * a + c, xy[:, 1] * e + f)))


def tile_cane(src, win, p):
    """Filtered cane mask (bool) of one tile window; the filter reads a one-cell halo."""
    r0, c0, r1, c1 = win
    rows, cols = src.height, src.width
    hr0, hr1, hc0, hc1 = max(r0 - 1, 0), min(r1 + 1, rows), max(c0 - 1, 0), min(c1 + 1, cols)
    data = src.read(1, window=Window(hc0, hr0, hc1 - hc0, hr1 - hr0))
    inner = (slice(r0 - hr0, r1 - hr0), slice(c0 - hc0, c1 - hc0))
    if p["do_filter"]:
        sides = (r0 == 0, r1 == rows, c0 == 0, c1 == cols)
        cane = filter_array(data, src.nodata, p["cane_value"], p["neighbors"], p["threshold"], sides)[inner]
    else:
        cane = data[inner] == p["cane_value"]
        if src.nodata is not None:
            cane &= data[inner] != src.nodata
    return np.ascontiguousarray(cane)


def polygonise(mask, r0, c0):
    """4-connected polygons of a bool block, in GLOBAL pixel coordinates (x = column, y = row).
    The GeoJSON rings from rasterio are turned into polygons in one vectorised call."""
    if not mask.any():
        return np.array([], dtype=object)
    shift = rasterio.Affine.translation(c0, r0)
    rings, ring_poly = [], []
    for k, (g, _) in enumerate(features.shapes(mask.view(np.uint8), mask=mask, transform=shift, connectivity=4)):
        for r in g["coordinates"]:
            rings.append(r)
            ring_poly.append(k)
    lens = np.fromiter((len(r) for r in rings), dtype=np.int64, count=len(rings))
    xy = np.array([c for r in rings for c in r], dtype=float)
    lr = shapely.linearrings(xy, indices=np.repeat(np.arange(len(rings)), lens))
    return np.asarray(shapely.polygons(lr, indices=np.asarray(ring_poly)), dtype=object)


def _block_lines(a, b):
    return [a] + [k for k in range((a // BLOCK + 1) * BLOCK, b, BLOCK)] + [b]


def _touches_sides(geoms, tf, win, rows, cols):
    """Pieces whose bounds reach an inner side (not a raster edge) of the window."""
    r0, c0, r1, c1 = win
    b = shapely.bounds(geoms)
    hit = np.zeros(len(geoms), dtype=bool)
    for col, inner in ((c0, c0 > 0), (c1, c1 < cols)):
        if inner:
            x = col * tf.a + tf.c
            hit |= (b[:, 0] <= x) & (x <= b[:, 2])
    for row, inner in ((r0, r0 > 0), (r1, r1 < rows)):
        if inner:
            y = row * tf.e + tf.f
            hit |= (b[:, 1] <= y) & (y <= b[:, 3])
    return hit


def tile_pieces(src, win, p, l1):
    """(pieces, edge flags, protected vertices) of one tile: filter, block polygonising,
    clipping and in-tile merging across block seams."""
    r0, c0, r1, c1 = win
    rows, cols, tf = src.height, src.width, src.transform
    cane = tile_cane(src, win, p)
    if not cane.any() or len(l1) == 0:
        return np.array([], dtype=object), np.zeros(0, bool), np.zeros((0, 2))
    shapely.prepare(l1)
    tree = STRtree(l1)
    rl, cl = _block_lines(r0, r1), _block_lines(c0, c1)
    fin, seam = [], []
    for i in range(len(rl) - 1):
        for j in range(len(cl) - 1):
            if _STOP.is_set():
                raise TileStopped()
            bw = (rl[i], cl[j], rl[i + 1], cl[j + 1])
            sub = cane[bw[0] - r0:bw[2] - r0, bw[1] - c0:bw[3] - c0]
            if not sub.any():
                continue
            pcs = clip_pieces(pix_to_world(polygonise(np.ascontiguousarray(sub), bw[0], bw[1]), tf), l1, tree)
            if len(pcs) == 0:
                continue
            t = _touches_sides(pcs, tf, bw, rows, cols)
            fin.append(pcs[~t])
            seam.append(pcs[t])
    prot = grid_protected(l1, tf)
    seam = np.concatenate(seam) if seam else np.array([], dtype=object)
    if len(seam):
        seam, _ = merge_pieces(seam, tf, prot, 1, cane_at=lambda rr, cc: node_cells(src, p, rr, cc))
    allp = np.concatenate(fin + [seam]) if (fin or len(seam)) else np.array([], dtype=object)
    if len(allp) == 0:
        return allp, np.zeros(0, bool), prot
    return allp, _touches_sides(allp, tf, win, rows, cols), prot


def _empty_result(tiles_dir, win):
    _save({"wkb": np.array([], dtype=object), "protected": np.zeros((0, 2))},
          Path(tiles_dir) / (win_name(win) + ".edge"))
    _save(np.array([], dtype=object), Path(tiles_dir) / (win_name(win) + ".fin"))


def _worker_init():
    """A worker stops when its parent process is gone (no orphans after the parent is killed):
    Linux asks the kernel for a death signal; every system also gets a 2-second watchdog thread."""
    import threading
    parent = os.getppid()
    if sys.platform.startswith("linux"):
        try:
            import ctypes
            import signal
            ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL)   # PR_SET_PDEATHSIG
        except Exception:
            pass

    def watch():
        while True:
            time.sleep(2)
            if (os.name != "nt" and os.getppid() != parent) or _pid_alive(parent) is False:
                os._exit(1)

    threading.Thread(target=watch, daemon=True).start()


def process_tile(task):
    """Worker entry. Returns ('ok', win) or ('memory', win)."""
    try:
        return _process_tile(task)
    except MemoryError:
        return ("memory", tuple(task["win"]))


def _process_tile(task):
    win = tuple(task["win"])
    p = task["params"]
    fails = os.environ.get("PREPROCESS_TEST_MEMFAIL", "").split(",")
    if win_name(win) in fails:
        raise MemoryError("injected test failure")
    if "kill:" + win_name(win) in fails and multiprocessing.parent_process() is not None:
        os._exit(9)   # test hook: simulate a worker process killed by the system
    tiles_dir = task["tiles_dir"]
    if not task["has_l1"]:
        _empty_result(tiles_dir, win)
        return ("ok", win)
    with rasterio.open(task["raster"]) as src:
        l1 = load_l1(task["l1"], src.crs, bbox=task["l1_bbox"], trusted=True)
        pcs, edge, prot = tile_pieces(src, win, p, l1)
    _save({"wkb": to_wkb(pcs[edge]) if len(pcs) else np.array([], dtype=object), "protected": prot},
          Path(tiles_dir) / (win_name(win) + ".edge"))
    _save(to_wkb(pcs[~edge]) if len(pcs) else np.array([], dtype=object),
          Path(tiles_dir) / (win_name(win) + ".fin"))
    return ("ok", win)


# ---------------------------------------------------------------- planning
def scan_raster(raster, cane_value, nodata):
    """One streaming pass: number of cane cells and the densest BLOCK x BLOCK window (for the probe)."""
    best, total = (-1, 0, 0), 0
    with rasterio.open(str(raster)) as src:
        rows, cols = src.height, src.width
        h = max(BLOCK, min(4096, (64 * 1024 ** 2) // max(cols, 1) // BLOCK * BLOCK))
        for r in range(0, rows, h):
            n = min(h, rows - r)
            a = src.read(1, window=Window(0, r, cols, n)) == cane_value
            for k in range(0, n, BLOCK):
                colsum = a[k:k + BLOCK].sum(axis=0)
                s = np.add.reduceat(colsum, np.arange(0, cols, BLOCK))
                total += int(s.sum())
                j = int(s.argmax())
                if s[j] > best[0]:
                    best = (int(s[j]), r + k, j * BLOCK)
    return total, (best[1], best[2])


def probe(raster, params, at):
    """(peak bytes per cell of the numpy/python part, WKB bytes per cane cell) on the densest block."""
    import tracemalloc
    with rasterio.open(str(raster)) as src:
        r0, c0 = at
        h, w = min(BLOCK * 2, src.height - r0), min(BLOCK * 2, src.width - c0)
        if h < 16 or w < 16:
            r0, c0 = max(0, src.height - BLOCK * 2), max(0, src.width - BLOCK * 2)
            h, w = src.height - r0, src.width - c0
        tracemalloc.start()
        try:
            cane = tile_cane(src, (r0, c0, r0 + h, c0 + w), params)
            comps = polygonise(cane[:BLOCK, :BLOCK].copy(), r0, c0)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        ncane = int(cane[:BLOCK, :BLOCK].sum())
        wkb = sum(len(x) for x in to_wkb(comps)) if len(comps) else 0
    return peak / (h * w), (wkb / ncane if ncane else 60.0)


def task_bytes(t, bpc):
    return WORKER_BASE_BYTES + t * t * bpc


def plan_workers(t, bpc, budget, cores, workers=None):
    """Number of workers whose tile tasks (tile edge t) fit the budget; 0 if even one does not."""
    want = workers or cores
    fit = int(budget // task_bytes(t, bpc))
    return min(want, fit)


def make_plan(rows, cols, budget, cores, bpc, workers=None, tile=None):
    """Workers and tile size so that workers x (base + tile cells x bytes per cell) fits the budget."""
    bpc = max(2.0 * bpc, 24.0)   # margin for vector work (GEOS memory is not traced)
    w = workers or cores
    while w > 1 and w * task_bytes(512, bpc) > budget:
        w -= 1
    if tile is None:
        t = int(math.sqrt(max(budget / w - WORKER_BASE_BYTES, 0) / bpc))
        t = min(max(t, 256), 4096)
        t = min(t, max(128, int(math.sqrt(rows * cols / (3 * w)))))   # a few tiles per worker
    else:
        t = int(tile)
    t = max(2, min(t, max(rows, cols)))
    return w, t, bpc


def plan_bpc(bpc, mode="container"):
    """Bytes per tile cell used for planning: container = 2 x the probe (worst case, a kill costs
    the run); desktop = 1.5 x the probe (a shortage only means paging)."""
    return max(2.0 * bpc, 24.0) if mode == "container" else max(1.5 * bpc, 20.0)


def thread_task_bytes(t, bpc_plan):
    """Memory of one tile task (no process base): L1 part, trees, buffers + tile cells."""
    return TASK_BASE_BYTES + t * t * bpc_plan


def choose_layout(total, fit_procs, cores):
    """(processes, threads) for `total` parallel tasks when at most fit_procs processes fit.
    Processes scale best, so as many as fit; the rest of the tasks run as threads. 1 process
    means: threads inside this program (nothing is spawned)."""
    total = max(1, total)
    best = (1, total)
    hi = max(1, min(total, fit_procs, 30 if os.name == "nt" else total))   # Windows: 2 wait handles each, 64 max
    lo = max(1, hi // 2)
    best_used = -1
    for p in range(hi, lo - 1, -1):
        t = max(1, total // p)
        used = p * t
        if used > best_used:
            best, best_used = (p, t), used
    return (1, total) if best[0] == 1 else best


def main_overhead(explicit_budget, rss_at_budget):
    """Memory of this (main) program that the worker layout must leave room for: with --max-memory-gb
    (a budget for the whole run) all of it; with the automatic budget (free memory, which already
    excludes what this program held then) only what it grew since."""
    now = process_rss() or 0
    return now if explicit_budget else max(0, now - (rss_at_budget or now))


def plan_layout(rows, cols, budget, cores, bpc_plan, workers=None, tile=None, processes=None, threads=None,
                base=None):
    """Tile size and layout. Returns (processes, threads, tile edge, total parallel tasks).

    total tasks = --workers (or processes x threads, or all cores); the tile is as large as allows a
    few tiles per task, shrunk until all tasks fit the budget as threads of ONE process; then as
    many worker processes as the rest of the budget holds (base bytes each, default the measured
    WORKER_BASE_BYTES): P x base + P x T x task <= budget, fewer processes and more threads when
    the budget is small."""
    base = base or WORKER_BASE_BYTES
    if processes and threads:
        total = processes * threads
    elif workers:
        total = workers
    elif processes:
        total = processes * max(1, cores // processes)
    elif threads:
        total = threads * max(1, cores // threads)
    else:
        total = cores
    total = max(1, total)
    if tile is None:
        t = min(4096, max(128, int(math.sqrt(rows * cols / (3 * total)))))   # a few tiles per task
        t = max(t, min(256, max(rows, cols)))
        while t > 256 and total * thread_task_bytes(t, bpc_plan) > budget:
            t = max(256, int(t * 0.9))
        if not (workers or processes or threads):
            while total > 1 and total * thread_task_bytes(t, bpc_plan) > budget:
                total -= 1
    else:
        t = int(tile)
    t = max(2, min(t, max(rows, cols)))
    fit = int(max(0, budget - total * thread_task_bytes(t, bpc_plan)) // base)
    if processes and threads:
        p, th = processes, threads
    elif processes:
        p, th = processes, max(1, -(-total // processes))
    elif threads:
        p, th = max(1, -(-total // threads)), threads
    else:
        p, th = choose_layout(total, fit if fit >= 2 else 1, max(cores, total))
        total = min(total, p * th)
    return p, th, t, total


def tile_windows(rows, cols, t):
    nr, nc = max(1, math.ceil(rows / t)), max(1, math.ceil(cols / t))
    rb = np.linspace(0, rows, nr + 1).astype(int)
    cb = np.linspace(0, cols, nc + 1).astype(int)
    return [(int(rb[i]), int(cb[j]), int(rb[i + 1]), int(cb[j + 1])) for i in range(nr) for j in range(nc)]


def tile_l1_flags(wins, tf, l1_valid):
    """Per tile: (has any L1 polygon, bbox padded by one cell) using bounds only (no geometry read)."""
    _, _, lb = l1_info(l1_valid)
    lb = lb[np.isfinite(lb).all(axis=1)]
    boxes, flags = [], []
    tree = STRtree(shapely.box(lb[:, 0], lb[:, 1], lb[:, 2], lb[:, 3])) if len(lb) else None
    for r0, c0, r1, c1 in wins:
        xa, xb = tf.c + (c0 - 1) * tf.a, tf.c + (c1 + 1) * tf.a
        ya, yb = tf.f + (r0 - 1) * tf.e, tf.f + (r1 + 1) * tf.e
        bx = (min(xa, xb), min(ya, yb), max(xa, xb), max(ya, yb))
        boxes.append(bx)
        flags.append(bool(tree is not None and len(tree.query(shapely.box(*bx)))))
    return flags, boxes


# ---------------------------------------------------------------- work folder
def _is_ours(name):
    return name in ("manifest.json", "manifest.json.tmp", "merged.bin", "merged.bin.tmp", "l1_valid.gpkg",
                    "l1_valid.gpkg-wal", "l1_valid.gpkg-shm", "l1_valid.tmp.gpkg", "l1_valid.tmp.gpkg-wal",
                    "l1_valid.tmp.gpkg-shm")


def open_work(work):
    """Create the work folder with its marker; refuse a folder of that name that is not ours."""
    work = Path(work)
    if work.exists():
        if not (work / MARKER).exists():
            raise UserError(f"the folder {work} already exists and was not made by this script. "
                            f"Rename or move it, or choose another output name.")
        return
    work.mkdir(parents=True)
    (work / MARKER).write_text("work folder of preprocess_classified.py; safe to delete when no run is active\n")


def clean_work(work, keep_lock=True, remove_folder=False):
    """Delete only this script's own files inside a marked work folder."""
    work = Path(work)
    if not (work / MARKER).exists():
        return
    td = work / "tiles"
    if td.is_dir():
        for f in list(td.iterdir()):
            if f.is_file() and _TILE_RE.match(f.name):
                f.unlink()
        try:
            td.rmdir()
        except OSError:
            pass
    for f in list(work.iterdir()):
        if f.is_file() and _is_ours(f.name):
            f.unlink()
    if remove_folder:
        if not keep_lock and (work / LOCK).exists():
            (work / LOCK).unlink()
        try:
            left = [f.name for f in work.iterdir()]
        except OSError:
            left = ["?"]
        if left == [MARKER]:
            (work / MARKER).unlink()
            try:
                work.rmdir()
            except OSError:
                pass


def acquire_lock(work):
    p = Path(work) / LOCK
    info = {"pid": os.getpid(), "started": time.strftime("%Y-%m-%d %H:%M:%S")}
    for _ in range(3):
        try:
            fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as f:
                json.dump(info, f)
            return p
        except FileExistsError:
            try:
                old = json.loads(p.read_text())
            except Exception:
                old = {}
            if _pid_alive(old.get("pid")) is not False and old.get("pid") != os.getpid():
                raise UserError(f"another run (process {old.get('pid')}, started {old.get('started', '?')}) "
                                f"is using {work}. Wait for it to finish or choose another output name.")
            try:
                p.unlink()   # stale lock of a dead run: take it over
            except OSError:
                pass
    raise UserError(f"could not lock the work folder {work}")


def release_lock(lock):
    try:
        if lock and Path(lock).exists() and json.loads(Path(lock).read_text()).get("pid") == os.getpid():
            Path(lock).unlink()
    except Exception:
        pass


_FOREIGN_LOCKS = []   # locks this run holds on work folders of earlier runs whose saved work it reads


def release_foreign_locks():
    while _FOREIGN_LOCKS:
        release_lock(_FOREIGN_LOCKS.pop())


def find_reusable_work(work, key, grid, candidates, key_name="tiles_key", needed=("merged.bin",), marker=None,
                       check=None):
    """A work folder of an earlier run whose saved stage results can be read in place.

    candidates: work folders to look at (the previous run's, or the one this folder reused before).
    A folder counts when it carries the marker, its manifest has the same stage key (same inputs and
    stage settings) and the same grid, and all `needed` files exist; `check(folder, manifest)` may add
    a test. Its lock is taken (released at the end of this run) so that no other run changes it while
    it is read; a folder locked by a live run is skipped. Nothing in that folder is ever changed or
    deleted. Returns the folder or None (then the run works normally)."""
    marker = marker or MARKER
    seen = {Path(work).resolve()}
    todo = [c for c in candidates if c]
    hops = 0
    while todo and hops < 20:
        c = todo.pop(0)
        hops += 1
        try:
            c = Path(c).resolve()
            if c in seen or not (c / marker).exists():
                continue
            seen.add(c)
            man = json.loads((c / "manifest.json").read_text())
            if man.get(key_name) != key or (grid is not None and man.get("grid") != grid):
                continue
            if not all((c / f).exists() for f in needed) or (check and not check(c, man)):
                # a folder that itself read its results from another one: follow that link
                todo[:0] = [man[k] for k in ("reused_from", "overlay_from") if man.get(k)]
                continue
            lock = acquire_lock(c)
        except (OSError, ValueError, UserError):
            continue
        _FOREIGN_LOCKS.append(lock)
        return c
    return None


def _write_json(path, obj):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1))
    os.replace(tmp, path)


# ---------------------------------------------------------------- stages
def _tile_state(tiles_dir, w):
    """'done', 'split' (some sub-tiles saved) or 'todo'."""
    if (tiles_dir / (win_name(w) + ".fin")).exists():
        return "done"
    subs = [(tiles_dir / (win_name(s) + ".fin")).exists() for s in split4(w)]
    if all(subs):
        return "done"
    return "split" if any(subs) else "todo"


def tiles_progress(work):
    """(done, total) tiles of a work folder, read from its manifest."""
    try:
        man = json.loads((Path(work) / "manifest.json").read_text())
        wins = tile_windows(man["grid"][0], man["grid"][1], man["tile"])
        if (Path(work) / "merged.bin").exists():
            return len(wins), len(wins)
        td = Path(work) / "tiles"
        return sum(1 for w in wins if _tile_state(td, w) == "done"), len(wins)
    except Exception:
        return 0, 0


def _make_pool(n):
    return ProcessPoolExecutor(max_workers=n, mp_context=multiprocessing.get_context("spawn"),
                               initializer=_worker_init)


def _kill_pool(ex):
    """Stop a pool now: cancel queued tasks and terminate its worker processes."""
    if ex is None:
        return
    procs = list(getattr(ex, "_processes", {}).values())
    try:
        ex.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass
    for pr in procs:
        try:
            if pr.is_alive():
                pr.terminate()
        except Exception:
            pass
    for pr in procs:
        try:
            pr.join(5)
        except Exception:
            pass


def _run_task_safe(task):
    """process_tile (or the task's own "fn", a module-level function that other tools of this folder pass)
    for a pool thread: ('done', result), ('stopped', None) or ('error', text)."""
    try:
        fn = task.get("fn") if isinstance(task, dict) else None
        return ("done", (fn or process_tile)(task))
    except TileStopped:
        return ("stopped", None)
    except BaseException as e:   # noqa: B902 - reported to the main program, which stops the run
        return ("error", f"{type(e).__name__}: {e}\n{traceback.format_exc()}")


def _pool_worker(conn, threads):
    """Worker process: `threads` threads take tile tasks from this process's own pipe and send
    results back on it. Each thread opens its own raster and L1 part (nothing is shared)."""
    import queue
    _worker_init()
    tasks, lock = queue.Queue(), threading.Lock()

    def run():
        while True:
            item = tasks.get()
            if item is None:
                return
            tid, task = item
            kind, val = _run_task_safe(task)
            with lock:
                conn.send((kind, tid, val))

    ths = [threading.Thread(target=run, daemon=True) for _ in range(threads)]
    for t in ths:
        t.start()
    try:
        while True:
            try:
                item = conn.recv()
            except (EOFError, OSError):
                os._exit(0)
            if item is None:
                break
            tasks.put(item)
    except KeyboardInterrupt:
        os._exit(1)
    for _ in ths:
        tasks.put(None)
    for t in ths:
        t.join()


class TilePool:
    """Runs tile tasks on `procs` spawned worker processes x `threads` threads each.

    procs <= 1: the threads run inside this program (nothing spawned, no pool to rebuild; a tile
    that fails with MemoryError is split as usual and finished tiles are already on disk).
    procs > 1: every process has its own pipe, so the tasks lost with a dead process are known
    exactly and only that process is restarted."""

    def __init__(self, procs, threads, inproc=None):
        import queue
        self.inproc = procs <= 1 if inproc is None else bool(inproc)
        self.threads = max(1, int(threads))
        self.nproc = 1 if self.inproc else int(procs)
        self.capacity = self.nproc * self.threads
        self._tid = 0
        if self.inproc:
            _STOP.clear()
            self._q, self._res = queue.Queue(), queue.Queue()
            self._ths = [threading.Thread(target=self._thread_main, daemon=True) for _ in range(self.threads)]
            for t in self._ths:
                t.start()
        else:
            self._ctx = multiprocessing.get_context("spawn")
            self._slots = [self._start() for _ in range(self.nproc)]

    # ---- in-process threads
    def _thread_main(self):
        while True:
            item = self._q.get()
            if item is None:
                return
            tid, task = item
            kind, val = _run_task_safe(task)
            self._res.put((kind, tid, val))

    # ---- worker processes
    def _start(self):
        a, b = self._ctx.Pipe(duplex=True)
        pr = self._ctx.Process(target=_pool_worker, args=(b, self.threads), daemon=True)
        pr.start()
        b.close()
        return {"proc": pr, "conn": a, "live": set()}

    def submit(self, task):
        """Queue one task; returns its id (a dead process is reported by wait())."""
        self._tid += 1
        tid = self._tid
        if self.inproc:
            self._q.put((tid, task))
            return tid
        slot = min(self._slots, key=lambda s: len(s["live"]))
        slot["live"].add(tid)
        try:
            slot["conn"].send((tid, task))
        except (OSError, EOFError):
            pass   # the process is dead; wait() reports the task as lost
        return tid

    def wait(self, timeout=2.0):
        """Events since the last call: ('done', tid, status), ('error', tid, text) and
        ('lost', [tids], None) for the tasks of a worker process that died."""
        import queue
        from multiprocessing.connection import wait as mp_wait
        ev = []
        if self.inproc:
            try:
                ev.append(self._res.get(timeout=timeout))
                while True:
                    ev.append(self._res.get_nowait())
            except queue.Empty:
                pass
            return ev
        objs = [s["conn"] for s in self._slots] + [s["proc"].sentinel for s in self._slots]
        ready = mp_wait(objs, timeout)
        for k, s in enumerate(self._slots):
            if s["conn"] in ready or s["proc"].sentinel in ready:
                try:
                    while s["conn"].poll():
                        kind, tid, val = s["conn"].recv()
                        s["live"].discard(tid)
                        ev.append((kind, tid, val))
                except (EOFError, OSError):
                    pass
            if not s["proc"].is_alive():
                lost = sorted(s["live"])
                self._close_slot(s)
                self._slots[k] = self._start()
                if lost:
                    ev.append(("lost", lost, None))
        return ev

    def _close_slot(self, s):
        try:
            s["conn"].close()
        except Exception:
            pass
        pr = s["proc"]
        try:
            if pr.is_alive():
                pr.terminate()
            pr.join(5)
        except Exception:
            pass

    def close(self):
        """Stop everything now (also used on Ctrl+C and errors)."""
        if self.inproc:
            _STOP.set()
            for _ in self._ths:
                self._q.put(None)
            end = time.time() + 5
            for t in self._ths:
                t.join(max(0.0, end - time.time()))
            _STOP.clear()
            return
        for s in self._slots:
            try:
                s["conn"].send(None)
            except Exception:
                pass
        for s in self._slots:
            self._close_slot(s)
        self._slots = []


def _hold_new_task(mode, buffer, held_since):
    """True while a new task should wait for free memory. container: below the buffer (strict);
    desktop: below DESKTOP_HOLD_BYTES and never longer than DESKTOP_HOLD_MAX_S."""
    avail = available_memory()
    if mode == "desktop":
        return avail < DESKTOP_HOLD_BYTES and (held_since is None or time.time() - held_since < DESKTOP_HOLD_MAX_S)
    return avail < buffer


def run_tiles(raster, l1_path, work, params, wins, flags, boxes, workers, buffer, presplit=False,
              on_progress=None, procs=None, threads=None, mode="container"):
    """Process all tiles (resumable). workers = parallel tasks; procs x threads = the layout
    (default: `workers` processes x 1 thread, the old behaviour)."""
    tiles_dir = Path(work) / "tiles"
    tiles_dir.mkdir(parents=True, exist_ok=True)
    if procs is None:
        procs, threads = workers, 1
    threads = threads or 1
    fin = lambda w: (tiles_dir / (win_name(w) + ".fin")).exists()  # noqa: E731
    state = [_tile_state(tiles_dir, w) for w in wins]
    todo = [i for i, s in enumerate(state) if s == "todo"]
    split = [i for i, s in enumerate(state) if s == "split"]
    ndone = state.count("done")
    if ndone:
        print(f"resuming: {ndone} of {len(wins)} tiles already done", flush=True)
    if split:
        print(f"resuming: {len(split)} tile(s) were being processed as sub-tiles; their missing sub-tiles follow")
    if on_progress:
        on_progress(f"{ndone} of {len(wins)} tiles done")
    mk = lambda i, w=None: {"raster": str(raster), "l1": str(l1_path), "win": w or wins[i],  # noqa: E731
                            "params": params, "tiles_dir": str(tiles_dir),
                            "has_l1": flags[i], "l1_bbox": boxes[i]}
    if presplit and todo:
        print("a saved tile does not fit today's memory budget: remaining tiles are processed as 4 sub-tiles")
        split += todo
        todo = []
    queue_, fails = deque(todo), {}
    total, finished, last = len(todo), 0, time.time()
    workers = max(1, min(workers, procs * threads))
    pool = None
    try:
        if queue_:
            pool = TilePool(procs, threads)
        live, held, held_since = {}, 0.0, None
        while queue_ or live:
            while queue_ and len(live) < workers:
                if live and _hold_new_task(mode, buffer, held_since):   # others need the RAM: wait
                    held_since = held_since or time.time()
                    if time.time() - held > 30:
                        print("  waiting: free memory is low", flush=True)
                        held = time.time()
                    break
                held_since = None
                i = queue_.popleft()
                live[pool.submit(mk(i))] = i
            for kind, tid, val in pool.wait(2.0):
                if kind == "lost":
                    lost_i = [live.pop(t) for t in tid if t in live]
                    lost = [i for i in lost_i if not fin(wins[i])]
                    finished += len(lost_i) - len(lost)
                    for i in lost:
                        fails[i] = fails.get(i, 0) + 1
                    again = [i for i in lost if fails[i] < 2]
                    split += [i for i in lost if fails[i] >= 2]
                    queue_ = deque(again + list(queue_))
                    print(f"a worker process died (probably out of memory): it is restarted; "
                          f"{len(again)} tile(s) are retried", flush=True)
                    continue
                if tid not in live:
                    continue
                i = live.pop(tid)
                if kind == "error":
                    raise RuntimeError(f"tile {win_name(wins[i])} failed: {val}")
                if kind == "stopped":
                    raise KeyboardInterrupt
                if val[0] == "memory":
                    split.append(i)
                finished += 1
            if time.time() - last > 10:
                print(f"  tiles {finished}/{total}", flush=True)
                if on_progress:
                    on_progress(f"{ndone + finished} of {len(wins)} tiles done")
                last = time.time()
    finally:
        if pool is not None:
            pool.close()
    subs = [(i, s) for i in dict.fromkeys(split) for s in split4(wins[i]) if not fin(s)]
    if subs:
        print(f"{len(subs)} sub-tile(s) of {len(set(split))} tile(s) are processed one at a time")
        pool = None
        try:
            for i, s in subs:
                st = "memory"
                for _attempt in (1, 2):
                    if pool is None:   # one worker process, or this program when no processes are used
                        pool = TilePool(1, 1, inproc=procs <= 1)
                    pool.submit(mk(i, s))
                    res = []
                    while not res:
                        res = pool.wait(2.0)
                    kind, _, val = res[0]
                    if kind == "done":
                        st = val[0]
                        break
                    if kind == "error":
                        raise RuntimeError(f"tile {win_name(s)} failed: {val}")
                    if kind == "stopped":
                        raise KeyboardInterrupt
                    pool.close()   # lost: the process died; one more try in a fresh process
                    pool = None
                if st != "ok":
                    raise UserError(f"tile {win_name(s)} does not fit in memory even when split; free memory, "
                                    "raise --max-memory-gb, or close other programs")
        finally:
            if pool is not None:
                pool.close()
    return tiles_dir


def tidy_tiles(tiles_dir, wins, rows, cols):
    """Drop stale files (sub-tiles of a finished parent, .edge without .fin) and check coverage."""
    tiles_dir = Path(tiles_dir)
    for w in wins:
        if (tiles_dir / (win_name(w) + ".fin")).exists():
            for s in split4(w):
                for ext in (".fin", ".edge"):
                    q = tiles_dir / (win_name(s) + ext)
                    if q.exists():
                        q.unlink()
    for f in list(tiles_dir.iterdir()):
        if f.name.endswith(".edge") and not (tiles_dir / (f.name[:-5] + ".fin")).exists():
            f.unlink()
    cover = 0
    for f in tiles_dir.iterdir():
        if f.name.endswith(".fin"):
            r0, c0, r1, c1 = (int(x) for x in f.name[:-4].split("_"))
            cover += (r1 - r0) * (c1 - c0)
    if cover != rows * cols:
        raise RuntimeError("tile files do not cover the raster exactly; delete the work folder and rerun")


def merge_stage(work, tiles_dir, tf, workers, budget, raster=None, params=None):
    """Union the seam pieces of neighbouring tiles (shared edges only) and save everything
    (tile pieces + merged seam pieces, raster CRS) in ONE file merged.bin (a stream of chunks)."""
    work, tiles_dir = Path(work), Path(tiles_dir)
    names = sorted(f.name[:-4] for f in tiles_dir.iterdir() if f.name.endswith(".fin"))
    nbytes = sum((tiles_dir / (n + ".edge")).stat().st_size for n in names if (tiles_dir / (n + ".edge")).exists())
    if budget and nbytes * 6 > budget:
        raise UserError(f"the pieces on tile seams need about {nbytes * 6 / 1024 ** 3:.1f} GB to merge, more than "
                        f"the memory budget; raise --max-memory-gb or use larger tiles (--tile-size)")
    parts, prot = [], []
    for n in names:
        q = tiles_dir / (n + ".edge")
        if q.exists():
            d = _load(q)
            if len(d["wkb"]):
                parts.append(shapely.from_wkb(d["wkb"]))
            if len(d["protected"]):
                prot.append(d["protected"])
    G = np.concatenate(parts) if parts else np.array([], dtype=object)
    P = np.unique(np.concatenate(prot), axis=0) if prot else np.zeros((0, 2))
    stats = {"n_raw": int(len(G)), "n_groups": 0}
    seam = np.array([], dtype=object)
    if len(G):
        cane_at = make_cane_at(raster, params) if raster is not None else None
        seam, stats["n_groups"] = merge_pieces(G, tf, P, workers, budget, progress=True, cane_at=cane_at)
    del G, parts
    tmp = work / "merged.bin.tmp"
    n = 0
    with open(tmp, "wb") as fh:
        for nm in names:
            w = _load(tiles_dir / (nm + ".fin"))
            if len(w):
                pickle.dump(w, fh, protocol=4)
                n += len(w)
        if len(seam):
            pickle.dump(to_wkb(seam), fh, protocol=4)
            n += len(seam)
    os.replace(tmp, work / "merged.bin")
    stats["n_pieces"] = n
    return stats


def _iter_chunks(path):
    with open(path, "rb") as fh:
        while True:
            try:
                yield pickle.load(fh)
            except EOFError:
                return


def install_output(tmp, out):
    """Swap the finished temporary output set in place of `out` (all old sidecars are removed).
    Returns (path written, note). If the swap fails, the old output is restored and the new
    result is kept under another name."""
    out, exts = Path(out), out_exts(out)
    new = {e: q for e, q in file_set(tmp, exts).items() if q.exists()}
    old = {e: q for e, q in file_set(out, exts).items() if q.exists()}
    dst = file_set(out, exts)
    aside = file_set(out.with_name(out.stem + "__old" + out.suffix), exts)
    for q in aside.values():
        if q.exists():
            q.unlink()

    def keep_alt(err):
        alt = out.with_name(out.stem + "_new_" + time.strftime("%Y%m%d_%H%M%S") + out.suffix)
        altp = file_set(alt, exts)
        for e, q in new.items():
            os.replace(q, altp[e])
        return alt, (f"could not replace {out.name} ({err}); is it open in ArcMap/QGIS? "
                     f"The old file is unchanged and the new result is saved as {alt}")

    moved = []
    try:
        for e, q in old.items():
            os.replace(q, aside[e])
            moved.append(e)
    except OSError as err:
        for e in moved:
            os.replace(aside[e], old[e])
        return keep_alt(err)
    placed = []
    try:
        for e, q in new.items():
            os.replace(q, dst[e])
            placed.append(e)
    except OSError as err:
        for e in placed:
            os.replace(dst[e], new[e])
        for e in moved:
            os.replace(aside[e], old[e])
        return keep_alt(err)
    note = None
    for e in moved:
        try:
            aside[e].unlink()
        except OSError:
            note = f"old copy {aside[e].name} could not be deleted (in use?); delete it later"
    return out, note


def final_stage(merged, out, raster_crs, utm_crs, min_acres, keep_slivers, workers, batch=200_000, unit=None):
    """Areas, noise and sliver removal, projection to EPSG:4326 and writing (temporary name, then swap).
    Slivers are compared in acres (min_acres is the physical size); the area field is written in the unit."""
    out = Path(out)
    unit = get_unit(unit)
    tmp = out.with_name(out.stem + "__partial" + out.suffix)
    for x in file_set(tmp, out_exts(out)).values():
        if x.exists():
            x.unlink()
    driver = driver_of(out)
    layer = None if driver == "ESRI Shapefile" else out.stem
    buf_w, buf_a, n_buf = [], [], 0
    st = {"count": 0, "acres": 0.0, "acres_exact": 0.0, "acres_all": 0.0, "sn": 0, "sa": 0.0, "noise": 0, "na": 0.0,
          "first": True, "unit": unit.key, "col_sum": 0.0, "repairs": {}}

    def flush():
        nonlocal buf_w, buf_a, n_buf
        if not n_buf:
            return
        w, a = np.concatenate(buf_w), np.concatenate(buf_a)
        write_vector(tmp, w, [np.full(len(w), PREDICTED, dtype="int32"), a], ["predicted", unit.field],
                     driver, layer, not st["first"])
        st["first"] = False
        buf_w, buf_a, n_buf = [], [], 0

    def process(wkbs):
        nonlocal buf_w, buf_a, n_buf
        g = shapely.from_wkb(np.concatenate(wkbs))
        m2 = area_m2(g, raster_crs, utm_crs, workers)
        st["acres_all"] += float(m2.sum()) / ACRE_M2       # every piece, before any removal (exact)
        ok = m2 >= NOISE_M2
        st["noise"] += int((~ok).sum())
        st["na"] += float(m2[~ok].sum()) / ACRE_M2
        g, area, m2 = g[ok], m2[ok] / ACRE_M2, m2[ok]
        if not keep_slivers:
            keep = area >= min_acres
            st["sn"] += int((~keep).sum())
            st["sa"] += float(area[~keep].sum())
            g, area, m2 = g[keep], area[keep], m2[keep]
        if len(g) == 0:
            return
        if pyproj.CRS(raster_crs) != pyproj.CRS(4326):
            g = project_geoms(g, raster_crs, 4326, workers)
        g2, rows, changed = gate(g, None, st["repairs"])    # the features exactly as they are written
        if changed.any() or len(g2) != len(g):
            area, m2 = area[rows], m2[rows]
            if changed.any():
                m2[changed] = area_m2(g2[changed], "EPSG:4326", utm_crs, workers)
                area[changed] = m2[changed] / ACRE_M2
            ok = m2 >= NOISE_M2
            if not ok.all():
                st["repairs"]["collapsed"] = st["repairs"].get("collapsed", 0) + int((~ok).sum())
            g, area, m2 = g2[ok], area[ok], m2[ok]
            if len(g) == 0:
                return
        a_ac = np.round(area, 3)
        a = a_ac if unit.key == "acres" else np.round(m2 / unit.m2, unit.decimals)
        buf_w.append(to_wkb(g))
        buf_a.append(a)
        n_buf += len(a)
        st["count"] += len(a)
        st["acres"] += float(a_ac.sum())                    # = sum of an area_ac column (acres, 3 decimals)
        st["col_sum"] += float(a.sum())                     # = sum of the written area column
        st["acres_exact"] += float(area.sum())
        if n_buf >= batch:
            flush()

    group, n_group, b_group = [], 0, 0
    for wkb in _iter_chunks(merged):   # groups of at most 100,000 pieces or about 32 MB of WKB (memory)
        for k in range(0, len(wkb), 20_000):
            part = wkb[k:k + 20_000]
            group.append(part)
            n_group += len(part)
            b_group += sum(map(len, part))
            if n_group >= 100_000 or b_group >= FINAL_GROUP_BYTES:
                process(group)
                group, n_group, b_group = [], 0, 0
    if group:
        process(group)
    flush()
    if st["first"]:   # nothing to write: create an empty layer
        write_vector(tmp, np.array([], dtype=object), [np.zeros(0, dtype="int32"), np.zeros(0)],
                     ["predicted", unit.field], driver, layer, False)
    finish_vector(tmp, driver)
    n_read = feature_count(tmp)
    if n_read != st["count"]:
        raise RuntimeError(f"output check failed: wrote {st['count']} polygons but the file has {n_read}")
    written, note = install_output(tmp, out)
    st["geom"] = geometry_check(written)               # the saved file, read back
    return st, written, note


# ---------------------------------------------------------------- last run / cache keys
STATE_ENV = "HARVEST_TOOLS_STATE_DIR"
_NOTED = set()


def _record_places(script_file, kind, home_dir):
    """(record next to the script, home-folder fallback record, script folder)."""
    script_dir = Path(script_file).resolve().parent
    env = os.environ.get(STATE_ENV)
    base = Path(env) if env else script_dir / ".harvest_tools"
    return base / f"last_run_{kind}.json", Path.home() / home_dir / "last_run.json", script_dir


def last_run_home(script_file):
    """The folder this script's last-run memory belongs to (shown on the previous-run screen)."""
    env = os.environ.get(STATE_ENV)
    return Path(env) if env else Path(script_file).resolve().parent


def _hide_folder(d):
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(str(d), 2)     # 2 = hidden
        except Exception:
            pass


def save_record(script_file, kind, home_dir, info):
    """Save a last-run record next to the script, found out by really writing it (a network drive may say
    "writable" and still refuse). On an OSError the old home-folder record is used instead, with one note
    per run. The record holds the script folder, so a fallback record is only ever read back by a script of
    the same folder. Never fails the run."""
    primary, home, script_dir = _record_places(script_file, kind, home_dir)
    info = dict(info, script_dir=str(script_dir))
    try:
        new = not primary.parent.exists()
        primary.parent.mkdir(parents=True, exist_ok=True)
        if new:
            _hide_folder(primary.parent)
        _write_json(primary, info)
        return
    except OSError:
        pass
    except Exception:
        return
    if kind not in _NOTED:
        _NOTED.add(kind)
        print(f"NOTE: cannot save the last-run record next to the script ({primary.parent.parent}); "
              f"using {home} instead.")
    try:
        home.parent.mkdir(parents=True, exist_ok=True)
        _write_json(home, info)
    except Exception:
        pass


def load_record(script_file, kind, home_dir):
    """The record next to the script; else the home fallback record when this same script folder wrote it
    (old records without a script folder never match); else None."""
    primary, home, script_dir = _record_places(script_file, kind, home_dir)
    try:
        return json.loads(primary.read_text())
    except Exception:
        pass
    try:
        d = json.loads(home.read_text())
        return d if d.get("script_dir") == str(script_dir) else None
    except Exception:
        return None


def last_run_file():
    """The record next to the script (where it is saved when that works)."""
    return _record_places(__file__, "preprocess", ".preprocess_classified")[0]


def save_last_run(info):
    save_record(__file__, "preprocess", ".preprocess_classified", info)


def load_last_run():
    d = load_record(__file__, "preprocess", ".preprocess_classified")
    return d if d and d.get("raster") and d.get("l1") and d.get("out") else None


def _hash(obj):
    """Short hash of a cache key. A unit of acres is left out, so keys written before units existed
    (always acres) still match."""
    if isinstance(obj, dict) and obj.get("unit") == ACRES.key:
        obj = {k: v for k, v in obj.items() if k != "unit"}
    return hashlib.sha1(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:16]


def _sig_paths(path, kind):
    p = Path(path)
    if kind == "l1":
        return list(file_set(p, L1_SIG.get(p.suffix.lower(), (p.suffix,))).values())
    extra = [Path(str(p) + s) for s in (".aux.xml", ".ovr", ".msk")]
    extra += list(file_set(p, (".tfw", ".tifw", ".wld", ".prj")).values())
    return [p] + extra


def _content_sample(path, n=16, size=4096):
    """Hash of 16 small pieces spread over the file (the whole file when it is small): a file that
    was replaced by other content with the same size and modification time is still noticed.
    Costs 16 short reads, also for very large rasters."""
    h = hashlib.sha1()
    try:
        total = os.path.getsize(path)
        with open(path, "rb") as f:
            if total <= n * size:
                h.update(f.read())
            else:
                for k in range(n):
                    f.seek((total - size) * k // (n - 1))
                    h.update(f.read(size))
    except OSError:
        return ""
    return h.hexdigest()[:16]


def _file_sig(path, kind="raster"):
    """Signature of an input file and its sidecars: [name, size, modification time, content sample]."""
    files = []
    for q in _sig_paths(path, kind):
        if q.exists():
            st = os.stat(q)
            files.append([q.name, st.st_size, st.st_mtime_ns, _content_sample(q)])
    sig = {"path": str(Path(path).resolve()), "files": files}
    if layer_kw(path):
        sig["layer"] = layer_kw(path)["layer"]
    return sig


def stage_keys(raster, l1_path, out, utm_crs, a):
    """Cache keys. tiles+merge depend on inputs, cane value and majority setting only; final on
    projection, sliver setting and output."""
    t = {"version": CACHE_VERSION, "raster": _file_sig(raster, "raster"), "l1": _file_sig(l1_path, "l1"),
         "cane_value": a["cane_value"], "majority": bool(a["do_filter"]), "block": BLOCK}
    if a["do_filter"]:
        t.update(neighbors=a["neighbors"], threshold=a["threshold"])
    f = {"version": CACHE_VERSION, "epsg": utm_crs.to_epsg() or utm_crs.to_wkt(),
         "slivers": not a["keep_slivers"], "min_acres": None if a["keep_slivers"] else a["min_acres"],
         "out": str(Path(out).resolve()), "unit": get_unit(a.get("unit")).key}
    return t, f


FINAL_KEY_NAMES = {"epsg": "projection", "slivers": "sliver removal", "min_acres": "sliver size", "out": "output file",
                   "unit": "unit"}
KEY_DEFAULTS = {"unit": ACRES.key}      # settings missing in keys of older runs
SIZE_KEYS = ("min_acres", "sliver_acres")   # sizes held in acres, shown in the run's unit
TILE_KEY_NAMES = {"raster": "raster file", "l1": "L1 file", "cane_value": "cane value", "majority": "majority filter",
                  "neighbors": "number of neighbours", "threshold": "majority threshold", "block": "block size"}


def _is_file_sig(v):
    return (isinstance(v, dict) and isinstance(v.get("path"), str) and isinstance(v.get("files"), list)
            and all(isinstance(f, list) and len(f) == 4 for f in v["files"]))


def _file_state(f, precise=False):
    """'12.3 MB, saved 2026-11-20 10:00:01' from a signature entry [name, size, mtime_ns]
    (precise: with milliseconds, for two states that differ by less than a second)."""
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(f[2] // 10 ** 9))
    if precise:
        when += f".{f[2] // 10 ** 6 % 1000:03d}"
    return f"{human_size(f[1])}, saved {when}"


def _setting_text(v):
    if isinstance(v, bool):
        return "yes" if v else "no"
    return "not set" if v is None else str(v)


def explain_changes(old, new, names=None):
    """Why saved work does not match: (made_by_older_version, [plain sentences]).

    Older version: the cache version differs or a file signature has an old format. A changed input
    file is named with its old and new size and time; a changed setting as 'name (old -> new)'. A
    setting missing in an older key takes its default (unit: acres); sizes are shown in the new unit."""
    names = names or TILE_KEY_NAMES
    if not isinstance(old, dict) or old.get("version") != new.get("version"):
        return True, []
    file_keys = [k for k in new if _is_file_sig(new[k])]
    if any(k in old and not _is_file_sig(old[k]) for k in file_keys):
        return True, []
    out, settings = [], []
    for k in file_keys:
        o, n = old.get(k), new[k]
        if o == n or o is None:
            continue
        label = names.get(k, k)
        if o["path"] != n["path"]:
            out.append(f"A different {label} is used (before: {o['path']}; now: {n['path']}).")
            continue
        if o.get("layer") != n.get("layer"):
            out.append(f"A different layer of the {label} is used (before: {o.get('layer') or 'the first'}; "
                       f"now: {n.get('layer') or 'the first'}).")
            continue
        of, nf = {f[0]: f for f in o["files"]}, {f[0]: f for f in n["files"]}
        main = Path(n["path"]).name
        added = [m for m in sorted(nf) if m not in of]
        removed = [m for m in sorted(of) if m not in nf]
        changed = [m for m in sorted(set(of) & set(nf)) if of[m] != nf[m]]
        if len(added) + len(removed) + len(changed) == 0:
            continue
        if len(added) + len(removed) + len(changed) > 1:      # a shapefile: one line for the layer
            parts = [f"{what} {' '.join(Path(m).suffix or m for m in names_)}" for what, names_ in
                     (("changed", changed), ("added", added), ("removed", removed)) if names_]
            line = f"The {label} {main} was modified since the last run ({'; '.join(parts)}"
            if main in changed and of[main][:3] != nf[main][:3]:
                line += f"; {main} before: {_file_state(of[main])}, now: {_file_state(nf[main])}"
            out.append(line + ").")
            continue
        for name in added:
            out.append(f"The file {name} ({label}) was added since the last run.")
        for name in removed:
            out.append(f"The file {name} ({label}) was removed since the last run.")
        for name in changed:
            if of[name][:3] == nf[name][:3]:
                out.append(f"The file {name} ({label}) has other content than at the last run (same size and "
                           f"time: {_file_state(nf[name])}).")
            else:
                exact = _file_state(of[name]) == _file_state(nf[name])
                out.append(f"The file {name} ({label}) was modified since the last run "
                           f"(before: {_file_state(of[name], exact)}; now: {_file_state(nf[name], exact)}).")
    unit = unit_of_record(new)
    for k in sorted(set(old) | set(new)):
        o, v = old.get(k, KEY_DEFAULTS.get(k)), new.get(k, KEY_DEFAULTS.get(k))
        if k in file_keys or k == "version" or o == v:
            continue
        if k in SIZE_KEYS and unit.key != ACRES.key:
            o, v = (size_in_unit(x, unit) if isinstance(x, (int, float)) and not isinstance(x, bool) else x
                    for x in (o, v))
        settings.append(f"{names.get(k, k)} ({_setting_text(o)} -> {_setting_text(v)})")
    if settings:
        out.append("Setting changed: " + ", ".join(settings) + ".")
    return False, out


def describe_changes(old, new, names=None):
    """explain_changes as one line of text."""
    older, why = explain_changes(old, new, names)
    return "This work folder was made by an older version of the tool." if older else " ".join(why)


def resolve_area_crs(utm, raster_crs, bounds, allow_non_utm=False, warn=True):
    """Area CRS from a CRS / text / None (auto). Raises UserError for unusable choices."""
    if utm is None or (isinstance(utm, str) and not utm.strip()):
        opts = utm_options(raster_crs, bounds)
        if not opts:
            raise UserError("the data cross the 180 degree meridian, so no UTM zone can be suggested; "
                            "give the projection explicitly (--utm EPSG:xxxxx)")
        return opts[0][0]
    if isinstance(utm, str):
        clat = data_centre(raster_crs, bounds)[1]
        crs, hint = parse_crs_msg(utm, clat >= 0, None)
        if crs is None:
            raise UserError(f"projection '{utm}': {hint}")
    else:
        crs = pyproj.CRS(utm)
        if not crs.is_projected or any(abs(ax.unit_conversion_factor - 1.0) > 1e-9 for ax in crs.axis_info):
            raise UserError(f"projection {crs.name} is not a projected CRS in metres; choose a UTM zone")
    is_utm, far = area_crs_notes(crs, raster_crs, bounds)
    if not is_utm and not allow_non_utm:
        raise UserError(f"{crs.name} (EPSG:{crs.to_epsg()}) is not a UTM zone. {NON_UTM_NOTE} "
                        f"Add --allow-non-utm to use it anyway.")
    if far and warn:
        print(f"WARNING: {far}")
    return crs


def estimate_disk(cane_cells, wkb_per_cane, l1_path):
    l1b = sum(q.stat().st_size for q in _sig_paths(l1_path, "l1") if q.exists())
    return int(cane_cells * wkb_per_cane * 3.5 + 2 * l1b + 50 * 1024 ** 2)


# ---------------------------------------------------------------- run
def run(raster, l1_path, out, min_acres=0.2, utm=None, cane_value=1, workers=None,
        keep_slivers=False, do_filter=True, neighbors=4, threshold="majority",
        max_memory_gb=None, memory_buffer_gb=None, tile_size=None, clean=False, confirm_clean=None,
        allow_non_utm=False, assume_l1_crs=False, processes=None, threads=None, memory_mode=None,
        confirm_format=None, summary_line=False, unit=None, l1_layer=None):
    """min_acres is the sliver size in acres (the physical size); unit = unit of the area field and of
    everything shown (default acres)."""
    raster, l1_path, out = Path(raster).resolve(), Path(l1_path).resolve(), Path(out).resolve()
    unit = get_unit(unit)
    if l1_path.suffix.lower() not in VECTOR_EXTS:
        raise UserError(f"the L1 layer must be a vector file. {FORMAT_HINT}")
    l1_layer = choose_layer(l1_path, l1_layer, "L1", "--l1-layer")
    start_clock()
    if out.suffix.lower() not in VECTOR_EXTS:
        raise UserError("the output must end with .shp, .gpkg, .geojson or .json")
    if not out.parent.is_dir():
        raise UserError(f"output folder does not exist: {out.parent}")
    if not folder_writable(out.parent):
        raise UserError(f"cannot write in the output folder {out.parent}")
    try:
        with rasterio.open(str(raster)) as src:
            rows, cols, crs, bounds, tf = src.height, src.width, src.crs, src.bounds, src.transform
            nodata = src.nodata
    except rasterio.errors.RasterioIOError as e:
        raise UserError(f"cannot read the raster {raster}: {e}")
    if crs is None:
        raise UserError("the raster has no coordinate system")
    if tf.b != 0 or tf.d != 0:
        raise UserError("rotated rasters are not supported")
    try:
        l1crs = l1_info(l1_path)[0]
    except Exception as e:
        raise UserError(f"cannot read the L1 layer {l1_path}: {e}")
    if not l1crs and not assume_l1_crs:
        raise UserError("the L1 layer has no coordinate system (.prj missing). Add one, or add --assume-l1-crs "
                        "to treat it as being in the raster's coordinate system")
    utm_crs = resolve_area_crs(utm, crs, bounds, allow_non_utm)
    bad = locked_outputs(out)
    if bad:
        raise UserError(f"the output {out.name} is open in another program ({', '.join(bad)}). "
                        f"Close it in ArcMap/QGIS and run again, or choose another output name.")
    ans = {"cane_value": cane_value, "do_filter": bool(do_filter), "neighbors": neighbors,
           "threshold": threshold, "keep_slivers": bool(keep_slivers), "min_acres": min_acres, "unit": unit.key}
    params = {k: v for k, v in ans.items() if k != "unit"}
    work = out.parent / (out.stem + "_work")
    prev_last = load_last_run()     # read before it is replaced: its work folder may be reusable
    last = {"raster": str(raster), "l1": str(l1_path), "out": str(out),
            "utm": f"EPSG:{utm_crs.to_epsg()}" if utm_crs.to_epsg() else utm_crs.to_wkt(),
            "work": str(work), "data_work": str(work), "answers": ans, "started": time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "running", "progress": "starting", "pid": os.getpid(),
            "allow_non_utm": bool(allow_non_utm), "assume_l1_crs": bool(assume_l1_crs), "l1_layer": l1_layer}
    save_last_run(last)

    def progress(msg, status="running"):
        last.update(progress=msg, status=status, updated=time.strftime("%Y-%m-%d %H:%M:%S"))
        save_last_run(last)

    def set_out(new):
        last["out"] = str(new)
        save_last_run(last)

    def set_data_work(folder):      # the folder that really holds the saved stage results
        last["data_work"] = str(folder)
        save_last_run(last)

    open_work(work)
    lock = acquire_lock(work)
    try:
        result = _run_locked(raster, l1_path, out, work, rows, cols, crs, tf, nodata, utm_crs, ans, params,
                             workers, max_memory_gb, memory_buffer_gb, tile_size, clean, confirm_clean,
                             progress, processes, threads, memory_mode, confirm_format,
                             set_out, summary_line, prev_last, set_data_work)
    except KeyboardInterrupt:
        release_lock(lock)
        release_foreign_locks()
        progress("stopped by the user", "interrupted")
        raise
    except BaseException as e:
        release_lock(lock)
        release_foreign_locks()
        progress(f"{type(e).__name__}: {e}"[:300], "failed")
        raise
    release_lock(lock)
    release_foreign_locks()
    if clean and (result is None or result.get("written") == result.get("out", str(out))):
        clean_work(work, keep_lock=False, remove_folder=True)
    if last.get("status") == "running":
        progress("finished", "finished")
    return result


def _run_locked(raster, l1_path, out, work, rows, cols, crs, tf, nodata, utm_crs, ans, params, workers,
                max_memory_gb, memory_buffer_gb, tile_size, clean, confirm_clean, progress,
                processes=None, threads=None, memory_mode_arg=None, confirm_format=None, set_out=None,
                summary_line=False, prev_last=None, set_data_work=None):
    tkey_items, fkey_items = stage_keys(raster, l1_path, out, utm_crs, ans)
    tkey, fkey = _hash(tkey_items), _hash(fkey_items)
    man_path, merged = work / "manifest.json", work / "merged.bin"
    old = None
    if man_path.exists() or any(_is_ours(f.name) or f.name == "tiles" for f in work.iterdir()):
        try:
            old = json.loads(man_path.read_text())
        except Exception:
            old = None
        if old is None or old.get("tiles_key") != tkey or old.get("grid") != [rows, cols, list(tf)[:6]]:
            if old is None:
                print("The saved work in this folder cannot be read; its tiles cannot be reused.")
            else:
                older, why = explain_changes(old.get("tiles_items"), tkey_items)
                if older:
                    print("This work folder was made by an older version of the tool; its saved tiles cannot be reused.")
                elif why:
                    print(" ".join(why) + " Tiles must be recomputed.")
                else:
                    print("The raster size or position differs from the saved tiles. Tiles must be recomputed.")
            if confirm_clean and not asked(confirm_clean, work):
                raise SystemExit("Stopped: the old work folder was kept.")
            clean_work(work)
            old = None
    grid = [rows, cols, list(tf)[:6]]
    src_work = None
    if not merged.exists() and not (work / "tiles").is_dir():
        src_work = find_reusable_work(work, tkey, grid, [old.get("reused_from") if old else None,
                                                         (prev_last or {}).get("data_work"),
                                                         (prev_last or {}).get("work")])
        if src_work is not None:
            if set_data_work:
                set_data_work(src_work)
            merged = src_work / "merged.bin"
            print(f"Reusing saved work from {src_work}")
    if tile_size is not None and old and old["tile"] != int(tile_size) and src_work is None:
        print(f"note: the saved tile plan ({old['tile']} cells) is kept for this resume; --tile-size {tile_size} "
              f"is ignored (remove the work folder to change it)")

    cores, (avail, total) = n_cpus(), memory_status()
    mode = memory_mode_arg if memory_mode_arg in ("desktop", "container") else memory_mode()
    buf = memory_buffer_bytes(total, memory_buffer_gb, avail)
    budget = int(max_memory_gb * 1024 ** 3) if max_memory_gb else max(avail - buf, 256 * 1024 ** 2)
    rss_at_budget = process_rss()
    cane_cells, densest = scan_raster(raster, ans["cane_value"], nodata)
    bpc, wkb_per_cane = probe(raster, params, densest)
    need = 0 if merged.exists() else estimate_disk(cane_cells, wkb_per_cane, l1_path)
    need += int(cane_cells * wkb_per_cane * (1.2 * GEOJSON_BYTES_RATIO if is_geojson(out) else 1.2))
    free = shutil.disk_usage(out.parent).free
    if need > free:
        e = DiskSpaceError(f"not enough disk space next to the output: about {need / 1024 ** 3:.2f} GB needed, "
                           f"{free / 1024 ** 3:.2f} GB free in {out.parent}")
        e.needed, e.free = need, free
        raise e
    bpc_plan = plan_bpc(bpc, mode)
    presplit = False
    t = old["tile"] if old else tile_size   # a resume keeps the saved tile plan; the layout is re-planned
    l1_valid, dt_l1 = None, 0.0
    if not merged.exists():   # prepared first: what this program holds afterwards is counted in the plan
        t_l1 = time.time()
        l1_rep = {}
        l1_valid = prepare_l1(l1_path, crs, work, cores, report=l1_rep)
        if input_note("L1", l1_rep):
            print(f"NOTE: {input_note('L1', l1_rep)}")
        dt_l1 = time.time() - t_l1
        _T0[0] += dt_l1       # shown in the "L1 prepared" line, not in the plan line
    elif input_note("L1", cached_l1_report(l1_path, crs)):   # saved work: the note of the prepared L1
        print(f"NOTE: {input_note('L1', cached_l1_report(l1_path, crs))}")
    room = max(budget - main_overhead(bool(max_memory_gb), rss_at_budget), 128 * 1024 ** 2)
    nproc, nthr, t, w = plan_layout(rows, cols, room, cores, bpc_plan, workers, t, processes, threads)
    if old and thread_task_bytes(t, bpc_plan) > budget:
        nproc, nthr, w, presplit = 1, 1, 1, True
    wins = tile_windows(rows, cols, t)
    lay = f"{nproc} processes x {nthr} thread{'s' if nthr > 1 else ''}" if nproc > 1 else \
        f"1 process x {nthr} thread{'s' if nthr > 1 else ''}"
    print(f"Machine: {cores} cores, {avail / 1024 ** 3:.1f} GB memory free -> using up to {budget / 1024 ** 3:.1f} GB "
          f"(memory mode: {mode}, {buf / 1024 ** 3:.1f} GB kept free)")
    print(f"Plan: {lay} = {plural(w, 'tile')} at a time, {plural(len(wins), 'tile')} of {t} x {t} cells "
          f"(planned {bpc_plan:.0f} bytes per cell){' [saved tile plan]' if old else ''}")
    notes = []
    if nproc < min(w, cores) and not (processes or threads):
        notes.append(memory_note(bool(max_memory_gb), nproc))
    est = int(cane_cells * wkb_per_cane)
    if out.suffix.lower() == ".shp" and est > SHP_WARN_BYTES:
        notes.append(f"The result may be about {est / 1024 ** 3:.1f} GB; a shapefile cannot be larger than 2 GB. "
                     f"Use a GeoPackage (.gpkg) output for very large areas.")
        if confirm_format and asked(confirm_format, est):
            out = out.with_suffix(".gpkg")
            notes[-1] = f"The result may be about {est / 1024 ** 3:.1f} GB: saving as GeoPackage {out.name} instead."
            fkey_items["out"] = str(out)
            fkey = _hash(fkey_items)
            if set_out:
                set_out(out)
    if is_geojson(out) and est * GEOJSON_BYTES_RATIO > GEOJSON_NOTE_BYTES:
        notes.append(f"The GeoJSON result may be about {est * GEOJSON_BYTES_RATIO / 1024 ** 3:.1f} GB of text; a "
                     f"GeoPackage (.gpkg) is about {GEOJSON_BYTES_RATIO:.0f} times smaller and opens faster.")
    for n in notes:
        print(f"NOTE: {n}")
    shown = len(notes)   # notes printed here are not repeated above the result block
    manifest = {"tiles_key": tkey, "tiles_items": tkey_items, "final_key": old.get("final_key") if old else None,
                "final_items": fkey_items, "grid": grid, "tile": t, "nodata": nodata}
    if old and old.get("final_stats"):
        manifest["final_stats"] = old["final_stats"]
    if src_work is not None:
        manifest["reused_from"] = str(src_work)
    if not old or manifest.get("reused_from") != old.get("reused_from"):
        _write_json(man_path, manifest)

    times = {"plan": tick(f"plan ({rows}x{cols} cells, {cane_cells:,} cane cells)")}
    out_ok = out.exists()
    if old and old.get("final_key") == fkey and out_ok and merged.exists() and old.get("final_stats") \
            and old.get("final_out_sig") == _file_sig(out, "l1"):   # the file is still the one written then
        progress("finished (up to date)", "finished")
        s = old["final_stats"]
        res = _result(s, out)
        print_run_result(s, ans, utm_crs, out, work, clean, merged, notes[shown:],
                         f"The output {out.name} was already up to date for these settings; nothing was written again.",
                         times, run_seconds(), summary_line, src_work)
        return res
    if not merged.exists():
        _T0[0] -= dt_l1
        flags, boxes = tile_l1_flags(wins, tf, l1_valid)
        times["prepare L1"] = tick(f"L1 prepared (reprojected, made valid): {sum(flags)} of {len(wins)} tiles touch L1")
        tiles_dir = run_tiles(raster, l1_valid, work, params, wins, flags, boxes, max(1, min(w, len(wins))),
                              buf, presplit, progress, nproc, nthr, mode)
        times["tiles"] = tick("tiles (filter, polygonise, clip)")
        tidy_tiles(tiles_dir, wins, rows, cols)
        progress("merging seams")
        st = merge_stage(work, tiles_dir, tf, w, budget, raster, params)
        times["merge"] = tick(f"merge seams: {st['n_raw']:,} seam pieces, {st['n_groups']:,} merged groups, "
                              f"{st['n_pieces']:,} pieces saved")
        clean_work_tiles(tiles_dir)
        for nm in ("l1_valid.gpkg", "l1_valid.gpkg-wal", "l1_valid.gpkg-shm"):
            if (work / nm).exists():
                (work / nm).unlink()
    elif src_work is None:
        why = []
        if old and old.get("final_items") and old.get("final_key") != fkey:
            why = explain_changes(old["final_items"], fkey_items, FINAL_KEY_NAMES)[1]
        if why:
            print(" ".join(why) + " Only the last step runs again.")
        else:
            print("reusing the saved tile and merge results (only the final stage runs)")
    progress("writing output")
    s, written, note = final_stage(merged, out, crs, utm_crs, ans["min_acres"], ans["keep_slivers"], w,
                                   unit=ans.get("unit"))
    times["write"] = tick(f"final stage: areas, slivers, write {written}")
    s = {k: v for k, v in s.items() if k != "first"}
    if note:
        notes.append(note)
    if written == out:
        manifest["final_key"] = fkey
        manifest["final_stats"] = s
        manifest["final_out_sig"] = _file_sig(out, "l1")
        _write_json(man_path, manifest)
    print_run_result(s, ans, utm_crs, written, work, clean, merged, notes[shown:], None, times, run_seconds(),
                     summary_line, src_work)
    return _result(s, written, out)


def _result(s, written, out=None):
    unit = unit_of_record(s)
    return {"polygons": s["count"], "acres": s["acres"], "acres_exact": s.get("acres_exact", s["acres"]),
            "geometry_ok": geometry_ok(s["geom"]) if s.get("geom") else True, "geometry": s.get("geom"),
            "repairs": s.get("repairs", {}),
            "slivers": s["sn"], "sliver_acres": s["sa"],
            "noise": s["noise"], "written": str(written), "out": str(out or written), "unit": unit.key,
            "area": from_acres(s.get("acres_exact", s["acres"]), unit), "area_column_sum": s.get("col_sum", s["acres"])}


def majority_text(ans):
    """'no' / 'yes, 4 neighbours' / 'yes, 8 neighbours, half rule'."""
    if not ans.get("do_filter"):
        return "no"
    return f"yes, {ans.get('neighbors')} neighbours" + (", half rule" if ans.get("threshold") == "half" else "")


def time_line(times, total_s, names):
    """'Time     : 7.3 s  (plan 0.5 | ...)'. The stages are consecutive, so they add up to the total
    (each part is rounded on its own)."""
    parts = " | ".join(f"{lab} {times[k]:.1f}" for k, lab in names if times and k in times)
    return f"Time     : {total_s:.1f} s" + (f"  ({parts})" if parts else "")


def print_run_result(s, ans, utm_crs, out, work, clean, merged, notes=(), headline=None, times=None, total_s=None,
                     summary_line=False, reused_from=None):
    """End-of-run summary: NOTE lines (only those not printed yet), one plain headline (if any), the
    RESULT block and, with --summary-line, the old machine-readable SUMMARY line.

    All acres rows are exact sums, each rounded once for display; the area_ac column of the file holds
    values rounded per polygon, and its sum is given in a footnote when it differs by more than 0.001."""
    for n in notes or ():
        print(f"NOTE: {n}")
    if headline:
        print(headline)
    n = lambda v: f"{v:,}"          # noqa: E731
    unit = unit_of_record(s if "unit" in s else ans)
    ac = lambda v: area_text(from_acres(v, unit), unit)          # noqa: E731  (acres in, the unit out)
    count, acres, sn, sa, noise, na = s["count"], s["acres"], s["sn"], s["sa"], s["noise"], s.get("na", 0.0)
    exact = s.get("acres_exact", acres)                       # older saved stats: sum of the rounded values
    col_sum = s.get("col_sum", acres)                         # older saved stats: acres
    total_in = s.get("acres_all", exact + sa + na)
    head = [("Remaining cane polygons written :", n(count)),
            ("Total area                      :", ac(exact) + " " + unit.text)]
    w = max(len(v) for _, v in head) - len(" " + unit.text)
    head = [(f"{k} {v.rjust(w) if k.startswith('Remaining') else v.rjust(w + len(unit.text) + 1)}",) for k, v in head]
    pct = lambda v: pct_text(v, total_in).rjust(9)           # noqa: E731
    table = [("How the result was reached", "Polygons", unit.header, "% of area"),
             ("  Cane pieces inside L1", n(count + sn + noise), ac(total_in), pct(total_in))]
    if not ans["keep_slivers"]:
        table.append((f"  Removed: slivers below {size_number(ans['min_acres'], unit)} {unit.word}", n(sn), ac(sa),
                      pct(sa)))
    table.append(("  Removed: numerical noise", n(noise), ac(na), pct(na)))
    table.append(("  Written to the output", n(count), ac(exact), pct(exact)))
    if abs(col_sum - from_acres(exact, unit)) > 10 ** -unit.decimals:
        table.extend(wrap_rows(f"Note: adding up the {unit.field} column of the file gives "
                               f"{area_text(col_sum, unit)} {unit.text} (each polygon is rounded to "
                               f"{unit.decimals} decimal{'s' if unit.decimals != 1 else ''})."))
    sl = "kept (not removed)" if ans["keep_slivers"] else f"removed below {size_in_unit(ans['min_acres'], unit)}"
    settings = [(f"  Majority filter : {majority_text(ans)}",), (f"  Projection      : {crs_label(utm_crs)}",),
                (f"  Slivers         : {sl}",), (f"  Unit            : {unit.text} (field {unit.field})",)]
    files = [(f"  Output : {out}",)]
    size = Path(merged).stat().st_size if Path(merged).exists() else 0
    if reused_from is not None:
        files.append((f"  Cache  : read from {reused_from} ({human_size(size)}); that folder was not changed.",))
        if clean:
            files.append((f"           --clean removed only {work}",))
    elif clean:
        files.append(("  Cache  : removed after the run (--clean)",))
    else:
        files.extend(cache_rows("Cache ", work, human_size(size), "A rerun that only changes the sliver size, the "
                                "projection, the unit or the output name reuses it and takes seconds. Safe to delete."))
    sections = [("", head), ("", table)]
    if s.get("geom"):
        sections.append((f"Geometry check (the saved file was read back, {s['geom'].get('seconds', 0):.1f} s)",
                         geometry_rows(s["geom"], s.get("repairs"))))
    sections += [("Settings used", settings), ("Files", files)]
    if total_s is not None:
        names = (("plan", "plan"), ("prepare L1", "prepare L1"), ("tiles", "tiles"), ("merge", "merge"),
                 ("write", "write"))
        sections.append(("", [(time_line(times, total_s, names),)]))
    print_result_block("RESULT", sections)
    if summary_line:
        if unit.key == ACRES.key:
            print(f"SUMMARY polygons={count:,} total_acres={acres:,.3f} "
                  f"slivers_removed={sn:,} slivers_acres={sa:,.3f} noise_pieces_removed={noise:,} "
                  f"before_removal: polygons={count + sn:,} acres={acres + sa:,.3f}", flush=True)
        else:
            k, f, d = unit.word.replace(" ", "_"), (lambda v: from_acres(v, unit)), unit.decimals
            print(f"SUMMARY polygons={count:,} total_{k}={col_sum:,.{d}f} "
                  f"slivers_removed={sn:,} slivers_{k}={f(sa):,.{d}f} noise_pieces_removed={noise:,} "
                  f"before_removal: polygons={count + sn:,} {k}={col_sum + f(sa):,.{d}f}", flush=True)


def clean_work_tiles(tiles_dir):
    tiles_dir = Path(tiles_dir)
    if not tiles_dir.is_dir():
        return
    for f in list(tiles_dir.iterdir()):
        if f.is_file() and _TILE_RE.match(f.name):
            f.unlink()
    try:
        tiles_dir.rmdir()
    except OSError:
        pass


# ---------------------------------------------------------------- interactive mode
def _input(prompt):
    try:
        return input(prompt)
    except EOFError:
        raise SystemExit("\nERROR: input ended before all questions were answered.")


def ask_text(question, default=None, check=None):
    """Ask until check(answer) returns (value, None); check returns (None, hint) to re-ask."""
    while True:
        tail = f" [{default}]" if default not in (None, "") else ""
        ans = _input(f"{question}{tail}: ").strip()
        if ans == "":
            if default in (None, ""):
                print("   Please type an answer.")
                continue
            ans = str(default)
        if check is None:
            return ans
        val, hint = check(ans)
        if hint is None:
            return val
        print(f"   {hint}")


def ask_yes_no(question, default=True):
    while True:
        ans = _input(f"{question} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        if ans == "":
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("   Please answer y or n.")


class EditAnswers(Exception):
    """Raised at 'Start?' when the user wants to change an answer; carries the answers as new defaults."""

    def __init__(self, answers):
        super().__init__("edit")
        self.answers = answers


START_QUESTION = "Start? [Y = start, e = edit the answers, c = cancel]: "
EDIT_NOTE = "\nEdit: every question shows your last answer in [ ]; press Enter to keep it, or type a new one.\n"


def ask_start():
    """The last question: 'start', 'edit' (go through the questions again, the answers as defaults) or
    'cancel'. 'n' means edit: a user who says no wants to correct something."""
    while True:
        ans = _input(START_QUESTION).strip().lower()
        if ans in ("", "y", "yes", "s", "start"):
            return "start"
        if ans in ("e", "edit", "n", "no"):
            return "edit"
        if ans in ("c", "cancel", "q", "quit", "exit"):
            return "cancel"
        print("   Type y to start, e to edit the answers or c to cancel.")


def ask_all(questions, defaults):
    """Run a question function until the user starts or cancels; 'edit' repeats it with the answers given."""
    while True:
        try:
            return questions(defaults)
        except EditAnswers as e:
            defaults = dict(defaults, **e.answers)
            print(EDIT_NOTE)


def _file_check(exts, what):
    def check(ans):
        p = Path(clean_path(ans))
        if not p.is_file():
            return None, f"File not found: {p}. Paste the full path of the {what}."
        if p.suffix.lower() not in exts:
            if tuple(exts) == VECTOR_EXTS:
                return None, f"The {what} must be a vector file. {FORMAT_HINT}"
            return None, f"The {what} must be one of: {', '.join(exts)}."
        if tuple(exts) == VECTOR_EXTS:
            try:
                list_layers(p)
            except UserError as e:
                return None, str(e)[0].upper() + str(e)[1:]
        return p, None
    return check


def parse_acres(a):
    try:
        v = float(a.strip().replace(",", "."))
    except ValueError:
        return None, "Type a number, e.g. 0.2."
    if not math.isfinite(v) or v < 0:
        return None, "Type a number of 0 or more, e.g. 0.2."
    return v, None


WIN_BAD_CHARS = set('<>:"/\\|?*')
WIN_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}
OUT_FORMATS = {"1": ".shp", "shp": ".shp", ".shp": ".shp", "2": ".gpkg", "gpkg": ".gpkg", ".gpkg": ".gpkg",
               "3": ".geojson", "geojson": ".geojson", ".geojson": ".geojson", "json": ".geojson", ".json": ".geojson"}
FORMAT_NUMBER = {".shp": "1", ".gpkg": "2", ".geojson": "3", ".json": "3"}


def output_name_check(a):
    """Output name without extension; refuses what Windows does not allow (re-asked with a hint)."""
    a = a.strip()
    if Path(a).suffix.lower() in VECTOR_EXTS:
        a = a[:-len(Path(a).suffix)].rstrip()
    if not a:
        return None, "Type a name."
    bad = sorted({c for c in a if c in WIN_BAD_CHARS or ord(c) < 32})
    if bad:
        return None, f"A file name cannot contain {' '.join(bad)} (Windows rule). Spaces, - _ ( ) [ ] are fine."
    if a[-1] in ". ":
        return None, "A file name cannot end with a dot or a space (Windows rule)."
    if a.split(".")[0].strip().lower() in WIN_RESERVED:
        return None, f"{a} is a reserved name on Windows (CON, NUL, COM1, ...). Choose another name."
    return a, None


def _format_check(a):
    v = OUT_FORMATS.get(a.strip().lower())
    return (v, None) if v else (None, "Type 1 (shapefile), 2 (GeoPackage) or 3 (GeoJSON).")


def _folder_check(raw):
    """(folder, stem or None, ext or None) or (None, hint). A full .shp/.gpkg path is split."""
    p = Path(clean_path(raw))
    stem = ext = None
    if p.suffix.lower() in VECTOR_EXTS:
        stem, ext, p = p.stem, OUT_FORMATS[p.suffix.lower()], p.parent
    if p.exists() and not p.is_dir():
        return None, f"{p} is a file, not a folder."
    if not p.exists():
        if not ask_yes_no(f"   The folder {p} does not exist. Create it?", True):
            return None, "Type another folder."
        try:
            p.mkdir(parents=True)
        except OSError as e:
            return None, f"Cannot create it: {e}"
    if not folder_writable(p):
        return None, f"Cannot write in {p}. Choose another folder."
    free = shutil.disk_usage(p).free
    if free < 200 * 1024 ** 2:
        return None, f"Only {free / 1024 ** 2:.0f} MB free in {p}. Choose a folder on a disk with more space."
    return (p, stem, ext), None


def ask_output(default_dir, default_stem, defaults, made_by=None, label="3) Output folder"):
    """Ask the output as three questions - folder, name (no extension), format - then confirm an
    existing file. Returns the output Path.

    default_dir / default_stem: defaults when there is no previous answer. defaults (dict, may be
    empty): "out" = a previous output path whose folder, name and format become the defaults;
    "folder" / "fmt" (".shp" / ".gpkg") override single defaults. A full .shp / .gpkg path pasted
    as the folder is accepted: its folder is used and its name and format become the next defaults.
    On "no" to replacing an existing file the name question is asked again. made_by(path): True when an
    existing file comes from an earlier run of the calling tool (default: this tool)."""
    defaults = defaults or {}
    made_by = made_by or made_by_this_tool
    prev = Path(defaults["out"]) if defaults.get("out") else None
    folder_def = str(defaults.get("folder") or (prev.parent if prev else default_dir))
    stem_def = prev.stem if prev else default_stem
    fmt = defaults.get("fmt") or (OUT_FORMATS[prev.suffix.lower()] if prev and prev.suffix.lower() in VECTOR_EXTS
                                  else ".shp")
    folder, stem, ext = ask_text(label, folder_def, _folder_check)
    stem_def, fmt = stem or stem_def, ext or fmt
    while True:
        stem = ask_text("   Output name", stem_def, output_name_check)
        print("   A shapefile cannot be larger than 2 GB; choose 2 for very large areas.")
        fmt = ask_text("   Save as:  1) Shapefile (.shp)   2) GeoPackage (.gpkg)   3) GeoJSON (.geojson) ",
                       FORMAT_NUMBER.get(fmt, "1"), _format_check)
        out = folder / (stem + fmt)
        stem_def = stem
        if out.exists():
            if made_by(out):
                if not ask_yes_no(f"   {out.name} was made by an earlier run of this tool. Replace it?", True):
                    continue
            elif not ask_yes_no(f"   {out.name} already exists. Overwrite?", False):
                continue
        return out


def output_defaults(prev_out, prev_input, new_input):
    """Defaults for the output questions: the previous folder, name and format when the input file
    (raster / FINAL) is the same as in the previous run; with another input only the previous format
    (folder and name then come from the new input)."""
    if not prev_out:
        return {}
    try:
        same = bool(prev_input) and Path(clean_path(str(prev_input))).resolve() == Path(new_input).resolve()
    except (OSError, ValueError):
        same = False
    return {"out": prev_out} if same else {"fmt": Path(prev_out).suffix.lower()}


def ask_format_switch(est_bytes):
    """Question-mode answer to 'the shapefile may get too large': True = save as GeoPackage."""
    return ask_yes_no(f"   The result may be about {est_bytes / 1024 ** 3:.1f} GB, close to the 2 GB shapefile limit. "
                      f"Save as GeoPackage (.gpkg) instead?", True)


def out_check(ans):
    p = Path(clean_path(ans))
    if p.suffix.lower() not in VECTOR_EXTS:
        return None, "The output must end with .shp, .gpkg, .geojson or .json."
    if not p.parent.is_dir():
        return None, f"Folder does not exist: {p.parent}"
    if not folder_writable(p.parent):
        return None, f"Cannot write in {p.parent}. Choose another folder."
    if p.exists():
        if made_by_this_tool(p):
            if not ask_yes_no(f"  {p.name} was made by an earlier run of this tool. Replace it?", True):
                return None, "Choose another output path."
        elif not ask_yes_no(f"  {p.name} already exists. Overwrite?", False):
            return None, "Choose another output path."
    return p, None


def made_by_this_tool(p):
    """True when an existing output comes from an earlier run of this tool (its marked work folder
    is next to it, or it is the last run's output)."""
    p = Path(p)
    if (p.parent / (p.stem + "_work") / MARKER).exists():
        return True
    last = load_last_run()
    try:
        return bool(last) and Path(last["out"]).resolve() == p.resolve()
    except Exception:
        return False


def say_earlier_choice(dflt):
    """When the default is the previous answer (not list number 1), say so on its own line, so that
    the default and the 'suggested' mark of the list do not look contradictory."""
    if dflt and dflt != "1":
        crs = parse_crs_msg(dflt, True, None)[0]
        if crs is not None:
            print(f"   Enter keeps your earlier choice: {crs_label(crs).split(' / ', 1)[-1].replace('UTM zone ', 'UTM ')}")


LARGE_ACRES = 5.0


def acres_check(warning):
    """parse_acres plus a confirmation above LARGE_ACRES; warning(v) is the question text."""
    def check(a):
        v, hint = parse_acres(a)
        if hint is None and v > LARGE_ACRES and not ask_yes_no(warning(v), False):
            return None, "Type a smaller size."
        return v, hint
    return check


def default_area_crs(text, raster_crs, bounds, opts):
    """Previous answer as default only if it fits this raster (a detected zone or within one zone
    of the data centre); otherwise the detected suggestion [1]."""
    fallback = "1" if opts else None
    if not text:
        return fallback
    crs, _ = parse_crs_msg(text, True, None)
    if crs is None:
        return fallback
    if any(c == crs for c, _, _ in opts):
        return text
    is_utm, far = area_crs_notes(crs, raster_crs, bounds)
    return text if (is_utm and far is None) else fallback


def interactive(defaults):
    print("Remaining cane from a classified raster - answer the questions, Enter keeps the [default].\n")
    raster = ask_text("1) Classified raster (.tif)", defaults.get("raster"), _file_check((".tif", ".tiff"), "raster"))
    l1 = ask_text("2) L1 layer (.shp / .gpkg / .geojson)", defaults.get("l1"), _file_check(VECTOR_EXTS, "L1 layer"))
    l1_layer = ask_layer(l1, "L1")
    assume = False
    try:
        l1crs = l1_info(l1)[0]
    except Exception as e:
        raise UserError(f"cannot read the L1 layer: {e}")
    if not l1crs:
        print("   The L1 layer has no coordinate system (.prj missing).")
        if not ask_yes_no("   Assume it uses the same coordinate system as the raster?", False):
            raise SystemExit("Stopped: give the L1 layer a coordinate system and run again.")
        assume = True
    od = output_defaults(defaults.get("out"), defaults.get("raster"), raster)
    out = ask_output(Path(raster).parent, Path(raster).stem + "_remaining_cane", od)

    do_filter = ask_yes_no("4) Apply the majority filter?", defaults.get("do_filter", True))
    neighbors = defaults.get("neighbors", 4)
    if do_filter:
        neighbors = ask_text("   Number of neighbours (4 or 8)", neighbors,
                             lambda a: (int(a), None) if a in ("4", "8") else (None, "Type 4 or 8."))

    with rasterio.open(str(raster)) as src:
        crs, bounds = src.crs, src.bounds
    opts = utm_options(crs, bounds)
    north = data_centre(crs, bounds)[1] >= 0
    if opts:
        print("5) Projection used to calculate areas (detected from the data extent):")
        for i, (c, label, note) in enumerate(opts, 1):
            print(f"   {i}) {label}  EPSG:{c.to_epsg()}  ({note})")
    else:
        print("5) The data cross the 180 degree meridian: no UTM zone can be suggested.")
    allow = False
    dflt = default_area_crs(defaults.get("utm"), crs, bounds, opts)
    say_earlier_choice(dflt)
    while True:
        utm = ask_text("   Choose number, zone (e.g. 47N) or EPSG code" if opts else "   Type a zone (e.g. 1S) or EPSG code",
                       dflt,
                       lambda a: parse_crs_msg(a, north, opts))
        is_utm, far = area_crs_notes(utm, crs, bounds)
        if not is_utm:
            print(f"   {NON_UTM_NOTE}")
            if not ask_yes_no(f"   Use {utm.name} anyway?", False):
                continue
            allow = True
        if far:
            print(f"   {far}")
            if not ask_yes_no("   Use it anyway?", False):
                continue
        break

    unit = ask_unit("6)", defaults.get("unit"))
    slivers = ask_yes_no("7) Remove slivers?", defaults.get("slivers", True))
    min_acres = defaults.get("min_acres", 0.2)
    if slivers:
        min_acres = ask_size("   Minimum area", unit, min_acres,
                             lambda t: f"   That is large: pieces smaller than {t} will be removed. Use it?")

    print("\nYour answers:")
    ll = f" (layer {l1_layer})" if layer_kw(l1) else ""
    print(f"  raster      : {raster}\n  L1 layer    : {l1}{ll}\n  output      : {out}")
    print(f"  majority    : {'yes, ' + str(neighbors) + ' neighbours' if do_filter else 'no'}")
    print(f"  projection  : {utm.name} (EPSG:{utm.to_epsg()})")
    print(f"  unit        : {unit.text} (field {unit.field})")
    print(f"  slivers     : {'remove below ' + size_in_unit(min_acres, unit) if slivers else 'keep all'}")
    choice = ask_start()
    if choice == "cancel":
        print("Cancelled.")
        return None
    if choice == "edit":
        epsg = utm.to_epsg()
        raise EditAnswers(dict(raster=str(raster), l1=str(l1), out=str(out), do_filter=do_filter,
                               neighbors=neighbors, utm=f"EPSG:{epsg}" if epsg else None, slivers=slivers,
                               min_acres=min_acres, unit=unit.key))
    return dict(raster=raster, l1_path=l1, out=out, min_acres=min_acres, utm=utm, keep_slivers=not slivers,
                do_filter=do_filter, neighbors=neighbors, allow_non_utm=allow, assume_l1_crs=assume, unit=unit,
                l1_layer=l1_layer)


def _ask_clean(work):
    return ask_yes_no(f"Old tiles in {work} cannot be reused. Discard them and recompute?", True)


def describe_last(last):
    ans = last.get("answers", {})
    d, n = tiles_progress(last.get("work", ""))
    state = last.get("status", "?")
    if state == "running" and _pid_alive(last.get("pid")) is False:
        state = "interrupted"
    print("A previous run was found:")
    print(f"  (remembered for this folder: {last_run_home(__file__)})")
    ll = f" (layer {last['l1_layer']})" if last.get("l1_layer") else ""
    print(f"  raster      : {last['raster']}\n  L1 layer    : {last['l1']}{ll}\n  output      : {last['out']}")
    print(f"  majority    : {majority_text(ans)}")
    unit = unit_of_record(ans)
    print(f"  projection  : {crs_label(last.get('utm'))}")
    print(f"  unit        : {unit.text}")
    sl = "keep all" if ans.get("keep_slivers") else "remove below " + size_in_unit(ans.get("min_acres", 0.2), unit)
    print(f"  slivers     : {sl}")
    extra = f" ({d} of {n} tiles saved)" if n and state != "finished" else ""
    print(f"  status      : {status_text(state, last.get('progress', ''))}{extra}, started {last.get('started', '?')}")


def _existing(path):
    """The path when that file exists, else None (no default that cannot be used)."""
    try:
        return path if path and Path(path).is_file() else None
    except (OSError, TypeError):
        return None


def last_inputs_exist(last, keys=("raster", "l1")):
    """True when the input files of the previous run still exist (else its screen is skipped)."""
    try:
        return all(Path(last[k]).is_file() for k in keys)
    except (KeyError, TypeError, OSError):
        return False


def _validate_cli(a, ap):
    """Check every CLI value; errors exit with code 2 and a plain message (no traceback)."""
    for name in ("raster", "l1"):
        p = Path(clean_path(getattr(a, name)))
        if not p.is_file():
            ap.error(f"--{name}: file not found: {p}")
    l1 = Path(clean_path(a.l1))
    if l1.suffix.lower() not in VECTOR_EXTS:
        ap.error(f"--l1: {l1.name} is not a vector file. {FORMAT_HINT}")
    try:
        choose_layer(l1, a.l1_layer, "L1", "--l1-layer")
    except UserError as e:
        ap.error(f"--l1: {e}")
    out = Path(clean_path(a.out))
    if out.suffix.lower() not in VECTOR_EXTS:
        ap.error("--out must end with .shp, .gpkg, .geojson or .json")
    if not out.parent.is_dir():
        ap.error(f"--out: folder does not exist: {out.parent}")
    for name in ("workers", "processes", "threads"):
        v = getattr(a, name)
        if v is not None and v < 1:
            ap.error(f"--{name} must be 1 or more")
    if a.tile_size is not None and a.tile_size < 2:
        ap.error("--tile-size must be 2 or more")
    if a.max_memory_gb is not None and a.max_memory_gb <= 0:
        ap.error("--max-memory-gb must be more than 0")
    if a.memory_buffer_gb is not None and a.memory_buffer_gb < 0:
        ap.error("--memory-buffer-gb must be 0 or more")
    try:
        with rasterio.open(clean_path(a.raster)) as src:
            crs, bounds = src.crs, src.bounds
    except Exception as e:
        ap.error(f"--raster: cannot read it ({e})")
    if crs is None:
        ap.error("--raster has no coordinate system")
    try:
        return resolve_area_crs(a.utm, crs, bounds, a.allow_non_utm, warn=False)
    except UserError as e:
        ap.error(f"--utm: {e}")


def cli_unit(text, ap):
    """--unit value -> Unit; an unknown or misspelled value is an error (exit 2) naming the closest unit."""
    unit, guess, hint = parse_unit(text)
    if unit is None:
        ap.error(f"--unit '{text}' is not known." + (f" Did you mean {guess.text}?" if guess else "")
                 + f" Use acres, hectares, rai, m2 or km2 (or a list number 1-{len(UNITS)}).")
    return unit


def cli_size(unit_text, acres, area, acres_flag, area_flag, ap, default=0.2):
    """(unit, size in acres, the flag as typed) from --unit and the two size flags: the acres flag is
    always in acres (old scripts), the area flag in the unit; both together are an error (exit 2)."""
    unit = cli_unit(unit_text, ap)
    if acres is not None and area is not None:
        ap.error(f"give either {acres_flag} (acres) or {area_flag} (in the --unit), not both")
    for flag, v in ((acres_flag, acres), (area_flag, area)):
        if v is not None and (v < 0 or not math.isfinite(v)):
            ap.error(f"{flag} must be 0 or more")
    if area is not None:
        return unit, to_acres(area, unit), f"{area_flag} {area:g}"
    v = default if acres is None else acres
    return unit, float(v) if acres is not None else v, f"{acres_flag} {v:g}"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raster")
    ap.add_argument("--l1", help="L1 layer (.shp, .gpkg, .geojson or .json)")
    ap.add_argument("--l1-layer", default=None, help="layer of the L1 file when it has several (a GeoPackage)")
    ap.add_argument("--out")
    ap.add_argument("--min-acres", type=float, default=None,
                    help="sliver size in acres, whatever --unit is (default 0.2 acres)")
    ap.add_argument("--min-area", type=float, default=None,
                    help="sliver size in the unit of --unit (default: the same size as 0.2 acres)")
    ap.add_argument("--unit", default="acres",
                    help="unit of the area field and of the result: acres (area_ac, default), hectares (area_ha), "
                         "rai (area_rai), m2 (area_m2) or km2 (area_km2); a list number 1-5 or a name also work")
    ap.add_argument("--utm", default=None, help="projected CRS for areas: 47N, 32647 or EPSG:32647 (default: auto)")
    ap.add_argument("--allow-non-utm", action="store_true", help="accept a projected CRS that is not a UTM zone")
    ap.add_argument("--assume-l1-crs", action="store_true",
                    help="L1 without coordinate system: assume the raster's coordinate system")
    ap.add_argument("--cane-value", type=int, default=1)
    ap.add_argument("--workers", type=int, default=None,
                    help="number of tiles processed in parallel (default: all cores)")
    ap.add_argument("--processes", type=int, default=None,
                    help="worker processes (default: planned from the memory budget; 1 = threads inside this program)")
    ap.add_argument("--threads", type=int, default=None,
                    help="threads per process (default: planned; processes x threads = parallel tiles)")
    ap.add_argument("--memory-mode", choices=("auto", "desktop", "container"), default="auto",
                    help="auto: container when a cgroup memory limit or MEM_GUARANTEE is set, else desktop")
    ap.add_argument("--keep-slivers", action="store_true")
    ap.add_argument("--no-filter", action="store_true", help="skip the majority filter")
    ap.add_argument("--neighbors", type=int, choices=(4, 8), default=4)
    ap.add_argument("--majority-threshold", choices=("majority", "half"), default="majority")
    ap.add_argument("--max-memory-gb", type=float, default=None,
                    help="memory budget (default: available memory minus the buffer)")
    ap.add_argument("--memory-buffer-gb", type=float, default=None,
                    help="memory kept free (default: 20%% of the available memory, between 0.5 and 1 GB)")
    ap.add_argument("--tile-size", type=int, default=None, help="tile edge in cells (default: planned)")
    ap.add_argument("--clean", action="store_true", help="remove the work folder (and the merge cache) after success")
    ap.add_argument("--summary-line", action="store_true",
                    help="also print the old one-line 'SUMMARY key=value' result for scripts")
    ap.add_argument("--ask", action="store_true", help="question-and-answer mode")
    a = ap.parse_args(argv)

    ask = a.ask or len(sys.argv if argv is None else argv) <= (1 if argv is None else 0)
    common = dict(cane_value=a.cane_value, workers=a.workers, threshold=a.majority_threshold,
                  max_memory_gb=a.max_memory_gb, memory_buffer_gb=a.memory_buffer_gb,
                  tile_size=a.tile_size, clean=a.clean, processes=a.processes, threads=a.threads,
                  memory_mode=None if a.memory_mode == "auto" else a.memory_mode, summary_line=a.summary_line)
    if ask:
        try:
            last = load_last_run()
            ans = None
            defaults = dict(raster=a.raster, l1=a.l1, out=a.out)
            if last and not last_inputs_exist(last):
                la = last.get("answers", {})
                defaults = dict(raster=a.raster or _existing(last.get("raster")), l1=a.l1 or _existing(last.get("l1")),
                                out=last["out"], do_filter=la.get("do_filter", True), neighbors=la.get("neighbors", 4),
                                utm=last.get("utm"), slivers=not la.get("keep_slivers", False),
                                min_acres=la.get("min_acres", 0.2), unit=unit_of_record(la).key)
                last = None    # its input files are gone: straight to the questions
            if last:
                describe_last(last)
                la = last.get("answers", {})
                if ask_yes_no("Continue the last run with these settings?", True):
                    ans = dict(raster=Path(last["raster"]), l1_path=Path(last["l1"]), out=Path(last["out"]),
                               min_acres=la.get("min_acres", 0.2), utm=pyproj.CRS(last["utm"]),
                               keep_slivers=la.get("keep_slivers", False), do_filter=la.get("do_filter", True),
                               neighbors=la.get("neighbors", 4), allow_non_utm=last.get("allow_non_utm", False),
                               assume_l1_crs=last.get("assume_l1_crs", False), unit=unit_of_record(la),
                               l1_layer=last.get("l1_layer"))
                    common.update(cane_value=la.get("cane_value", 1), threshold=la.get("threshold", "majority"))
                else:
                    epsg = pyproj.CRS(last["utm"]).to_epsg()
                    defaults = dict(raster=last["raster"], l1=last["l1"], out=last["out"],
                                    do_filter=la.get("do_filter", True), neighbors=la.get("neighbors", 4),
                                    utm=f"EPSG:{epsg}" if epsg else None, slivers=not la.get("keep_slivers", False),
                                    min_acres=la.get("min_acres", 0.2), unit=unit_of_record(la).key)
                    print()
            if ans is None:
                ans = ask_all(interactive, defaults)
            while ans:
                try:
                    run(**ans, **common, confirm_clean=_ask_clean, confirm_format=ask_format_switch)
                    break
                except DiskSpaceError as e:
                    print(f"\n{e}")
                    if not ask_yes_no("Choose another output path?", True):
                        break
                    ans["out"] = ask_output(Path(ans["out"]).parent, Path(ans["out"]).stem, {})
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
    if not (a.raster and a.l1 and a.out):
        ap.error("--raster, --l1 and --out are required (or run without arguments for questions)")
    utm = _validate_cli(a, ap)
    unit, min_acres, given = cli_size(a.unit, a.min_acres, a.min_area, "--min-acres", "--min-area", ap)
    if min_acres > LARGE_ACRES and not a.keep_slivers:
        print(f"NOTE: {given} is large: pieces smaller than {size_in_unit(min_acres, unit)} are removed.")
    try:
        res = run(Path(clean_path(a.raster)), Path(clean_path(a.l1)), Path(clean_path(a.out)), min_acres,
                  utm, keep_slivers=a.keep_slivers, do_filter=not a.no_filter, neighbors=a.neighbors,
                  allow_non_utm=a.allow_non_utm, assume_l1_crs=a.assume_l1_crs, unit=unit, l1_layer=a.l1_layer,
                  **common)
        if res and not res.get("geometry_ok", True):
            sys.exit(3)                     # the file is written, but the geometry check found a problem
    except UserError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
