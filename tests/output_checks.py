"""Shared assertion for every file the tools write (imported by the end-to-end tests).

assert_clean_output(path): the layer type is Polygon (2D), every feature is one non-empty valid
Polygon without repeated consecutive vertices or Z values, and no two polygons share area."""
import warnings

import numpy as np
import pyogrio
import pyogrio.raw
import shapely
from shapely import STRtree


def assert_clean_output(path, min_area=1e-12):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        info = pyogrio.read_info(str(path))
        _, _, wkb, _ = pyogrio.raw.read(str(path), columns=[])
    assert info["geometry_type"] == "Polygon", (path, info["geometry_type"])
    g = shapely.from_wkb(wkb) if len(wkb) else np.array([], dtype=object)
    assert not shapely.is_missing(g).any(), "null geometry"
    assert not shapely.is_empty(g).any(), "empty geometry"
    assert (shapely.get_type_id(g) == 3).all(), "not all single Polygons"
    assert not shapely.has_z(g).any(), "Z values"
    assert shapely.is_valid(g).all(), shapely.is_valid_reason(g[~shapely.is_valid(g)])[:3]
    assert (shapely.area(g) > 0).all(), "zero area"
    nc = shapely.get_num_coordinates(g)
    assert (shapely.get_num_coordinates(shapely.remove_repeated_points(g, 0.0)) == nc).all(), "repeated vertices"
    if len(g) > 1:
        a, b = STRtree(g).query(g)
        m = a < b
        a, b = a[m], b[m]
        if len(a):
            inter = shapely.area(shapely.intersection(g[a], g[b]))
            assert (inter <= min_area).all(), f"{int((inter > min_area).sum())} overlapping pairs"
    return len(g)
