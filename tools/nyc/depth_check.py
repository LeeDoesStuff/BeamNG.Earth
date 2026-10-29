"""Real-data check: street-level abutments vs the Prospect Expressway under each street bridge (DEM, the server's)."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "server"))
import dem as demmod
import grading
import mapcells
import mapdata
import netfetch
import roads

CACHE = os.path.join(os.path.expanduser("~"), ".cache", "beamng-earth")
net = netfetch.Fetcher(offline=False)
md = mapdata.MapData(CACHE, fetcher=net)
dem = demmod.Dem(os.path.join(CACHE, "dem"), fetcher=net)
z = lambda la, lo: np.asarray(dem.sample(np.atleast_1d(la), np.atleast_1d(lo), grading.PROFILE_ZOOM), float)
lat, lon = 40.6476, -73.9770
key = mapcells.cells_for_bbox(lat - 1e-4, lon - 1e-4, lat + 1e-4, lon + 1e-4)[0]
f = md.cell(key, layers=("roads",), kind="demand").layers.get("roads")
names = f.attrs["name"] if "name" in f.attrs else [""] * len(f.kind)
cls = f.attrs["class"]
low = [i for i in range(len(f.kind)) if str(names[i]) == "Prospect Expressway" and roads.bridge_specs(f)[i] is None]
low_ll = np.concatenate([np.asarray(p, float) for i in low for p in roads.ground_parts(f, i)])
need = roads.DECK_ROAD_CLEAR
print(f"deck top needs {need:.2f} m over the road under it (headroom {roads.MIN_BRIDGE_HEADROOM_M} m); "
      f"was {6.0 + roads.BRIDGE_HEADROOM_MARGIN_M + 2.2:.2f} m")
for i, bs in enumerate(roads.bridge_specs(f)):
    if bs is None or str(cls[i]) in ("footway", "cycleway", "path") or str(names[i]) == "Prospect Expressway":
        continue
    for part in roads.bridge_parts(f, i):
        part = np.asarray(part, float)
        ends = z(part[[0, -1], 1], part[[0, -1], 0])
        mid = part[len(part) // 2]
        d = np.hypot((low_ll[:, 0] - mid[0]) * 84000, (low_ll[:, 1] - mid[1]) * 111000)
        if d.min() > 25:
            continue
        under = float(z(low_ll[d.argmin(), 1], low_ll[d.argmin(), 0])[0])
        depth = float(ends.mean()) - under
        print(f"  {str(names[i]) or cls[i]:24s} abutments {ends.mean():6.1f} m, expressway {under:6.1f} m: "
              f"depth {depth:4.1f} m -> deck {'fits' if depth >= need else f'{need - depth:.1f} m over the street'}")
