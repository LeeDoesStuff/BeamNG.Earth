"""Real-data check of the town cut (grading.TOWN_CUT_ON), off and on, on the graded ground the game drives:
  (1) every deck part's free end (an abutment: no other part's end within 0.75 m) within 900 m of --at: the deck's
      top there, the graded street minus the deck and the lift (graded minus the raw DEM: the made-up ramp) 2, 12, 40,
      80 and 120 m past it along the part's last segment. A lift at 2 m only is the abutment's fill over the DEM's
      smoothed trench wall; at 12 m and on it is a ramp you drive over;
  (2) the --motorway's ground parts, every 2 m: max grade, max vertical curvature over a 20 m window (1/m; times v^2 is
      the load: 0.003 at 80 km/h is ~0.15 g; the DEM's own in []), lowest below the DEM, and on minus off.
Without TOWN_CUT_ON (main before it) both runs are the same code.
python tools/nyc/approach_check.py [--at 40.6476,-73.9770] [--bbox 40.640,-73.995,40.668,-73.970]"""
import argparse
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

ap = argparse.ArgumentParser()
ap.add_argument("--at", default="40.6476,-73.9770", help="lat,lon: the frame's origin; ends within 900 m of it")
ap.add_argument("--bbox", default="40.640,-73.995,40.668,-73.970", help="lat0,lon0,lat1,lon1: the map cells read")
ap.add_argument("--motorway", default="Prospect Expressway", help="name of the road whose profile is printed")
args = ap.parse_args()
lat, lon = (float(v) for v in args.at.split(","))
bbox = [float(v) for v in args.bbox.split(",")]

CACHE = os.path.join(os.path.expanduser("~"), ".cache", "beamng-earth")
net = netfetch.Fetcher(offline=False)
md = mapdata.MapData(CACHE, fetcher=net)
dem = demmod.Dem(os.path.join(CACHE, "dem"), fetcher=net)
frame = geodesy.EnuFrame(lat, lon)
sf = surface.Surface(md)
keys = sorted(set(mapcells.cells_for_bbox(*bbox)))
AT = (2.0, 12.0, 40.0, 80.0, 120.0)


def z(la, lo):
    return np.asarray(dem.sample(np.atleast_1d(la), np.atleast_1d(lo), grading.PROFILE_ZOOM), float)


def cell(k):
    return md.cell(k, layers=("roads",), kind="demand").layers.get("roads")


for k in keys:                          # (the land cover's cells, as the server's prefetch would have them)
    md.cell(k, layers=("landcover",), kind="demand")
sf.classes(lat, lon)
feats = [f for f in (cell(k) for k in keys) if f is not None and len(f)]
label = {}
for f in feats:
    for i in range(len(f.kind)):
        nm = str(f.attrs["name"][i]) if "name" in f.attrs else ""
        label.setdefault(str(f.ids[i]), f"{str(f.attrs['class'][i])[:10]:10s} {nm[:24]:24s}")


def to_ll(xy):
    la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
    return np.asarray(la, float), np.asarray(lo, float)


def enu(ll):
    e, n, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], np.zeros(len(ll)))
    return np.c_[e, n]


def resample(xy, step=2.0):
    s = np.r_[0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    t = np.arange(0, s[-1], step)
    return np.c_[np.interp(t, s, xy[:, 0]), np.interp(t, s, xy[:, 1])]


def run(on):
    grading.TOWN_CUT_ON = on
    g = grading.Grader(frame, md, z, urban=sf.urban)

    def graded(xy):
        return g.apply(*to_ll(xy), z(*to_ll(xy)))

    def graded_ll(la, lo):
        return g.apply(np.atleast_1d(la), np.atleast_1d(lo), z(la, lo))
    bp = roads.BridgeProfiles(frame, cell, graded_ll)
    parts = []
    for f in feats:
        for fid, _sp, xy, dz, _lo in bp.parts([f], (-1500, -1500, 1500, 1500)):
            parts.append((str(fid), np.asarray(xy, float)[:, :2], np.asarray(dz, float)))
    ends = np.array([[xy[0], xy[-1]] for _f, xy, _d in parts]).reshape(-1, 2)
    out = {}
    for n, (fid, xy, dz) in enumerate(parts):
        for e, (p, q, zd) in enumerate(((xy[0], xy[1], dz[0]), (xy[-1], xy[-2], dz[-1]))):
            d = np.hypot(*(ends - p).T)
            d[2 * n + e] = np.inf
            if (d < 0.75).any() or np.hypot(*p) > 900:
                continue                        # a joint with another part (not an abutment), or out of range
            u = (p - q) / max(np.hypot(*(p - q)), 1e-9)
            pts = np.array([p + u * t for t in AT])
            zg = graded(pts)
            out[(fid, tuple(np.round(p, 0)))] = (float(zd), zg - zd, zg - z(*to_ll(pts)))
    prof = {}
    for f in feats:
        for i in range(len(f.kind)):
            nm = str(f.attrs["name"][i]) if "name" in f.attrs else ""
            if nm != args.motorway or roads.bridge_specs(f)[i] is not None:
                continue
            for part in roads.ground_parts(f, i):
                xy = resample(enu(np.asarray(part, float)))
                if len(xy) > 20:
                    prof.setdefault((str(f.ids[i]), len(xy)), (graded(xy), z(*to_ll(xy))))
    return out, prof


def fmt(v):
    return " ".join(f"{x:+5.1f}" for x in v)


def stats(v, zd):
    c2 = np.abs(v[20:] - 2 * v[10:-10] + v[:-20]) / 400.0
    return np.abs(np.diff(v)).max() / 2.0 * 100, c2.max(), float((v - zd).min())


has_cut = hasattr(grading, "TOWN_CUT_ON")
res = {on: run(on) for on in ((False, True) if has_cut else (False,))}
print(f"deck free ends within 900 m of {lat},{lon} (town cut {'off / on' if has_cut else 'not in this code'}): "
      f"deck top m | street - deck | lift over DEM, at {'/'.join(f'{t:g}' for t in AT)} m")
for k in sorted(res[False][0], key=lambda k: (label.get(k[0], ""), k)):
    for on in res:
        got = res[on][0].get(k)
        if got is not None:
            print(f"  {k[0][:8]} {label.get(k[0], ''):35s} {'on ' if on else 'off'} {got[0]:6.2f} | {fmt(got[1])} | "
                  f"{fmt(got[2])}")
print(f"{args.motorway} ground parts: max grade %, max curvature over 20 m 1/m [DEM's], lowest below DEM m")
rows = []
for k, (zg, zd) in res[max(res)][1].items():
    a, dc = stats(zg, zd), stats(zd, zd)[1]
    zo = res[False][1].get(k, (None,))[0]
    b = stats(zo, zd) if zo is not None and has_cut else None
    rows.append((a[1], k, a, dc, b, float((zg - zo).min()) if b else None))
for _c, k, a, dc, b, dip in sorted(rows, key=lambda r: -r[0])[:10]:
    s = f"  {k[0][:8]} {k[1] * 2:5d} m  grade {a[0]:4.1f}% curv {a[1]:.4f} [{dc:.4f}] below {a[2]:6.2f}"
    if b:
        s += f" | off: grade {b[0]:4.1f}% curv {b[1]:.4f} below {b[2]:6.2f} | on - off min {dip:+.2f}"
    print(s)
