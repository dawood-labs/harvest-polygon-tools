"""Tests for harvest_stats_excel.py (run: python -m pytest -q tests/test_harvest_stats_excel.py).

Synthetic data only: square boundary units and crop squares in UTM zone 31N around neutral open-sea
coordinates. Every number is compared with a brute-force reference written here independently: plain
shapely, one boundary combination at a time (union of its polygons after the first-wins rule for overlaps),
intersected with every crop polygon.
"""
import contextlib
import io
import json
import os
import sys
import zipfile
from pathlib import Path

import numpy as np
import openpyxl
import pyogrio.raw
import pytest
import shapely
from shapely.geometry import box

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import harvest_stats_excel as hs  # noqa: E402
import preprocess_classified as pc  # noqa: E402

CRS = "EPSG:32631"
X0, Y0 = 500000.0, 100000.0
ACRE = pc.ACRE_M2


def B(x0, y0, x1, y1):
    return box(X0 + x0, Y0 + y0, X0 + x1, Y0 + y1)


def write(path, geoms, fields, crs=CRS, driver=None):
    """A polygon layer with text / integer fields ({name: values})."""
    path = Path(path)
    names = list(fields)
    data = []
    for n in names:
        vals = fields[n]
        if all(isinstance(v, int) for v in vals):
            data.append(np.array(vals, dtype=np.int64))
        else:
            data.append(np.array(vals, dtype=object))
    for p in path.parent.glob(path.stem + ".*"):
        if p.suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg", ".gpkg", ".geojson"):
            p.unlink()
    pyogrio.raw.write(str(path), geometry=shapely.to_wkb(np.asarray(geoms, dtype=object)), field_data=data,
                      fields=names, geometry_type="Unknown", crs=crs, driver=driver)
    return path


# ---------------------------------------------------------------- scenario
LEVEL1 = ["North", "North", "South", "South"]
LEVEL2 = ["Block 1", "Block 2", "Block 1", "Block 2"]
LEVEL3 = ["Part A", "Part B"]
UNIT_BOXES = [B(0, 1000, 1000, 2000), B(1000, 1000, 2000, 2000), B(0, 0, 1000, 1000), B(1000, 0, 2000, 1000)]


def boundary(path, three=False, extra=()):
    """Four units (two per level-1 value); with three=True every unit is split in two halves (level 3).
    extra = [(geometry, (v1, v2[, v3]))] appended in file order."""
    geoms, f1, f2, f3 = [], [], [], []
    for g, a, b in zip(UNIT_BOXES, LEVEL1, LEVEL2):
        if three:
            x0, y0, x1, y1 = g.bounds
            for k, half in enumerate((box(x0, y0, (x0 + x1) / 2, y1), box((x0 + x1) / 2, y0, x1, y1))):
                geoms.append(half)
                f1.append(a)
                f2.append(b)
                f3.append(LEVEL3[k])
        else:
            geoms.append(g)
            f1.append(a)
            f2.append(b)
    for g, vals in extra:
        geoms.append(g)
        f1.append(vals[0])
        f2.append(vals[1])
        if three:
            f3.append(vals[2])
    fields = {"region": f1, "block": f2}
    if three:
        fields["part"] = f3
    return write(path, geoms, fields)


def crop_squares(x0=0, y0=0, x1=2000, y1=2000, step=100, size=90):
    out = []
    for x in np.arange(x0 + 5, x1 - size + 1, step):
        for y in np.arange(y0 + 5, y1 - size + 1, step):
            out.append(B(x, y, x + size, y + size))
    return out


def harvest_classes(geoms, rule):
    """Class per crop square: rule(x, y) of the square centre (local metres) -> code."""
    out = []
    for g in geoms:
        c = g.centroid
        out.append(rule(c.x - X0, c.y - Y0))
    return out


def report(path, geoms, classes, crs=CRS, field="predicted"):
    return write(path, geoms, {field: [int(c) for c in classes]}, crs)


def step_rule(limit, extra=None):
    """82 left of x = limit (local metres), else 81; extra(x, y) may return another code."""
    def rule(x, y):
        if extra is not None:
            v = extra(x, y)
            if v is not None:
                return v
        return 82 if x < limit else 81
    return rule


# ---------------------------------------------------------------- brute-force reference
def reference(bpath, levels, rpath, unit_m2=ACRE, field="predicted"):
    """{key: {code: area}}, outside {code: area}: first boundary polygon wins, shapely only."""
    _, _, wkb, data = pyogrio.raw.read(str(bpath))
    names = list(pyogrio.read_info(str(bpath))["fields"])
    cols = [data[names.index(f)] for f in levels]
    bg = shapely.from_wkb(wkb)
    eff, acc = [], None
    for g in bg:
        g = shapely.make_valid(g)
        eff.append(g if acc is None else g.difference(acc))
        acc = g if acc is None else acc.union(g)
    keys = {}
    for i, g in enumerate(eff):
        k = tuple(hs.norm_value(c[i]) for c in cols)
        keys.setdefault(k, []).append(g)
    _, _, cw, cdata = pyogrio.raw.read(str(rpath))
    crops = shapely.from_wkb(cw)
    rnames = list(pyogrio.read_info(str(rpath))["fields"])
    codes = [hs.code_text(v) for v in cdata[rnames.index(field)]]
    res, outside = {}, {}
    total = {}
    for g, c in zip(crops, codes):
        total[c] = total.get(c, 0.0) + g.area / unit_m2
    inside = {}
    for k, gs in keys.items():
        u = shapely.union_all(gs)
        for g, c in zip(crops, codes):
            a = g.intersection(u).area / unit_m2
            if a > 0:
                res.setdefault(k, {})
                res[k][c] = res[k].get(c, 0.0) + a
                inside[c] = inside.get(c, 0.0) + a
    for c in total:
        if total[c] - inside.get(c, 0.0) > 1e-9:
            outside[c] = total[c] - inside.get(c, 0.0)
    return res, outside


def quiet(fn, *a, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = fn(*a, **kw)
    return r, buf.getvalue()


def run(tmp, bpath, levels, rpath, date, name, previous=None, titles=None, **kw):
    lv = [{"field": f, "title": t} for f, t in zip(levels, titles or levels)]
    out = Path(tmp) / name
    kw.setdefault("utm", "EPSG:32631")
    kw.setdefault("workers", 2)
    res, txt = quiet(hs.run, bpath, lv, rpath, date, out, previous, **kw)
    return res, txt, out


def hidden(out):
    meta, values, labels = hs.read_previous(out)
    return meta, values


def stats_table(out, sheet="Stats"):
    wb = openpyxl.load_workbook(out)
    ws = wb[sheet]
    rows = list(ws.iter_rows(values_only=True))
    return rows[1], rows[2:], wb


def close(a, b, tol=1e-6):
    return abs(a - b) <= tol


def assert_matches(values, date, ref, ref_out):
    """The workbook's hidden numbers of one date equal the brute-force reference (to 1e-6 of the unit)."""
    got = {k: v[date] for k, v in values.items() if k != hs.OUTSIDE_KEY and date in v}
    for k in set(got) | set(ref):
        g, r = got.get(k, {}), ref.get(k, {})
        for c in set(g) | set(r):
            assert close(g.get(c, 0.0), r.get(c, 0.0)), (k, c, g.get(c), r.get(c))
    go = values.get(hs.OUTSIDE_KEY, {}).get(date, {})
    for c in set(go) | set(ref_out):
        assert close(go.get(c, 0.0), ref_out.get(c, 0.0), 1e-5), (c, go.get(c), ref_out.get(c))


# ---------------------------------------------------------------- numbers
@pytest.mark.parametrize("n_levels", [1, 2, 3])
def test_levels_match_reference(tmp_path, n_levels):
    bp = boundary(tmp_path / "b.gpkg", three=n_levels == 3)
    crops = crop_squares()
    rp = report(tmp_path / "r.gpkg", crops, harvest_classes(crops, step_rule(730)))
    levels = ["region", "block", "part"][:n_levels]
    res, txt, out = run(tmp_path, bp, levels, rp, "2026-01-10", "s.xlsx")
    ref, ref_out = reference(bp, levels, rp)
    _, values = hidden(out)
    assert_matches(values, "2026-01-10", ref, ref_out)
    header, rows, wb = stats_table(out)
    assert list(header[:n_levels]) == levels
    assert rows[-1][0] == "Totals"
    expected_rows = {1: 2, 2: 4, 3: 8}[n_levels]
    assert len(rows) - 1 == expected_rows == res["rows"]
    total = sum(sum(v.values()) for v in ref.values())
    assert close(rows[-1][n_levels], total, 1e-6) and res["check_ok"]
    assert "OK" in txt and "RESULT" in txt
    assert not [s for s in wb.sheetnames if s.startswith("By ")]       # larger levels: Pivot and Dashboard


def chain3(tmp_path, b_name="b.gpkg"):
    bp = boundary(tmp_path / b_name)
    crops = crop_squares()
    rps = []
    for i, limit in enumerate((400, 900, 1500)):
        rps.append(report(tmp_path / f"r{i}.gpkg", crops, harvest_classes(crops, step_rule(limit))))
    dates = ["2026-01-05", "2026-01-15", "2026-01-25"]
    outs, prev = [], None
    for i, (rp, d) in enumerate(zip(rps, dates)):
        res, txt, out = run(tmp_path, bp, ["region", "block"], rp, d, f"s{i}.xlsx", prev)
        outs.append(out)
        prev = out
    return bp, rps, dates, outs, res, txt


def test_chain_of_three_equals_reference(tmp_path):
    bp, rps, dates, outs, res, txt = chain3(tmp_path)
    meta, values = hidden(outs[-1])
    assert [r["date"] for r in meta["reports"]] == dates
    for rp, d in zip(rps, dates):
        ref, ref_out = reference(bp, ["region", "block"], rp)
        assert_matches(values, d, ref, ref_out)
    header, rows, _ = stats_table(outs[-1])
    hcols = [i for i, h in enumerate(header) if str(h).startswith("Crop Harvested on")]
    assert [header[i] for i in hcols] == ["Crop Harvested on 05-01-2026", "Crop Harvested on 15-01-2026",
                                          "Crop Harvested on 25-01-2026"]
    th = header.index("Total Harvested Crop (25-01-2026)")
    for r in rows:                                  # the periods add up to the cumulative harvested
        assert close(sum(r[i] for i in hcols), r[th], 1e-9)
        assert close(r[header.index("Total Harvested Crop % (25-01-2026)")]
                     + r[header.index("Total Remaining Crop % (25-01-2026)")], 1.0, 1e-12) or r[2] == 0
    ref, _ = reference(bp, ["region", "block"], rps[0])
    first = sum(v.get("82", 0.0) for v in ref.values())
    assert close(rows[-1][hcols[0]], first, 1e-6)
    assert header[:3] == ("region", "block", "Total Crop Area")
    assert header[-4:] == ("Total Harvested Crop (25-01-2026)", "Total Remaining Crop (25-01-2026)",
                           "Total Harvested Crop % (25-01-2026)", "Total Remaining Crop % (25-01-2026)")


def test_other_code_is_remaining_with_its_own_column(tmp_path):
    bp = boundary(tmp_path / "b.gpkg")
    crops = crop_squares()
    r1 = report(tmp_path / "r1.gpkg", crops, harvest_classes(crops, step_rule(400)))
    r2 = report(tmp_path / "r2.gpkg", crops, harvest_classes(
        crops, step_rule(800, lambda x, y: 282 if 1500 < y < 1800 and 300 < x < 1300 else None)))
    r3 = report(tmp_path / "r3.gpkg", crops, harvest_classes(crops, step_rule(1200)))
    _, _, o1 = run(tmp_path, bp, ["region", "block"], r1, "2026-01-05", "s1.xlsx")
    res2, txt2, o2 = run(tmp_path, bp, ["region", "block"], r2, "2026-01-15", "s2.xlsx", o1)
    header, rows, _ = stats_table(o2)
    assert "282 on 15-01-2026" in header
    c282 = header.index("282 on 15-01-2026")
    assert header[c282 - 1] == "Crop Harvested on 15-01-2026"           # right after that report's harvest
    ref, _ = reference(bp, ["region", "block"], r2)
    want = sum(v.get("282", 0.0) for v in ref.values())
    assert want > 0 and close(rows[-1][c282], want, 1e-6)
    th, tr = header.index("Total Harvested Crop (15-01-2026)"), header.index("Total Remaining Crop (15-01-2026)")
    h82 = sum(v.get("82", 0.0) for v in ref.values())
    assert close(rows[-1][th], h82, 1e-6)                                 # 282 never counts as harvested
    assert close(rows[-1][tr], rows[-1][2] - h82, 1e-6)                   # ... it is remaining
    for r in rows:
        if r[2]:
            assert close(r[-2] + r[-1], 1.0, 1e-12)
    assert "of which code 282 (counted as remaining)" in txt2
    res3, _, o3 = run(tmp_path, bp, ["region", "block"], r3, "2026-01-25", "s3.xlsx", o2)
    header3, rows3, wb3 = stats_table(o3)
    assert "282 on 15-01-2026" in header3
    assert not any(str(h).startswith("282") and "25-01-2026" in str(h) for h in header3)
    assert close(rows3[-1][header3.index("282 on 15-01-2026")], want, 1e-6)
    _, values = hidden(o3)
    ref3, ref3_out = reference(bp, ["region", "block"], r3)
    assert_matches(values, "2026-01-25", ref3, ref3_out)
    pv3 = wb3["Pivot"]                                   # the code's column is ticked in the pivot too
    assert "Sum of 282 on 15-01-2026" in [c.value for c in pv3[hs.PIVOT_TOP]]


def test_class_names_for_other_codes(tmp_path, monkeypatch):
    bp = boundary(tmp_path / "b.gpkg")
    crops = crop_squares()

    def with_282(limit):
        return harvest_classes(crops, step_rule(limit, lambda x, y: 282 if 1500 < y < 1800 and 300 < x < 1300 else None))
    r1 = report(tmp_path / "r1.gpkg", crops, with_282(400))
    r2 = report(tmp_path / "r2.gpkg", crops, with_282(800))
    r3 = report(tmp_path / "r3.gpkg", crops, harvest_classes(crops, step_rule(1200)))
    assert hs.other_codes(r1, "predicted") == ["282"] and hs.other_codes(r3, "predicted") == []
    assert hs.parse_class_names("282=Cloud, 232 = Smoke cover") == {"282": "Cloud", "232": "Smoke cover"}
    with pytest.raises(hs.UserError):
        hs.parse_class_names("282")
    plain, _, _ = stats_table(run(tmp_path, bp, ["region", "block"], r1, "2026-01-05", "plain.xlsx")[2])
    _, txt1, o1 = run(tmp_path, bp, ["region", "block"], r1, "2026-01-05", "s1.xlsx",
                      class_names={"282": "Cloud", "999": "Nothing"})
    h1, rows1, wb1 = stats_table(o1)
    assert "Cloud on 05-01-2026" in h1 and "282 on 05-01-2026" not in h1
    assert "the report holds no such class" in txt1                       # 999 is not in the report
    assert [r[2:] for r in rows1] == [r[2:] for r in stats_table(tmp_path / "plain.xlsx")[1]]   # only the title changes
    assert "282 on 05-01-2026" in plain
    assert "Sum of Cloud on 05-01-2026" in [c.value for c in wb1["Pivot"][hs.PIVOT_TOP]]
    texts = [c.value for row in wb1["Dashboard"].iter_rows() for c in row if isinstance(c.value, str)]
    assert any("of class Cloud (counted as not harvested)" in t for t in texts)
    # the next report: the name is the default (what Enter gives in question mode) and goes on by itself
    meta, _, renamed = hs.read_previous(o1)
    assert hs.code_label("282", hs.merge_labels(meta.get("labels"), renamed)) == "Cloud"
    _, _, o2 = run(tmp_path, bp, ["region", "block"], r2, "2026-01-15", "s2.xlsx", o1)
    h2, _, _ = stats_table(o2)
    assert "Cloud on 05-01-2026" in h2 and "Cloud on 15-01-2026" in h2
    # the same code as the name: the plain header
    _, _, o2b = run(tmp_path, bp, ["region", "block"], r2, "2026-01-15", "s2b.xlsx", o1, class_names={"282": "282"})
    h2b, _, _ = stats_table(o2b)
    assert "282 on 15-01-2026" in h2b and "Cloud on 05-01-2026" in h2b


def typed(monkeypatch, answers):
    """Feed question mode the lines an analyst would type; fails when a question is left without an answer."""
    it = iter(answers)

    def one(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise AssertionError(f"unexpected question: {prompt}")
    monkeypatch.setattr(pc, "_input", one)
    monkeypatch.setattr("builtins.input", one)
    return it


def test_question_mode_first_report_then_only_the_new_report(tmp_path, monkeypatch, capsys):
    bp = boundary(tmp_path / "b.gpkg")
    crops = crop_squares()
    r1 = report(tmp_path / "r_2026-01-05.gpkg", crops, harvest_classes(crops, step_rule(400)))
    r2 = report(tmp_path / "r_2026-01-15.gpkg", crops, harvest_classes(
        crops, step_rule(800, lambda x, y: 282 if 1500 < y < 1800 and 300 < x < 1300 else None)))
    r3 = report(tmp_path / "r_2026-01-25.gpkg", crops, harvest_classes(crops, step_rule(1200)))
    # first report: every question (boundary, 2 levels + titles, report, date, no previous, zone, unit, output)
    left = typed(monkeypatch, [str(bp), "2", "region", "Zone", "block", "Part", str(r1), "", "", "32631", "2",
                               str(tmp_path), "one", "", ""])
    hs.main(["--ask"])
    assert list(left) == [] and (tmp_path / "one.xlsx").is_file()
    text1 = capsys.readouterr().out
    assert "1) Boundary layer" not in text1 or "This folder already holds a season" not in text1
    # second report: the folder remembers the season, so only the report (and the name of class 282) is asked
    left = typed(monkeypatch, ["", str(r2), "", "Cloud", str(tmp_path), "two", "", ""])
    hs.main(["--ask"])
    text2 = capsys.readouterr().out
    assert list(left) == []
    assert "This folder already holds a season" in text2 and "levels      : Zone > Part" in text2
    assert "unit        : hectares" in text2 and "The report holds 1 class other than 81 and 82: 282." in text2
    h2, rows2, _ = stats_table(tmp_path / "two.xlsx")
    assert h2[:2] == ("Zone", "Part") and "Cloud on 15-01-2026" in h2
    meta, _ = hidden(tmp_path / "two.xlsx")
    assert meta["unit"] == "hectares" and [r["date"] for r in meta["reports"]] == ["2026-01-05", "2026-01-15"]
    ref, _ = reference(bp, ["region", "block"], r2)
    want_ha = sum(v.get("82", 0.0) for v in ref.values()) * ACRE / 10000.0
    assert close(rows2[-1][h2.index("Total Harvested Crop (15-01-2026)")], want_ha, 1e-6)
    # third report: the same report given again by mistake is refused; Enter keeps the name 'Cloud' ... but this
    # report has no 282, so no name question at all
    left = typed(monkeypatch, ["", str(r2), str(r3), "", str(tmp_path), "three", "", ""])
    hs.main(["--ask"])
    text3 = capsys.readouterr().out
    assert list(left) == [] and "That report is already in the workbook" in text3 and "Name for class" not in text3
    h3, _, _ = stats_table(tmp_path / "three.xlsx")
    assert "Cloud on 15-01-2026" in h3 and "Crop Harvested on 25-01-2026" in h3
    # 'n' at the first question gives the full questions back
    typed(monkeypatch, ["n"])
    assert hs.next_report(hs.load_last_run()) is None


def test_negative_period_written_and_warned(tmp_path):
    bp = boundary(tmp_path / "b.gpkg")
    crops = crop_squares()
    r1 = report(tmp_path / "r1.gpkg", crops, harvest_classes(crops, step_rule(900)))
    r2 = report(tmp_path / "r2.gpkg", crops, harvest_classes(crops, step_rule(600)))    # harvest went DOWN
    _, _, o1 = run(tmp_path, bp, ["region"], r1, "2026-01-05", "s1.xlsx")
    res, txt, o2 = run(tmp_path, bp, ["region"], r2, "2026-01-15", "s2.xlsx", o1)
    assert "WARNING: the harvested area went DOWN" in txt and "North on 15-01-2026" in txt
    header, rows, wb = stats_table(o2)
    c = header.index("Crop Harvested on 15-01-2026")
    assert rows[0][c] < 0 and rows[-1][c] < 0
    cell = wb["Stats"].cell(row=3, column=c + 1)
    assert cell.fill.start_color.rgb.endswith(hs.C_NEG_FILL)
    assert res["warnings"]


def test_slivers_left_out_and_outside_row(tmp_path):
    sliver = (B(2000, 0, 2000.0001, 1000), ("East", "Sliver"))       # no crop inside
    bp = boundary(tmp_path / "b.gpkg", extra=[sliver, (B(5000, 5000, 5100, 5100), ("Far", "Empty"))])
    crops = crop_squares(0, 0, 2400, 2000)                            # a strip of crop east of every unit
    rp = report(tmp_path / "r.gpkg", crops, harvest_classes(crops, step_rule(500)))
    res, txt, out = run(tmp_path, bp, ["region", "block"], rp, "2026-01-10", "s.xlsx")
    header, rows, _ = stats_table(out)
    names = [r[0] for r in rows]
    assert hs.OUTSIDE in names and "East" not in names and "Far" not in names
    assert res["left_out"] == 2 and "Left out: combinations without crop" in txt
    ref, ref_out = reference(bp, ["region", "block"], rp)
    out_row = rows[names.index(hs.OUTSIDE)]
    assert close(out_row[2], sum(ref_out.values()), 1e-5) and out_row[2] > 1
    total = sum(sum(v.values()) for v in ref.values()) + sum(ref_out.values())
    assert close(rows[-1][2], total, 1e-5) and res["check_ok"]
    assert "outside every boundary polygon" in txt


def test_overlapping_boundaries_counted_once(tmp_path):
    over = (B(500, 500, 1500, 1500), ("Overlap", "Big"))              # overlaps all four units
    bp = boundary(tmp_path / "b.gpkg", extra=[over])
    crops = crop_squares()
    rp = report(tmp_path / "r.gpkg", crops, harvest_classes(crops, step_rule(1000)))
    res, txt, out = run(tmp_path, bp, ["region", "block"], rp, "2026-01-10", "s.xlsx")
    assert "boundary polygons overlap each other" in txt
    header, rows, _ = stats_table(out)
    crop_total = sum(g.area for g in crops) / ACRE
    assert close(rows[-1][2], crop_total, 1e-6)                        # nothing counted twice
    assert "Overlap" not in [r[0] for r in rows]                       # the earlier polygons won
    ref, ref_out = reference(bp, ["region", "block"], rp)
    _, values = hidden(out)
    assert_matches(values, "2026-01-10", ref, ref_out)


def test_overlap_first_in_file_wins(tmp_path):
    bp = write(tmp_path / "b.gpkg", [B(0, 0, 1000, 1000), B(500, 0, 1500, 1000)], {"zone": ["First", "Second"]})
    crops = crop_squares(0, 0, 1500, 1000)
    rp = report(tmp_path / "r.gpkg", crops, [81] * len(crops))
    res, _, out = run(tmp_path, bp, ["zone"], rp, "2026-01-10", "s.xlsx")
    _, rows, _ = stats_table(out)
    first = [r for r in rows if r[0] == "First"][0][1]
    want = sum(g.intersection(B(0, 0, 1000, 1000)).area for g in crops) / ACRE
    assert close(first, want, 1e-6)


# ---------------------------------------------------------------- refusals
def test_unit_projection_and_levels_must_match(tmp_path):
    bp = boundary(tmp_path / "b.gpkg")
    crops = crop_squares()
    r1 = report(tmp_path / "r1.gpkg", crops, harvest_classes(crops, step_rule(400)))
    r2 = report(tmp_path / "r2.gpkg", crops, harvest_classes(crops, step_rule(800)))
    _, _, o1 = run(tmp_path, bp, ["region", "block"], r1, "2026-01-05", "s1.xlsx")
    with pytest.raises(pc.UserError, match="unit must stay the same"):
        run(tmp_path, bp, ["region", "block"], r2, "2026-01-15", "s2.xlsx", o1, unit="hectares")
    with pytest.raises(pc.UserError, match="projection must stay the same"):
        run(tmp_path, bp, ["region", "block"], r2, "2026-01-15", "s2.xlsx", o1, utm="EPSG:32632",
            allow_non_utm=True)
    with pytest.raises(pc.UserError, match="levels must stay the same"):
        run(tmp_path, bp, ["block", "region"], r2, "2026-01-15", "s2.xlsx", o1)
    with pytest.raises(pc.UserError, match="levels must stay the same"):
        run(tmp_path, bp, ["region"], r2, "2026-01-15", "s2.xlsx", o1)


def test_same_date_and_earlier_date_rejected(tmp_path):
    bp, rps, dates, outs, _, _ = chain3(tmp_path)
    with pytest.raises(pc.UserError, match="already contains the report of 15-01-2026") as e:
        run(tmp_path, bp, ["region", "block"], rps[1], "2026-01-15", "x.xlsx", outs[2])
    assert "report of 05-01-2026" in str(e.value)
    with pytest.raises(pc.UserError, match="earlier than the last report"):
        run(tmp_path, bp, ["region", "block"], rps[1], "2026-01-20", "x.xlsx", outs[2])


def test_incremental_report_rejected(tmp_path):
    bp = boundary(tmp_path / "b.gpkg")
    crops = crop_squares()
    cls = harvest_classes(crops, step_rule(700))
    r1 = report(tmp_path / "r1.gpkg", crops, cls)
    _, _, o1 = run(tmp_path, bp, ["region", "block"], r1, "2026-01-05", "s1.xlsx")
    rest = [g for g, c in zip(crops, cls) if c == 81]                 # only what was still standing
    inc = report(tmp_path / "inc.gpkg", rest, harvest_classes(rest, step_rule(1200)))
    with pytest.raises(pc.UserError, match="looks INCREMENTAL"):
        run(tmp_path, bp, ["region", "block"], inc, "2026-01-15", "s2.xlsx", o1)


def test_total_change_and_new_rows_warned(tmp_path):
    bp = boundary(tmp_path / "b.gpkg")
    crops = crop_squares()
    r1 = report(tmp_path / "r1.gpkg", crops, harvest_classes(crops, step_rule(400)))
    fewer = [g for g in crops if not (g.centroid.y - Y0 > 1000 and g.centroid.x - X0 < 300)]
    r2 = report(tmp_path / "r2.gpkg", fewer, harvest_classes(fewer, step_rule(800)))
    _, _, o1 = run(tmp_path, bp, ["region", "block"], r1, "2026-01-05", "s1.xlsx")
    res, txt, _ = run(tmp_path, bp, ["region", "block"], r2, "2026-01-15", "s2.xlsx", o1)
    assert "WARNING: the total crop area changed since 05-01-2026" in txt and "North / Block 1" in txt


# ---------------------------------------------------------------- the previous workbook
def test_edited_visible_sheet_still_read_and_rename_kept(tmp_path):
    bp = boundary(tmp_path / "b.gpkg")
    crops = crop_squares()
    top = lambda x, y: 282 if y > 1900 else None  # noqa: E731
    r1 = report(tmp_path / "r1.gpkg", crops, harvest_classes(crops, step_rule(400, top)))
    top = lambda x, y: 282 if y > 1900 else None  # noqa: E731
    r2 = report(tmp_path / "r2.gpkg", crops, harvest_classes(crops, step_rule(800, top)))
    _, _, o1 = run(tmp_path, bp, ["region", "block"], r1, "2026-01-05", "s1.xlsx")
    wb = openpyxl.load_workbook(o1)
    ws = wb["Stats"]
    for c in ws[2]:
        if c.value == "282 on 05-01-2026":
            c.value = "Burnt on 05-01-2026"
    ws.column_dimensions["A"].width = 40
    ws["A1"] = "my note"
    edited = tmp_path / "edited.xlsx"
    wb.save(edited)
    res, txt, o2 = run(tmp_path, bp, ["region", "block"], r2, "2026-01-15", "s2.xlsx", edited)
    header, rows, _ = stats_table(o2)
    assert "Burnt on 05-01-2026" in header and "Burnt on 15-01-2026" in header
    assert "column title 'Burnt on 05-01-2026'" in txt
    _, v1 = hidden(o1)
    _, v2 = hidden(o2)
    for k in v1:
        assert v1[k]["2026-01-05"] == v2[k]["2026-01-05"]               # history carried over exactly


def test_missing_or_damaged_hidden_sheet(tmp_path):
    bp = boundary(tmp_path / "b.gpkg")
    crops = crop_squares()
    r1 = report(tmp_path / "r1.gpkg", crops, harvest_classes(crops, step_rule(400)))
    r2 = report(tmp_path / "r2.gpkg", crops, harvest_classes(crops, step_rule(800)))
    _, _, o1 = run(tmp_path, bp, ["region"], r1, "2026-01-05", "s1.xlsx")
    wb = openpyxl.load_workbook(o1)
    del wb[hs.DATA_SHEET]
    gone = tmp_path / "gone.xlsx"
    wb.save(gone)
    with pytest.raises(pc.UserError, match="not written by this tool or its hidden data sheet was removed"):
        run(tmp_path, bp, ["region"], r2, "2026-01-15", "s2.xlsx", gone)
    wb = openpyxl.load_workbook(o1)
    ws = wb[hs.DATA_SHEET]
    last = ws.max_row
    ws.cell(row=last, column=5).value = "1.0"                         # a number changed by hand
    bad = tmp_path / "bad.xlsx"
    wb.save(bad)
    with pytest.raises(pc.UserError, match="hidden data sheet is damaged"):
        run(tmp_path, bp, ["region"], r2, "2026-01-15", "s2.xlsx", bad)
    plain = tmp_path / "plain.xlsx"
    openpyxl.Workbook().save(plain)
    with pytest.raises(pc.UserError, match="not written by this tool"):
        run(tmp_path, bp, ["region"], r2, "2026-01-15", "s2.xlsx", plain)


@pytest.mark.parametrize("unit", ["hectares", "rai"])
def test_other_units_consistent_with_acres(tmp_path, unit):
    bp = boundary(tmp_path / "b.gpkg")
    crops = crop_squares()
    rp = report(tmp_path / "r.gpkg", crops, harvest_classes(crops, step_rule(700)))
    _, _, oa = run(tmp_path, bp, ["region", "block"], rp, "2026-01-10", "a.xlsx")
    _, txt, ou = run(tmp_path, bp, ["region", "block"], rp, "2026-01-10", "u.xlsx", unit=unit)
    m2 = pc.UNIT_BY_KEY[unit].m2
    _, va = hidden(oa)
    meta, vu = hidden(ou)
    assert meta["unit"] == unit
    for k in va:
        for c, a in va[k]["2026-01-10"].items():
            assert close(a * ACRE / m2, vu[k]["2026-01-10"][c], 1e-9)
    _, rows, wb = stats_table(ou)
    assert wb["Stats"]["A1"].value == f"All areas in {pc.UNIT_BY_KEY[unit].text}. Report date 10-01-2026."
    assert wb["Stats"].cell(row=3, column=3).number_format.startswith("#,##0.000")


# ---------------------------------------------------------------- workbook structure
def chart_refs(path):
    """Every cell range the charts read, as (sheet, range) from the chart XML."""
    import re
    out = []
    with zipfile.ZipFile(path) as z:
        for n in z.namelist():
            if n.startswith("xl/charts/chart"):
                out += re.findall(r"<(?:c:)?f>'?([^'!<]+)'?!([^<]+)</(?:c:)?f>", z.read(n).decode())
    return out


def test_workbook_structure_charts_and_pivot(tmp_path):
    bp, rps, dates, outs, res, txt = chain3(tmp_path)
    wb = openpyxl.load_workbook(outs[-1])
    assert wb.sheetnames == ["Dashboard", "Stats", "Pivot", "Chart data", hs.DATA_SHEET]
    assert wb["Chart data"].sheet_state == "hidden"
    assert [w.title for w in wb.worksheets if w.sheet_state == "visible"] == ["Dashboard", "Stats", "Pivot"]
    assert wb[hs.DATA_SHEET].sheet_state == "veryHidden"
    assert wb.active.title == "Dashboard"
    assert wb.properties.title and wb.properties.subject
    with zipfile.ZipFile(outs[-1]) as z:
        core = z.read("docProps/core.xml").decode()
    assert "creator" not in core and "lastModifiedBy" not in core
    st = wb["Stats"]
    assert st.freeze_panes == "C3" and st.auto_filter.ref.startswith("A2:")
    assert st["A1"].value == "All areas in acres. Report date 25-01-2026."
    assert st.page_setup.orientation == "landscape"
    assert not st.merged_cells.ranges
    refs = chart_refs(outs[-1])
    assert len(refs) >= 8
    cd = wb["Chart data"]
    for sheet, rng in refs:
        assert sheet == "Chart data"
        for part in rng.replace("$", "").split(":"):
            col = "".join(ch for ch in part if ch.isalpha())
            row = int("".join(ch for ch in part if ch.isdigit()))
            assert cd[f"{col}{row}"].value is not None, (rng, part)          # a reference never points at a blank
    with zipfile.ZipFile(outs[-1]) as z:
        names = z.namelist()
        assert "xl/pivotTables/pivotTable1.xml" in names and "xl/pivotCache/pivotCacheDefinition1.xml" in names
        cdef = z.read("xl/pivotCache/pivotCacheDefinition1.xml").decode()
        ptab = z.read("xl/pivotTables/pivotTable1.xml").decode()
        header, rows, _ = stats_table(outs[-1])
        n_val = len(header) - 2 - 4                      # shown: all but the 2 levels and the 4 total columns
        last_col = openpyxl.utils.get_column_letter(len(header))
        assert 'refreshOnLoad="1"' in cdef and 'sheet="Stats"' in cdef
        assert f'ref="A2:{last_col}{2 + len(rows) - 1}"' in cdef          # the table without its Totals row
        assert f'recordCount="{len(rows) - 1}"' in cdef and cdef.count("<cacheField ") == len(header)
        assert ptab.count("<pivotField ") == len(header) and ptab.count('dataField="1"') == n_val     # 4 unticked
        assert "pageField" not in ptab and '<field x="-2"' in ptab and ptab.count("<dataField ") == n_val
        assert 'name="Sum of Total Crop Area"' in ptab and 'rowHeaderCaption="region / block"' in ptab
        assert "r:id" not in ptab.split(">", 1)[0]
        assert "<f>" not in z.read("xl/worksheets/sheet2.xml").decode()       # no formulas anywhere
    pv = wb["Pivot"]
    top = hs.PIVOT_TOP
    assert pv["A1"].value == "Harvest by region and block" and pv.cell(row=top, column=1).value == "region / block"
    heads = [pv.cell(row=top, column=2 + j).value for j in range(n_val)]
    assert heads == [f"Sum of {h}" for h in header[2:-4]]
    assert pv.cell(row=top, column=2 + n_val).value is None
    assert any(sel.activeCell == f"B{top + 1}" for sel in pv.sheet_view.selection)     # inside: field list shows
    body = [r for r in pv.iter_rows(min_row=top + 1, max_col=1 + n_val)]
    grand = [c.value for c in body[-1]]
    assert grand[0] == "Grand Total"
    for j in range(n_val):                               # every column: Stats totals = level-1 rows = finest rows
        assert close(grand[1 + j], rows[-1][2 + j], 1e-9)
        assert close(sum(r[1 + j].value for r in body[:-1] if r[0].font.b), grand[1 + j], 1e-9)
        assert close(sum(r[1 + j].value for r in body[:-1] if not r[0].font.b), grand[1 + j], 1e-9)
    res2, _, o2 = run(tmp_path, bp, ["region", "block"], rps[0], "2026-01-05", "np.xlsx", pivot=False)
    with zipfile.ZipFile(o2) as z:
        assert not any("pivot" in n for n in z.namelist())
    pv2 = openpyxl.load_workbook(o2)["Pivot"]            # the same cells, as a plain table
    assert pv2["A1"].value == "Harvest by region and block" and pv2.cell(row=hs.PIVOT_TOP, column=2).value


def test_one_report_dashboard_note(tmp_path):
    bp = boundary(tmp_path / "b.gpkg")
    crops = crop_squares()
    rp = report(tmp_path / "r.gpkg", crops, harvest_classes(crops, step_rule(700)))
    _, _, out = run(tmp_path, bp, ["region"], rp, "2026-01-10", "s.xlsx")
    d = openpyxl.load_workbook(out)["Dashboard"]
    texts = [c.value for row in d.iter_rows() for c in row if isinstance(c.value, str)]
    assert any(t.startswith("The progress line and the harvest pace appear from the second") for t in texts)
    assert any("First report of the season" in t for t in texts)
    assert len(d._charts) == 1          # one level, one report: the stacked bars per unit


def dash_res(n_dates=3, other=0.0, levels=("region", "block"), outside=0.0, period=None):
    """A hand-made results structure (see hs.dashboard_results) to draw the Dashboard from: reports 10 days
    apart, North 1000 (600 harvested), South 500 (100 harvested)."""
    by1 = [("North", 600.0, 400.0, 1000.0), ("South", 100.0, 400.0, 500.0)]
    if outside:
        by1.append((hs.OUTSIDE, outside / 2, outside / 2, outside))
    total = sum(r[3] for r in by1)
    harv = sum(r[1] for r in by1)
    period = period or [harv / n_dates] * n_dates
    cum = [sum(period[:i + 1]) for i in range(n_dates)]
    days = [None] + [10] * (n_dates - 1)
    return {"unit": {"text": "acres", "header": "ac", "decimals": 3, "format": "#,##0.000"},
            "level_titles": list(levels), "dates": [f"{5 + 10 * i:02d}-01-2026" for i in range(n_dates)],
            "total": total, "harvested": harv, "remaining": total - harv, "other": other,
            "other_codes": ["282"] if other else [], "period": period, "days": days,
            "pace": [None if not n else v / n for v, n in zip(period, days)], "cum_harvested": cum,
            "cum_pct": [c / total for c in cum], "by_level1": by1,
            "top_level2": [("b1 (North)", 300.0), ("b2 (South)", 250.0)] if len(levels) > 1 else [],
            "finest": [(590.0, 600.0), (10.0, 400.0), (100.0, 500.0)],
            "report": "r.shp", "boundary": "b.shp", "previous": None, "projection": "UTM"}


def draw(res):
    wb = openpyxl.Workbook()
    dash, cd = wb.active, wb.create_sheet("Chart data")
    blocks = hs.write_dashboard(dash, cd, res)
    return dash, cd, blocks


def series_ranges(chart):
    return [s.val.numRef.f.replace("$", "").split("!")[1] for s in chart.series]


def test_dashboard_tiles_table_and_charts():
    res = dash_res(period=[300.0, 200.0, 200.0])
    dash, cd, blocks = draw(res)
    assert [dash.cell(row=7, column=c).value for c in (2, 4, 6, 8, 10)] == [1500.0, 700.0, 800.0, 20.0, "06-03-2026"]
    assert dash["D8"].value == "46.7 % of the crop" and dash["F8"].value == "53.3 % of the crop"
    assert dash["H8"].value == "acres per day over 10 days"          # 200 in 10 days = 20 a day
    assert dash["J8"].value == "about 40 days at the latest pace"    # 800 left / 20 a day; 25 Jan + 40 days
    for r in (6, 7, 8):                                 # no text leaves its tile
        assert all(dash.cell(row=r, column=c).alignment.shrink_to_fit for c in (2, 4, 6, 8, 10))
    assert len(dash._charts) == 4                       # progress, pace, area, top
    a0, a1 = blocks["a"]
    b0, b1 = blocks["b"]
    assert series_ranges(dash._charts[0]) == [f"B{b0}:B{b1}"]
    assert series_ranges(dash._charts[1]) == [f"F{b0 + 1}:F{b1}"]          # pace: no value for the first report
    assert cd.cell(row=b0, column=6).value is None and cd.cell(row=b1, column=6).value == 20.0
    assert series_ranges(dash._charts[2]) == [f"B{a0}:B{a1}", f"C{a0}:C{a1}"]
    assert close(sum(cd.cell(row=r, column=6).value for r in range(a0, a1 + 1)), 1.0, 1e-12)   # shares of remaining
    rows = {r[1].value: r for r in dash.iter_rows(min_row=20) if r[1].value in ("North", "South", "Totals")}
    assert [c.value for c in rows["North"][3:10:2]] == [1000.0, 400.0, 0.6, 0.5]
    assert [c.value for c in rows["Totals"][3:10:2]] == [1500.0, 800.0, 700.0 / 1500.0, 1.0]
    bars = {str(cf.sqref): cf.rules[0].type for cf in dash.conditional_formatting}
    n, t = rows["North"][0].row, rows["Totals"][0].row
    assert bars == {f"H{n}:H{t}": "dataBar", f"J{n}:J{t - 1}": "dataBar"}
    assert not dash.merged_cells.ranges


def test_key_findings_say_what_the_numbers_mean():
    f = hs.key_findings(dash_res(period=[300.0, 200.0, 200.0]))
    assert f[0] == "46.7 % of the crop is harvested (700.000 of 1,500.000 acres); 800.000 acres remain on 25-01-2026."
    assert f[1] == ("Latest period (15-01-2026 to 25-01-2026, 10 days): 200.000 acres harvested, 20.0 acres per "
                    "day, about the same pace as the period before (20.0).")
    assert f[2] == ("At the pace of the latest period the remaining crop would be harvested in about 40 days, "
                    "around 06-03-2026 (an estimate, not a plan).")
    assert f[3] == "Where the work is left: North holds 50 % of the remaining crop (400.000 acres)."
    assert f[4] == "Most advanced region: North (60.0 % harvested). Least advanced: South (20.0 %)."
    assert f[5] == "Of the 3 block units with crop, 1 is at least 95 % harvested and 2 are below 50 %."
    assert len(f) == 6
    faster = hs.key_findings(dash_res(period=[400.0, 100.0, 200.0]))[1]
    assert faster.endswith("20.0 acres per day, 100 % faster than the period before (10.0).")
    slower = hs.key_findings(dash_res(period=[100.0, 400.0, 200.0]))[1]
    assert slower.endswith("20.0 acres per day, 50 % slower than the period before (40.0).")
    stopped = dash_res(period=[400.0, 300.0, 0.0])
    assert hs.forecast(stopped) is None
    assert "No harvest was measured in the latest period" in " ".join(hs.key_findings(stopped))
    back = hs.key_findings(dash_res(period=[400.0, 350.0, -50.0]))
    assert any(t.startswith("Check: the harvested area went down in 1 period") for t in back)
    full = dash_res()
    full.update(harvested=1500.0, remaining=0.0, by_level1=[("North", 1000.0, 0.0, 1000.0), ("South", 500.0, 0.0, 500.0)])
    assert hs.key_findings(full)[0].startswith("Harvest is complete") and hs.forecast(full) is None
    dash, _, _ = draw(full)
    assert dash["J7"].value == "Complete"


def test_dashboard_other_classes_one_report_and_outside_trace():
    res = dash_res(n_dates=1, other=50.0, levels=("region",), outside=0.02)
    dash, cd, blocks = draw(res)
    assert dash["H6"].value == "HARVEST PACE" and dash["H7"].value == "-" and dash["J7"].value == "-"
    assert dash["H8"].value == dash["J8"].value == "known from the second report"
    texts = [c.value for row in dash.iter_rows() for c in row if isinstance(c.value, str)]
    assert "-  The remaining crop includes 50.000 acres of class 282 (counted as not harvested)." in texts
    assert not any("Latest period" in t or "At the pace" in t for t in texts)      # one report: no pace, no forecast
    assert len(dash._charts) == 1                       # area only: no progress, no pace, no top
    a0, a1 = blocks["a"]
    assert a1 - a0 + 1 == 3                             # the outside trace is written ...
    assert series_ranges(dash._charts[0]) == [f"B{a0}:B{a1 - 1}", f"C{a0}:C{a1 - 1}"]     # ... but not charted
    names = [r[1].value for r in dash.iter_rows(min_row=12)]
    assert hs.OUTSIDE in names and "Totals" in names    # and it stays in the table, so the rows add up
    big = dash_res(outside=40.0)
    dash2, _, b2 = draw(big)
    assert series_ranges(dash2._charts[2])[0] == f"B{b2['a'][0]}:B{b2['a'][1]}"            # real area: charted


AWKWARD = ["Area / Zone: [main]?", "a field name that is longer than thirty-one characters", "Zone", "zone*",
           "name", "name_2"]


def test_awkward_field_names_and_titles(tmp_path):
    geoms = UNIT_BOXES
    fields = {AWKWARD[0]: ["N\\W", "N\\W", "S*E", "S*E"], AWKWARD[1]: ["Block 1", "Block 2", "Block 1", "Block 2"],
              AWKWARD[2]: ["a", "a", "b", "b"], AWKWARD[3]: ["x", "y", "x", "y"], AWKWARD[4]: ["p", "q", "p", "q"],
              AWKWARD[5]: ["=1+1", "@x", "-y", "+z"]}
    bp = write(tmp_path / "b.geojson", geoms, fields, driver="GeoJSON")
    crops = crop_squares()
    rp = report(tmp_path / "r.gpkg", crops, harvest_classes(crops, step_rule(700)))
    res, txt, out = run(tmp_path, bp, [AWKWARD[0], AWKWARD[1], AWKWARD[5]], rp, "2026-01-10", "s.xlsx")
    wb = openpyxl.load_workbook(out)
    for n in wb.sheetnames:
        assert len(n) <= 31 and not any(ch in n for ch in "[]:*?/\\")
    assert len({n.casefold() for n in wb.sheetnames}) == len(wb.sheetnames)
    header, rows, _ = stats_table(out)
    assert header[:3] == (AWKWARD[0], AWKWARD[1], AWKWARD[5])
    assert "=1+1" in [r[2] for r in rows]                                # text, not a formula
    with zipfile.ZipFile(out) as z:
        assert all("<f>" not in z.read(n).decode() for n in z.namelist() if n.startswith("xl/worksheets/"))
    res2, txt2, out2 = run(tmp_path, bp, [AWKWARD[2], AWKWARD[3]], rp, "2026-01-10", "t.xlsx",
                           titles=["Zone title", "Sub: unit / part"])
    wb2 = openpyxl.load_workbook(out2)
    assert wb2["Pivot"]["A1"].value == "Harvest by Zone title and Sub: unit / part"
    h2, _, _ = stats_table(out2)
    assert h2[:2] == ("Zone title", "Sub: unit / part")
    meta, _ = hidden(out2)
    assert meta["levels"] == [{"field": "Zone", "title": "Zone title"}, {"field": "zone*", "title": "Sub: unit / part"}]
    res3, txt3, out3 = run(tmp_path, bp, ["name", "name_2"], rp, "2026-01-10", "u.xlsx", titles=["Area", "Measure"])
    h3, _, _ = stats_table(out3)
    assert len({str(h).casefold() for h in h3}) == len(h3)                 # unique table headers


@pytest.mark.parametrize("fmt", [".shp", ".gpkg", ".geojson"])
def test_input_formats(tmp_path, fmt):
    bp = boundary(tmp_path / ("b" + fmt))
    crops = crop_squares()
    rp = report(tmp_path / ("r" + fmt), crops, harvest_classes(crops, step_rule(700)))
    res, txt, out = run(tmp_path, bp, ["region", "block"], rp, "2026-01-10", "s.xlsx")
    ref, ref_out = reference(bp, ["region", "block"], rp)
    _, values = hidden(out)
    assert_matches(values, "2026-01-10", ref, ref_out)


def test_wgs84_inputs_projected(tmp_path):
    import pyproj
    tr = pyproj.Transformer.from_crs(CRS, 4326, always_xy=True)

    def to_ll(gs):
        out = []
        for g in gs:
            xy = shapely.get_coordinates(g)
            x, y = tr.transform(xy[:, 0], xy[:, 1])
            out.append(shapely.set_coordinates(g, np.column_stack([x, y])))
        return out
    bp = write(tmp_path / "b.geojson", to_ll(UNIT_BOXES), {"region": LEVEL1, "block": LEVEL2}, crs="EPSG:4326",
               driver="GeoJSON")
    crops = crop_squares()
    rp = report(tmp_path / "r.shp", to_ll(crops), harvest_classes(crops, step_rule(700)), crs="EPSG:4326")
    res, txt, out = run(tmp_path, bp, ["region", "block"], rp, "2026-01-10", "s.xlsx", utm=None)
    assert "UTM zone 31N" in txt
    assert close(res["file_total"], sum(g.area for g in crops) / ACRE, 1e-3)


# ---------------------------------------------------------------- command line and questions
def cli(argv):
    buf, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        try:
            hs.main(argv)
        except SystemExit as e:
            code = e.code
    return code, buf.getvalue() + err.getvalue()


def test_cli_flags_and_errors(tmp_path):
    bp = boundary(tmp_path / "b.gpkg")
    crops = crop_squares()
    rp = report(tmp_path / "rep_2026-01-10.gpkg", crops, harvest_classes(crops, step_rule(700)))
    code, txt = cli(["--boundary", str(bp), "--levels", "region,block", "--level-titles", "Region,Block",
                     "--report", str(rp)])
    assert code == 0, txt
    out = tmp_path / "rep_2026-01-10_stats.xlsx"
    assert out.exists() and "Report of 10-01-2026" in txt
    meta, _ = hidden(out)
    assert meta["levels"][0] == {"field": "region", "title": "Region"}
    rp2 = report(tmp_path / "rep_2026-01-20.gpkg", crops, harvest_classes(crops, step_rule(900)))
    code, txt = cli(["--boundary", str(bp), "--levels", "region,block", "--report", str(rp2), "--previous", str(out)])
    assert code == 0, txt
    meta2, _ = hidden(tmp_path / "rep_2026-01-20_stats.xlsx")
    assert [x["title"] for x in meta2["levels"]] == ["Region", "Block"]         # titles default to the previous
    code, txt = cli(["--boundary", str(bp), "--levels", "nofield", "--report", str(rp2), "--out",
                     str(tmp_path / "e.xlsx")])
    assert code == 2 and "ERROR: the boundary layer b.gpkg has no field 'nofield'" in txt
    code, txt = cli(["--boundary", str(bp), "--levels", "region,region", "--report", str(rp2), "--out",
                     str(tmp_path / "e.xlsx")])
    assert code == 2 and "chosen twice" in txt
    code, txt = cli(["--boundary", str(bp), "--levels", "region", "--level-titles", "A,B", "--report", str(rp2)])
    assert code == 2 and "--level-titles has 2 titles for 1 levels" in txt
    code, txt = cli(["--boundary", str(bp), "--levels", "region", "--report", str(rp2), "--out", str(tmp_path / "e.csv")])
    assert code == 2 and "--out must end with .xlsx" in txt
    code, txt = cli(["--boundary", str(bp), "--levels", "region", "--report", str(rp2), "--date", "31-02-2026"])
    assert code == 2 and "not a real date" in txt


def ask(answers, argv=None):
    old = sys.stdin
    sys.stdin = io.StringIO("".join(a + "\n" for a in answers))
    try:
        return cli(argv or [])
    finally:
        sys.stdin = old


def test_question_mode(tmp_path):
    bp = boundary(tmp_path / "b.gpkg", three=True)
    crops = crop_squares()
    rp = report(tmp_path / "Harvest_2026-01-10_x.gpkg", crops, harvest_classes(crops, step_rule(700)), field="cls")
    answers = [str(bp), "2", "1", "", "block", "Block title",    # 2 levels: region (by number), block (by name)
               str(rp), "1",                                    # no 'predicted': pick the class field from the list
               "",                                              # date from the file name
               "",                                              # first report
               "EPSG:32631", "", "", "", ""]                    # projection, unit, folder, name, Start
    code, txt = ask(answers)
    assert code == 0, txt
    assert "1) region (2 values, e.g. North, South)" in txt
    assert "2) block (2 values, e.g. Block 1, Block 2)" in txt
    assert "3) part (2 values, e.g. Part A, Part B)" in txt
    assert "2) How many levels do you want statistics for? [1]:" in txt
    assert "Field for level 1 (the largest areas):" in txt and "Field for level 2 (inside level 1):" in txt
    assert "Column title for this level [region]:" in txt and "Column title for this level [block]:" in txt
    assert "Report date (YYYY-MM-DD or DD-MM-YYYY) [2026-01-10]:" in txt
    assert "Workbook of the previous report (Enter = this is the first report):" in txt
    out = tmp_path / "Harvest_2026-01-10_x_stats.xlsx"
    assert out.exists(), txt
    h, rows, _ = stats_table(out)
    assert h[:2] == ("region", "Block title")
    # second report: the previous run's answers are the defaults, the last workbook is offered as previous
    rp2 = report(tmp_path / "Harvest_2026-01-20_x.gpkg", crops, harvest_classes(crops, step_rule(900)), field="cls")
    # ('n' at "Add the next report to this season?" asks for all the questions again)
    answers = ["n", str(bp), "", "", "", "", "", str(rp2), "", "", "", "", "", "", "", ""]
    code, txt = ask(answers)
    assert code == 0, txt
    assert "This folder already holds a season:" in txt and "A previous run was found:" in txt
    assert f"Workbook of the previous report (type 0 if this is the first report) [{out}]:" in txt
    assert "Enter keeps it (it must stay the same)" in txt
    meta, _ = hidden(tmp_path / "Harvest_2026-01-20_x_stats.xlsx")
    assert [r["date"] for r in meta["reports"]] == ["2026-01-10", "2026-01-20"]
    assert [x["title"] for x in meta["levels"]] == ["region", "Block title"]


def test_question_mode_rejects_wrong_answers(tmp_path):
    bp = boundary(tmp_path / "b.gpkg")
    crops = crop_squares()
    rp = report(tmp_path / "r.gpkg", crops, harvest_classes(crops, step_rule(700)))
    answers = [str(bp), "5", "2", "1", "", "1", "2", "", str(rp), "2026-13-01", "12-01-2026",
               str(tmp_path / "missing.xlsx"), "", "EPSG:32631", "", "", "", ""]
    code, txt = ask(answers)
    assert code == 0, txt
    assert "Type a number from 1 to 2." in txt
    assert "region is already level 1. Choose another field." in txt
    assert "is not a real date" in txt
    assert "Previous workbook not found" in txt
    assert (tmp_path / "r_stats.xlsx").exists()


def test_last_run_record_and_state_folder(tmp_path, _state_dir):
    bp = boundary(tmp_path / "b.gpkg")
    crops = crop_squares()
    rp = report(tmp_path / "r.gpkg", crops, harvest_classes(crops, step_rule(700)))
    run(tmp_path, bp, ["region", "block"], rp, "2026-01-10", "s.xlsx", titles=["R", "B"])
    rec = json.loads((Path(os.environ["HARVEST_TOOLS_STATE_DIR"]) / "last_run_stats.json").read_text())
    assert rec["status"] == "finished" and rec["levels"][1] == {"field": "block", "title": "B"}
    assert rec["out"].endswith("s.xlsx") and rec["unit"] == "acres"


def test_output_open_in_excel_stops_early(tmp_path):
    bp = boundary(tmp_path / "b.gpkg")
    crops = crop_squares()
    rp = report(tmp_path / "r.gpkg", crops, harvest_classes(crops, step_rule(700)))
    (tmp_path / "~$s.xlsx").write_bytes(b"x")
    with pytest.raises(pc.UserError, match="is open in another program"):
        run(tmp_path, bp, ["region"], rp, "2026-01-10", "s.xlsx")
