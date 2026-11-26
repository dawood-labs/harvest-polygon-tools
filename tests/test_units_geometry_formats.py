"""Tests of the area units, the percentage columns, the geometry quality gate and check, dirty inputs
and the three vector formats of preprocess_classified.py and postprocess_remaining.py (synthetic
data only). Run: python -m pytest -q tests/test_units_geometry_formats.py"""
import contextlib
import io
import json
import re
import sys
import tempfile
from pathlib import Path

import numpy as np
import pyogrio
import pyogrio.raw
import pyproj
import rasterio
import shapely
from rasterio.transform import from_origin
from shapely.geometry import GeometryCollection, LineString, MultiPolygon, Point, Polygon

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import postprocess_remaining as ppr  # noqa: E402
import preprocess_classified as pc  # noqa: E402
from output_checks import assert_clean_output  # noqa: E402

pc.LAST_RUN = Path(tempfile.mkdtemp()) / "last_run.json"     # never touch the user's own last-run records
ppr.LAST_RUN = Path(tempfile.mkdtemp()) / "last_run.json"
CRS = "EPSG:32631"
X0, Y0 = 500000.0, 100000.0


# ---------------------------------------------------------------- helpers
def quiet(fn, *a, **k):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        res = fn(*a, **k)
    return res, out.getvalue()


def piped(fn, answers, *a, **k):
    old = sys.stdin
    sys.stdin = io.StringIO("".join(x + "\n" for x in answers))
    try:
        return quiet(fn, *a, **k)
    finally:
        sys.stdin = old


def exits(fn, argv):
    """(exit code, stdout + stderr) of a main() call."""
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            fn(argv)
        return 0, out.getvalue() + err.getvalue()
    except SystemExit as e:
        return e.code, out.getvalue() + err.getvalue()


def small_raster(d, l1_name="l1.gpkg", l1_geoms=None, driver="GPKG", geometry_type="Polygon"):
    rng = np.random.default_rng(3)
    z = rng.random((150, 170))
    for _ in range(3):
        z = (z + np.roll(z, 1, 0) + np.roll(z, -1, 0) + np.roll(z, 1, 1) + np.roll(z, -1, 1)) / 5
    arr = np.where(z > np.median(z), 1, 4).astype(np.uint8)
    with rasterio.open(d / "r.tif", "w", driver="GTiff", height=150, width=170, count=1, dtype="uint8",
                       crs="EPSG:4326", transform=from_origin(4.0, 1.0, 0.0001, 0.0001)) as o:
        o.write(arr, 1)
    boxes = l1_geoms if l1_geoms is not None else L1_BOXES
    write(d / l1_name, boxes, "EPSG:4326", driver, geometry_type)
    return d / "r.tif", d / l1_name


L1_BOXES = [shapely.box(4.0010, 0.9860, 4.0100, 0.9990), shapely.box(4.0090, 0.9850, 4.0165, 0.9950),
            shapely.box(4.0020, 0.9840, 4.0140, 0.9880)]


def write(path, geoms, crs, driver=None, geometry_type="Unknown", layer=None):
    driver = driver or pc.driver_of(path)
    wkb = np.array([None if g is None else shapely.to_wkb(g) for g in geoms], dtype=object)
    pyogrio.raw.write(str(path), geometry=wkb, field_data=[], fields=[], geometry_type=geometry_type, crs=crs,
                      driver=driver, layer=layer, **({"append": True} if layer and Path(path).exists() else {}))
    return path


def B(x0, y0, x1, y1):
    return shapely.box(X0 + x0, Y0 + y0, X0 + x1, Y0 + y1)


def post_case(d, crs=CRS, ext=".gpkg"):
    rng = np.random.default_rng(11)
    l1 = [B(i * 120, j * 120, i * 120 + 110 + 15 * (i == 1), j * 120 + 110) for i in range(3) for j in range(3)]
    final = [B(x, y, x + w, y + h) for x, y, w, h in rng.uniform([0, 0, 20, 20], [300, 300, 90, 90], (14, 4))]
    if crs != CRS:
        tr = pyproj.Transformer.from_crs(CRS, crs, always_xy=True)
        f = lambda g: shapely.transform(g, lambda xy: np.column_stack(tr.transform(xy[:, 0], xy[:, 1])))  # noqa: E731
        l1, final = [f(g) for g in l1], [f(g) for g in final]
    return write(d / ("final" + ext), final, crs), write(d / ("l1" + ext), l1, crs)


def read_layer(path, field=None):
    meta, _, w, fd = pyogrio.raw.read(str(path))
    names = list(meta["fields"])
    g = shapely.from_wkb(w) if len(w) else np.array([], dtype=object)
    vals = np.asarray(fd[names.index(field)], float) if field else None
    return g, names, vals, np.asarray(fd[names.index("predicted")])


def norm_key(g, vals):
    return sorted(zip(shapely.to_wkb(shapely.normalize(g)).tolist(), np.round(vals, 6).tolist()))


def result_block(txt):
    lines = txt.splitlines()
    i = [k for k, x in enumerate(lines) if "= RESULT =" in x][0]
    j = [k for k, x in enumerate(lines) if k > i and set(x) == {"="}][0]
    return lines[i:j + 1]


# ---------------------------------------------------------------- unit parser
def test_unit_parser_numbers_names_short_forms():
    want = {"acres": ["1", "Acres", "ACRES", "acre", "ac", "Ac.", " acres "],
            "hectares": ["2", "Hectares", "hectare", "ha", "HA", "hectar"],
            "rai": ["3", "Rai", "rai", "RAIS", "Rai (Thailand)", "rai thailand"],
            "m2": ["4", "Square metres", "square meters", "square metre", "square meter", "m2", "M2", "sqm", "sq m",
                   "sq. m", "meters", "metres", "m\u00b2"],
            "km2": ["5", "Square kilometres", "square kilometers", "square kilometer", "km2", "sqkm", "sq km", "Sq km"]}
    for key, forms in want.items():
        for f in forms:
            unit, guess, hint = pc.parse_unit(f)
            assert unit is not None and unit.key == key and guess is None and hint is None, (f, unit, guess)
    assert pc.parse_unit(pc.UNIT_BY_KEY["rai"])[0].key == "rai"
    assert pc.get_unit(None).key == "acres" and pc.get_unit("").key == "acres"


def test_unit_parser_misspellings_and_nonsense():
    table = {"hactare": "hectares", "hectars": "hectares", "hectre": "hectares", "acers": "acres", "akre": "acres",
             "acrs": "acres", "rye": "rai", "ria": "rai", "squre metres": "m2", "square meeters": "m2",
             "kilometers": "km2", "sq kilometres": "km2"}
    for text, key in table.items():
        unit, guess, hint = pc.parse_unit(text)
        assert unit is None and guess is not None and guess.key == key, (text, unit, guess)
    for text in ("banana", "xyz", "7", "0", "", "!!", "12.5", "feet"):
        unit, guess, hint = pc.parse_unit(text)
        assert unit is None and guess is None and hint == pc.UNIT_HINT, (text, unit, guess)
    try:
        pc.get_unit("hactare")
        raise AssertionError("expected UserError")
    except pc.UserError as e:
        assert "Did you mean hectares?" in str(e)


def test_ask_unit_question_and_did_you_mean():
    unit, txt = piped(pc.ask_unit, [""], "6)")
    assert unit.key == "acres"
    assert "6) Unit for areas:\n   1) Acres            2) Hectares         3) Rai (Thailand)\n" \
           "   4) Square metres    5) Square kilometres\n   Choose a number or type the name [1]:" in txt
    unit, txt = piped(pc.ask_unit, ["hactare", "y"], "6)")
    assert unit.key == "hectares" and "Did you mean Hectares? [Y/n]:" in txt
    unit, txt = piped(pc.ask_unit, ["hactare", "n", "rai"], "6)")
    assert unit.key == "rai" and pc.UNIT_HINT in txt
    unit, txt = piped(pc.ask_unit, ["banana", "4"], "6)")
    assert unit.key == "m2" and pc.UNIT_HINT in txt and "Did you mean" not in txt
    unit, txt = piped(pc.ask_unit, [""], "5)", "rai")             # previous unit is the default
    assert unit.key == "rai" and "Choose a number or type the name [3]:" in txt


def test_size_question_default_is_the_same_physical_size():
    for key, text in (("acres", "[0.2]"), ("hectares", "[0.081 = 0.2 acres]"), ("rai", "[0.506 = 0.2 acres]"),
                      ("m2", "[809.4 = 0.2 acres]"), ("km2", "[0.000809 = 0.2 acres]")):
        u = pc.UNIT_BY_KEY[key]
        v, txt = piped(pc.ask_size, [""], "   Minimum area", u, 0.2, lambda t: f"large {t}?")
        assert v == 0.2 and f"Minimum area in {u.text} {text}:" in txt, (key, txt)
    v, _ = piped(pc.ask_size, ["0.5"], "   Minimum area", pc.UNIT_BY_KEY["hectares"], 0.2, lambda t: t)
    assert abs(v - 0.5 * 10_000 / pc.ACRE_M2) < 1e-12
    v, txt = piped(pc.ask_size, ["3", "n", "1"], "   Minimum area", pc.UNIT_BY_KEY["hectares"], 0.2,
                   lambda t: f"   That is large: {t}. Use it?")
    assert "That is large: 3 hectares. Use it?" in txt and abs(v - 10_000 / pc.ACRE_M2) < 1e-12   # 3 ha > 5 acres
    v, txt = piped(pc.ask_size, ["2"], "   Minimum area", pc.UNIT_BY_KEY["hectares"], 0.2, lambda t: t)
    assert "large" not in txt                                                                       # 2 ha < 5 acres


# ---------------------------------------------------------------- units on a run
def test_each_unit_field_decimals_values_and_same_slivers():
    d = Path(tempfile.mkdtemp())
    r, l1 = small_raster(d)
    res, txt = quiet(pc.run, r, l1, d / "acres.gpkg", workers=2, max_memory_gb=1)
    g0, names, ac, _ = read_layer(d / "acres.gpkg", "area_ac")
    assert names == ["predicted", "area_ac"] and res["slivers"] > 0 and len(g0) > 0
    for key, field, dec in (("hectares", "area_ha", 3), ("rai", "area_rai", 3), ("m2", "area_m2", 1),
                            ("km2", "area_km2", 6)):
        u = pc.UNIT_BY_KEY[key]
        res_u, txt = quiet(pc.run, r, l1, d / f"{key}.gpkg", workers=2, max_memory_gb=1, unit=key)
        g, names, v, pred = read_layer(d / f"{key}.gpkg", field)
        assert names == ["predicted", field], names                 # only one area field
        assert (pred == 81).all() and len(g) == len(g0) == res_u["polygons"] and res_u["slivers"] == res["slivers"]
        assert all(a == b for a, b in zip(shapely.to_wkb(g), shapely.to_wkb(g0)))
        assert np.allclose(v, np.round(v, dec), atol=0)              # written with the unit's decimals
        f = pc.ACRE_M2 / u.m2
        assert (np.abs(v - ac * f) <= 0.0005 * f + 0.5 * 10 ** -dec + 1e-9).all(), key
        total = float(re.search(r"Total area\s+:\s+([\d,.]+) " + u.text, txt).group(1).replace(",", ""))
        assert abs(total - round(res["acres_exact"] * f, dec)) <= 10 ** -dec, (key, total)
        assert "How the result was reached" in txt and u.header in txt and "% of area" in txt


def test_changing_only_the_unit_reruns_only_the_last_step():
    d = Path(tempfile.mkdtemp())
    r, l1 = small_raster(d)
    quiet(pc.run, r, l1, d / "o.gpkg", workers=2, max_memory_gb=1)
    _, txt = quiet(pc.run, r, l1, d / "o.gpkg", workers=2, max_memory_gb=1, unit="hectares")
    assert "Setting changed: unit (acres -> hectares). Only the last step runs again." in txt
    assert "tiles (filter" not in txt and "area_ha" in txt
    _, txt = quiet(pc.run, r, l1, d / "o.gpkg", workers=2, max_memory_gb=1, unit="hectares")
    assert "already up to date" in txt
    f, lp = post_case(d)
    _, txt = quiet(ppr.run, f, lp, d / "p.gpkg", workers=2, max_memory_gb=1, utm="31N")
    _, txt = quiet(ppr.run, f, lp, d / "p.gpkg", workers=2, max_memory_gb=1, utm="31N", unit="rai")
    assert "Setting changed: unit (acres -> rai). Only the last step runs again." in txt
    assert "reusing the saved classes (stage 2)" in txt and "stage 1 overlay" not in txt
    assert read_layer(d / "p.gpkg")[1] == ["predicted", "area_rai"]


def test_old_records_and_work_folders_without_unit():
    d = Path(tempfile.mkdtemp())
    r, l1 = small_raster(d)
    quiet(pc.run, r, l1, d / "o.gpkg", workers=2, max_memory_gb=1)
    man_p = d / "o_work" / "manifest.json"
    man = json.loads(man_p.read_text())
    man["final_items"].pop("unit")                                   # as written before units existed
    man["final_stats"] = {k: v for k, v in man["final_stats"].items() if k not in ("unit", "col_sum")}
    man_p.write_text(json.dumps(man))
    _, txt = quiet(pc.run, r, l1, d / "o.gpkg", workers=2, max_memory_gb=1)
    assert "already up to date" in txt and "acres" in txt               # same key: nothing recomputed
    _, txt = quiet(pc.run, r, l1, d / "o.gpkg", workers=2, max_memory_gb=1, unit="ha")
    assert "Setting changed: unit (acres -> hectares). Only the last step runs again." in txt
    last = {"raster": str(r), "l1": str(l1), "out": str(d / "o.gpkg"), "utm": "EPSG:32631",
            "answers": {"do_filter": True, "neighbors": 4, "min_acres": 0.2}, "status": "finished",
            "progress": "finished", "started": "x"}
    _, scr = quiet(pc.describe_last, last)
    assert "unit        : acres" in scr and "remove below 0.2 acres" in scr
    pc.save_last_run(dict(last, answers=dict(last["answers"], unit="rai")))
    _, txt = piped(pc.main, ["n"] + [""] * 8 + ["", "", "", "n"], ["--ask"])
    assert "Choose a number or type the name [3]:" in txt and "Minimum area in rai [0.506 = 0.2 acres]:" in txt
    _, scr = quiet(ppr.describe_last, {"final": "f", "l1": "l", "out": "o", "utm": "EPSG:32631",
                                       "answers": {"rule": True, "sliver_acres": 0.2}, "status": "finished"})
    assert "unit        : acres" in scr and "classes" not in scr
    _, scr = quiet(ppr.describe_last, {"final": "f", "l1": "l", "out": "o", "utm": "EPSG:32631",
                                       "answers": {"rule": True, "sliver_acres": 0.2, "classes": "82"}})
    assert "classes     : 82 only" in scr
    assert pc._hash({"a": 1, "unit": "acres"}) == pc._hash({"a": 1})


def test_cli_unit_flags_and_errors():
    d = Path(tempfile.mkdtemp())
    r, l1 = small_raster(d)
    base = ["--raster", str(r), "--l1", str(l1), "--workers", "2", "--max-memory-gb", "1"]
    code, txt = exits(pc.main, base + ["--out", str(d / "a.gpkg"), "--unit", "hactare"])
    assert code == 2 and "Did you mean hectares?" in txt
    code, txt = exits(pc.main, base + ["--out", str(d / "a.gpkg"), "--min-acres", "0.2", "--min-area", "0.1"])
    assert code == 2 and "not both" in txt
    code, txt = exits(pc.main, base + ["--out", str(d / "a.gpkg"), "--min-area", "-1"])
    assert code == 2 and "--min-area must be 0 or more" in txt
    code, txt = exits(pc.main, base + ["--out", str(d / "a.gpkg"), "--unit", "ha", "--min-area", "0.081"])
    assert code == 0 and "removed below 0.081 hectares" in txt and "area_ha" in txt
    code, txt = exits(pc.main, base + ["--out", str(d / "b.gpkg"), "--unit", "2", "--min-acres", "0.2"])
    assert code == 0 and "removed below 0.081 hectares" in txt
    code, txt = exits(pc.main, base + ["--out", str(d / "c.gpkg"), "--unit", "rai", "--min-area", "20"])
    assert code == 0 and "NOTE: --min-area 20 is large" in txt
    f, lp = post_case(d)
    pb = ["--final", str(f), "--l1", str(lp), "--utm", "31N", "--workers", "2", "--max-memory-gb", "1"]
    code, txt = exits(ppr.main, pb + ["--out", str(d / "p.gpkg"), "--sliver-acres", "0.2", "--sliver-area", "1"])
    assert code == 2 and "give either --sliver-acres (acres) or --sliver-area" in txt
    code, txt = exits(ppr.main, pb + ["--out", str(d / "p.gpkg"), "--unit", "rye"])
    assert code == 2 and "Did you mean rai?" in txt
    code, txt = exits(ppr.main, pb + ["--out", str(d / "p.gpkg"), "--unit", "m2", "--sliver-area", "809.4"])
    assert code == 0 and "pieces of 809.4 square metres or smaller" in txt and "Square metres" in txt


# ---------------------------------------------------------------- percentage columns
def _pre_stats(scale):
    return {"count": 2_123_456, "acres": 40_345_122.0 * scale, "acres_exact": 40_345_122.0 * scale,
            "acres_all": 41_683_829.7 * scale, "sn": 1_436_400, "sa": 1_338_707.7 * scale, "noise": 46, "na": 0.0}


def test_percent_columns_add_up_and_fit_in_every_unit():
    ans = {"keep_slivers": False, "min_acres": 0.2, "do_filter": True, "neighbors": 4, "threshold": "majority"}
    for key in pc.UNIT_BY_KEY:
        for scale in (1.0, 0.001):
            s = dict(_pre_stats(scale), unit=key, col_sum=pc.from_acres(_pre_stats(scale)["acres"], pc.UNIT_BY_KEY[key]))
            _, txt = quiet(pc.print_run_result, s, dict(ans, unit=key), pc.zone_crs(31, True), Path("/x/o.shp"),
                           Path("/x/o_work"), False, Path("/x/none"))
            block = result_block(txt)
            assert all(len(x) <= 80 for x in block), [x for x in block if len(x) > 80]
            assert "-0.0" not in txt
            rows = {x.split("  ")[1].strip(): float(x.split()[-1]) for x in block
                    if x.startswith("  Removed") or x.startswith("  Written") or x.startswith("  Cane")}
            assert rows["Cane pieces inside L1"] == 100.0
            parts = [v for k, v in rows.items() if k != "Cane pieces inside L1"]
            assert 99.9 <= sum(parts) <= 100.1, rows
            head = [x for x in block if x.startswith("How the result")][0]
            assert head.endswith("% of area") and pc.UNIT_BY_KEY[key].header in head
    st = {"acres81": 31_260_373.3, "acres82": 9_084_749.1, "l1_union_acres": 40_345_122.4, "n81": 2_820_800,
          "n82": 1_907_300, "pieces": 19_471_700, "touching": 17_781_800, "absorbed": 17_564_400,
          "absorbed_acres": 2_529_345.0, "noise_overlay": 5655, "noise": 0, "noise_overlay_m2": 1.0,
          "l1_overlap_groups": 0, "trimmed_82_pieces": 0}
    for key in pc.UNIT_BY_KEY:
        _, txt = quiet(ppr.print_summary, st, {}, {"rule": True, "sliver_acres": 0.2, "classes": "both", "unit": key},
                       area_crs=pc.zone_crs(31, True))
        block = result_block(txt)
        assert all(len(x) <= 80 for x in block), [x for x in block if len(x) > 80]
        pct = [float(x.split()[-1]) for x in block if x.startswith("  81") or x.startswith("  82")]
        assert 99.9 <= sum(pct) <= 100.1 and [x for x in block if x.startswith("  Total")][0].endswith("100.0")
        assert any("Changed to remaining cane" in x and "(6.3 % of L1)" in x for x in block)
    assert pc.pct_text(-1e-12, 5) == "0.0" and pc.pct_text(1, 0) == "-"


# ---------------------------------------------------------------- geometry gate
def test_gate_repairs_and_keeps_good_features_unchanged():
    good = shapely.box(0, 0, 1, 1)
    bow = Polygon([(2, 0), (3, 1), (3, 0), (2, 1)])
    rep_v = Polygon([(4, 0), (5, 0), (5, 0), (5, 1), (4, 1)])
    spike = Polygon([(6, 0), (7, 0), (7, 1), (6.5, 1), (6.5, 2), (6.5, 1), (6, 1)])
    multi = MultiPolygon([shapely.box(8, 0, 9, 1), shapely.box(10, 0, 11, 1)])
    coll = GeometryCollection([shapely.box(12, 0, 13, 1), LineString([(0, 0), (1, 1)]), Point(5, 5)])
    z = shapely.force_3d(shapely.box(14, 0, 15, 1), 3.0)
    ov_a, ov_b = shapely.box(16, 0, 18, 1), shapely.box(17, 0, 19, 1)          # same class: dissolved
    c81, c82 = shapely.box(20, 0, 22, 1), shapely.box(21, 0, 23, 1)            # 81 wins over 82
    g = np.array([good, bow, rep_v, spike, multi, coll, z, None, LineString([(0, 0), (1, 0)]),
                  Polygon([(0, 0), (1, 0), (2, 0), (0, 0)]), ov_a, ov_b, c81, c82], dtype=object)
    cls = np.array([81] * 13 + [82])
    rep = {}
    out, rows, changed = pc.gate(g, cls, rep)
    assert out[0] is good and not changed[0]
    assert (shapely.get_type_id(out) == 3).all() and shapely.is_valid(out).all() and not shapely.has_z(out).any()
    nc = shapely.get_num_coordinates(out)
    assert (shapely.get_num_coordinates(shapely.remove_repeated_points(out, 0.0)) == nc).all()
    assert len(pc._real_overlaps(out, 1e-9)) == 0
    for k in ("invalid", "split", "z", "null", "lines", "points", "zero_area", "repeated", "overlaps"):
        assert rep.get(k), (k, rep)
    k82 = [i for i, r in enumerate(rows) if cls[r] == 82]
    assert len(k82) == 1 and abs(out[k82[0]].area - 1.0) < 1e-9                  # 82 cut back to its own part
    assert "1 null geometry ignored" in pc.report_text(rep) and "made 2D" in pc.report_text(rep)
    same, rows2, ch2 = pc.gate(np.array([good, shapely.box(2, 2, 3, 3)], dtype=object))
    assert same[0] is good and not ch2.any()


def test_geometry_rows_show_check_and_warning():
    c = {"features": 3, "polygons": 2, "other_types": 1, "null_empty": 0, "invalid": 1, "invalid_reasons":
         ["Self-intersection"], "zero_area": 0, "repeated": 0, "z": 0, "overlaps": 1, "layer_type": "Polygon",
         "unreadable": 0, "seconds": 0.1}
    rows = pc.geometry_rows(c, {"invalid": 2})
    text = "\n".join(" ".join(r) for r in rows)
    assert "CHECK" in text and "WARNING: the saved file has geometry problems" in text
    assert "1 invalid (Self-intersection)" in text and "Before saving: 2 invalid polygons repaired." in text
    assert not pc.geometry_ok(c)


def _dirty(base, crs_is_geo):
    """Dirty versions of a list of polygons: a null, a bow-tie, a duplicate vertex, an overlap, Z, a
    zero-area polygon, a multi-part, a collection with a line, a line and a point."""
    a = base[0]
    x0, y0, x1, y1 = a.bounds
    w, h = x1 - x0, y1 - y0
    bow = Polygon([(x0, y0), (x1, y1), (x1, y0), (x0, y1)])
    dup = Polygon([a.exterior.coords[0]] + list(a.exterior.coords[:-1]))
    over = shapely.affinity.translate(a, w / 3, 0)
    z = shapely.force_3d(base[1], 7.0)
    flat = Polygon([(x0, y0), (x0 + w, y0), (x0 + 2 * w, y0), (x0, y0)])
    multi = MultiPolygon([base[2]] + ([base[3]] if len(base) > 3 else []))
    coll = GeometryCollection([shapely.box(x0, y0 - 2 * h, x1, y0 - h), LineString([(x0, y0), (x1, y1)])])
    return [None, bow, dup, over, z, flat, multi, coll, LineString([(x0, y0), (x1, y1)]), Point(x0, y0)] + base[4:]


def test_dirty_inputs_tool1():
    d = Path(tempfile.mkdtemp())
    l1 = _dirty(L1_BOXES + [shapely.box(4.0010, 0.9900, 4.0030, 0.9950)], True)
    r, lp = small_raster(d, "l1.gpkg", l1, geometry_type="Unknown")
    res, txt = quiet(pc.run, r, lp, d / "o.gpkg", workers=2, max_memory_gb=1)
    note = [x for x in txt.splitlines() if x.startswith("NOTE: L1:")]
    assert note, txt
    for part in ("1 null geometry ignored", "2 lines ignored", "1 point ignored", "1 zero-area polygon ignored",
                 "made 2D", "invalid polygon", "split into single polygons"):
        assert part in note[0], (part, note)
    assert_clean_output(d / "o.gpkg") and res["geometry_ok"]


def test_dirty_inputs_tool2():
    d = Path(tempfile.mkdtemp())
    f, lp = post_case(d)
    fg = list(shapely.from_wkb(pyogrio.raw.read(str(f))[2]))
    lg = list(shapely.from_wkb(pyogrio.raw.read(str(lp))[2]))
    write(d / "fd.gpkg", _dirty(fg, False), CRS)
    write(d / "ld.gpkg", _dirty(lg, False), CRS)
    res, txt = quiet(ppr.run, d / "fd.gpkg", d / "ld.gpkg", d / "o.gpkg", workers=2, max_memory_gb=1, utm="31N")
    for what in ("L1", "FINAL"):
        note = [x for x in txt.splitlines() if x.startswith(f"NOTE: {what}:")]
        assert note and "1 null geometry ignored" in note[0] and "1 point ignored" in note[0], (what, txt)
    assert "overlaps between polygons resolved" in txt or "overlap between polygons resolved" in txt
    assert_clean_output(res["written"]) and res["geometry_ok"]
    assert "OK" in [x for x in txt.splitlines() if "Difference" in x][0]


def test_forced_repair_path_shows_rows_and_exit_code(monkeypatch=None):
    d = Path(tempfile.mkdtemp())
    f, lp = post_case(d)
    orig = pc.project_geoms

    def damaging(geoms, src, dst, workers=1):
        g = orig(geoms, src, dst, workers)
        if len(g) > 1:
            c = list(g[0].exterior.coords)
            g[0] = Polygon([c[0]] + c[:-1])                                  # a repeated vertex
            g[1] = MultiPolygon([g[1]])                                       # a multi-part
        return g
    pc.project_geoms = damaging
    try:
        res, txt = quiet(ppr.run, f, lp, d / "o.gpkg", workers=2, max_memory_gb=1, utm="31N")
    finally:
        pc.project_geoms = orig
    block = "\n".join(result_block(txt))
    m = re.search(r"Repaired before saving\s+(\d+)", block)
    assert m and int(m.group(1)) >= 2, block
    assert "Before saving:" in block and "repeated vertices cleaned" in block
    assert_clean_output(res["written"]) and res["geometry_ok"]
    orig_check = pc.geometry_check
    pc.geometry_check = lambda p, progress=True: dict(orig_check(p), invalid=1, invalid_reasons=["Self-intersection"])
    try:
        code, txt = exits(ppr.main, ["--final", str(f), "--l1", str(lp), "--out", str(d / "x.gpkg"), "--utm", "31N",
                                     "--workers", "2", "--max-memory-gb", "1"])
    finally:
        pc.geometry_check = orig_check
    assert code == 3 and "WARNING: the saved file has geometry problems" in txt and "CHECK" in txt
    assert (d / "x.gpkg").exists()                                             # the file is still written


# ---------------------------------------------------------------- formats
def test_output_formats_hold_the_same_polygons_and_format_change_reruns_last_step():
    d = Path(tempfile.mkdtemp())
    r, l1 = small_raster(d)
    keys = {}
    for i, ext in enumerate((".shp", ".gpkg", ".geojson", ".json")):
        _, txt = quiet(pc.run, r, l1, d / f"o{ext}", workers=2, max_memory_gb=1, min_acres=0.01)
        if i:
            assert "Only the last step runs again." in txt and "tiles (filter" not in txt, txt
        assert_clean_output(d / f"o{ext}")
        g, names, v, _ = read_layer(d / f"o{ext}", "area_ac")
        keys[ext] = (g, v)
    g0, v0 = keys[".shp"]
    for ext, (g, v) in keys.items():
        a = shapely.get_coordinates(shapely.normalize(g))
        b = shapely.get_coordinates(shapely.normalize(g0))
        assert a.shape == b.shape and np.abs(a - b).max() < 1e-12 and np.array_equal(v, v0), ext
    text = (d / "o.geojson").read_text()
    assert '"area_ac":' in text and '"predicted":81' in text and '"area_ac":"' not in text                # numbers, not strings
    g = shapely.from_wkb(pyogrio.raw.read(str(d / "o.geojson"))[2])
    assert shapely.is_ccw(shapely.get_exterior_ring(g)).all()                 # RFC 7946 ring order
    f, lp = post_case(d)
    for ext in (".shp", ".gpkg", ".geojson"):
        res, _ = quiet(ppr.run, f, lp, d / f"p{ext}", workers=2, max_memory_gb=1, utm="31N")
        assert_clean_output(res["written"])
    a = read_layer(d / "p.gpkg", "area_ac")
    b = read_layer(d / "p.geojson", "area_ac")
    assert len(a[0]) == len(b[0]) and np.array_equal(np.sort(a[2]), np.sort(b[2]))


def test_input_formats_give_the_same_result():
    d = Path(tempfile.mkdtemp())
    r, _ = small_raster(d)
    outs = {}
    for ext in (".shp", ".gpkg", ".geojson", ".json"):
        lp = write(d / f"l1{ext}", L1_BOXES, "EPSG:4326", geometry_type="Polygon")
        res, _ = quiet(pc.run, r, lp, d / f"o_{ext[1:]}.gpkg", workers=2, max_memory_gb=1)
        outs[ext] = norm_key(*read_layer(d / f"o_{ext[1:]}.gpkg", "area_ac")[::2])
    assert all(v == outs[".gpkg"] for v in outs.values())
    (d / "post").mkdir()
    res_all = {}
    for ext in (".shp", ".gpkg", ".geojson", ".json"):
        f, lp = post_case(d / "post", "EPSG:4326", ext)
        res, _ = quiet(ppr.run, f, lp, d / "post" / f"p_{ext[1:]}.gpkg", workers=2, max_memory_gb=1, utm="31N")
        g, _, v, pred = read_layer(res["written"], "area_ac")
        res_all[ext] = (len(g), round(float(v.sum()), 3), int((pred == 81).sum()))
    assert len(set(res_all.values())) == 1, res_all


def test_geojson_without_crs_dirty_and_broken_files():
    d = Path(tempfile.mkdtemp())
    r, _ = small_raster(d)
    feats = [{"type": "Feature", "properties": {}, "geometry": shapely.geometry.mapping(b)} for b in L1_BOXES]
    (d / "nocrs.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    assert pc.vector_crs(d / "nocrs.geojson") in ("EPSG:4326",) or pyproj.CRS(pc.vector_crs(d / "nocrs.geojson")) \
        == pyproj.CRS(4326)
    res, txt = quiet(pc.run, r, d / "nocrs.geojson", d / "a.gpkg", workers=2, max_memory_gb=1)
    assert res["polygons"] > 0 and "no coordinate system" not in txt
    answers = [str(r), str(d / "nocrs.geojson"), str(d), "q", "", "", "", "", "", "", "", "n"]
    _, txt = piped(pc.interactive, answers, {})
    assert "no coordinate system" not in txt and "Cancelled." in txt
    dirty = feats + [{"type": "Feature", "properties": {}, "geometry": None},
                     {"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [4.0, 1.0]}},
                     {"type": "Feature", "properties": {}, "geometry": {"type": "GeometryCollection", "geometries": [
                         shapely.geometry.mapping(shapely.box(4.002, 0.99, 4.003, 0.991)),
                         {"type": "LineString", "coordinates": [[4.0, 1.0], [4.1, 1.1]]}]}},
                     {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [[
                         [4.004, 0.99, 1], [4.005, 0.99, 1], [4.005, 0.991, 1], [4.004, 0.991, 1], [4.004, 0.99, 1]]]}}]
    (d / "dirty.json").write_text(json.dumps({"type": "FeatureCollection", "features": dirty}))
    res, txt = quiet(pc.run, r, d / "dirty.json", d / "b.gpkg", workers=2, max_memory_gb=1)
    note = [x for x in txt.splitlines() if x.startswith("NOTE: L1:")][0]
    assert "1 null geometry ignored" in note and "1 point ignored" in note and "made 2D" in note
    assert_clean_output(d / "b.gpkg")
    (d / "empty.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": []}))
    (d / "broken.geojson").write_text("{ this is not json")
    (d / "notvector.json").write_text(json.dumps({"hello": [1, 2, 3]}))
    for name in ("empty.geojson", "broken.geojson", "notvector.json"):
        try:
            quiet(pc.run, r, d / name, d / "c.gpkg", workers=2, max_memory_gb=1)
            raise AssertionError(f"expected UserError for {name}")
        except pc.UserError as e:
            assert "L1" in str(e) or name in str(e) or "vector" in str(e), (name, e)
    code, txt = exits(pc.main, ["--raster", str(r), "--l1", str(d / "broken.geojson"), "--out", str(d / "c.gpkg")])
    assert code == 2 and "Traceback" not in txt


def test_multi_layer_geopackage_in_flag_and_question_mode():
    d = Path(tempfile.mkdtemp())
    r, _ = small_raster(d)
    lp = d / "multi.gpkg"
    write(lp, L1_BOXES[:1], "EPSG:4326", geometry_type="Polygon", layer="small")
    write(lp, L1_BOXES, "EPSG:4326", geometry_type="Polygon", layer="fields")
    code, txt = exits(pc.main, ["--raster", str(r), "--l1", str(lp), "--out", str(d / "o.gpkg")])
    assert code == 2 and "has 2 layers: small (Polygon, 1 features), fields (Polygon, 3 features)" in txt
    assert "--l1-layer NAME" in txt
    code, txt = exits(pc.main, ["--raster", str(r), "--l1", str(lp), "--l1-layer", "fields", "--out", str(d / "o.gpkg"),
                                "--workers", "2", "--max-memory-gb", "1"])
    assert code == 0, txt
    code, txt = exits(pc.main, ["--raster", str(r), "--l1", str(lp), "--l1-layer", "nope", "--out", str(d / "o.gpkg")])
    assert code == 2 and "has no layer 'nope'" in txt
    name, txt = piped(pc.ask_layer, ["9", "2"], lp, "L1")
    assert name == "fields" and "1) small  (Polygon, 1 features)" in txt and "2) fields  (Polygon, 3 features)" in txt
    assert "Which layer is the L1 [1]:" in txt and "Type a number from 1 to 2 or a layer name." in txt
    lp2 = d / "one_polygon_layer.gpkg"
    write(lp2, L1_BOXES, "EPSG:4326", geometry_type="Polygon", layer="fields")
    pyogrio.raw.write(str(lp2), geometry=np.array([shapely.to_wkb(Point(4.0, 1.0))], dtype=object), field_data=[],
                      fields=[], geometry_type="Point", crs="EPSG:4326", driver="GPKG", layer="pts", append=True)
    assert pc.choose_layer(lp2) == "fields"
    res, _ = quiet(pc.run, r, lp2, d / "p.gpkg", workers=2, max_memory_gb=1)
    assert res["polygons"] > 0
    f, l1p = post_case(d)
    write(f, [B(0, 0, 10, 10)], CRS, layer="other")                       # FINAL file with two layers now
    code, txt = exits(ppr.main, ["--final", str(f), "--l1", str(l1p), "--out", str(d / "q.gpkg"), "--utm", "31N"])
    assert code == 2 and "--final-layer NAME" in txt
    code, txt = exits(ppr.main, ["--final", str(f), "--final-layer", "final", "--l1", str(l1p), "--out", str(d / "q.gpkg"),
                                 "--utm", "31N", "--workers", "2", "--max-memory-gb", "1"])
    assert code == 0, txt


def test_wrong_extensions_and_format_question():
    d = Path(tempfile.mkdtemp())
    r, l1 = small_raster(d)
    (d / "l1.txt").write_text("x")
    try:
        quiet(pc.run, r, d / "l1.txt", d / "o.gpkg")
        raise AssertionError("expected UserError")
    except pc.UserError as e:
        assert pc.FORMAT_HINT in str(e)
    code, txt = exits(pc.main, ["--raster", str(r), "--l1", str(d / "l1.txt"), "--out", str(d / "o.gpkg")])
    assert code == 2 and pc.FORMAT_HINT in txt
    code, txt = exits(pc.main, ["--raster", str(r), "--l1", str(l1), "--out", str(d / "o.csv")])
    assert code == 2 and "--out must end with .shp, .gpkg, .geojson or .json" in txt
    v, hint = pc._file_check(pc.VECTOR_EXTS, "L1 layer")(str(d / "l1.txt"))
    assert v is None and pc.FORMAT_HINT in hint
    for a, want in (("3", ".geojson"), ("geojson", ".geojson"), (".json", ".geojson"), ("JSON", ".geojson"),
                    ("2", ".gpkg"), ("shp", ".shp")):
        assert pc._format_check(a)[0] == want, a
    res, txt = piped(pc.ask_output, ["", "", "3"], d, "x", {})
    assert res == d / "x.geojson" and "3) GeoJSON (.geojson)  [1]:" in txt
    res, txt = piped(pc.ask_output, [str(d / "y.json"), "", ""], d, "x", {})
    assert res == d / "y.geojson" and "[3]" in txt


def test_geojson_writer_reader_exact_parallel_and_fallback():
    d = Path(tempfile.mkdtemp())
    rng = np.random.default_rng(7)
    g = np.array([shapely.box(4 + i * 1e-3, 1, 4 + i * 1e-3 + rng.random() * 1e-4, 1.0003) for i in range(300)]
                 + [Polygon(shapely.box(5, 1, 5.01, 1.01).exterior.coords, [shapely.box(5.002, 1.002, 5.003, 1.003)
                                                                            .exterior.coords])], dtype=object)
    old = pc.PARALLEL_TEXT_BYTES
    pc.PARALLEL_TEXT_BYTES = 1000                                         # force the worker processes
    try:
        p = d / "x.geojson"
        pc.write_vector(p, shapely.to_wkb(g[:150]), [np.full(150, 81, dtype="int32"), np.arange(150) / 7],
                        ["predicted", "area_ha"], "GeoJSON", "x", False)
        pc.write_vector(p, shapely.to_wkb(g[150:]), [np.full(151, 82, dtype="int32"), np.arange(151) / 3],
                        ["predicted", "area_ha"], "GeoJSON", "x", True)
        pc.finish_vector(p, "GeoJSON")
        back = pc._read_own_geojson(p)
    finally:
        pc.PARALLEL_TEXT_BYTES = old
    assert pc.feature_count(p) == 301 and len(back) == 301
    assert shapely.equals_exact(back, pc.orient_ccw(g), 0).all()            # the very same numbers
    meta, _, w, fd = pyogrio.raw.read(str(p))
    assert len(w) == 301 and list(meta["fields"]) == ["predicted", "area_ha"] and fd[1][149] == 149 / 7
    assert shapely.equals_exact(shapely.from_wkb(w), pc.orient_ccw(g), 0).all()   # GDAL reads the same numbers
    assert pc.geometry_ok(pc.geometry_check(p))
    gd = d / "gdal.geojson"                                                # a GeoJSON written by another program
    pyogrio.raw.write(str(gd), geometry=shapely.to_wkb(g), field_data=[], fields=[], geometry_type="Polygon",
                      crs="EPSG:4326", driver="GeoJSON")
    assert pc._read_own_geojson(gd) is None and pc.geometry_check(gd)["features"] == 301
    pc.write_vector(d / "e.geojson", np.array([], dtype=object), [np.zeros(0, dtype="int32"), np.zeros(0)],
                    ["predicted", "area_ac"], "GeoJSON", "e", False)
    pc.finish_vector(d / "e.geojson", "GeoJSON")
    assert pc.feature_count(d / "e.geojson") == 0 and pyogrio.read_info(str(d / "e.geojson"))["features"] == 0


def test_tool2_format_only_rerun_and_check_in_every_format():
    d = Path(tempfile.mkdtemp())
    f, lp = post_case(d)
    for i, ext in enumerate((".shp", ".gpkg", ".geojson")):
        res, txt = quiet(ppr.run, f, lp, d / f"p{ext}", workers=2, max_memory_gb=1, utm="31N", unit="ha")
        if i:
            assert "Setting changed: output file" in txt and "Only the last step runs again." in txt, txt
            assert "stage 1 overlay (" not in txt and "stage 2 sliver rule" not in txt
        assert "Geometry check (the saved file was read back" in txt and res["geometry_ok"]
        assert_clean_output(res["written"])
        assert read_layer(res["written"])[1] == ["predicted", "area_ha"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name, flush=True)
