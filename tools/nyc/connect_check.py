"""Real-data check that the roads connect and make sense, on the graded ground and the solved decks the game drives
(grading.Grader, roads.BridgeProfiles, as approach_check builds them), town cut on (and off with --compare):
  wall      a 2 m stretch of a road steeper than WALL_GRADE, or a jump of more than WALL_STEP_M in 1 m
  step      a jump in 1 m of more than STEP_M beyond the road's own grade there (a lip you feel)
  deck      where a deck's free end meets its road: graded street - deck over DECK_STEP_M; or a free end no ground road
            leads onto (APPROACH_TOUCH_M: a deck in the air or a road dropped short of it)
  gap       the map joins roads at a node (their ends within NODE_TOL) and one of them is drawn nowhere near it (no
            ground part, deck or tunnel end): a dropped road, or a hole; and drivable map roads with no spec at all
  building  a road's centreline through a building's footprint (--buildings)
  hump      a deck end over the line of its street's own ground (the DEM HUMP_FIT_M past the end, extrapolated to it) by
            more than HUMP_M: the made-up ramp onto a bridge (approach_check's lift at 12 m reads the DEM's smoothed
            trench wall as the street where Overture's bridge range ends short: Fort Hamilton Parkway, 2026-10-29)
  hill      a city street (CITY) lifted more than HILL_M over the DEM farther than ABUT_SKIP_M from any deck end, or its
            graded grade over 20 m more than HILL_GRADE over the DEM's own there with a lift of LIFT_MIN_M
  curve     vertical curvature over 20 m (1/m, as approach_check) over CURV_CITY on a city street or CURV_FAST on a
            motorway/trunk and over 1.5 times the DEM's own (times v^2 is the load: 0.01 at 40 km/h is ~0.12 g)
  junction  the planned heights of the roads meeting at a node spread over JUNCTION_M (the ground blends them: a slope)
  ramp      a motorway/trunk link beside its mainline (carriageways overlapping, within RAMP_REACH_M of where it
            leaves) planned more than RAMP_M off the mainline's height there
  divided   a divided road's two one-way carriageways (same name, parallel, centre lines within PAIR_M) graded more than
            PAIR_DZ_M apart beyond what the DEM has between them
Each failure: kind, value, class, name, feature id, lat/lon. Region: --bbox, or --corridor (the roads within --near m
of the named road, in its bbox). --save writes the failures as JSON; --against compares with a saved run (new / fixed).
python tools/nyc/connect_check.py --corridor prospect [--compare] [--buildings] [--save out.json] [--against base.json]
python tools/nyc/connect_check.py --at 40.6476,-73.9770 --bbox 40.640,-73.995,40.668,-73.970
--server PATH: another tree's server/ (main's, for "no new failure against main")."""
import argparse
import json
import math
import os
import sys
from collections import defaultdict

import numpy as np

ap = argparse.ArgumentParser()
ap.add_argument("--corridor", help="a name in CORRIDORS (or 'list')")
ap.add_argument("--at", help="lat,lon: the frame's origin (default: the bbox's centre)")
ap.add_argument("--bbox", help="lat0,lon0,lat1,lon1: the map cells read and the region checked")
ap.add_argument("--near", type=float, default=80.0, help="m: with --corridor, the roads this close to it are checked")
ap.add_argument("--compare", action="store_true", help="also with the town cut off, and the difference")
ap.add_argument("--buildings", action="store_true", help="also roads through buildings (reads the buildings layer)")
ap.add_argument("--service", action="store_true", help="also service roads (left out: lots, alleys, drive-throughs)")
ap.add_argument("--save", help="write the failures (town cut on) to this JSON file")
ap.add_argument("--against", help="a --save file to compare with: new and fixed failures")
ap.add_argument("--server", help="the server/ directory to import (default: this tree's)")
ap.add_argument("--max", type=int, default=60, help="failures listed per kind")
args = ap.parse_args()

sys.path.insert(0, os.path.abspath(args.server) if args.server else
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "server"))
import dem as demmod        # noqa: E402
import geodesy              # noqa: E402
import grading              # noqa: E402
import mapcells             # noqa: E402
import mapdata              # noqa: E402
import netfetch             # noqa: E402
import roads                # noqa: E402
import surface              # noqa: E402

# name aliases (case-insensitive, whole names) and the bbox the corridor is looked for in (lat0, lon0, lat1, lon1)
CORRIDORS = {
    "prospect": (["Prospect Expressway"], (40.640, -74.000, 40.668, -73.970)),
    "bqe": (["Brooklyn-Queens Expressway", "Brooklyn Queens Expressway"], (40.640, -74.035, 40.775, -73.890)),
    "gowanus": (["Gowanus Expressway"], (40.605, -74.045, 40.680, -73.990)),
    "flatbush": (["Flatbush Avenue", "Flatbush Avenue Extension"], (40.575, -73.995, 40.700, -73.900)),
    "4thave": (["4th Avenue", "Fourth Avenue"], (40.610, -74.035, 40.690, -73.975)),
    "oceanpkwy": (["Ocean Parkway"], (40.570, -73.985, 40.650, -73.960)),
    "fdr": (["FDR Drive", "Franklin D. Roosevelt Drive", "Franklin D Roosevelt Drive"],
            (40.700, -74.020, 40.800, -73.925)),
    "broadway": (["Broadway"], (40.700, -74.020, 40.880, -73.900)),
    "harlemriver": (["Harlem River Drive"], (40.795, -73.945, 40.860, -73.915)),
    "crossbronx": (["Cross Bronx Expressway"], (40.825, -73.945, 40.850, -73.820)),
    "concourse": (["Grand Concourse"], (40.810, -73.935, 40.880, -73.880)),
    "lie": (["Long Island Expressway"], (40.725, -73.965, 40.775, -73.700)),
    "queensblvd": (["Queens Boulevard"], (40.700, -73.950, 40.750, -73.800)),
    "gcp": (["Grand Central Parkway"], (40.700, -73.930, 40.780, -73.700)),
    "siexpwy": (["Staten Island Expressway"], (40.570, -74.200, 40.640, -74.040)),
}
if args.corridor == "list":
    for k, (names, bb) in CORRIDORS.items():
        print(f"{k:12s} {', '.join(names)}  bbox {bb}")
    sys.exit(0)

CITY = {"primary", "secondary", "tertiary", "residential", "unclassified", "living_street"}
FAST = {"motorway", "trunk"}
DRIVE = CITY | FAST | {"service"}
WALL_GRADE = 0.25           # a 2 m stretch this steep: a wall (the steepest city streets are ~0.2)
WALL_STEP_M = 0.30          # m in 1 m: a wall
STEP_M = 0.10               # m in 1 m beyond the road's own grade (over 20 m) there: a step
DECK_STEP_M = 0.05          # m between a deck's free end and its street (NYC_NOTES: <= 5 cm)
HUMP_M = 1.0                # m a deck end may stand over its street's own line (the NEXT done criterion's 1 m)
HUMP_FIT_M = (20.0, 60.0)   # m past the end the street's own ground is fitted on (beyond the DEM's smoothed walls)
HILL_M = 1.0                # m a city street may be lifted over the DEM away from decks
HILL_GRADE = 0.02           # graded grade over the DEM's own (NYC_NOTES: + 2 %)
LIFT_MIN_M = 0.3            # ... where the street is lifted at least this (a cut into a hump is no made-up hill)
ABUT_SKIP_M = 25.0          # m from a deck end the lifts are the deck's (hump), not a hill
CURV_CITY = 0.01            # 1/m over 20 m on a city street (~0.12 g at 40 km/h)
CURV_FAST = 0.003           # 1/m on a motorway or trunk (~0.15 g at 80 km/h)
JUNCTION_M = 0.05           # m spread of the planned heights at a node (NYC_NOTES: <= 5 cm)
RAMP_M = 0.15               # m a link's plan off its mainline's where their carriageways overlap
RAMP_REACH_M = 60.0         # m from where a link leaves its mainline that it is compared
PAIR_M = 35.0               # m between a divided road's centre lines
PAIR_DZ_M = 0.5             # m apart beyond the DEM's difference
NODE_TOL = 0.5              # m (junctions.NODE_TOL)
SEVERITY = ["wall", "step", "deck", "gap", "building", "hump", "hill", "curve", "junction", "ramp", "divided"]

if args.corridor:
    names, bbox = CORRIDORS[args.corridor]
    names = {n.lower() for n in names}
else:
    names, bbox = None, (40.640, -73.995, 40.668, -73.970)
if args.bbox:
    bbox = tuple(float(v) for v in args.bbox.split(","))
if args.at:
    lat0, lon0 = (float(v) for v in args.at.split(","))
else:
    lat0, lon0 = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2

CACHE = os.path.join(os.path.expanduser("~"), ".cache", "beamng-earth")
net = netfetch.Fetcher(offline=False)
md = mapdata.MapData(CACHE, fetcher=net)
dem = demmod.Dem(os.path.join(CACHE, "dem"), fetcher=net)
frame = geodesy.EnuFrame(lat0, lon0)
sf = surface.Surface(md)
keys = sorted(set(mapcells.cells_for_bbox(*bbox)), key=str)


def z(la, lo):
    return np.asarray(dem.sample(np.atleast_1d(la), np.atleast_1d(lo), grading.PROFILE_ZOOM), float)


def cell(k):
    return md.cell(k, layers=("roads",), kind="demand").layers.get("roads")


for k in keys:                          # (the land cover's cells, as the server's prefetch would have them)
    md.cell(k, layers=("landcover",), kind="demand")
sf.classes(lat0, lon0)
feats = [f for f in (cell(k) for k in keys) if f is not None and len(f)]


def enu(ll):
    e, n, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], np.zeros(len(ll)))
    return np.c_[np.asarray(e, float), np.asarray(n, float)]


def to_ll(xy):
    la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
    return np.asarray(la, float), np.asarray(lo, float)


def up(xy, h):
    """Heights h (m above the datum) at xy as the frame's up: the decks' (BridgeProfiles) and the game's, the earth's
    curvature taken off (d^2 / 2R: 7 cm 930 m from the frame's origin, a "step" at every deck end of a raw comparison)."""
    la, lo = to_ll(np.asarray(xy, float).reshape(-1, 2))
    return np.asarray(frame.geodetic_to_enu(la, lo, np.asarray(h, float))[2], float)


def resample(xy, step):
    s = np.r_[0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    if s[-1] <= 0:
        return xy[:1], np.zeros(1)
    t = np.r_[np.arange(0, s[-1], step), s[-1]]
    return np.c_[np.interp(t, s, xy[:, 0]), np.interp(t, s, xy[:, 1])], t


class Grid:
    """Points or segments bucketed for "what is within d m of this point" (the checks' only spatial query)."""

    def __init__(self, segs, cell=50.0):
        self.cell, self.b = cell, defaultdict(list)
        self.segs = segs                        # [(a (2,), b (2,), payload)]
        for n, (a, b, _p) in enumerate(segs):
            lo, hi = np.floor(np.minimum(a, b) / cell).astype(int), np.floor(np.maximum(a, b) / cell).astype(int)
            for i in range(lo[0], hi[0] + 1):
                for j in range(lo[1], hi[1] + 1):
                    self.b[(i, j)].append(n)

    def near(self, p, d):
        """[(distance, payload, t along the segment)] of the segments within d of p."""
        out, seen = [], set()
        lo, hi = np.floor((p - d) / self.cell).astype(int), np.floor((p + d) / self.cell).astype(int)
        for i in range(lo[0], hi[0] + 1):
            for j in range(lo[1], hi[1] + 1):
                for n in self.b.get((i, j), ()):
                    if n in seen:
                        continue
                    seen.add(n)
                    a, b, pl = self.segs[n]
                    ab = b - a
                    t = float(np.clip(np.dot(p - a, ab) / max(float(np.dot(ab, ab)), 1e-12), 0.0, 1.0))
                    dd = float(np.hypot(*(a + t * ab - p)))
                    if dd <= d:
                        out.append((dd, pl, t))
        return out


# ---- the map: every feature once, its spec as the road tiles have it, its ground / deck / tunnel pieces in the frame
feat_of, rows = {}, []
for f in feats:
    specs = roads.road_specs(f, sf.urban, None)
    bspecs = roads.bridge_specs(f)
    for i in range(len(f)):
        fid = str(f.ids[i])
        if fid in feat_of:
            continue
        sub = str(f.attrs["subtype"][i]) if "subtype" in f.attrs else "road"
        cls = str(f.attrs["class"][i]) if "class" in f.attrs else "unknown"
        if sub != "road" or cls not in DRIVE or (cls == "service" and not args.service):
            continue
        nm = str(f.attrs["name"][i]) if "name" in f.attrs and f.attrs["name"][i] is not None else ""
        feat_of[fid] = (f, i)
        a = f.coord_off[f.ring_off[f.part_off[i]]]
        b = f.coord_off[f.ring_off[f.part_off[i + 1]]]
        rows.append(dict(fid=fid, cls=cls, name=nm if nm not in ("None", "nan") else "", spec=specs[i],
                         bspec=bspecs[i], whole=enu(f.lonlat[a:b]) if b > a else np.zeros((0, 2)),
                         ground=[enu(np.asarray(r, float)) for r in roads.ground_parts(f, i)] if specs[i] else [],
                         decks=[enu(np.asarray(r, float)) for r in roads.bridge_parts(f, i)] if bspecs[i] else [],
                         tunnels=[enu(np.asarray(r, float)) for r in roads.tunnel_parts(f, i)],
                         levels=roads.raw_level_ranges(f, i), link=bool(specs[i] and specs[i].link)))

# ---- the region: the corridor's roads and everything within --near of them (or the whole bbox)
bb_xy = enu(np.array([[bbox[1], bbox[0]], [bbox[3], bbox[2]]]))
if names:
    core = [r for r in rows if r["name"].lower() in names]
    if not core:
        sys.exit(f"no road named {sorted(names)} in {bbox}")
    cg = Grid([(ln[k], ln[k + 1], None) for r in core for ln in [r["whole"]] for k in range(len(ln) - 1)])

    def inside(xy):
        return np.array([bool(cg.near(p, args.near)) for p in xy])
else:
    def inside(xy):
        return ((xy[:, 0] >= bb_xy[0, 0]) & (xy[:, 0] <= bb_xy[1, 0]) & (xy[:, 1] >= bb_xy[0, 1])
                & (xy[:, 1] <= bb_xy[1, 1]))
for r in rows:
    w = r["whole"]
    r["in"] = bool(len(w)) and bool(inside(resample(w, 20.0)[0]).any())
region = [r for r in rows if r["in"]]
print(f"connect_check: {len(region)} roads of {len(rows)} in the region "
      f"({args.corridor or 'bbox'} {bbox}, frame {lat0:.5f},{lon0:.5f})")


def latlon(p):
    la, lo = to_ll(np.asarray(p, float).reshape(1, 2))
    return f"{la[0]:.5f},{lo[0]:.5f}"


def label(r):
    return f"{r['cls'][:11]:11s} {(r['name'] or '-')[:28]:28s} {r['fid'][:8]}"


# ---- the checks that do not depend on the grading: dropped roads, map joins, buildings
def map_checks():
    out = []
    for r in region:
        if r["spec"] is None and not r["decks"] and r["cls"] != "service":
            lv = r["levels"]
            if not lv or sum(b - a for a, b, _ in lv) < 0.99:
                out.append(("gap", 0.0, r, r["whole"][len(r["whole"]) // 2], "no spec: not drawn at all"))
        for lo, hi, kind in r["levels"]:
            if kind == "bridge" and r["bspec"] is None and (hi - lo) * float(np.hypot(*np.diff(r["whole"], axis=0).T)
                                                                     .sum()) > 2.0:
                out.append(("gap", 0.0, r, r["whole"][len(r["whole"]) // 2], "bridge range with no deck"))
    # every drawn piece's ends (ground, deck, tunnel) by feature; the map's nodes: drivable features' ends
    ends = defaultdict(list)
    for r in rows:
        for ln in r["ground"] + r["decks"] + r["tunnels"]:
            if len(ln) >= 2:
                ends[r["fid"]] += [ln[0], ln[-1]]
    node = defaultdict(set)
    for r in rows:
        w = r["whole"]
        if len(w) >= 2:
            for p in (w[0], w[-1]):
                node[(round(p[0] / NODE_TOL), round(p[1] / NODE_TOL))].add(r["fid"])
    by = {r["fid"]: r for r in rows}
    for kk, fids in node.items():
        if len(fids) < 2:
            continue
        p = np.array(kk, float) * NODE_TOL
        if not inside(p[None])[0]:
            continue
        for fid in fids:
            r = by[fid]
            if r["spec"] is None and not r["decks"]:
                continue                        # (reported above)
            e = ends.get(fid)
            dmin = min(float(np.hypot(*(q - p))) for q in e) if e else math.inf
            if dmin > grading.APPROACH_TOUCH_M:
                out.append(("gap", dmin, r, p, f"drawn {dmin:.1f} m short of the map's node "
                                                 f"({len(fids)} roads meet)"))
    if args.buildings:
        segs = []
        for k in keys:
            bf = md.cell(k, layers=("buildings",), kind="demand").layers.get("buildings")
            if bf is None:
                continue
            for i in range(len(bf)):
                for part in bf.parts(i):
                    if part and len(part[0]) >= 3:
                        ring = enu(np.asarray(part[0], float))
                        segs.append((ring.min(0), ring.max(0), ring))
        bg = Grid([(lo, hi, ring) for lo, hi, ring in segs], cell=60.0)
        for r in region:
            for ln in r["ground"]:
                xy, _t = resample(ln, 4.0)
                for p in xy[1:-1]:
                    hit = None
                    for _d, ring, _t2 in bg.near(p, 0.0):
                        x, y = ring[:, 0], ring[:, 1]
                        xn, yn = np.roll(x, -1), np.roll(y, -1)
                        c = ((y > p[1]) != (yn > p[1])) & (p[0] < (xn - x) * (p[1] - y) / np.where(yn == y, 1e-12,
                                                                                                    yn - y) + x)
                        if int(c.sum()) % 2:
                            hit = ring
                            break
                    if hit is not None:
                        out.append(("building", 0.0, r, p, "centreline inside a building's footprint"))
                        break
    return out


# ---- the graded checks
def graded_checks(on):
    grading.TOWN_CUT_ON = on
    g = grading.Grader(frame, md, z, urban=sf.urban)

    def graded(xy):
        la, lo = to_ll(xy)
        return g.apply(la, lo, z(la, lo))

    def graded_ll(la, lo):
        return g.apply(np.atleast_1d(la), np.atleast_1d(lo), z(la, lo))
    bp = roads.BridgeProfiles(frame, cell, graded_ll)
    lo_b, hi_b = bb_xy.min(0) - 300, bb_xy.max(0) + 300
    decks = []                                  # (fid, xy (k, 2), z (k,))
    for f in feats:
        for fid, _sp, xy, dz, _lo in bp.parts([f], (lo_b[0], lo_b[1], hi_b[0], hi_b[1])):
            decks.append((str(fid), np.asarray(xy, float)[:, :2], np.asarray(dz, float)))
    seen, uniq = set(), []
    for d in decks:
        k = (d[0], round(float(d[1][0, 0]), 1), round(float(d[1][0, 1]), 1), len(d[1]))
        if k not in seen:
            seen.add(k)
            uniq.append(d)
    decks = uniq
    dend = np.array([[d[1][0], d[1][-1]] for d in decks]).reshape(-1, 2) if decks else np.zeros((0, 2))
    out = []
    by = {r["fid"]: r for r in rows}
    ground = [(r, ln) for r in rows for ln in r["ground"] if len(ln) >= 2]
    gends = Grid([(ln[0], ln[0], (r, ln, 0)) for r, ln in ground] + [(ln[-1], ln[-1], (r, ln, 1)) for r, ln in ground])
    deck_near = Grid([(p, p, None) for p in dend]) if len(dend) else None

    # (1) profiles of the region's ground parts, every 1 m
    prof = []
    for r in region:
        for ln in r["ground"]:
            if len(ln) < 2:
                continue
            xy, t = resample(ln, 1.0)
            if len(xy) < 3:
                continue
            prof.append((r, ln, xy, t))
    allxy = np.concatenate([p[2] for p in prof]) if prof else np.zeros((0, 2))
    la, lo = to_ll(allxy) if len(allxy) else (np.zeros(0), np.zeros(0))
    zd_all = z(la, lo) if len(allxy) else np.zeros(0)
    zg_all = g.apply(la, lo, zd_all) if len(allxy) else np.zeros(0)
    k0 = 0
    for r, ln, xy, t in prof:
        n = len(xy)
        zg, zd = zg_all[k0:k0 + n], zd_all[k0:k0 + n]
        k0 += n
        keep = inside(xy[::10])
        keep = np.repeat(keep, 10)[:n]
        if not keep.any():
            continue
        dd = (np.array([min((dd for dd, _p, _t in deck_near.near(p, ABUT_SKIP_M)), default=math.inf) for p in xy])
              if deck_near is not None else np.full(n, math.inf))
        d1 = np.diff(zg)
        # (a road's own grade over 20 m around each 1 m step)
        if n > 21:
            g20 = np.convolve(np.diff(zg), np.ones(20) / 20.0, mode="same")
        else:
            g20 = np.full(n - 1, (zg[-1] - zg[0]) / max(t[-1], 1e-9))
        res = np.abs(d1 - g20)
        kk = keep[1:]
        cls = r["cls"]
        if kk.any():
            j = int(np.argmax(np.where(kk, np.abs(d1), -1)))
            g2 = np.abs(zg[2:] - zg[:-2]) / 2.0
            j2 = int(np.argmax(np.where(keep[2:], g2, -1))) if n > 2 else 0
            if abs(d1[j]) > WALL_STEP_M or (n > 2 and g2[j2] > WALL_GRADE):
                jj = j if abs(d1[j]) > WALL_STEP_M else j2
                out.append(("wall", float(max(abs(d1[j]), g2[j2] if n > 2 else 0)), r, xy[jj],
                            f"jump {abs(d1[j]):.2f} m in 1 m / grade {g2[j2] * 100 if n > 2 else 0:.0f} % over 2 m"))
            else:
                j = int(np.argmax(np.where(kk, res, -1)))
                if res[j] > STEP_M:
                    out.append(("step", float(res[j]), r, xy[j], f"{res[j]:.2f} m in 1 m beyond its grade"))
        if n > 41:
            c = np.abs(zg[40:] - 2 * zg[20:-20] + zg[:-40]) / 400.0
            cd = np.abs(np.convolve(zd, np.ones(9) / 9, "same")[40:] - 2 * np.convolve(zd, np.ones(9) / 9, "same")[20:-20]
                        + np.convolve(zd, np.ones(9) / 9, "same")[:-40]) / 400.0
            thr = CURV_CITY if cls in CITY else CURV_FAST if cls in FAST else None
            ok = keep[20:-20]
            if thr is not None and ok.any():
                bad = (c > thr) & (c > 1.5 * cd) & ok
                if bad.any():
                    j = int(np.argmax(np.where(bad, c, -1)))
                    out.append(("curve", float(c[j]), r, xy[j + 20], f"{c[j]:.4f} /m [DEM {cd[j]:.4f}]"))
        if cls in CITY:
            lift = zg - zd
            far = (dd > ABUT_SKIP_M) & keep
            if (far & (lift > HILL_M)).any():
                j = int(np.argmax(np.where(far, lift, -9)))
                out.append(("hill", float(lift[j]), r, xy[j], f"lifted {lift[j]:.1f} m over the DEM"))
            elif n > 41:
                gg = (zg[20:] - zg[:-20]) / 20.0
                gd = (zd[20:] - zd[:-20]) / 20.0
                lm = np.minimum(lift[20:], lift[:-20])
                bad = (np.abs(gg) > np.abs(gd) + HILL_GRADE) & (lm > LIFT_MIN_M) & far[20:] & far[:-20]
                if bad.any():
                    j = int(np.argmax(np.where(bad, np.abs(gg) - np.abs(gd), -1)))
                    out.append(("hill", float(abs(gg[j]) - abs(gd[j])), r, xy[j + 10],
                                f"grade {gg[j] * 100:+.1f} % over the DEM's {gd[j] * 100:+.1f} %, lifted {lm[j]:.1f} m"))

    # (2) decks' free ends: the step to the street, loose ends, the hump over the street's own line
    gl = [ln for _r, ln in ground]
    for n, (fid, xy, dz) in enumerate(decks):
        for e, (p, zd_end) in enumerate(((xy[0], dz[0]), (xy[-1], dz[-1]))):
            d = np.hypot(*(dend - p).T)
            d[2 * n + e] = np.inf
            if (d < 0.75).any() or not inside(p[None])[0]:
                continue
            r0 = by.get(fid)
            if r0 is None:
                continue
            app = [(dd, pl) for dd, pl, _t in gends.near(p, grading.APPROACH_TOUCH_M)]
            if not app:
                out.append(("deck", math.inf, r0, p, "free end with no road leading onto it"))
                continue
            for _dd, (r, ln, at) in sorted(app, key=lambda a: a[0])[:2]:
                inward = ln if at == 0 else ln[::-1]
                on = grading._goes_on(ln, at == 0, gl, HUMP_FIT_M[1] + 10.0)     # (on past its far end)
                path = inward if on is None else np.vstack([inward, on[1:]])
                pxy, pt = resample(path, 1.0)
                zz = up(pxy[:3], graded(pxy[:3]))
                step = float(zz[0] - zd_end)
                if abs(step) > DECK_STEP_M:
                    out.append(("deck", abs(step), r, p, f"street {step:+.2f} m against the deck of {fid[:8]}"))
                fit = (pt >= HUMP_FIT_M[0]) & (pt <= HUMP_FIT_M[1])
                if fit.sum() >= 10:
                    la2, lo2 = to_ll(pxy[fit])
                    b, a = np.polyfit(pt[fit], up(pxy[fit], z(la2, lo2)), 1)
                    hump = float(zd_end - a)
                    lift12 = float(graded(pxy[12:13])[0] - z(*to_ll(pxy[12:13]))[0]) if len(pxy) > 12 else 0.0
                    if r["cls"] in CITY and hump > HUMP_M:
                        out.append(("hump", hump, r, p, f"deck end {hump:.1f} m over its street's line "
                                                        f"(grade {b * -100:+.1f} %; lift at 12 m {lift12:.1f})"))

    # (3) junctions: the planned heights at the nodes where ground part ends meet
    pl = []
    for k in g._keys(*to_ll(np.array([lo_b, hi_b]))):
        for fid_part, p in g._cell_plans(k):
            if grading.TUNNEL_TAG in fid_part or fid_part.startswith(grading.RAIL_TAG):
                continue
            pxy = np.asarray(p.xy, float)[:, :2]
            for m in range(len(pxy) - 1):
                pl.append((pxy[m], pxy[m + 1], (fid_part, float(p.z[m]), float(p.z[m + 1]), p.half)))
    pg = Grid(pl)
    nodes = defaultdict(list)
    for r, ln in ground:
        for p in (ln[0], ln[-1]):
            nodes[(round(p[0] / NODE_TOL), round(p[1] / NODE_TOL))].append((r, p))
    for kk, items in nodes.items():
        if len({it[0]["fid"] for it in items}) < 2:
            continue
        p = items[0][1]
        if not inside(p[None])[0]:
            continue
        zs = {}
        for dd, (fp, za, zb, _h), t in pg.near(p, NODE_TOL):
            zs.setdefault(fp.split("#")[0], []).append(za + (zb - za) * t)
        per = {f: float(np.mean(v)) for f, v in zs.items()}
        if len(per) >= 2:
            spread = max(per.values()) - min(per.values())
            if spread > JUNCTION_M:
                rr = max((it[0] for it in items), key=lambda x: (x["cls"] in FAST, x["cls"] in CITY))
                out.append(("junction", spread, rr, p, f"{len(per)} plans spread {spread:.2f} m: " + ", ".join(
                    f"{(by[f]['name'] or by[f]['cls']) if f in by else f[:8]} {v:.2f}" for f, v in sorted(per.items()))))

    # (4) links beside their mainline
    mains = [(r, ln) for r, ln in ground if r["cls"] in FAST and not r["link"]]
    mg = Grid([(ln[m], ln[m + 1], r) for r, ln in mains for m in range(len(ln) - 1)])
    for r in region:
        if not r["link"]:
            continue
        for ln in r["ground"]:
            for at in (0, 1):
                p = ln[0] if at == 0 else ln[-1]
                hits = [(dd, mr) for dd, mr, _t in mg.near(p, NODE_TOL + 0.5) if mr["fid"] != r["fid"]]
                if not hits:
                    continue
                main = min(hits, key=lambda h: h[0])[1]
                xy, t = resample(ln if at == 0 else ln[::-1], 2.0)
                sel = t <= RAMP_REACH_M
                xs = xy[sel]
                worst = None
                for q in xs:
                    lk = [(dd, za + (zb - za) * tt, h) for dd, (fp, za, zb, h), tt in pg.near(q, 0.5)
                          if fp.split("#")[0] == r["fid"]]
                    mn = [(dd, za + (zb - za) * tt, h) for dd, (fp, za, zb, h), tt in pg.near(q, 15.0)
                          if fp.split("#")[0] == main["fid"]]
                    if not lk or not mn:
                        continue
                    lz = min(lk)[1]
                    dm, mz, mh = min(mn)
                    if dm > mh + (r["spec"].width / 2 if r["spec"] else 3.0):
                        continue                    # (apart: no longer side by side)
                    dzv = abs(lz - mz)
                    if worst is None or dzv > worst[0]:
                        worst = (dzv, q)
                if worst is not None and worst[0] > RAMP_M:
                    out.append(("ramp", worst[0], r, worst[1], f"link {worst[0]:.2f} m off {main['name'] or 'its'} "
                                                               f"mainline ({main['fid'][:8]}) beside it"))

    # (5) divided roads: same-name one-way carriageways side by side
    ows = [(r, ln) for r, ln in ground if r["spec"] is not None and r["spec"].oneway and r["name"]]
    og = Grid([(ln[m], ln[m + 1], (r, ln)) for r, ln in ows for m in range(len(ln) - 1)])
    done = set()
    for r, ln in ows:
        if not r["in"] or r["fid"] in done:
            continue
        xy, _t = resample(ln, 10.0)
        if len(xy) < 2:
            continue
        hd = np.diff(ln, axis=0)
        worst = None
        for q in xy:
            best = None
            for dd, (r2, ln2), tt in og.near(q, PAIR_M):
                if r2["name"] != r["name"] or r2["fid"] == r["fid"] or dd < 4.0:
                    continue
                m = min(range(len(ln2) - 1), key=lambda m: np.hypot(*(ln2[m] + 0.5 * (ln2[m + 1] - ln2[m]) - q)))
                u2 = ln2[m + 1] - ln2[m]
                u1 = hd[min(range(len(hd)), key=lambda m: np.hypot(*(ln[m] - q)))]
                cosang = abs(float(np.dot(u1, u2))) / max(float(np.hypot(*u1) * np.hypot(*u2)), 1e-9)
                if cosang < math.cos(math.radians(25)):
                    continue
                if best is None or dd < best[0]:
                    ab = ln2[1:] - ln2[:-1]
                    tt2 = np.clip(((q - ln2[:-1]) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-12), 0, 1)
                    pts = ln2[:-1] + tt2[:, None] * ab
                    pq = pts[int(np.argmin(np.hypot(*(pts - q).T)))]
                    # (side by side: square to the road, not the same street going on past a node)
                    if abs(float(np.dot(pq - q, u1))) / max(float(np.hypot(*(pq - q)) * np.hypot(*u1)), 1e-9) > 0.5:
                        continue
                    best = (dd, pq, r2)
            if best is None:
                continue
            pair = np.array([q, best[1]])
            zz, zdd = graded(pair), z(*to_ll(pair))
            ex = abs((zz[0] - zz[1]) - (zdd[0] - zdd[1]))
            if abs(zz[0] - zz[1]) <= PAIR_DZ_M:
                continue                        # (level side by side: the DEM's step is under a deck or a wall)
            if worst is None or ex > worst[0]:
                worst = (ex, q, best[2], zz[0] - zz[1], zdd[0] - zdd[1])
        if worst is not None and worst[0] > PAIR_DZ_M:
            done.add(worst[2]["fid"])
            out.append(("divided", worst[0], r, worst[1], f"{worst[3]:+.2f} m to its other carriageway "
                                                          f"({worst[2]['fid'][:8]}), the DEM {worst[4]:+.2f} m"))
    return out


def key_of(fl):
    kind, _v, r, p, _msg = fl
    return (kind, r["fid"], round(float(p[0]) / 10), round(float(p[1]) / 10))


def show(fails, title):
    by = defaultdict(list)
    for fl in fails:
        by[fl[0]].append(fl)
    print(f"{title}: " + ", ".join(f"{k} {len(by[k])}" for k in SEVERITY if by[k]) if fails else f"{title}: none")
    for kind in SEVERITY:
        items = sorted(by[kind], key=lambda fl: -fl[1])
        for kind_, v, r, p, msg in items[:args.max]:
            print(f"  {kind_:8s} {label(r)} {latlon(p)}  {msg}")
        if len(items) > args.max:
            print(f"  {kind:8s} ... {len(items) - args.max} more")


def dump(fails):
    return [dict(kind=k, value=None if not np.isfinite(v) else round(float(v), 3), fid=r["fid"], cls=r["cls"],
                 name=r["name"], at=latlon(p), x=round(float(p[0]), 1), y=round(float(p[1]), 1), msg=m)
            for k, v, r, p, m in fails]


base = map_checks()
res = {True: base + graded_checks(True)}
if args.compare and hasattr(grading, "TOWN_CUT_ON"):
    res[False] = base + graded_checks(False)
show(res[True], "town cut on" if hasattr(grading, "TOWN_CUT_ON") else "this code")
if False in res:
    on_k = {key_of(fl) for fl in res[True]}
    off_k = {key_of(fl) for fl in res[False]}
    show([fl for fl in res[True] if key_of(fl) not in off_k], "only with the cut on (new)")
    show([fl for fl in res[False] if key_of(fl) not in on_k], "only with the cut off (fixed by it)")
if args.save:
    with open(args.save, "w") as fh:
        json.dump(dump(res[True]), fh, indent=0)
if args.against:
    with open(args.against) as fh:
        old = json.load(fh)
    ok = {(d["kind"], d["fid"], round(d["x"] / 10), round(d["y"] / 10)) for d in old}
    nk = {key_of(fl) for fl in res[True]}
    new = [fl for fl in res[True] if key_of(fl) not in ok]
    show(new, f"new against {os.path.basename(args.against)}")
    gone = [d for d in old if (d["kind"], d["fid"], round(d["x"] / 10), round(d["y"] / 10)) not in nk]
    print(f"fixed against {os.path.basename(args.against)}: {len(gone)}")
    for d in gone[:args.max]:
        print(f"  {d['kind']:8s} {d['cls'][:11]:11s} {(d['name'] or '-')[:28]:28s} {d['fid'][:8]} {d['at']}  {d['msg']}")
