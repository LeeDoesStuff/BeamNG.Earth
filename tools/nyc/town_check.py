"""Real-data check: are Prospect Expressway's carriageways 'urban' (town walls) with the server's own land cover?"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "server"))
import grading
import mapcells
import mapdata
import netfetch
import roads
import surface

CACHE = os.path.join(os.path.expanduser("~"), ".cache", "beamng-earth")
md = mapdata.MapData(CACHE, fetcher=netfetch.Fetcher(offline=False))
sf = surface.Surface(md)
for lat, lon in ((40.6476, -73.9770), (40.66556, -73.99318)):
    key = mapcells.cells_for_bbox(lat - 1e-4, lon - 1e-4, lat + 1e-4, lon + 1e-4)[0]
    f = md.cell(key, layers=("roads",), kind="demand").layers.get("roads")
    # the land cover the urban check reads (its cells, fetched as the server's prefetch would)
    sf.classes(lat, lon)
    md.cell(key, layers=("landcover",), kind="demand")
    rural = roads.road_specs(f, None, None)
    g = grading.Grader(None, md, None)
    g.urban = sf.urban
    town = g._town_walls(f, rural)
    seen = {}
    for i, (r, t) in enumerate(zip(rural, town)):
        if r is None or r.cls not in roads.CONCRETE_WALL_CLASSES:
            continue
        name = str(f.attrs.get("name", [""] * len(f.kind))[i]) if "name" in f.attrs else ""
        k = (r.cls, name)
        a = seen.setdefault(k, [0, 0])
        a[0] += 1
        a[1] += grading.wall_max_h(t) == grading.WALL_MAX_H_CITY_M
    print(f"cell {key} at {lat},{lon}:")
    for (cls, name), (n, city) in sorted(seen.items()):
        print(f"  {cls:14s} {name[:30]:30s} {city}/{n} get the {grading.WALL_MAX_H_CITY_M:.0f} m town wall")
