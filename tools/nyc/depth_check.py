"""Real-data check: street-level abutments vs the Prospect Expressway under each street bridge, on the server's DEM
and on its graded ground (grading.Grader with the land cover's urban function, as earthd's: the town cut)."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "server"))
import dem as demmod
import geodesy
import grading
import mapcells
import mapdata
import netfetch
import roads
import surface

CACHE = os.path.join(os.path.expanduser("~"), ".cache", "beamng-earth")
net = netfetch.Fetcher(offline=False)
md = mapdata.MapData(CACHE, fetcher=net)
dem = demmod.Dem(os.path.join(CACHE, "dem"), fetcher=net)
z = lambda la, lo: np.asarray(dem.sample(np.atleast_1d(la), np.atleast_1d(lo), grading.PROFILE_ZOOM), float)
lat, lon = 40.6476, -73.9770
key = mapcells.cells_for_bbox(lat - 1e-4, lon - 1e-4, lat + 1e-4, lon + 1e-4)[0]
f = md.cell(key, layers=("roads",), kind="demand").layers.get("roads")
sf = surface.Surface(md)
sf.classes(lat, lon)                    # (the land cover's cells, as the server's prefetch would have them)
md.cell(key, layers=("landcover",), kind="demand")
frame = geodesy.EnuFrame(lat, lon)
grader = grading.Grader(frame, md, z, urban=sf.urban)
graded = lambda la, lo: grader.apply(np.atleast_1d(la), np.atleast_1d(lo), z(la, lo))
# the decks as the road tiles solve them (roads.BridgeProfiles on the graded ground: chains whole, so a part's inner
# joint over the trench is no abutment)
bridges = roads.BridgeProfiles(frame, lambda k: md.cell(k, layers=("roads",), kind="demand").layers.get("roads"), graded)
decks = {}
for fid, _spec, xy, dz, _lon in bridges.parts([f], (-1500, -1500, 1500, 1500)):
    decks.setdefault(fid, []).append((np.asarray(xy, float), np.asarray(dz, float)))


def deck_z(fid, lonlat):
    """The solved deck's top at lon/lat (its nearest profile point), or None."""
    e, n, _ = frame.geodetic_to_enu(lonlat[1], lonlat[0], 0.0)
    best = None
    for xy, dz in decks.get(fid, ()):
        d = np.hypot(xy[:, 0] - float(e), xy[:, 1] - float(n))
        if best is None or d.min() < best[0]:
            best = (float(d.min()), float(dz[d.argmin()]))
    return None if best is None or best[0] > 5.0 else best[1]
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
        g_under = float(graded(low_ll[d.argmin(), 1], low_ll[d.argmin(), 0])[0])
        dz = [deck_z(str(f.ids[i]), p) for p in (part[0], mid, part[-1])]
        if any(v is None for v in dz):
            print(f"  {'graded':24s} expressway {g_under:6.1f} m; no deck solved")
            continue
        # the street 12 m past each end of the part along its line (a joint with the next part of the chain is over the
        # trench: that end's street is the trench floor)
        u = (part[-1] - part[0]) / max(float(np.hypot((part[-1, 0] - part[0, 0]) * 84000, (part[-1, 1] - part[0, 1]) * 111000)), 1e-9)
        beyond = np.array([part[0] - u * 12.0, part[-1] + u * 12.0])
        g_street = graded(beyond[:, 1], beyond[:, 0])
        hump = max(dz[0] - g_street[0], dz[-1] - g_street[1])
        print(f"  {'graded':24s} deck top {dz[0]:5.1f}/{dz[1]:5.1f}/{dz[-1]:5.1f} m, expressway {g_under:6.1f} m: "
              f"clear {dz[1] - g_under:4.1f} m ({'fits' if dz[1] - g_under >= need - 0.05 else 'short'}); "
              f"deck ends {hump:+.1f} m against the street 12 m on")
