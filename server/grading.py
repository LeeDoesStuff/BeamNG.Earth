"""Ground graded under roads (docs/ROADMAP.md v1.6; docs/research/2026-10-11-official-road-study.md §3, §5.3):
the physics block and the near mesh tiles sample one graded elevation, so tyres, DecalRoads and the
visible ground agree.

Per road (roads.road_specs; bridges, tunnels and off-ground segments are left out by it):
- along: the centreline height is the DEM sampled every PROFILE_STEP m and smoothed (Gaussian,
  SIGMA_M m; dirt roads DIRT_SIGMA_M), so grades stay real and small bumps go (the official maps:
  |2nd difference| per 10 m 0.02-0.1 m);
- across: flat over the core (the carriageway as a city street would have it, parking included, plus
  SHOULDER_M, or the sidewalk plus SIDEWALK_MARGIN_M on the classes that get sidewalks in towns, so a
  raised sidewalk slab sits on flat ground), then back to the natural ground over a transition of 2x the height difference (a
  1:2 cut or fill slope), clamped to [T_MIN, T_MAX] m;
- several roads at one point (junctions, divided carriageways) blend their targets by inverse distance
  from each centreline (BLEND_R), so they meet in a gradual slope.
The core width never depends on the land cover (it is taken as urban for every built road), and
each map cell's plan is made once per anchor and cached, so the block and the tiles built later from
the same anchor always agree.

GradedDem(dem, grader) is a drop-in for dem.Dem where only sample() is graded; use it for the block
and the levels <= 0 tiles only (coarser tiles sink under the block and cover far more ground).
"""
import logging
import math
import os
import threading
from collections import OrderedDict
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

import mapcells
import roads as roadsmod

log = logging.getLogger("earthd.grading")

VERSION = 2
PROFILE_STEP = 2.0          # m between profile samples
SIGMA_M = 12.0              # m, profile smoothing of built roads
# ... and of motorways and trunks: real ones keep long, gentle grades, and 12 m kept every bump of the elevation data in
# them where it is a 30 m surface model (BR-381 in Brazil: a junction's pad, forest edges; 20 % grades between them, a
# flat bench halfway down a long descent; GitHub #266). Lidar areas change little: their roads are smooth already.
SIGMA_MAJOR_M = float(os.environ.get("BNE_GRADE_MAJOR", "40"))
# (and primary roads: one through Russian forest kept every 1-3 m bump of GLO-30 over 50-100 m at 12 m, a road of hills
# and walls on flat land, GitHub #335. Not secondary yet: Otto Road's junction with I-80's ramps came out 0.29 m apart
# once its dip under I-80 (underpass_offsets) was laid on the longer smoothing; the ramps' pins need a look first)
MAJOR_CLASSES = ("motorway", "trunk", "primary")      # (not their slip roads: Overture's subclass "link", Spec.link)
DIRT_SIGMA_M = 5.0          # m, profile smoothing of gravel / dirt roads
# A running median before the smoothing (median_profile): a bridge deck left in the elevation data where it crosses
# over a road is a spike a few samples wide, and the Gaussian spread it into a hump in the road under (GitHub #271, an
# 8.5 m spike near Thetford: a 3.2 m hump). Slopes, crests and steps longer than the window stay.
MEDIAN_M = float(os.environ.get("BNE_GRADE_MEDIAN", "24"))
DIRT_MEDIAN_M = 10.0        # (tracks keep their small real bumps)
RAIL_GRADE_ON = os.environ.get("BNE_RAIL_GRADE", "1") != "0"    # the ground laid to the railways' bed (railways.py)
RAIL_TAG = "rail:"
SHOULDER_M = 1.0            # m of flat shoulder beyond the carriageway, each side
SIDEWALK_MARGIN_M = 0.5     # m of flat ground beyond a sidewalk (roads.SIDEWALK_CLASSES)
LONG_PART_M = 3000.0        # a road part longer than this is planned per cell, only within CLIP_MARGIN_M of the cell
CLIP_MARGIN_M = 400.0
PLAN_THREADS = 4            # cells planned at once (_plan_missing)
PLAN_SETS_MAX = 16          # cell sets whose merged plans are kept (Grader._plan_set; ~1 MB each in a town)
SPREAD_CELLS = 16           # plans(): more map cells in the points' bbox: only the cells around the points
T_MIN, T_MAX = 5.0, 12.0    # m, transition back to the natural ground (5: roads close together meet gently)
SLOPE_RUN = 2.0             # transition = SLOPE_RUN x the height difference (1:2 slope)
VERTEX_STEP = 4.0           # m between the plan's vertices (segments for the grading pass; linear between: at 16 m a
                            # crest or a sag was a kink every 16 m, 'bumpy roads, worst uphill and at speed' on r/BeamNG,
                            # 2026-10-17: a San Francisco street's slope changed 4.4 % rms per 2 m, 1.2 % at 4 m;
                            # grading a 1 km block 0.06 -> 0.14 s)
BUCKET = 32.0               # m, query-point buckets
ROAD_LIFT = 0.04            # m the physics ground is raised under roads (see Grader.apply)
BLEND_R = 4.0               # m: roads near each other blend their targets by 1 / (1 + (d / BLEND_R)^2)
PROFILE_ZOOM = int(os.environ.get("BNE_DEM_ZOOM", "16"))   # DEM zoom of the profiles (one zoom for every caller)
WALL_CUT_M = 2.0            # a road cut deeper than this into the ground: a retaining wall at the core's edge
WALL_T_M = 0.8              # (roadmesh.retaining_walls) and the ground at its natural height right behind it, over
                            # this; on a 1:2 slope Central Park's sunken transverse was a grassy ditch between its
                            # stone walls (the user, 2026-10-17)
WALL_FLAT_M = 2.0           # ... after one terrain grid step at the road's level behind the wall: rising at the wall
                            # itself, the 2 m grid's triangles stood as dark wedges in front of its face; the wall's
                            # top (roadmesh.WALL_THICK_M) covers the strip
TUNNEL_TAG = ":tun"          # in the fid of the tunnels' own plans (open sections, mouths): apply(roads_only) skips them
MOUTH_PLAN_M = 4.0          # m into a tunnel from a portal its floor is a grading plan too (Grader._mouth_plans)
TUNNEL_KEEP_IN_M = 3.0      # (roads.LOW_HOLE_M: a low arch's mouths, flat at its floor)
TUNNEL_KEEP_M = 5.0         # m beside a covered tunnel's road where the ground over it is left ungraded ...
OVER_TUNNEL_SMALL_M = 0.5   # ... but for a change smaller than this (a road crossing over it, graded flush)
WALL_AHEAD_M = 2.5          # ... only beside a segment or this little past its end (a joint between two: VERTEX_STEP)
# Roads at other heights beside a road's core (GitHub #92: a street in a cutting had its half under a service road on
# the bank 7.5 m above it pulled 2-4 m up its slope; hairpins blended into each other; an upper road's edge dragged into
# a pit by the road under it). Beside a point in a core, another road's segments outside their own core count less the
# further their height is from that core's: not at all from SEP_FULL_M, as before up to SEP_FROM_M, and their weight
# comes back over CORE_FADE_M past the core (no step at its edge).
SEP_ON = os.environ.get("BNE_GRADE_SEP", "on") != "off"
# Sharp cuttings (the user, 2026-10-21: the elevation data is smoothed, "sharper terrain features get lost": a street in
# a walled cutting read as a 4 m slope its asphalt climbed, McGraw-Graben, Munich). Beside a built road whose ground
# rises SHARP_MIN_M or more within SHARP_REACH_M of its core at SHARP_GRADE or steeper on average (a wall the data
# smoothed into a ramp), the ground is raised to that top from the core's edge out: the road's wall rule (WALL_CUT_M)
# then flattens a strip, steps up and roads.retaining_walls stands a wall there.
SHARP_ON = os.environ.get("BNE_SHARP", "on") != "off"
SHARP_REACH_M = 10.0
SHARP_STEP_M = 1.0
SHARP_MIN_M = 2.0
SHARP_GRADE = 0.6
SHARP_RUN_N = 3             # consecutive plan vertices (VERTEX_STEP apart) a cutting holds for at least
SEP_FROM_M = 2.0
SEP_FULL_M = 4.0
CORE_FADE_M = 2.0
SIDE_FADE_M = 2.0           # m of (side - ahead) over which a segment counts as beside the point (ahead: its own road)
RETRY_S = 60.0              # s before a cell whose road data failed is asked for again

# Overpass approach ramps (from KyzoFox's 1.5.4, PR #64). A short bridge over a road had its ends on the ground on both
# sides (the DEM is too coarse for the real approach embankments), and roads.deck_profile_once caps how steeply a deck
# climbs from its ends: the deck over I-20 near Temple, GA reached ~2.5 m over the lanes (trucks hit it, 2026-10-19).
# The roads leading onto such a bridge are now graded up to the height the deck needs, over a smooth ramp, as real
# approach fills are: the bridge's ends (sampled from this graded ground) then sit high enough.
APPROACH_ON = os.environ.get("BNE_APPROACH_RAMPS", "on") != "off"
APPROACH_GRADE = float(os.environ.get("BNE_APPROACH_GRADE", "0.05"))   # steepest grade of a ramp (its middle)
APPROACH_MAX_LIFT = 10.0    # m an abutment is raised at most
APPROACH_MIN_LIFT = 0.5     # m: less is left as it is
APPROACH_MIN_RUN = 30.0     # m: the shortest ramp
APPROACH_MAX_RUN = 320.0    # m: the longest ramp
APPROACH_TOUCH_M = 2.5      # m: a road whose end is this close to an abutment leads onto the bridge
APPROACH_MIN_CROSS_M = 6.0  # m: a road crossing a deck closer than this to its end is a junction at grade (not
                            # under it)
APPROACH_FLAT_M = 20.0      # m from an abutment the deck needs its full clearance (beyond: it may fall at DECK_GRADE)
APPROACH_MARGIN_M = 0.3     # m over the clearance (the deck profile's smoothing takes a little off near its ends)
APPROACH_SKIP = ("motorway", "trunk")   # never raised (their links are: _never_raised); the roads over them rise
APPROACH_GAP_M = 1.0        # m of flat ground kept between a ramp's fill slope and the road beside it
ENU_CACHE_MAX = 512         # cells' road parts in the anchor frame kept (Grader._cell_enu; ~0.02-0.5 MB each)
APPROACH_CELL_M = 450.0     # m: a plan's end this far from a cell cannot raise what that cell's queries see
APPROACH_CAP_GRADE = 0.10   # steepest grade a ramp falls at where a road beside it caps it (a cap alone dropped 3 m
                            # in 8 m by a side road north of the I-20 bridge near Temple, GA, 2026-10-20)

# Embankments at abutments (GitHub #38: a GPS drive near Presov, Slovakia flipped on the D1). A bridge's map line can end
# down its embankment, short of the abutment: the road's last metres followed the valley's side in the elevation data and
# the deck end, taken from that ground, sat 1.4 m under the road 2 m on (a 12 % deck and a step at 55 mph). A road ending
# at a bridge whose last ABUT_NEAR_M fall much more steeply toward it than its own grade further on keeps that grade to
# its end instead, as the fill under a real approach does (every class, motorways too).
ABUT_FILL_ON = os.environ.get("BNE_ABUT_FILL", "on") != "off"
# A road split at a junction (the map's segments end at every one) is smoothed as one: each part's profile also samples
# the ground along the straightest part that goes on from its end, so the parts meet at one height. Each was smoothed
# alone, and on a slope each end took the average of its own side: on BR-381's 20 % grades the next part began 2 m
# over where the last ended, a ramp at every junction (GitHub #266).
CHAIN_ON = os.environ.get("BNE_GRADE_CHAIN", "on") != "off"
CHAIN_JOIN_M = 1.0          # m: two parts' ends this close are one node
CHAIN_TURN_DEG = 35.0       # the part that goes on turns at most this at the node
CHAIN_LOOK_M = 10.0         # m along each part its direction at the node is taken over
# Underpasses (GitHub #138, #142, #126's rule: "the road under gives way"): a ground road crossing under a short
# overpass that is never raised (APPROACH_SKIP classes and links: a motorway's bridge over a local road) dips to the
# deck's height (its abutments' ground, interpolated) minus UNDER_CLEAR_M, falling back at UNDER_GRADE. Lidar terrain
# removes bridges and interpolates across the gap, at about the deck's height: the road under ran 0.7-2.5 m under the
# girders (A28 near Zwolle, I-84 over NE 181st Ave in Portland, I-395 in Springfield VA, 2026-10-22). Never raises.
UNDER_ON = os.environ.get("BNE_UNDERPASS", "on") != "off"
UNGRADED = {"raceway"}      # classes planned by nobody: the terrain is their surface (an oval's banking, GitHub #143) ...
UNGRADED_MARGIN_M = 2.0     # ... and nothing else grades their carriageway (+ this: kerbs, apron): a road crossing a
                            # track, or a tunnel's open approach under one, cut a flat trench through Daytona's banking
# The same structural headroom as the deck solver, including the lower road's
# physical surface lift. A stale 6.7 m target forced short motorway decks into
# clearance humps even after their underpass had been graded (Otto, Wyoming).
UNDER_CLEAR_M = roadsmod.DECK_ROAD_CLEAR + ROAD_LIFT
UNDER_GRADE = 0.06          # the dip's ramps
UNDER_MAX_BRIDGE_M = 120.0  # only single short spans (both ends on the ground): a chain's inner ends are no abutments
UNDER_MAX_DROP_M = 8.0      # never lowered more than this (bad data: rather a low bridge than a pit)
UNDER_FLAT_M = 12.0         # m either side of the crossing kept at the dip's full depth (under_dip)
UNDER_MAX_GRADE = 0.10      # a dip's ramp made steeper, up to this, to end inside its road part (under_dip)
UNDER_JOINT_FLAT_M = 12.0   # at a junction, every road takes the deepest dip within this of it ...
UNDER_JOINT_FADE_M = 20.0   # ... blended back to its own over this
# ... a road joined there rising back from it no steeper than the dip's own ramps (UNDER_GRADE; deeper, never less
# deep, its corners rounded over UNDER_GENTLE_SIGMA_M, its ends' shared offsets kept within UNDER_GENTLE_END_M).
# The kernel blends a junction's roads back over UNDER_JOINT_FADE_M whatever the depth (1.5 x depth / 20 m at most):
# the exit from Brooklyn's Prospect Expressway at Fort Hamilton Parkway left the town cut's 2.4 m dip at its gore
# within 32 m, 12-18 % on a motorway link beside the trench, a 0.11 m jolt and the link 2.6 m over its mainline
# (tools/nyc/connect_check.py, 2026-10-29)
UNDER_JOINT_GENTLE_ON = os.environ.get("BNE_UNDER_JOINT_GENTLE", "on") != "off"
UNDER_GENTLE_STEP_M = 1.0
UNDER_GENTLE_SIGMA_M = 3.0
UNDER_GENTLE_END_M = 10.0
# m around a cell's plans whose roads and spans its dips are solved on: the farthest a dip or a loose end reaches
# (underpass_offsets: flat + the longest ramp, a junction's share) and a span's length; every cell sees the same
UNDER_NEAR_M = 4 * VERTEX_STEP  # m past a cell's box its plans' dips are made (to their pieces' cut ends there)
UNDER_REACH_M = UNDER_FLAT_M + UNDER_MAX_DROP_M * math.pi / UNDER_GRADE + 2 * UNDER_JOINT_FLAT_M \
    + UNDER_JOINT_FADE_M + 10.0
# ... and under any other short overpass, by what its ramps could not raise it (BR-381 near Pouso Alegre: a local
# road's bridge whose ramps the frontage roads at both ends held down sat ~1 m over the motorway; the 30 m elevation
# data has no cutting under it, and the AI car drove into its abutment wall, 2026-10-25)
UNDER_ANY_ON = os.environ.get("BNE_UNDERPASS_ANY", "on") != "off"
# ... and a motorway in a town cut gives way first (gives_way): the street bridges over it stay at the street's level
# and its trench is cut to the clearance under them (the dip, UNDER_MAX_DROP_M at most; the ramps raise only what is
# left). A rural overpass stands on earth ramps; a city's depressed expressway runs under streets at grade, and the
# bare-earth DEM is interpolated across its trench under their decks (-0.1 to 2.4 m deep at the 11th Avenue bridges
# over Brooklyn's Prospect Expressway, 6.5 m between them), so the ramps lifted the streets 5-7 m into humps
# (tools/nyc/depth_check.py, 2026-10-29)
TOWN_CUT_ON = os.environ.get("BNE_TOWN_CUT", "on") != "off"
ABUT_NEAR_M = 14.0          # m from the bridge end the road is checked ...
ABUT_FAR_M = 40.0           # ... against its grade from ABUT_NEAR_M to this far
ABUT_STEEP = 0.15           # a fall this much steeper than that grade, near the end: the valley's side, not the road
ABUT_MIN_M = 0.3            # m: a smaller fill at the end is left out
ABUT_MAX_M = 8.0            # m filled at most
# ... and where the deck spans a road or a track (wide): a dip at the end up to ABUT_SLOPE_MAX_M wide, ABUT_GAP_MIN_M
# deep at least, is filled to its rim. The 10 m DEM spreads an embankment's end over 30-40 m: the Brooklyn-Queens
# Expressway on its embankment over Kent Avenue (40.6999,-73.9617) has a 37 m deck whose end lies at the bottom of the
# DEM's 100 m wide gap, 15 m of flat street level and then the slope up to the embankment 40 m out; nothing was filled,
# the deck sat at the street's 3.5 m, and Kent Avenue was dug 7 m under it into a pit (connect_check, 2026-10-29).
# Not over water or a valley: a real descent to a creek bridge is no gap
ABUT_SLOPE_MAX_M = 50.0
ABUT_GAP_MIN_M = 2.0
ABUT_LOOK_M = ABUT_SLOPE_MAX_M + (ABUT_FAR_M - ABUT_NEAR_M) + PROFILE_STEP   # m of the road on a wide fill reads (the
# profiles of the parts leading onto it, their chain extensions, abutment_height: each sees the same rim, or a 5 m
# stub onto the Washington Bridge's Bronx end, its extension 3 sigma long, saw none and stayed 8 m under the street it
# meets, filled to the rim 47 m on, 2026-10-29)
ABUT_STEEP_WIDE = 0.06      # a slope's grade over the one past it there (10-15 % on Kent Avenue's 34 m: a deck over a
                            # road is climbed to, so a dip down to it is the DEM's)


def median_profile(z, step, width):
    """The running median of a profile sampled every `step` m over `width` m (the window shrunk evenly near the ends, so
    an end keeps its height). Pure."""
    import fast
    half = int(round(width / 2.0 / step))
    if half < 1 or len(z) < 3:
        return np.asarray(z, np.float64)
    fast.require(71, "median_profile")
    return fast._ef.median_profile(np.ascontiguousarray(z, np.float64), half)


def smooth_profile(z, step, sigma):
    """Gaussian smoothing of a profile sampled every `step` m (turned about its end points past the ends: the ends keep
    their heights, so the parts of a road split at a junction meet at one height). Pure."""
    import fast
    fast.require(69, "smooth_profile")      # the Rust loop (~7000 roads a 9-tile set)
    return fast._ef.smooth_profile(np.ascontiguousarray(z, np.float64), float(step), float(sigma))


def resample(xy, step):
    """Points every `step` m along a polyline (xy (k, 2)), the last point kept, and their arc lengths."""
    import fast
    if not len(xy):
        return xy[:1].copy(), np.zeros(1)
    fast.require(0, "resample")
    return fast._ef.resample(np.ascontiguousarray(np.asarray(xy, np.float64)[:, :2]), float(step))


class Plan:
    """One road's grading: vertices (n, 2) in the frame's ENU metres, their target heights, core half width; abut: does
    it lead onto a bridge at its (first, last) vertex (grade_points)."""
    __slots__ = ("xy", "z", "half", "bbox", "abut", "cut", "road_half", "rid", "max_wall", "bank")

    def __init__(self, xy, z, half, abut=(False, False), cut=None, road_half=None):
        self.xy, self.z, self.half, self.abut = xy, z, float(half), (bool(abut[0]), bool(abut[1]))
        # (n - 1, 6) per segment, NaN where none: a cutting's wall offset from the centre line, its top's height and
        # the top's offset, on the left, then the right (_add_cuts)
        self.cut = cut
        self.road_half = float(road_half) if road_half is not None else float(half)    # the carriageway's half width
        self.rid = -1           # the road (map feature) it is part of: _plan_cell (each cell plans its own stretch of it)
        self.max_wall = WALL_MAX_H_M    # its cuttings' walls at most this tall (wall_max_h)
        self.bank = None                # (n,) its cross slope (m per m toward its left: a banked curve, bank) or None
        pad = half + T_MAX
        self.bbox = (xy[:, 0].min() - pad, xy[:, 1].min() - pad, xy[:, 0].max() + pad, xy[:, 1].max() + pad)


def clip_polyline(xy, box):
    """The runs (each (k, 2), k >= 2) of polyline xy (k, 2) inside box (x0, y0, x1, y1), segments cut at its edges
    (Liang-Barsky). Pure."""
    x0, y0, x1, y1 = box
    runs, cur = [], None
    for i in range(len(xy) - 1):
        px, py = float(xy[i, 0]), float(xy[i, 1])
        dx, dy = float(xy[i + 1, 0]) - px, float(xy[i + 1, 1]) - py
        ta, tb, ok = 0.0, 1.0, True
        for p, q in ((-dx, px - x0), (dx, x1 - px), (-dy, py - y0), (dy, y1 - py)):
            if p == 0.0:
                if q < 0.0:
                    ok = False
                    break
                continue
            r = q / p
            if p < 0.0:
                if r > tb:
                    ok = False
                    break
                ta = max(ta, r)
            else:
                if r < ta:
                    ok = False
                    break
                tb = min(tb, r)
        if not ok or tb <= ta:
            if cur is not None:
                runs.append(cur)
                cur = None
            continue
        a = (px + dx * ta, py + dy * ta) if ta > 0.0 else (px, py)
        b = (px + dx * tb, py + dy * tb) if tb < 1.0 else (float(xy[i + 1, 0]), float(xy[i + 1, 1]))
        if cur is not None and ta == 0.0:
            cur.append(b)
        else:
            if cur is not None:
                runs.append(cur)
            cur = [a, b]
        if tb < 1.0:
            runs.append(cur)
            cur = None
    if cur is not None:
        runs.append(cur)
    return [np.array(r, np.float64) for r in runs if len(r) >= 2]


def plan_road(frame, lonlat, spec, elev):
    """The Plan of one road part (lonlat (k, 2)); elev(lat, lon) -> m at PROFILE_ZOOM. Pure given elev."""
    return plan_roads(frame, [(lonlat, spec)], elev)[0]


def plan_roads(frame, parts, elev, abut=None, ext=None, pins=None, cuts=True, ext_abut=None):
    """plan_road for many (lonlat, spec) parts with one elev call (its cost is per call); abut: per part (start, end)
    flags, the ends leading onto a bridge (fill_abutment; 2: one spanning a road or a track, wide); ext: per part
    (before, after), the lines (k, 2) in the frame
    from its start / end outward along the part the road goes on in (Grader._extensions), or None: their ground is in
    the part's profile smoothing; ext_abut: per part (before, after) flags, that extension ends on a bridge (its ground
    filled there as the part leading onto it is); pins: per part (start, end) heights its ends are held at, or None
    (Grader._pins: a road ending on a more smoothed one meets it at its height). Pure given elev."""
    if not parts:
        return []
    ll = np.concatenate([lonlat for lonlat, _ in parts])
    E, N, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.0)
    prof, k = [], 0
    for j, (lonlat, spec) in enumerate(parts):
        m = len(lonlat)
        line = np.stack([E[k:k + m], N[k:k + m]], axis=1)
        k += m
        # (an even step, PROFILE_STEP or a little under, so the samples end on both ends: a short last step made the
        # smoothing lean at that end, and the parts of a road met 0.2 m apart on a 20 % grade)
        length = float(np.hypot(*np.diff(line, axis=0).T).sum()) if m >= 2 else 0.0
        step = length / max(1, math.ceil(length / PROFILE_STEP - 1e-9)) * (1.0 + 1e-9) if length > 0 else PROFILE_STEP
        xy, _ = resample(line, step)
        if len(xy) < 2:
            prof.append(None)
            continue
        side = []
        for out in (ext[j] if ext is not None else (None, None)):
            if out is None or len(out) < 2:
                side.append(np.zeros((0, 2)))
            else:                       # (from the node outward; the node is the part's own end sample)
                side.append(resample(np.asarray(out, np.float64), step)[0][1:])
        prof.append((xy, side[0][::-1], side[1], step))
    pts = [np.concatenate([b, xy, a]) for xy, b, a, _ in (p for p in prof if p is not None)]
    if not pts:
        return [None] * len(parts)
    allxy = np.concatenate(pts)
    la, lo, _ = frame.enu_to_geodetic(allxy[:, 0], allxy[:, 1], 0.0)
    zall = np.asarray(elev(la, lo), np.float64)
    out, k = [], 0
    for j, ((lonlat, spec), pr) in enumerate(zip(parts, prof)):
        if pr is None:
            out.append(None)
            continue
        xy, before, after, step = pr
        nb, na = len(before), len(after)
        zx = zall[k:k + nb + len(xy) + na]
        k += nb + len(xy) + na
        ab = (False, False)
        if abut is not None and ABUT_FILL_ON:
            ab = (bool(abut[j][0]), bool(abut[j][1]))
            if ab[0] and nb:            # (no extension past an end onto a bridge: its fill is the part's own)
                zx, nb = zx[nb:], 0
            if ab[1] and na:
                zx, na = zx[:len(zx) - na], 0
            # (filled with the extension past its other end in the profile: the fill's grade is read ABUT_NEAR_M to
            # ABUT_FAR_M from the abutment, further than a short part reaches)
            for at_end in (False, True):
                if ab[int(at_end)]:
                    zx = fill_abutment(zx, at_end, step, abut[j][int(at_end)] == 2)
        # (an extension running on to a bridge's abutment: its ground filled there as the part it runs along fills
        # it, so the two plans see the same ground. Raw, the DEM's smoothed trench wall at the abutment was in this
        # part's smoothing and not in that part's: Seeley Street planned 1.4 m under the DEM at its node with 18th
        # Street, 12 m from its bridge over Brooklyn's Prospect Expressway, where 18th Street's and the stub's plans
        # met the DEM: a 1.4 m spread at the junction, 2026-10-29)
        if ext_abut is not None and ABUT_FILL_ON:
            if ext_abut[j][0] and nb:
                zx = fill_abutment(zx, False, step, ext_abut[j][0] == 2)
            if ext_abut[j][1] and na:
                zx = fill_abutment(zx, True, step, ext_abut[j][1] == 2)
        pin = pins[j] if pins is not None else (None, None)
        if pin[0] is not None and not nb and not ab[0]:     # (the end sample held: the smoothing keeps an end's height)
            zx = zx.copy()
            zx[0] = pin[0]
        if pin[1] is not None and not na and not ab[1]:
            zx = zx.copy()
            zx[-1] = pin[1]
        out.append(_plan(xy, zx, spec, ab, (nb, na), step))
    if SHARP_ON and cuts:           # (cuts False: the profile alone, Grader._stronger_z)
        _add_cuts(frame, [p if p is not None and sp.kind in roadsmod.BUILT else None
                          for p, (_ll, sp) in zip(out, parts)], elev)
    return out


WALL_MAX_H_M = 4.0      # m a sharp cutting's wall stands over the road at most (the slope above it is graded) ...
WALL_MAX_H_CITY_M = 10.0  # ... a major road's in town (concrete): the full depth of its trench (Munich's McGraw-Graben:
                        # vertical concrete walls the whole way up, Mapillary 2026-10-21; with the 4 m cap the bank above
                        # stood as a bare cliff over the wall once the near ground was drawn sharp, 2026-10-22)


def wall_max_h(spec):
    """How tall a sharp cutting's wall beside road spec (roads.Spec) may stand: WALL_MAX_H_CITY_M for a major road in
    town (roads.CONCRETE_WALL_CLASSES, the concrete walls), else WALL_MAX_H_M (stone walls, mountain passes)."""
    return WALL_MAX_H_CITY_M if (spec.urban and spec.cls in roadsmod.CONCRETE_WALL_CLASSES) else WALL_MAX_H_M
HAIRPIN_SPAN_M = 20.0   # a road's direction this far back and on ...
HAIRPIN_TURN_DEG = 90.0  # ... turned this much: a tight bend (earthfast.tight_bends) ...
HAIRPIN_PAD_M = 15.0    # ... and this far either side of it: no sharp cutting, no retaining wall


def bend_zone(xy):
    """Which points of a polyline (k, 2) lie at or within HAIRPIN_PAD_M (along it) of a tight bend."""
    import fast
    fast.require(42, "tight_bends")
    xy = np.ascontiguousarray(np.asarray(xy, np.float64)[:, :2])
    m = np.asarray(fast._ef.tight_bends(xy, float(HAIRPIN_SPAN_M), float(HAIRPIN_TURN_DEG)))
    if not m.any():
        return m
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    sb = s[m]
    return np.abs(s[:, None] - sb[None, :]).min(axis=1) <= HAIRPIN_PAD_M


def _add_cuts(frame, plans, elev):
    """Sets Plan.cut on the plans (None entries skipped): per segment and side a cutting's wall (SHARP_*), from the raw
    ground elev(lat, lon) sampled outward from the carriageway's edge: the ground rising SHARP_MIN_M or more at
    SHARP_GRADE or steeper on average. The data spreads a vertical wall evenly round where it stands: its wall is put
    where the climb is half done (never on the carriageway; a fall: never inside the core, its sidewalk on the level),
    its top where it is done (the Rust kernels earthfast.sharp_probes / sharp_cuts; the ground lookup between them)."""
    import fast
    fast.require(42, "sharp cuttings")
    f64 = np.float64
    keep = [p for p in plans if p is not None and len(p.xy) >= 2]
    jobs = []
    for p in keep:
        q, o = fast._ef.sharp_probes(np.ascontiguousarray(p.xy[:, :2], f64), float(p.road_half), float(p.half),
                                     float(SHARP_REACH_M), float(SHARP_STEP_M))
        jobs.append((p, q, o))
    if not jobs:
        return
    allp = np.concatenate([q for _, q, _ in jobs])
    la, lo, _ = frame.enu_to_geodetic(allp[:, 0], allp[:, 1], 0.0)
    g = np.asarray(elev(np.asarray(la), np.asarray(lo)), f64)       # (as plan_roads' heights)
    k = 0
    for p, q, o in jobs:
        cut = fast._ef.sharp_cuts(np.ascontiguousarray(g[k:k + len(q)]), np.ascontiguousarray(p.z, f64), o,
                                  float(p.road_half), float(p.half), float(SHARP_MIN_M), float(SHARP_GRADE),
                                  float(SHARP_STEP_M), int(SHARP_RUN_N))
        k += len(q)
        if cut is not None:
            # (none at a tight bend: a hairpin's apex and approaches are graded smoothly, no wall into the turn)
            bend = bend_zone(p.xy[:, :2])
            cut = cut.copy()
            if bend.any():
                cut[bend[:-1] | bend[1:]] = np.nan
            # (a cutting's wall at most WALL_MAX_H_M tall, the ground above it sloping back to the hill as real pass
            # walls do; a 12 m wall stood between the Stelvio's hairpin legs, 2026-10-21)
            zmin = np.minimum(p.z[:-1], p.z[1:])
            for c in (1, 4):
                cut[:, c] = np.minimum(cut[:, c], zmin + getattr(p, "max_wall", WALL_MAX_H_M))
            if np.isfinite(cut[:, 0]).any() or np.isfinite(cut[:, 3]).any():
                p.cut = cut


def _point_along(line, d):
    """The point d m along polyline line (k, 2) from its first point (its last when shorter). Walks only as far as it
    needs (a whole line's cumulative length per road end cost Grader._extensions most of its time, 2026-10-25)."""
    acc = 0.0
    for i in range(len(line) - 1):
        ax, ay = float(line[i][0]), float(line[i][1])
        bx, by = float(line[i + 1][0]), float(line[i + 1][1])
        seg = math.hypot(bx - ax, by - ay)
        if seg > 0 and acc + seg >= d:
            t = (d - acc) / seg
            return np.array([ax + (bx - ax) * t, ay + (by - ay) * t])
        acc += seg
    return np.array([float(line[-1][0]), float(line[-1][1])])


def profile_sigma(spec):
    """m: the profile smoothing of a road of this spec."""
    if spec.kind not in roadsmod.BUILT:
        return DIRT_SIGMA_M
    return max(SIGMA_M, SIGMA_MAJOR_M) if spec.cls in MAJOR_CLASSES and not getattr(spec, "link", False) else SIGMA_M


def _plan(xy, z, spec, abut=(False, False), ext=(0, 0), step=PROFILE_STEP):
    """ext: (before, after), samples of z past the part's ends (plan_roads): smoothed with it, then dropped. step: m
    between the samples."""
    built = spec.kind in roadsmod.BUILT
    zs = smooth_profile(median_profile(z, step, MEDIAN_M if built else DIRT_MEDIAN_M), step,
                        profile_sigma(spec))[ext[0]:len(z) - ext[1]]
    # thin to VERTEX_STEP (the profile stays smooth between: linear in the grading pass)
    every = max(1, int(round(VERTEX_STEP / PROFILE_STEP)))
    keep = np.r_[np.arange(0, len(xy) - 1, every), len(xy) - 1]
    # the carriageway as a city street would have it (never from the land cover), or the measured one
    width = max(spec.width, roadsmod.carriageway(spec.cls, spec.kind, True, None, spec.oneway)) if built else spec.width
    if spec.median:             # a divided road's carriageway: never over the other one
        width = max(spec.width, roadsmod.dual_cap(spec.median, width))
    plan = Plan(xy[keep], zs[keep], width / 2 + shoulder(spec), abut, road_half=width / 2)
    plan.max_wall = wall_max_h(spec)
    b = bank(xy, spec, abut, ext)
    if b is not None:
        plan.bank = b[keep]
    return plan


# Banked curves (the user, 2026-10-27: "improve how roads generate"; BeamNG's Hero Road editor banks its curves, our
# carriageways were graded level across): a motorway's or trunk's curve leans into its turn, the cross slope by the
# design-speed rule e = BANK_E_MAX x min(1, r_min / R), r_min = v² / (127 (e_max + f)) of the class's design speed v
# (km/h) and side friction f; a gentle bend (under 1 %) keeps level. It comes in over BANK_RUNOFF_M and fades out over
# BANK_FADE_M where the road meets a bridge (a deck is level across) or ends without a road going on (a tunnel's
# floor, a junction). The grading pass (grade_points) tilts the core across the carriageway by it.
BANK_ON = os.environ.get("BNE_BANK", "on") != "off"
BANK_E_MAX = 0.08
BANK_DESIGN = {"motorway": (110.0, 0.10), "trunk": (90.0, 0.12)}     # km/h, side friction
BANK_LINK = (60.0, 0.16)                                              # their slip roads
BANK_STENCIL_M = 12.0       # the curve's circle through the points this far either side
BANK_RUNOFF_M = 18.0
BANK_FADE_M = 30.0


def bank(xy, spec, abut=(False, False), ext=(0, 0)):
    """(n,) the cross slope of a road part's centreline xy (n, 2) (m per m toward its left), or None (not banked)."""
    if not BANK_ON or spec.kind not in roadsmod.BUILT or spec.cls not in BANK_DESIGN or len(xy) < 3:
        return None
    v, f = BANK_LINK if getattr(spec, "link", False) else BANK_DESIGN[spec.cls]
    r_min = v * v / (127.0 * (BANK_E_MAX + f))
    import fast
    fast.require(84, "bank_profile")
    fade = [BANK_FADE_M if (abut[k] or not ext[k]) else 0.0 for k in (0, 1)]
    b = np.asarray(fast._ef.bank_profile(np.ascontiguousarray(np.asarray(xy, np.float64)[:, :2]), float(BANK_E_MAX),
                                         float(r_min), float(BANK_STENCIL_M), float(BANK_RUNOFF_M),
                                         float(fade[0]), float(fade[1])), np.float64)
    return b if np.any(b != 0.0) else None


def shoulder(spec):
    """m of flat ground beyond the carriageway edge, each side. Like the core width, never from the land
    cover: a road class that gets sidewalks in towns keeps a sidewalk-wide verge everywhere."""
    if spec.kind not in roadsmod.BUILT:
        return 0.0
    if spec.cls in roadsmod.SIDEWALK_CLASSES or (spec.cls == "trunk" and (not spec.oneway
                                                                          or roadsmod._arterial(spec))):
        return spec.walk + SIDEWALK_MARGIN_M
    return SHOULDER_M


def _twin_trimmed(secs):
    """secs ([(fid, spec, xy, z, kind)]) with the covered sections of twin tunnels (roads.twin_tunnels: side by side,
    one tube round both) cut to the stretch they share: one twin's portal a few metres deeper than the other's, the
    other's ground kept as it is over it (TUNNEL_KEEP_M wide) left the deeper one's approach on the hill there, its road
    mesh a wedge up into their shared mouth (Réunion's Route des Tamarins, GitHub #84). Pure."""
    cov = [n for n, sec in enumerate(secs) if sec[4] and len(sec[2]) >= 2]
    if len(cov) < 2:
        return secs
    parts = [(secs[n][0], secs[n][1], np.asarray(secs[n][2], np.float64), np.asarray(secs[n][3], np.float64), 0.0)
             for n in cov]
    out = list(secs)
    for i, j, _mid, _w in roadsmod.twin_tunnels(parts):
        for a, b in ((i, j), (j, i)):
            xa, xb = parts[a][2], parts[b][2]
            ub = xb[-1] - xb[0]
            ub = ub / max(float(np.hypot(*ub)), 1e-9)
            t = (xa - xb[0]) @ ub                   # along b, from its start
            keep = (t >= 0.0) & (t <= float((xb[-1] - xb[0]) @ ub))
            if keep.sum() >= 2 and not keep.all():
                fid, spec, xy, z, kind = secs[cov[a]]
                k = np.flatnonzero(keep)
                out[cov[a]] = (fid, spec, np.asarray(xy)[k[0]:k[-1] + 1], np.asarray(z)[k[0]:k[-1] + 1], kind)
    return out


class Grader:
    """The road plans of one anchor frame, per map cell, made once (worker threads; it fetches the road
    cells it lacks)."""

    def __init__(self, frame, mapdata, elev, widths=None, urban=None):
        self.frame, self.md, self.elev, self.widths = frame, mapdata, elev, widths
        # urban(lat, lon) -> bool array (surface.Surface.urban, the road tiles' own) or None: which major roads are in
        # town, for their cuttings' wall height only (wall_max_h; _town_walls). Without it every plan was rural, so a
        # city trench (Brooklyn's Prospect Expressway, ~7 m) got a 4 m wall and a graded bank of earth above it under
        # the road tiles' full-height concrete wall
        self.urban = urban
        self.cells = {}
        self.failed = {}        # cell key -> monotonic time of the last failed fetch
        self.lock = threading.Lock()
        self.key_locks = {}     # cell key -> Lock: one thread plans a cell, the others wait for it
        self.covers = {}        # cell key -> [(xy (k, 2), half width)]: the tunnels whose ground over them stays
        self.keeps = {}         # cell key -> [(xy (k, 2), half width)]: the UNGRADED roads' carriageways
        self.opens = {}         # cell key -> [(xy (k, 2), Spec)]: the tunnels' open sections (roads in the open)
        self.shared = None      # plancache.PlanCache: the cells' plans shared with the server's other processes
        self.wet = None         # wet(lat, lon) -> water depth (m): tunnels under water are left out, as
                                # roads.TunnelProfiles leaves them (their "open" sections graded a dam across the river:
                                # the Maastunnel's bed raised from -6 m to -0.4 m, storm-ace's GitHub #85)

    def _town_walls(self, feats, specs):
        """specs with urban set as the road tiles have it (self.urban) on the concrete-wall classes, the only use of
        urban in a plan (wall_max_h); as they are without self.urban."""
        if self.urban is None:
            return specs
        try:
            town = roadsmod.road_specs(feats, self.urban, None)
            out = [sp._replace(urban=True) if sp is not None and t is not None and t.urban
                   and sp.cls in roadsmod.CONCRETE_WALL_CLASSES else sp for sp, t in zip(specs, town)]
            # (a link is never urban in road_specs, its width being its own: the land cover at its points, in one
            # call; the ramps in a city trench beside the expressway take its concrete wall and give way under the
            # street bridges as it does, gives_way: Brooklyn's Prospect Expressway, 2026-10-29)
            links = [i for i, sp in enumerate(out) if sp is not None and _is_link(sp) and not sp.urban
                     and sp.cls in roadsmod.CONCRETE_WALL_CLASSES]
            if links:
                spans = [(feats.coord_off[feats.ring_off[feats.part_off[i]]],
                          feats.coord_off[feats.ring_off[feats.part_off[i + 1]]]) for i in links]
                ll = np.concatenate([feats.lonlat[a:b] for a, b in spans])
                u = np.asarray(self.urban(ll[:, 1], ll[:, 0]), bool)
                k = 0
                for i, (a, b) in zip(links, spans):
                    if b > a and float(np.mean(u[k:k + (b - a)])) >= roadsmod.URBAN_SHARE:
                        out[i] = out[i]._replace(urban=True)
                    k += b - a
            return out
        except Exception as e:          # rural walls rather than no grading
            log.debug("grading: town roads: %s: %s", type(e).__name__, e)
            return specs

    def _cell_plans(self, key):
        with self.lock:
            got = self.cells.get(key)
            kl = self.key_locks.setdefault(key, threading.Lock())
        if got is not None:
            return got
        with kl:
            return self._plan_cell(key)

    def _plan_cell(self, key):
        with self.lock:
            got = self.cells.get(key)
            failed = self.failed.get(key)
        if got is not None:
            return got
        if failed is not None and time.monotonic() - failed < RETRY_S:
            return []               # offline: no block waits on the same failing fetch again
        shared, mine = (self.shared.claim(key) if self.shared is not None else (None, False))
        try:
            import revisit
            f0 = revisit.faults()
            got = shared if shared is not None else self._make_cell(key)
            if got is None:
                return []
            if mine:        # (kept for later visits too when complete: nothing it read failed or was missing)
                self.shared.store(key, *got, persist=revisit.faults() == f0)
        finally:
            if mine:
                self.shared.release(key)
        plans, keeps, covers, opens = got
        with self.lock:
            self.cells.setdefault(key, plans)
            self.keeps.setdefault(key, keeps)
            self.covers.setdefault(key, covers)
            self.opens.setdefault(key, opens)
            return self.cells[key]

    def _make_cell(self, key):
        """(plans, keeps, covers, opens) of cell key, or None when its roads could not be fetched."""
        try:
            feats = self.md.cell(key, layers=("roads",), kind="demand").layers.get("roads")
        except Exception as e:      # no road data: ungraded (asked again after RETRY_S)
            log.debug("grading: roads of %s: %s: %s", key, type(e).__name__, e)
            with self.lock:
                self.failed[key] = time.monotonic()
            return None
        plans = []
        if feats is not None and len(feats):
            specs = self._town_walls(feats, roadsmod.road_specs(feats, None, self.widths))
            parts, ids, clipped = [], [], []
            box = self._clip_box(key)
            keeps = []
            for i in range(len(feats)):
                if specs[i] is not None and specs[i].cls in UNGRADED:
                    for ring in roadsmod.ground_parts(feats, i):
                        e, n, _ = self.frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
                        keeps.append((np.stack([np.asarray(e), np.asarray(n)], axis=1),
                                      specs[i].width / 2 + UNGRADED_MARGIN_M))
                    continue
                if specs[i] is None:
                    continue
                for ring in roadsmod.ground_parts(feats, i):     # not bridges / tunnels
                    pieces = self._near_cell(np.asarray(ring, np.float64), box)
                    for r in pieces:
                        parts.append((r, specs[i]))
                        ids.append(str(feats.ids[i]))
                        clipped.append(len(pieces) > 1 or r is not ring)
            pspecs = []
            abut = None
            if (ABUT_FILL_ON or APPROACH_ON) and parts:
                try:
                    pe = np.array([[r[0], r[-1]] for r, _sp in parts], np.float64).reshape(-1, 2)
                    e, n, _ = self.frame.geodetic_to_enu(pe[:, 1], pe[:, 0], 0.0)
                    pxy = np.stack([np.asarray(e), np.asarray(n)], axis=1)
                    abut = self._abut_ends(key, pxy).astype(np.int64)
                    for m in np.flatnonzero(abut):      # (2: the deck there spans a road or a track)
                        abut[m] = 2 if self._end_wide(pxy[m]) else 1
                    abut = abut.reshape(-1, 2)
                except Exception as e:      # no fills rather than no grading
                    log.debug("grading: abutments of %s: %s: %s", key, type(e).__name__, e)
            ext = pins = ext_ab = None
            if CHAIN_ON and parts:
                try:
                    self._chain_cands(key, feats, specs)        # (this cell's own: its specs are at hand)
                    # (ABUT_LOOK_M either way for a part onto a deck spanning a road, or whose extension runs onto one)
                    long = [bool(abut is not None and (abut[j] == 2).any()) for j in range(len(parts))]
                    ext = self._extensions(parts, ids, long)
                    ext_ab = self._ext_abuts(key, ext) if ABUT_FILL_ON else None
                    more = [lg or bool(ext_ab is not None and 2 in ext_ab[j]) for j, lg in enumerate(long)]
                    if more != long:
                        ext = self._extensions(parts, ids, more)
                        ext_ab = self._ext_abuts(key, ext)
                    pins = self._pins(parts, ids, ext)
                except Exception as e:      # each part smoothed alone rather than no grading
                    log.debug("grading: road chains of %s: %s: %s", key, type(e).__name__, e)
            pclip = []
            for fid, part, cl, p in zip(ids, parts, clipped, plan_roads(self.frame, parts, self.elev, abut, ext, pins,
                                                                        ext_abut=ext_ab)):
                if p is not None:
                    p.rid = road_id(fid)
                    plans.append((fid, p))
                    pspecs.append(part[1])
                    pclip.append(cl)
            if APPROACH_ON and plans:
                try:
                    self._lift_approaches(key, plans, pspecs)
                except Exception as e:      # ungraded approaches rather than no grading
                    log.debug("grading: approaches of %s: %s: %s", key, type(e).__name__, e)
            if UNDER_ON and plans:
                try:
                    self._lower_underpasses(key, plans)
                except Exception as e:      # as the data has it rather than no grading
                    log.debug("grading: underpasses of %s: %s: %s", key, type(e).__name__, e)
            # a long road's piece (planned with CLIP_MARGIN_M of the road either side for its profile) kept only within
            # the cell's own box: every stretch of road in one cell's plans, once (overlapping pieces from neighbouring
            # cells counted a road up to 9 times, and every batch of points graded differently: patches of ground
            # through the Stelvio's asphalt, GitHub #108's 2.5 m step at a bridge end in Meppen, 2026-10-21)
            own = self._clip_box(key, 0.0)
            sub = feats.attrs.get("subtype")
            if RAIL_GRADE_ON and own is not None and sub is not None and (np.asarray(sub).astype(str) == "rail").any():
                try:            # the railways' bed lines (railways.grade_plans): cuttings and embankments for the track
                    import railways     # (the cells around only where this one has a line: their fetches cost)
                    rcells = [self.md.cell(k, layers=("roads",), kind="demand").layers.get("roads")
                              for k in railways.cells_near(self.frame, own)]
                    for fid, xy, z in railways.grade_plans(self.frame, rcells, own, self.elev):
                        p = Plan(xy, z, railways.GRADE_HALF_M)
                        p.rid = road_id(RAIL_TAG + fid)
                        plans.append((RAIL_TAG + fid, p))
                        pclip.append(True)
                except Exception as e:      # no rail grading rather than no grading
                    log.debug("grading: railways of %s: %s: %s", key, type(e).__name__, e)
            # every plan only within the cell's own box, so each stretch of road is planned by one cell (its dips
            # made with every road around: _lower_underpasses); the tunnels' ends graded as before, a part shorter
            # than LONG_PART_M by all of it (TunnelProfiles.ends grades them by the roads' plans alone)
            tplans = plans
            if own is not None:
                kept, tplans = [], []
                for (fid, p), cl in zip(plans, pclip):
                    pieces = trim_plan(p, own)
                    kept.extend((fid, q) for q in pieces)
                    tplans.extend((fid, q) for q in (pieces if cl else [p]))
                plans = kept
            secs = self._tunnel_sections(feats, key, [p for _f, p in tplans])
            tun = [(fid + TUNNEL_TAG, Plan(xy, z, spec.width / 2 + shoulder(spec), road_half=spec.width / 2))
                   for fid, spec, xy, z, k in secs if k == 0] + self._mouth_plans(secs)
            self._tunnel_walls(feats, specs, tun)
            plans.extend(tun)
            covers = [(xy, spec.width / 2 + TUNNEL_KEEP_M) for _f, spec, xy, _z, k in _twin_trimmed(secs) if k]
            opens = [(xy, spec) for _f, spec, xy, _z, k in secs if k == 0]
        else:
            covers, opens, keeps = [], [], []
        return plans, keeps, covers, opens

    def _chain_cands(self, key, feats=None, specs=None):
        """The graded ground parts of cell key's roads, as the continuations at a node in it are looked for (every cell
        that plans a part looks in its nodes' own cells, so its plan is the same in each): [(xy (k, 2) in the frame,
        smoothing sigma, road id, lon/lat ring, spec, feature id)], in the map's feature order. Kept per cell. feats, specs: the cell's roads and their
        road_specs when the caller has them."""
        memo = self.__dict__.setdefault("_chain_memo", {})
        with self.lock:
            got = memo.get(key)
        if got is not None:
            return got
        out = []
        if feats is None:
            feats = self.md.cell(key, layers=("roads",), kind="demand").layers.get("roads")
        if feats is not None and len(feats):
            if specs is None:
                specs = roadsmod.road_specs(feats, None, self.widths)
            rows = []
            for i in range(len(feats)):
                if specs[i] is None or specs[i].cls in UNGRADED:
                    continue
                for ring in roadsmod.ground_parts(feats, i):
                    rows.append((i, np.asarray(ring, np.float64)))
            if rows:            # (the frame's conversion once for all: it is per call)
                allr = np.concatenate([r for _, r in rows])
                e, n, _ = self.frame.geodetic_to_enu(allr[:, 1], allr[:, 0], 0.0)
                en = np.stack([np.asarray(e, np.float64), np.asarray(n, np.float64)], axis=1)
                k0 = 0
                for i, ring in rows:
                    out.append((en[k0:k0 + len(ring)], profile_sigma(specs[i]), road_id(str(feats.ids[i])), ring,
                                specs[i], str(feats.ids[i])))
                    k0 += len(ring)
        with self.lock:
            if len(memo) > 64:
                memo.clear()
            memo[key] = out
        return out

    def _extensions(self, parts, ids, long=None):
        """plan_roads' ext of a cell's parts [(lonlat, spec)] (ids: their features'): per part (before, after), the line
        from each end outward along the part the road goes on in there (earthfast.chain_extensions), 3 sigma of its
        smoothing long (ABUT_LOOK_M at least where long[j]: a wide fill's), or None."""
        import fast
        fast.require(69, "road chains")
        f64 = np.float64
        q_node, q_dir, q_class, q_road, q_reach, q_cell = [], [], [], [], [], []
        if not parts:
            return []
        # (the frame's conversion once for every part: it is per call)
        alll = np.concatenate([np.asarray(ll, f64) for (ll, _sp) in parts])
        ae, an, _ = self.frame.geodetic_to_enu(alll[:, 1], alll[:, 0], 0.0)
        axy = np.stack([np.asarray(ae, f64), np.asarray(an, f64)], axis=1)
        k0 = 0
        for n, ((lonlat, spec), fid) in enumerate(zip(parts, ids)):
            xy = axy[k0:k0 + len(lonlat)]
            k0 += len(lonlat)
            sg = profile_sigma(spec)
            reach = max(3.0 * sg + PROFILE_STEP, ABUT_LOOK_M) if long is not None and long[n] else 3.0 * sg + PROFILE_STEP
            for end in (0, 1):
                line = xy if end == 0 else xy[::-1]          # from this end inward
                p = _point_along(line, CHAIN_LOOK_M)
                q_node.append(line[0])
                q_dir.append(line[0] - p)                    # the way the road goes on out of this end
                q_class.append(int(round(sg * 1000)))
                q_road.append(road_id(fid))
                q_reach.append(reach)
                q_cell.append(lonlat[0] if end == 0 else lonlat[-1])
        if q_cell:                  # (the ends' cells at once)
            qll = np.asarray(q_cell, np.float64).reshape(-1, 2)
            q_cell = mapcells.cells_of(qll[:, 1], qll[:, 0])
        out = [[None, None] for _ in parts]
        for key in sorted(set(q_cell), key=str):
            cands = self._chain_cands(key)
            qs = [i for i, c in enumerate(q_cell) if c == key]
            if not cands or not qs:
                continue
            cxy, coff, ccls, croad = self._cand_arrays(key, cands)
            pts, off = fast._ef.chain_extensions(
                cxy, coff, ccls, croad,
                np.ascontiguousarray([q_node[i] for i in qs], f64), np.ascontiguousarray([q_dir[i] for i in qs], f64),
                np.array([q_class[i] for i in qs], np.int64), np.array([q_road[i] for i in qs], np.int64),
                np.array([q_reach[i] for i in qs], f64), float(CHAIN_JOIN_M), float(CHAIN_TURN_DEG), float(CHAIN_LOOK_M))
            for j, i in enumerate(qs):
                if off[j + 1] - off[j] >= 2:
                    out[i // 2][i % 2] = np.asarray(pts[off[j]:off[j + 1]])
        return [tuple(o) for o in out]

    def _ext_abuts(self, key, ext):
        """plan_roads' ext_abut of a cell's extensions (_extensions): per part (before, after), does that extension end
        on a bridge's end (_abut_ends: within APPROACH_TOUCH_M)? None when none does."""
        far = [(j, e, np.asarray(o, np.float64)[-1, :2]) for j, pair in enumerate(ext or ())
               for e, o in enumerate(pair) if o is not None and len(o) >= 2]
        if not far:
            return None
        hit = self._abut_ends(key, np.array([p for _j, _e, p in far], np.float64))
        if not hit.any():
            return None
        out = [[0, 0] for _ in ext]
        for (j, e, p), h in zip(far, hit):
            out[j][e] = (2 if self._end_wide(p) else 1) if h else 0
        return [tuple(o) for o in out]

    def _cand_arrays(self, key, cands):
        """_chain_cands(key) as the Rust kernels take them (points, offsets, sigma class, road id), made once per cell."""
        memo = self.__dict__.setdefault("_cand_arrays_memo", {})
        got = memo.get(str(key))
        if got is not None and got[0] is cands:
            return got[1]
        arr = (np.ascontiguousarray(np.concatenate([c[0] for c in cands]), np.float64),
               np.r_[0, np.cumsum([len(c[0]) for c in cands])].astype(np.int64),
               np.array([int(round(c[1] * 1000)) for c in cands], np.int64),
               np.array([c[2] for c in cands], np.int64))
        with self.lock:
            if len(memo) > 64:
                memo.clear()
            memo[str(key)] = (cands, arr)
        return arr

    def _pins(self, parts, ids, ext):
        """plan_roads' pins of a cell's parts: an end that no part of its own smoothing goes on from (ext None there) and
        that ends on another road's part smoothed as much or more (a slip road on a motorway, a side road on a trunk or
        on a street of its own class) is held at that road's planned height there (_stronger_z), so the two meet at one
        height (a same-class side road ended at the raw ground, the street through the junction at its smoothed height:
        0.6 m apart on a hill). The most smoothed such road, the first of equals. None when no end is held."""
        import fast
        fast.require(70, "road pins")
        f64 = np.float64
        q_node, q_cell, q_who = [], [], []
        for j, ((lonlat, spec), fid) in enumerate(zip(parts, ids)):
            for end in (0, 1):
                if ext is not None and ext[j][end] is not None:
                    continue
                ll = lonlat[-1] if end else lonlat[0]
                q_node.append((float(ll[1]), float(ll[0])))         # (lat, lon: converted below, all at once)
                q_who.append((j, end, profile_sigma(spec), road_id(fid)))
        if q_node:
            qa = np.asarray(q_node, f64)
            q_cell = mapcells.cells_of(qa[:, 0], qa[:, 1])
            qe, qn, _ = self.frame.geodetic_to_enu(qa[:, 0], qa[:, 1], 0.0)
            q_node = list(zip(np.asarray(qe, f64).tolist(), np.asarray(qn, f64).tolist()))
        out = [[None, None] for _ in parts]
        held = False
        todo = []                           # (part, end, candidate, at its last point): heights asked all at once
        for key in sorted(set(q_cell), key=str):
            cands = self._chain_cands(key)
            qs = [i for i, c in enumerate(q_cell) if c == key]
            weakest = min(q_who[i][2] for i in qs)
            strong = [c for c in cands if c[1] >= weakest]
            if not strong:
                continue
            cxy = np.concatenate([c[0] for c in strong])
            coff = np.r_[0, np.cumsum([len(c[0]) for c in strong])].astype(np.int64)
            hits, off = fast._ef.node_parts(np.ascontiguousarray(cxy, f64), coff,
                                            np.ascontiguousarray([q_node[i] for i in qs], f64), float(CHAIN_JOIN_M))
            for k, i in enumerate(qs):
                j, end, sg, rid = q_who[i]
                best = None
                for h in hits[off[k]:off[k + 1]]:
                    c = strong[int(h) // 2]
                    if c[1] >= sg and c[2] != rid and (best is None or c[1] > best[0][1]):
                        best = (c, bool(int(h) % 2))
                if best is not None:
                    todo.append((j, end, best[0], best[1]))
        for (j, end, _c, _l), z in zip(todo, self._stronger_zs([(c, last) for _j, _e, c, last in todo])):
            if z is not None:
                out[j][end] = z
                held = True
        return [tuple(o) for o in out] if held else None

    def _stronger_zs(self, asks):
        """_stronger_z of each (candidate, at_last) of asks, the ones not kept yet planned in one plan_roads call (the
        same plans: each part is planned on its own; its cost is per call, ~1 ms, 935 calls in 4 Penn Station cells,
        2026-10-25)."""
        memo = self.__dict__.setdefault("_strong_memo", {})
        keys = [self._strong_key(c, last) for c, last in asks]
        with self.lock:
            miss = [n for n, k in enumerate(keys) if k not in memo]
        jobs, seen = [], set()
        for n in miss:
            if keys[n] not in seen:
                seen.add(keys[n])
                jobs.append(n)
        if len(jobs) > 1:
            try:
                preps = [self._strong_part(*asks[n]) for n in jobs]
                parts = [(part, asks[n][0][4]) for n, (part, _last, _whole) in zip(jobs, preps)]
                ext = self._extensions(parts, [asks[n][0][5] for n in jobs])
                plans = plan_roads(self.frame, parts, self.elev, None, ext, cuts=False)
                for n, (part, last, whole), p in zip(jobs, preps, plans):
                    self._strong_keep(asks[n][0], keys[n], p, last, whole)
            except Exception as e:      # one at a time, each its own failure (_stronger_z)
                log.debug("grading: pin heights at once: %s: %s", type(e).__name__, e)
        return [self._stronger_z(c, last) for c, last in asks]

    @staticmethod
    def _strong_key(cand, at_last):
        xy, rid = cand[0], cand[2]
        e = xy[-1 if at_last else 0]
        return (rid, round(float(e[0]), 1), round(float(e[1]), 1))

    def _strong_part(self, cand, at_last):
        """(the lon/lat part planned for _stronger_z, whose end is asked for: its last point?, the whole part?)"""
        xy, _sg, rid, ring, spec, fid = cand
        part = ring
        if float(np.hypot(*np.diff(xy, axis=0).T).sum()) > LONG_PART_M:
            end = xy[-1] if at_last else xy[0]
            m = CLIP_MARGIN_M
            runs = clip_polyline(xy, (end[0] - m, end[1] - m, end[0] + m, end[1] + m))
            run = min(runs, key=lambda r: min(np.hypot(*(r[0] - end)), np.hypot(*(r[-1] - end)))) if runs else None
            if run is not None:
                la, lo, _ = self.frame.enu_to_geodetic(run[:, 0], run[:, 1], 0.0)
                part = np.stack([np.asarray(lo), np.asarray(la)], axis=1)
                at_last = bool(np.hypot(*(run[-1] - end)) <= np.hypot(*(run[0] - end)))
        return part, at_last, part is ring

    def _strong_keep(self, cand, k, p, at_last, whole):
        """Keep a _stronger_z height (and the other end's, for a part planned whole)."""
        z, other = None, None
        if p is not None:
            z = float(p.z[-1] if at_last else p.z[0])
            if whole:
                xy, rid = cand[0], cand[2]
                e2 = xy[0 if at_last else -1]
                other = ((rid, round(float(e2[0]), 1), round(float(e2[1]), 1)), float(p.z[0] if at_last else p.z[-1]))
        memo = self.__dict__.setdefault("_strong_memo", {})
        with self.lock:
            if len(memo) > 4096:
                memo.clear()
            memo.setdefault(k, z)
            if other is not None:
                memo.setdefault(*other)

    def _stronger_z(self, cand, at_last):
        """The planned height of a candidate part (_chain_cands') at its first or last point: its own plan (its chains,
        no pins), within CLIP_MARGIN_M of that point when it is long. Kept per part end (a short part's plan gives both
        ends' heights: the other kept too)."""
        k = self._strong_key(cand, at_last)
        memo = self.__dict__.setdefault("_strong_memo", {})
        with self.lock:
            if k in memo:
                return memo[k]
        try:
            part, last, whole = self._strong_part(cand, at_last)
            # (the profile alone: its cutting's walls cost most of a plan and no height, 2026-10-25)
            p = plan_roads(self.frame, [(part, cand[4])], self.elev, None,
                           self._extensions([(part, cand[4])], [cand[5]]), cuts=False)[0]
            self._strong_keep(cand, k, p, last, whole)
        except Exception as e:      # not held rather than no grading
            log.debug("grading: pin height of %s: %s: %s", cand[5], type(e).__name__, e)
            with self.lock:
                memo[k] = None
        with self.lock:
            return memo.get(k)

    def _clip_box(self, key, margin=CLIP_MARGIN_M):
        """The cell's box in the frame (east / north), margin wider each side; None for a key without bounds."""
        if not hasattr(key, "bounds"):
            return None
        lon0, lat0, lon1, lat1 = key.bounds()
        e, n, _ = self.frame.geodetic_to_enu(np.array([lat0, lat0, lat1, lat1]), np.array([lon0, lon1, lon0, lon1]),
                                             0.0)
        e, n = np.asarray(e), np.asarray(n)
        m = margin
        return float(e.min()) - m, float(n.min()) - m, float(e.max()) + m, float(n.max()) + m

    def _near_cell(self, ring, box):
        """A road part (lon/lat ring) as planned for a cell: as it is, or, longer than LONG_PART_M, the pieces of it
        within the cell's box (each its own plan). A cell's plans sampled the ground along every road through it
        to its ends: on the Bonneville salt flats, tracks one segment 27 km long, ~2,500 DEM tiles for one road
        tile's four cells (2026-10-20). The profile's smoothing (SIGMA_M) reaches a few tens of metres: the plan
        within the cell is the same."""
        if box is None or len(ring) < 2:
            return [ring]
        e, n, _ = self.frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
        xy = np.stack([np.asarray(e, np.float64), np.asarray(n, np.float64)], axis=1)
        if float(np.hypot(*np.diff(xy, axis=0).T).sum()) <= LONG_PART_M:
            return [ring]
        out = []
        for run in clip_polyline(xy, box):
            la, lo, _ = self.frame.enu_to_geodetic(run[:, 0], run[:, 1], 0.0)
            out.append(np.stack([np.asarray(lo, np.float64), np.asarray(la, np.float64)], axis=1))
        return out

    def _lift_approaches(self, key, plans, specs):
        """Raise, in place, the plans of the roads leading onto overpasses (APPROACH_ON). A road is raised where one of
        its ends is a bridge's abutment that must go up for the deck to clear the road under it (_end_lift); the raise
        falls off along the road (approach_add) and never comes nearer another road than its fill slope allows. Only
        what the plan's own line and the map around its ends say counts, the same in every cell that plans it (a long
        road is planned in each cell it passes; two cells raising the same road differently drew the road under a
        raised ground, 2026-10-20 I-20 in Greene County, AL). Major roads (APPROACH_SKIP) are never raised."""
        # every plan end at once: near the cell, and on a bridge end of its own cell (most touch none)
        pe = np.array([[p.xy[0][:2], p.xy[-1][:2]] for _f, p in plans], np.float64).reshape(-1, 2)
        ok = np.repeat([s is not None and not _never_raised(s) for s in specs], 2)
        touch = self._abut_ends(key, pe, ok).reshape(-1, 2)
        for i, (fid, p) in enumerate(plans):
            if not touch[i].any():
                continue
            add = None
            for at_end in (False, True):
                e = p.xy[-1] if at_end else p.xy[0]
                if not touch[i, int(at_end)]:
                    continue
                got = self._end_lift(e)
                if got is None:
                    continue
                lift, others = got[0], got[1]
                a = approach_add(p.xy, at_end, lift, p.half, others)
                if a is not None:
                    add = a if add is None else np.maximum(add, a)
            if add is not None:
                np_ = Plan(p.xy, p.z + add, p.half, p.abut, p.cut, p.road_half)
                np_.rid, np_.max_wall, np_.bank = p.rid, p.max_wall, getattr(p, "bank", None)
                plans[i] = (fid, np_)

    def _lower_underpasses(self, key, plans):
        """Lower, in place, the plans that cross under a short never-raised overpass (UNDER_*), and the roads joined
        to them (underpass_offsets), only within the cell's box (each plan is trimmed to it after). Solved on every
        ground road and span within UNDER_REACH_M of the box, from whichever cells they lie in, each road whole, its
        depth from its ground (_under_ground) rather than its plan: every cell gets the same dips. (On its own cell's roads alone, a road ending at a junction whose other roads
        lay only in the next cell was pinned there by one cell and lowered 3.5 m by the other; their copies met as a
        step on US-101 by Los Angeles' Union Station, 2026-10-27.)"""
        f64 = np.float64
        box = self._clip_box(key, UNDER_NEAR_M)
        if box is None:
            return
        xys = [np.asarray(p.xy, f64)[:, :2] for _fid, p in plans]
        lo = np.array(box[:2]) - UNDER_REACH_M
        hi = np.array(box[2:]) + UNDER_REACH_M
        # (the spans' cells a span's length further: one joined to another past the box is no single span)
        pad = UNDER_MAX_BRIDGE_M + 10.0
        cla, clo, _ = self.frame.enu_to_geodetic(np.array([lo[0] - pad, hi[0] + pad] * 2),
                                                 np.array([lo[1] - pad] * 2 + [hi[1] + pad] * 2), 0.0)
        cla, clo = np.asarray(cla, f64), np.asarray(clo, f64)
        keys = sorted(set(mapcells.cells_for_bbox(float(cla.min()), float(clo.min()), float(cla.max()),
                                                  float(clo.max()))) | {key}, key=str)
        bridges, ground = [], {}
        for k in keys:
            c = self._cell_enu(k)
            for line, cls in zip(c["bridges"], c.get("bridge_cls", [None] * len(c["bridges"]))):
                if cls is None:
                    continue
                if not (UNDER_ANY_ON or cls in APPROACH_SKIP or cls.endswith("_link")):
                    continue
                bridges.append((line, cls in APPROACH_SKIP, False))     # (a link's "<cls>_link": raised)
            # (a line's deck: never raised; straight between its ends whatever its length or what it joins, as
            # railways.deck_heights lays it: no single-span test)
            bridges += [(line, True, True) for line in c.get("rail_bridges", ())]
            for (line, _h, cls), (fid, j, sg, med) in zip(c["ground"], c["ground_ids"]):
                if cls in UNGRADED or len(line) < 2:
                    continue
                if (line.max(0) < lo).any() or (line.min(0) > hi).any():
                    continue
                got = ground.get((fid, j))      # (the longest copy of a part, as each cell has it)
                if got is None or len(line) > len(got[0]):
                    ground[(fid, j)] = (line, sg, med)
        if not bridges or not ground:
            return
        # (single spans, or short chains of parts joined at their ends: the pieces a deck is carried onto the roads it
        # joins (roads._skew_decks), a deck forking at its end (11th Avenue's over Brooklyn's Prospect Expressway: three
        # parts, so no span, and no dip under a street deck 5 m over the trench floor, 2026-10-29); UNDER_MAX_BRIDGE_M
        # over the chain, its abutments at the chain's free ends (an inner joint is none); deduplicated across cells;
        # a railway's line alone, straight whatever it joins)
        uniq, seen = [], set()
        for b, fixed, rail in bridges:
            k2 = _span_key(b)
            if k2 not in seen:
                seen.add(k2)
                uniq.append((b, fixed, rail))
        ue = np.array([[b[0, :2], b[-1, :2]] for b, _f, _r in uniq], f64)        # (n, 2 ends, 2)
        parent = list(range(len(uniq)))

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a
        for n, (b, _f, rail) in enumerate(uniq):
            if rail:
                continue
            d = np.hypot(ue[:, :, None, 0] - ue[n, None, None, :, 0], ue[:, :, None, 1] - ue[n, None, None, :, 1])
            d[n] = np.inf                       # (another part's end within 0.75 m of either of its ends: joined)
            for m in np.flatnonzero((d < 0.75).any(axis=(1, 2))):
                if not uniq[m][2]:
                    parent[find(int(m))] = find(n)
        groups = {}
        for n in range(len(uniq)):
            groups.setdefault(find(n), []).append(n)
        spans = []          # (line, L, fixed, (start free, end free), the chain's free ends [(xy, fixed)] or None alone)
        for ms in groups.values():
            Ls = {m: float(np.hypot(*np.diff(uniq[m][0], axis=0).T).sum()) for m in ms}
            if not uniq[ms[0]][2] and sum(Ls.values()) > UNDER_MAX_BRIDGE_M:
                continue
            pts = [(m, e) for m in ms for e in (0, 1)]
            free = {(m, e): not any(np.hypot(*(ue[m2, e2] - ue[m, e])) < 0.75 for m2, e2 in pts if m2 != m)
                    for m, e in pts}
            ends = [(ue[m, e], uniq[m][1]) for (m, e), fr in free.items() if fr]
            if not ends:
                continue                        # (a ring of decks: no abutment)
            for m in ms:
                if len(ms) == 1 and Ls[m] < 2 * APPROACH_MIN_CROSS_M:
                    continue
                spans.append((uniq[m][0], Ls[m], uniq[m][1], (free[(m, 0)], free[(m, 1)]), ends if len(ms) > 1 else None))
        if not spans:
            return
        gids = sorted(ground)
        lines = [ground[g][0] for g in gids]
        crs = roadsmod._crossings_by([sp[0] for sp in spans], lines)
        ez, raised = {}, set()
        first = np.array([ln[0] for ln in lines], f64)
        last = np.array([ln[-1] for ln in lines], f64)

        def z_at(p):
            pla, plo, _ = self.frame.enu_to_geodetic(p[:, 0], p[:, 1], 0.0)
            return np.asarray(self.elev(np.asarray(pla, f64), np.asarray(plo, f64)), f64).ravel()

        def end_height(end, fixed):     # (an overpass's abutment's ground as the approaches' plans fill it
            k2 = (round(float(end[0]), 1), round(float(end[1]), 1))     # (abutment_height), raised when it is: as high
            got = ez.get(k2)            # as its ramps could take it; asked only for one a road crosses under: every
            if got is None:             # span's took 2 s a Canton cell, 2026-10-25)
                touch = (np.hypot(*(first - end).T) <= APPROACH_TOUCH_M) | (np.hypot(*(last - end).T) <= APPROACH_TOUCH_M)
                got = abutment_height(end, [lines[m] for m in np.flatnonzero(touch)], z_at, lines, self._end_wide(end))
                if not fixed:
                    got += self._end_raised(np.asarray(end, f64))
                ez[k2] = got
            return got
        cands = []
        for i, cr in enumerate(crs):
            if not cr:
                continue
            xy = lines[i]
            s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
            ab = np.diff(xy, axis=0)
            for c in cr:
                bi, arc_b, pt = c[0], c[1], np.asarray(c[2], f64)
                b, L, fixed, free, chain_ends = spans[bi]
                if (arc_b < APPROACH_MIN_CROSS_M and free[0]) or (arc_b > L - APPROACH_MIN_CROSS_M and free[1]):
                    continue                    # a junction at the bridge's end
                if chain_ends is None:
                    ze = (end_height(b[0], fixed), end_height(b[-1], fixed))
                    corridor = np.clip([arc_b - roadsmod.DECK_CLEAR_REACH_M, arc_b + roadsmod.DECK_CLEAR_REACH_M], 0.0, L)
                    deck = float(np.min(ze[0] + (ze[1] - ze[0]) * corridor / L))
                else:                           # (a chain: as low as its lowest abutment, the whole deck over)
                    deck = min(end_height(p, fx) for p, fx in chain_ends)
                # where along this road (its nearest vertex segment)
                t = np.clip(((pt - xy[:-1]) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-12), 0.0, 1.0)
                j = int(np.argmin(np.linalg.norm(xy[:-1] + t[:, None] * ab - pt, axis=1)))
                cands.append((i, float(s[j] + t[j] * (s[j + 1] - s[j])), deck))
        if not cands:
            return
        zc = self._under_ground(lines, [ground[g][1:] for g in gids], cands)
        requirements = []
        for (i, sc, deck), z in zip(cands, zc):
            target = max(deck - UNDER_CLEAR_M, z - UNDER_MAX_DROP_M)
            if target < z - 0.1:
                requirements.append((i, sc, z - target))
        if not requirements:
            return
        # each plan on its road's line (of its feature's parts, the nearest)
        by_fid = {}
        for n, (fid, _j) in enumerate(gids):
            by_fid.setdefault(fid, []).append(n)
        queries, which = [], []
        inside = []
        for n, (fid, p) in enumerate(plans):
            cand = by_fid.get(fid)
            xy = xys[n]
            near = (xy[:, 0] >= box[0]) & (xy[:, 1] >= box[1]) & (xy[:, 0] <= box[2]) & (xy[:, 1] <= box[3])
            if not cand or not near.any():
                continue
            mid = xy[near][int(near.sum()) // 2]
            best = min(cand, key=lambda m: float(np.min(np.hypot(*(lines[m] - mid).T))))
            queries.append((best, xy[near]))
            which.append(n)
            inside.append(near)
        if not queries:
            return
        for n, near, got in zip(which, inside, underpass_offsets(lines, requirements, queries)):
            if not np.any(got < -1e-8):
                continue
            fid, p = plans[n]
            offset = np.zeros(len(near))        # (past the box: the plan's piece there is another cell's)
            offset[near] = got
            np_ = Plan(p.xy, np.asarray(p.z) + offset, p.half, p.abut, p.cut, p.road_half)
            np_.rid, np_.max_wall, np_.bank = p.rid, p.max_wall, getattr(p, "bank", None)
            plans[n] = (fid, np_)

    def _under_ground(self, lines, smooth, cands):
        """The heights of ground roads where they cross under a bridge, cands [(line, arc m, _)]: their ground along
        them, median-filtered and smoothed as their plans are (_plan: smooth (sigma, median width) per line), at the
        crossing. A pure function of the road and the ground (its plan's own height is only in the cells planning it).
        """
        f64 = np.float64
        pts, spans = [], []
        for i, sc, _d in cands:
            sg, med = smooth[i]
            xy = lines[i]
            s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
            k = int(math.ceil(3.0 * sg / PROFILE_STEP))
            a = np.clip(sc + PROFILE_STEP * np.arange(-k, k + 1), 0.0, s[-1])
            pts.append(np.stack([np.interp(a, s, xy[:, 0]), np.interp(a, s, xy[:, 1])], axis=1))
            spans.append((k, sg, med))
        allp = np.concatenate(pts)
        la, lo, _ = self.frame.enu_to_geodetic(allp[:, 0], allp[:, 1], 0.0)
        z = np.asarray(self.elev(np.asarray(la, f64), np.asarray(lo, f64)), f64).ravel()
        out, k0 = [], 0
        for k, sg, med in spans:
            zz = z[k0:k0 + 2 * k + 1]
            k0 += 2 * k + 1
            out.append(float(smooth_profile(median_profile(zz, PROFILE_STEP, med), PROFILE_STEP, sg)[k]))
        return out

    def _abut_ends(self, key, pe, ok=None):
        """Which of the points pe (n, 2) (road ends; ok: those to look at) near cell key are within APPROACH_TOUCH_M
        of a bridge's end in their own cell."""
        la, lo, _ = self.frame.enu_to_geodetic(pe[:, 0], pe[:, 1], 0.0)
        la, lo = np.asarray(la, np.float64), np.asarray(lo, np.float64)
        lon0, lat0, lon1, lat1 = (float(v) for v in key.bounds())
        dla = np.maximum(np.maximum(lat0 - la, la - lat1), 0.0) * 111320.0
        dlo = np.maximum(np.maximum(lon0 - lo, lo - lon1), 0.0) * 111320.0 * np.cos(np.radians(la))
        touch = np.hypot(dla, dlo) <= APPROACH_CELL_M
        if ok is not None:
            touch &= ok
        homes = {}
        tj = np.flatnonzero(touch)
        for j, home in zip(tj.tolist(), mapcells.cells_of(la[tj], lo[tj])):
            homes.setdefault(home, []).append(j)
        for home, js in homes.items():
            ends = self._cell_enu(home, ground=False)["ends"]
            js = np.asarray(js)
            if not len(ends):
                touch[js] = False
                continue
            d = np.hypot(pe[js, None, 0] - ends[None, :, 0], pe[js, None, 1] - ends[None, :, 1])
            touch[js] = (d <= APPROACH_TOUCH_M).any(axis=1)
        return touch

    def _end_raised(self, xy):
        """m the abutment at bridge end xy is raised by its approach ramps (_lift_approaches: the most any road leading
        onto it rises there, its fill slope kept clear of the roads beside), 0 when it is not raised."""
        got = self._end_lift(xy)
        if got is None:
            return 0.0
        lift, others, approaches = got
        best = 0.0
        for line, half in approaches:
            at_end = bool(np.hypot(*(line[-1] - xy)) <= np.hypot(*(line[0] - xy)))
            a = approach_add(line, at_end, lift, half, others)
            if a is not None:
                best = max(best, float(a[-1] if at_end else a[0]))
        return best

    def _end_lift(self, xy):
        """(lift m, other road lines [(xy (k, 2), half width)], the roads leading onto it [(xy, half width)]) when the
        road end xy is a bridge abutment to raise, else None. A pure function of the map around the bridge (its cells,
        fetched as needed), cached per point."""
        k = (round(float(xy[0]) * 2), round(float(xy[1]) * 2))
        with self.lock:
            if not hasattr(self, "end_cache"):
                self.end_cache = {}
            if k in self.end_cache:
                return self.end_cache[k]
        res = None
        try:
            res = self._end_lift_calc(np.asarray(xy, np.float64))
        except Exception as e:      # no ramp rather than no grading
            log.debug("grading: approach at %s: %s: %s", k, type(e).__name__, e)
        with self.lock:
            return self.end_cache.setdefault(k, res)

    def _end_wide(self, xy):
        """Does the bridge whose end is at road end xy (APPROACH_TOUCH_M) span a ground road or a drawn track away from
        its chain's free ends (fill_abutment's wide)? The cells and lines _end_lift_calc reads; cached per point."""
        k = (round(float(xy[0]) * 2), round(float(xy[1]) * 2))
        with self.lock:
            memo = self.__dict__.setdefault("wide_cache", {})
            if k in memo:
                return memo[k]
        res = False
        try:
            res = self._end_wide_calc(np.asarray(xy, np.float64)[:2])
        except Exception as e:      # (the narrow fill rather than no grading)
            log.debug("grading: deck span at %s: %s: %s", k, type(e).__name__, e)
        with self.lock:
            return memo.setdefault(k, res)

    def _end_wide_calc(self, xy):
        la, lo, _ = self.frame.enu_to_geodetic(np.array([xy[0]]), np.array([xy[1]]), 0.0)
        home = mapcells.cell_of(float(la[0]), float(lo[0]))
        hc = self._cell_enu(home, ground=False)
        if not len(hc["ends"]):
            return False
        d = np.hypot(hc["ends"][:, 0] - xy[0], hc["ends"][:, 1] - xy[1])
        hit = np.flatnonzero(d <= APPROACH_TOUCH_M)
        if not len(hit):
            return False
        bridge = hc["bridges"][hc["end_of"][int(hit[0])]]
        lam, lom, _ = self.frame.enu_to_geodetic(bridge[:, 0], bridge[:, 1], 0.0)
        pad = 50.0 / 111320.0
        padlon = pad / max(0.05, math.cos(math.radians(float(np.mean(lam)))))
        keys = sorted(set(mapcells.cells_for_points(lam, lom, pad, padlon)) | {home}, key=str)
        cs = [self._cell_enu(k) for k in keys]
        spans = [bridge] + bridge_chain(bridge, [b for c in cs for b in c["bridges"]])
        blo = np.min([b.min(0) for b in spans], axis=0)
        bhi = np.max([b.max(0) for b in spans], axis=0)
        table = self._ground_table(keys, cs)
        # (one road leading onto it: at a junction on the deck's end each part's fill read its own profile, and a 5 m
        # stub onto the Washington Bridge's Bronx end stayed 8 m under the street it meets, filled to the rim 47 m on;
        # the narrow fill there as before, 2026-10-29)
        touch = (np.hypot(*(table[4] - xy).T) <= APPROACH_TOUCH_M) | (np.hypot(*(table[5] - xy).T) <= APPROACH_TOUCH_M)
        if int(touch.sum()) != 1:
            return False
        lines = [ln for ln in table[0] + [r for c in cs for r in c.get("rail_ground", ())]
                 if not ((ln.max(0) < blo).any() or (ln.min(0) > bhi).any())]
        return spans_a_road(spans, lines)

    def _cell_enu(self, k, ground=True):
        """Cell k's road parts in the anchor frame, made once: {"bridges": [line (k, 2)], "ends": (n, 2) bridge end
        points, "end_of": [bridge index per end], "ground": [(line, half width, cls)], "ground_ids":
        [(feature id, part index, profile sigma, median width)] per ground part} (the longest copy of each part).
        With ground False, only the bridges (most plan ends touch none: their cells' ground roads are not needed).
        Kept for the ENU_CACHE_MAX cells used last; a cell another thread is making is waited for (cleared whole at
        128 cells, while the planning threads made the same cells at once, a 36-tile downtown Los Angeles set made
        318 cells for 242, ~125 ms each, 2026-10-27)."""
        key = (str(k), ground)
        with self.lock:
            if not hasattr(self, "enu_cache"):
                self.enu_cache, self.enu_making = OrderedDict(), {}
            while True:
                got = self.enu_cache.get(key) or (self.enu_cache.get((str(k), True)) if not ground else None)
                if got is not None:
                    self.enu_cache.move_to_end(key if key in self.enu_cache else (str(k), True))
                    return got
                ev = self.enu_making.get(key)
                if ev is None:
                    ev = self.enu_making[key] = threading.Event()
                    break
                self.lock.release()
                try:
                    ev.wait(60.0)
                finally:
                    self.lock.acquire()
                if key not in self.enu_cache and self.enu_making.get(key) is ev:    # (its maker gave up: make it here)
                    break
        try:
            out = self._cell_enu_calc(k, ground)
            with self.lock:
                self.enu_cache[key] = out
                self.enu_cache.move_to_end(key)
                while len(self.enu_cache) > ENU_CACHE_MAX:
                    self.enu_cache.popitem(last=False)
            return out
        finally:
            with self.lock:
                if self.enu_making.get(key) is ev:
                    del self.enu_making[key]
            ev.set()

    def _cell_enu_calc(self, k, ground):
        parts = {}
        f = self.md.cell(k, layers=("roads",), kind="demand").layers.get("roads")
        if f is not None and len(f):
            bs = roadsmod.bridge_specs(f)
            # (in town on the concrete-wall classes, for gives_way: as _make_cell's plans have it)
            rs = self._town_walls(f, roadsmod.road_specs(f, None, self.widths)) if ground else [None] * len(f)
            for i in range(len(f)):
                if bs[i] is None and rs[i] is None:
                    continue
                fid = str(f.ids[i])
                for kind, sp, ps in (("bridge", bs[i], roadsmod.bridge_parts(f, i) if bs[i] is not None else ()),
                                     ("ground", rs[i], roadsmod.ground_parts(f, i) if rs[i] is not None else ())):
                    for j, ring in enumerate(ps):
                        ring = np.asarray(ring, np.float64)
                        kk = (fid, kind, j)
                        if len(ring) >= 2 and (kk not in parts or len(ring) > len(parts[kk][0])):
                            parts[kk] = (ring, sp)
        # the rail bridges (railways.DRAWN lines' bridge pieces): the roads under them dip as under a road overpass
        # (_lower_underpasses), nothing else (no ramps, no abutments: a line's deck is never raised for a road).
        # (GitHub #311 / #345 / #364: the track lay across the road under a rail bridge)
        rail, rail_g = [], []
        if f is not None and len(f) and f.attrs.get("subtype") is not None:
            import railways
            sub, rcls = f.attrs["subtype"], f.attrs.get("class")
            for i in range(len(f)):
                if str(sub[i]) != "rail" or railways.drawn(f, i) is None:
                    continue
                for kind, rings in railways._pieces(f, i):
                    if kind == "bridge":
                        rail += [np.asarray(r, np.float64) for r in rings if len(r) >= 2]
                    elif kind == "ground" and ground:
                        # (and the tracks on the ground: a road bridge's approaches rise over them as over a road,
                        # GitHub #324)
                        rail_g += [np.asarray(r, np.float64) for r in rings if len(r) >= 2]
        bridges, ground_l, ends, end_of, bcls, ground_ids, sunk = [], [], [], [], [], [], []
        items = sorted(parts.items())
        if items:               # (the frame's conversion once for every part: it is per call)
            allr = np.concatenate([ring for _k, (ring, _sp) in items])
            ae, an, _ = self.frame.geodetic_to_enu(allr[:, 1], allr[:, 0], 0.0)
            axy = np.stack([np.asarray(ae, np.float64), np.asarray(an, np.float64)], axis=1)
        k0 = 0
        for (fid, kind, j), (ring, sp) in items:
            line = axy[k0:k0 + len(ring)]
            k0 += len(ring)
            if kind == "bridge":
                end_of += [len(bridges), len(bridges)]
                ends += [line[0], line[-1]]
                bridges.append(line)
                cls = getattr(sp, "cls", None)
                bcls.append(cls + "_link" if cls is not None and _is_link(sp) else cls)
            else:
                ground_l.append((line, sp.width / 2 + shoulder(sp), sp.cls))
                ground_ids.append((fid, j, profile_sigma(sp), MEDIAN_M if sp.kind in roadsmod.BUILT else DIRT_MEDIAN_M))
                sunk.append(gives_way(sp))
        rail_xy, rail_gxy = [], []
        if rail or rail_g:
            allr = np.concatenate(rail + rail_g)
            re_, rn, _ = self.frame.geodetic_to_enu(allr[:, 1], allr[:, 0], 0.0)
            rxy = np.stack([np.asarray(re_, np.float64), np.asarray(rn, np.float64)], axis=1)
            k0 = 0
            for ring, out_ in [(r, rail_xy) for r in rail] + [(r, rail_gxy) for r in rail_g]:
                out_.append(rxy[k0:k0 + len(ring)])
                k0 += len(ring)
        return {"bridges": bridges, "bridge_cls": bcls, "ground": ground_l, "ground_ids": ground_ids, "end_of": end_of,
                "ends": np.asarray(ends, np.float64).reshape(-1, 2), "rail_bridges": rail_xy, "rail_ground": rail_gxy,
                "gives_way": sunk}      # (per ground part: lowered under a bridge before its ramps rise, gives_way)

    def _ground_table(self, keys, cs):
        """The ground road parts of the cells keys (cs: their _cell_enu), each once (the first copy, by its end points
        and length), in their order: (lines, half widths, boxes' lo (n, 2), hi (n, 2), first points, last points,
        gives way (n,) bool). Kept per cell set (every bridge end near them asks: a loop over each part per end was
        most of _lift_approaches, 2026-10-25)."""
        mk = tuple(str(k) for k in keys)
        memo = self.__dict__.setdefault("_ground_tables", {})
        with self.lock:
            got = memo.get(mk)
        if got is not None and all(a is b for a, b in zip(got[0], cs)):
            return got[1]
        lines, halves, sunk, seen = [], [], [], set()
        for c in cs:
            for (line, half, _cls), way in zip(c["ground"], c.get("gives_way") or [False] * len(c["ground"])):
                # (its last point too: two roads from one node with as many points, two 2-point segments, were one)
                key = (round(float(line[0, 0]), 1), round(float(line[0, 1]), 1), round(float(line[-1, 0]), 1),
                       round(float(line[-1, 1]), 1), len(line))
                if key in seen:
                    continue
                seen.add(key)
                lines.append(line)
                halves.append(half)
                sunk.append(bool(way))
        f64 = np.float64
        if lines:
            tab = (lines, halves, np.array([ln.min(0) for ln in lines], f64), np.array([ln.max(0) for ln in lines], f64),
                   np.array([ln[0] for ln in lines], f64), np.array([ln[-1] for ln in lines], f64), np.array(sunk, bool))
        else:
            z = np.zeros((0, 2), f64)
            tab = (lines, halves, z, z, z, z, np.zeros(0, bool))
        with self.lock:
            if len(memo) > 64:
                memo.clear()
            memo[mk] = (list(cs), tab)
        return tab

    def _end_lift_calc(self, xy):
        la, lo, _ = self.frame.enu_to_geodetic(np.array([xy[0]]), np.array([xy[1]]), 0.0)
        home = mapcells.cell_of(float(la[0]), float(lo[0]))
        hc = self._cell_enu(home, ground=False)
        if not len(hc["ends"]):
            return None
        d = np.hypot(hc["ends"][:, 0] - xy[0], hc["ends"][:, 1] - xy[1])
        hit = np.flatnonzero(d <= APPROACH_TOUCH_M)
        if not len(hit):
            return None
        bridge = hc["bridges"][hc["end_of"][int(hit[0])]]
        # the bridge's own cells (a few, it is short): the roads crossing under it and the parts it joins
        lam, lom, _ = self.frame.enu_to_geodetic(bridge[:, 0], bridge[:, 1], 0.0)
        pad = 50.0 / 111320.0
        padlon = pad / max(0.05, math.cos(math.radians(float(np.mean(lam)))))
        keys = sorted(set(mapcells.cells_for_points(lam, lom, pad, padlon)) | {home}, key=str)
        cs = [self._cell_enu(k) for k in keys]
        bridges = [b for c in cs for b in c["bridges"]]
        lo_b, hi_b = bridge.min(0) - APPROACH_MAX_RUN - 50.0, bridge.max(0) + APPROACH_MAX_RUN + 50.0
        ground, others, approaches = [], [], []
        chain = bridge_chain(bridge, bridges)
        spans = [bridge] + chain        # (a road under any part of the chain is spanned, not beside the ramp: #138)
        blo = np.min([b.min(0) for b in spans], axis=0)
        bhi = np.max([b.max(0) for b in spans], axis=0)
        lines, halves, lo_l, hi_l, first, last, way = self._ground_table(keys, cs)
        near = np.flatnonzero(~((hi_l < lo_b).any(1) | (lo_l > hi_b).any(1)))   # (the rest nowhere near the bridge)
        tch = np.minimum(np.hypot(first[near, 0] - xy[0], first[near, 1] - xy[1]),
                         np.hypot(last[near, 0] - xy[0], last[near, 1] - xy[1])) <= APPROACH_TOUCH_M
        box = ~((hi_l[near] < blo).any(1) | (lo_l[near] > bhi).any(1))
        inbox = np.flatnonzero(box)     # (which of them cross under the chain: one kernel call for all)
        crossing = np.zeros(len(near), bool)
        for n, cr in zip(inbox, roadsmod._crossings_by(spans, [lines[near[n]] for n in inbox])):
            crossing[n] = bool(cr)
        sunk = []       # (the roads under it that give way: a town motorway's trench is cut under the bridge first)
        for n, i in enumerate(near):
            line, half = lines[i], halves[i]
            (sunk if way[i] else ground).append(line)
            touches = bool(tch[n])
            under = bool(crossing[n])
            if touches:
                approaches.append((line, half))
            # the roads beside the ramp, which its fill slope must not reach (not the ones the bridge spans: its
            # abutment stands between)
            if not touches and not under:
                others.append((line, half))

        # (the tracks near it: crossed under like a road, never an approach or a road beside the ramp)
        rails = [line for c in cs for line in c.get("rail_ground", ())
                 if not ((line.max(0) < lo_b).any() or (line.min(0) > hi_b).any())]

        def z_at(p):
            la_, lo_, _ = self.frame.enu_to_geodetic(p[:, 0], p[:, 1], 0.0)
            return np.asarray(self.elev(la_, lo_), np.float64)
        # (the abutment as the approaches' plans fill it, at the end asked for; the other end's lift is not returned)
        ez = abutment_height(xy, [ln for ln, _h in approaches], z_at, [lines[i] for i in near], self._end_wide(xy))
        end_z = tuple(ez if np.hypot(*(e - xy)) <= APPROACH_TOUCH_M else None for e in (bridge[0], bridge[-1]))
        for end, lift in bridge_lifts(bridge, bridges, ground, z_at, chain, rails, sunk, end_z):
            if np.hypot(*(end - xy)) <= APPROACH_TOUCH_M:
                return lift, others, approaches
        return None

    def _longest(self, fid, j, ring, key):
        """Part j of tunnel fid as the cells at its ends have it, the longest (a part comes clipped differently in
        the cells around: graded as the 30 m piece, 97th Street's 45 m arch got an open road graded in it; the
        same rule as roads.BridgeProfiles._load, 2026-10-17)."""
        best = ring
        for p in (ring[0], ring[-1]):
            ck = mapcells.cell_of(float(p[1]), float(p[0]))
            if key is not None and str(ck) == str(key):
                continue
            try:
                f = self.md.cell(ck, layers=("roads",), kind="demand").layers.get("roads")
            except Exception:
                continue
            if f is None or not len(f):
                continue
            ids = [str(x) for x in f.ids]
            if fid not in ids:
                continue
            i = ids.index(fid)
            parts = roadsmod.tunnel_parts(f, i)
            if j < len(parts) and len(parts[j]) > len(best):
                best = parts[j]
        return best

    def _chains_for(self):
        """A roads.TunnelProfiles of this frame for its chain questions (drowned): the same chains, from the same map
        cells, as the road tiles' and terrain blocks' own."""
        if getattr(self, "_chains", None) is None:
            md = self.md
            t = roadsmod.TunnelProfiles(self.frame, lambda key: md.cell(key, layers=("roads",), kind="demand")
                                        .layers.get("roads"), self.elev)
            t.wet = self.wet
            self._chains = t
        return self._chains

    def _tunnel_sections(self, feats, key=None, road_plans=()):
        """[(fid, spec, xy, z, kind)] of the tunnels' sections (roads.tunnel_sections on the ungraded ground, its
        ends graded by road_plans: the cell's road Plans, as roads.TunnelProfiles.ends grades them with the roads'
        plans alone): kind 0 an open road at z, 1 a low arch, 2 a full tunnel."""
        out = []
        ts = roadsmod.tunnel_specs(feats)
        for i in range(len(feats)):
            if ts[i] is None:
                continue
            for j, ring in enumerate(roadsmod.tunnel_parts(feats, i)):
                ring = self._longest(str(feats.ids[i]), j, ring, key)
                ll = np.asarray(ring, np.float64)
                if len(ll) < 2:
                    continue
                E, N, _ = self.frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.0)
                xy, s = resample(np.stack([np.asarray(E), np.asarray(N)], axis=1), VERTEX_STEP)
                if len(xy) < 2 or s[-1] <= 0:
                    continue
                la, lo, _ = self.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
                if self.wet is not None and \
                        (np.asarray(self.wet(la, lo)) > 0.5).mean() > roadsmod.TUNNEL_WET_SHARE:
                    continue
                if self.wet is not None and self._chains_for().drowned((str(feats.ids[i]), j), ring):
                    continue            # (a dry piece of an underwater tunnel: left out with it, GitHub #128)
                g = np.asarray(self.elev(la, lo), np.float64)
                if road_plans:
                    g = g.copy()
                    g[[0, -1]] = grade_points(xy[[0, -1], 0], xy[[0, -1], 1], g[[0, -1]], list(road_plans))
                floor, kinds = roadsmod.tunnel_sections(s, g)
                i0 = 0
                while i0 < len(kinds):
                    i1 = i0
                    while i1 + 1 < len(kinds) and kinds[i1 + 1] == kinds[i0]:
                        i1 += 1
                    a = max(0, i0 - 1)          # sharing the end sample with the section before
                    if i1 > a:
                        out.append((str(feats.ids[i]), ts[i], xy[a:i1 + 1], floor[a:i1 + 1], int(kinds[i0])))
                    i0 = i1 + 1
        return out

    @staticmethod
    def _mouth_plans(secs):
        """(fid, Plan) of the covered sections' mouths: the floor MOUTH_PLAN_M into the tube from a portal (where an
        open section or nothing comes before / after it): the ground at the portal graded to the road on both sides
        of it (the grading faded from the road's plan into the ungraded hill there, and the road tile and the
        terrain block, sampled at different zooms, came out a few cm apart: the block poked through the road
        outside 97th Street's arch, 2026-10-17)."""
        out = []
        for n, (fid, spec, xy, z, kind) in enumerate(secs):
            if not kind or len(xy) < 2:
                continue
            same = lambda m: 0 <= m < len(secs) and secs[m][0] == fid     # noqa: E731
            s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
            hw = spec.width / 2 + shoulder(spec)
            for at_start in (True, False):
                nb = n - 1 if at_start else n + 1
                if same(nb) and secs[nb][4] != 0:
                    continue                    # inside the hill: a full tunnel goes on
                if at_start:
                    m = s <= MOUTH_PLAN_M
                    t = np.interp(MOUTH_PLAN_M, s, np.arange(len(s)))
                else:
                    m = s >= s[-1] - MOUTH_PLAN_M
                    t = np.interp(s[-1] - MOUTH_PLAN_M, s, np.arange(len(s)))
                i0 = int(np.floor(t))
                p = xy[i0] + (t - i0) * (xy[min(i0 + 1, len(xy) - 1)] - xy[i0])
                pz = z[i0] + (t - i0) * (z[min(i0 + 1, len(z) - 1)] - z[i0])
                if at_start:
                    pxy, pzz = np.r_[xy[m], p[None]], np.r_[z[m], pz]
                else:
                    pxy, pzz = np.r_[p[None], xy[m]], np.r_[pz, z[m]]
                if len(pxy) >= 2:
                    out.append((fid + TUNNEL_TAG + ("m0" if at_start else "m1"),
                                Plan(pxy, pzz, hw, road_half=spec.width / 2)))
        return out

    def _tunnel_walls(self, feats, specs, tun):
        """The tunnels' open sections and mouths' plans (tun: [(name, Plan)]) walled as the road's own ground parts are:
        its cuttings' walls (_add_cuts) as tall as its class has them in town (wall_max_h of its spec, specs: the
        cell's road_specs with _town_walls' urban; a road all in tunnels has none: the land cover at the plan). Without,
        the floor of a motorway's trench mapped as a run of short tunnels (the BQE in Williamsburg under every cross
        street) was graded out to the streets on an earth slope: Division Avenue's abutment over it was pulled from its
        street's 15.5 m to 12.9 m 7 m from the tunnel's mouth, a 2.6 m ramp in 4 m onto its deck (connect_check,
        2026-10-29)."""
        if not tun:
            return
        by = {str(feats.ids[i]): sp for i, sp in enumerate(specs) if sp is not None}
        ts = roadsmod.tunnel_specs(feats)
        tby = {str(feats.ids[i]): sp for i, sp in enumerate(ts) if sp is not None}
        for name, p in tun:
            fid = name.split(TUNNEL_TAG)[0]
            sp = by.get(fid)
            if sp is None and self.urban is not None and fid in tby and tby[fid].cls in roadsmod.CONCRETE_WALL_CLASSES:
                la, lo, _ = self.frame.enu_to_geodetic(p.xy[:, 0], p.xy[:, 1], 0.0)
                try:
                    town = float(np.mean(np.asarray(self.urban(np.asarray(la), np.asarray(lo)), bool))) \
                        >= roadsmod.URBAN_SHARE
                except Exception as e:          # (rural walls rather than no grading)
                    log.debug("grading: tunnel's town: %s: %s", type(e).__name__, e)
                    town = False
                sp = tby[fid]._replace(urban=town)
            p.max_wall = wall_max_h(sp) if sp is not None else WALL_MAX_H_M
        if SHARP_ON:
            try:
                _add_cuts(self.frame, [p for _n, p in tun], self.elev)
            except Exception as e:      # (graded on slopes as before rather than no grading)
                log.debug("grading: tunnels' cuttings: %s: %s", type(e).__name__, e)

    def _covered_tunnels(self, feats):
        """[(xy, half width)] of the tunnels' covered sections (not open roads): the grading leaves the ground over
        them as it is (apply): the approaches' grading reached up to T_MAX past a portal and cut a park bridge's soil
        down under its arch's vault, the terrain then cut away over it (2026-10-17)."""
        return [(xy, spec.width / 2 + TUNNEL_KEEP_M) for _f, spec, xy, _z, kind in _twin_trimmed(self._tunnel_sections(feats))
                if kind]

    def _open_tunnels(self, feats):
        """(fid, Plan) of the tunnels' open sections (roads.TUNNEL_OPEN_COVER): the ground graded to the road's floor
        (the lower hull of the ground), as the road is drawn (roads._tunnel_chunks): over a footbridge's hump in the
        DEM the road climbed it, the ground over the road (Central Park's transverses, 2026-10-17)."""
        return [(fid, Plan(xy, z, spec.width / 2 + shoulder(spec)))
                for fid, spec, xy, z, kind in self._tunnel_sections(feats) if kind == 0]

    def planned(self, key):
        """True once cell key's plans exist (made, or failed recently)."""
        with self.lock:
            f = self.failed.get(key)
            return key in self.cells or (f is not None and time.monotonic() - f < RETRY_S)

    def warm(self, key):
        """Make cell key's plans now (worker threads: the lookahead ahead of the player)."""
        self._cell_plans(key)

    def _plan_missing(self, keys):
        """The cells of keys not planned yet, planned at once on up to PLAN_THREADS threads (their map data and
        elevation downloads overlap: a fresh place's first block planned its 3-4 cells one after another, 27-35 s of
        its wait, 2026-10-22); the caller's urgent elevation lane (a teleport's first block) carried over."""
        with self.lock:
            missing = [k for k in keys if k not in self.cells]
        if len(missing) < 2:
            return
        dem = getattr(self, "dem", None)
        urgent = dem is not None and getattr(dem, "is_urgent", lambda: False)()

        def one(k):
            if urgent:
                with dem.urgent():
                    return self._cell_plans(k)
            return self._cell_plans(k)
        with ThreadPoolExecutor(min(len(missing), PLAN_THREADS), thread_name_prefix="plans") as ex:
            list(ex.map(one, missing))

    def plans(self, lat, lon, roads_only=False):
        """The Plans touching the points' area (each road once); roads_only: not the tunnels' (TUNNEL_TAG)."""
        return self._plan_set(self._keys(lat, lon), roads_only)[0]

    @staticmethod
    def _keys(lat, lon):
        """The map cells whose plans can reach the points."""
        lat = np.asarray(lat, np.float64)
        lon = np.asarray(lon, np.float64)
        pad = (T_MAX + 40.0) / 111320.0
        padlon = pad / max(0.05, math.cos(math.radians(float(np.mean(lat)))))
        keys = mapcells.cells_for_points(lat, lon, pad, padlon)
        if len(keys) > SPREAD_CELLS:        # points far apart: only the cells around them
            keys = mapcells.cells_near_points(lat, lon, pad, padlon)
        return keys

    def _plan_set(self, keys, roads_only):
        """(plans, PlanPack of them) of the cells keys: each road once, in the order of their keys. Remembered once all
        the cells are planned (a road tile asks for the same few cells' plans dozens of times: merging them cost a
        Canton tile set ~1.6 s, 2026-10-25)."""
        self._plan_missing(keys)
        mk = (tuple(sorted(str(k) for k in keys)), bool(roads_only))
        memo = self.__dict__.setdefault("_plan_sets", {})
        with self.lock:
            hit = memo.get(mk)
        if hit is not None:
            return hit
        got = {}
        for key in keys:
            for fid_part, p in self._cell_plans(key):
                if roads_only and TUNNEL_TAG in fid_part:
                    continue
                got.setdefault((fid_part, float(p.xy[0, 0]), float(p.xy[0, 1])), p)
        # in the order of their keys: grade_points' sums run in plan order, so every caller (the block, a road
        # tile, a far tile) gets the same heights at a point whatever other cells its points reached
        plans = [got[k] for k in sorted(got)]
        out = (plans, PlanPack(plans))
        with self.lock:
            if all(k in self.cells for k in keys):      # (not while a cell's fetch failed: it is asked again)
                if len(memo) >= PLAN_SETS_MAX:
                    memo.pop(next(iter(memo)))
                memo[mk] = out
        return out

    def apply(self, lat, lon, base, lift=0.0, roads_only=False, planned_only=False):
        """base (the ungraded heights at the points) graded under the roads. Same shape as base.
        lift: m added under the roads (tapering with the grading's weight): the physics block and the
        road mesh use ROAD_LIFT, the visible ground tiles 0, so the road mesh lies exactly on the
        ground the tyres touch while the visible ground stays a little below it (no z-fighting;
        2026-10-11 the user saw the tyres sink into a road mesh lifted above the physics ground).
        planned_only: graded only under the cells planned already (none planned here: the AI graph's wide window, whose
        planning held a Windows PC's road processes for minutes, 2026-10-25)."""
        lat = np.asarray(lat, np.float64)
        lon = np.asarray(lon, np.float64)
        base = np.asarray(base, np.float64)
        shape = base.shape
        if base.size == 0:
            return base
        keys = self._keys(lat, lon)
        if planned_only:
            with self.lock:
                keys = [k for k in keys if k in self.cells]
            if not keys:
                return base
        plans, pack = self._plan_set(keys, roads_only)
        if not plans:
            return base
        e, n, _ = self.frame.geodetic_to_enu(lat.ravel(), lon.ravel(), 0.0)
        e, n = np.asarray(e), np.asarray(n)
        out = grade_points(e, n, base.ravel(), pack, lift)
        race = self._on_kept(e, n)
        if race is not None:                # (the terrain is a racetrack's surface: GitHub #143)
            out[race] = base.ravel()[race]
        keep = self._over_tunnels(e, n)
        if keep is not None:
            # (a small change stays: a road over the tunnel lies on its own graded profile, the ground over the tunnel
            # left ungraded under it stood a few cm over the E6's lanes as patches of bare ground, GitHub #2)
            keep &= np.abs(out - base.ravel()) >= OVER_TUNNEL_SMALL_M
            out[keep] = base.ravel()[keep]
        return out.reshape(shape)

    def open_tunnels(self, lat, lon):
        """[(lat (k,), lon (k,), Spec)] of the tunnels' open sections planned near the points: roads on the ground
        that the map calls tunnels (surface.paint_roads paints them: grass grew over the open road of Central Park's
        79th Street Transverse, 2026-10-17)."""
        lat = np.asarray(lat, np.float64)
        lon = np.asarray(lon, np.float64)
        if lat.size == 0:
            return []
        for key in mapcells.cells_for_points(lat, lon):
            self._cell_plans(key)
        with self.lock:
            secs = [o for v in self.opens.values() for o in v]
        out, seen = [], set()
        for xy, spec in secs:
            k = (round(float(xy[0, 0]), 2), round(float(xy[0, 1]), 2), len(xy))
            if k in seen:
                continue
            seen.add(k)
            la, lo, _ = self.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
            la, lo = np.asarray(la), np.asarray(lo)
            if la.max() < lat.min() - 0.001 or la.min() > lat.max() + 0.001 or \
                    lo.max() < lon.min() - 0.0015 or lo.min() > lon.max() + 0.0015:
                continue
            out.append((la, lo, spec))
        return out

    def _on_kept(self, e, n):
        """Which points lie on an UNGRADED road's carriageway (+ UNGRADED_MARGIN_M), or None."""
        with self.lock:
            zones = [z for v in self.keeps.values() for z in v]
        if not zones or not len(e):
            return None
        import fast
        fast.require(0, "over_tunnel")
        mask = np.zeros(len(e), bool)
        ee, nn = np.ascontiguousarray(e, np.float64), np.ascontiguousarray(n, np.float64)
        for xy, hw in zones:
            fast._ef.over_tunnel(mask, ee, nn, np.ascontiguousarray(xy[:, :2], np.float64), float(hw), 0.0)
        return mask if mask.any() else None

    def _over_tunnels(self, e, n):
        """Which points lie over a covered tunnel (its half width + TUNNEL_KEEP_M), or None."""
        with self.lock:
            zones = [z for v in self.covers.values() for z in v]
        if not zones or not len(e):
            return None
        import fast
        fast.require(0, "over_tunnel")      # (Rust: per segment near the points, without the GIL)
        mask = np.zeros(len(e), bool)
        ee, nn = np.ascontiguousarray(e, np.float64), np.ascontiguousarray(n, np.float64)
        for xy, hw in zones:
            fast._ef.over_tunnel(mask, ee, nn, np.ascontiguousarray(xy[:, :2], np.float64), float(hw),
                                 float(TUNNEL_KEEP_IN_M))
        return mask if mask.any() else None


def trim_plan(p, box):
    """The pieces of Plan p within box (x0, y0, x1, y1): cut at its edges (earthfast.clip_segments), heights and
    cuttings with them; an end not at p's own end leads onto no bridge. [Plan]."""
    import fast
    fast.require(44, "clip_segments")
    xy = np.ascontiguousarray(np.asarray(p.xy, np.float64)[:, :2])
    if len(xy) < 2:
        return [p]
    t = np.asarray(fast._ef.clip_segments(xy, tuple(float(v) for v in box)))
    ok = np.isfinite(t[:, 0])
    if ok.all() and (t[:, 0] == 0.0).all() and (t[:, 1] == 1.0).all():
        return [p]
    out, i, n = [], 0, len(ok)
    while i < n:
        if not ok[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and ok[j + 1] and t[j, 1] == 1.0 and t[j + 1, 0] == 0.0:
            j += 1
        t0, t1 = t[i, 0], t[j, 1]
        a = xy[i] + (xy[i + 1] - xy[i]) * t0
        b = xy[j] + (xy[j + 1] - xy[j]) * t1
        za = p.z[i] + (p.z[i + 1] - p.z[i]) * t0
        zb = p.z[j] + (p.z[j + 1] - p.z[j]) * t1
        pxy = np.vstack([a, xy[i + 1:j + 1], b])
        pz = np.r_[za, p.z[i + 1:j + 1], zb]
        pb = getattr(p, "bank", None)
        if pb is not None:
            pb = np.r_[pb[i] + (pb[i + 1] - pb[i]) * t0, pb[i + 1:j + 1], pb[j] + (pb[j + 1] - pb[j]) * t1]
        if np.hypot(*(pxy[-1] - pxy[0])) > 1e-6 or len(pxy) > 2:
            q = Plan(pxy, pz, p.half, (p.abut[0] and i == 0 and t0 == 0.0, p.abut[1] and j == n - 1 and t1 == 1.0),
                     None if p.cut is None else np.asarray(p.cut)[i:j + 1].copy(), p.road_half)
            q.rid = getattr(p, "rid", -1)
            q.max_wall = getattr(p, "max_wall", WALL_MAX_H_M)
            q.bank = pb
            out.append(q)
        i = j + 1
    return out


def road_id(fid):
    """A stable integer id of a map feature (grade_points: a road never gives way to itself, though each map cell
    plans its own stretch of it: the Stelvio Pass's hairpins sank under their own other stretches, 2026-10-21)."""
    import zlib
    return zlib.crc32(str(fid).encode()) & 0x7FFFFFFF


class PlanPack:
    """Plans (a list, in grade_points' order) as grade_points takes them: every segment's arrays concatenated once, with
    each plan's box; a call keeps the segments of the plans whose boxes meet its points', in the same order (built per
    call, the plans' Python loop cost a Canton tile set ~1 s, 2026-10-25)."""

    def __init__(self, plans):
        rows = [(k, p) for k, p in enumerate(plans) if len(p.xy) >= 2]
        self.n = len(rows)
        if not self.n:
            return
        f64 = np.float64
        npts = np.array([len(p.xy) for _k, p in rows], np.int64)
        nseg = npts - 1
        xy = np.concatenate([np.asarray(p.xy, f64)[:, :2] for _k, p in rows])
        z = np.concatenate([np.asarray(p.z, f64) for _k, p in rows])
        last = np.cumsum(npts) - 1
        ia = np.delete(np.arange(len(xy)), last)            # each segment's first point (not a plan's last point)
        self.a, self.b = np.ascontiguousarray(xy[ia]), np.ascontiguousarray(xy[ia + 1])
        self.za, self.zb = np.ascontiguousarray(z[ia]), np.ascontiguousarray(z[ia + 1])
        bk = np.concatenate([np.asarray(p.bank, f64) if getattr(p, "bank", None) is not None else np.zeros(len(p.xy))
                             for _k, p in rows])
        self.ba, self.bb = np.ascontiguousarray(bk[ia]), np.ascontiguousarray(bk[ia + 1])
        self.h = np.repeat(np.array([p.half for _k, p in rows], f64), nseg)
        self.rh = np.repeat(np.array([getattr(p, "road_half", p.half) for _k, p in rows], f64), nseg)
        rids = [getattr(p, "rid", -1) for _k, p in rows]
        self.rid = np.repeat(np.array([r if r is not None and r >= 0 else 2 ** 40 + k
                                       for (k, _p), r in zip(rows, rids)], np.int64), nseg)
        s0 = np.r_[0, np.cumsum(nseg)]
        ab = np.zeros(int(s0[-1]), bool)
        cut = np.full((int(s0[-1]), 6), np.nan)
        for n, (_k, p) in enumerate(rows):
            abut = getattr(p, "abut", (False, False))
            if abut[0] or abut[1]:
                arc = np.r_[0.0, np.cumsum(np.hypot(*np.diff(p.xy[:, :2], axis=0).T))]
                if abut[0]:
                    ab[s0[n]:s0[n + 1]] |= arc[:-1] < ABUT_NEAR_M
                if abut[1]:
                    ab[s0[n]:s0[n + 1]] |= arc[-1] - arc[1:] < ABUT_NEAR_M
            c = getattr(p, "cut", None)
            if c is not None:
                cut[s0[n]:s0[n + 1]] = c
        self.ab = ab.astype(np.uint8)
        self.cut = cut
        self.box = np.array([p.bbox for _k, p in rows], f64).reshape(-1, 4)
        self.of = np.repeat(np.arange(self.n), nseg)

    def near(self, x0, y0, x1, y1):
        """The segment arrays of the plans whose boxes meet (x0, y0, x1, y1), or None."""
        if not self.n:
            return None
        bx = self.box
        keep = ~((bx[:, 2] < x0) | (bx[:, 0] > x1) | (bx[:, 3] < y0) | (bx[:, 1] > y1))
        if not keep.any():
            return None
        if keep.all():
            return self.a, self.b, self.za, self.zb, self.h, self.rh, self.rid, self.ab, self.cut, self.ba, self.bb
        sk = keep[self.of]
        return tuple(np.ascontiguousarray(v[sk]) for v in (self.a, self.b, self.za, self.zb, self.h, self.rh, self.rid,
                                                           self.ab, self.cut, self.ba, self.bb))


def grade_points(px, py, base, plans, lift=0.0):
    """base at the points (px, py) graded under the plans (see the module doc; a list or a PlanPack of them), by the
    Rust loop (native/earthfast grade_points: a Python loop per segment was the server's largest CPU cost while driving,
    2026-10-12). Pure."""
    px = np.asarray(px, np.float64)
    py = np.asarray(py, np.float64)
    base = np.asarray(base, np.float64)
    out = base.copy()
    if not len(px):
        return out
    pack = plans if isinstance(plans, PlanPack) else PlanPack(plans)
    # every segment of the plans near the points: ends, end heights, reach (core half width + T_MAX)
    got = pack.near(px.min(), py.min(), px.max(), py.max())
    if got is None:
        return out
    A, B, ZA, ZB, H, RH, RID, AB, CUT, BA, BB = got
    import fast
    fast.require(84, "grade_points")         # (the Rust loop: native/earthfast; its banked curves: 84)
    f64 = np.float64
    return fast._ef.grade_points(np.ascontiguousarray(px, f64), np.ascontiguousarray(py, f64),
                                 np.ascontiguousarray(base, f64), A, B, ZA, ZB, H, RH, RID, AB, CUT, BA, BB, float(lift),
                                 float(SLOPE_RUN), float(T_MIN), float(T_MAX), float(BLEND_R), float(BUCKET),
                                 float(WALL_CUT_M), float(WALL_T_M), float(WALL_FLAT_M), float(WALL_AHEAD_M),
                                 bool(SEP_ON), float(SEP_FROM_M), float(SEP_FULL_M), float(CORE_FADE_M),
                                 float(SIDE_FADE_M), float(SHARP_STEP_M))


def _span_key(b):
    """A bridge part's identity across the cells' copies of it (its ends to 0.1 m and its point count)."""
    b = np.asarray(b, np.float64)
    return (round(float(b[0, 0]), 1), round(float(b[0, 1]), 1), round(float(b[-1, 0]), 1), round(float(b[-1, 1]), 1),
            len(b))


def spans_a_road(spans, lines):
    """Does a bridge chain (spans: its parts (k, 2)) cross over one of lines (ground roads, tracks) farther than
    APPROACH_MIN_CROSS_M from each of its free ends (a road meeting a deck at its end is a junction there)? Pure."""
    spans = [np.asarray(b, np.float64)[:, :2] for b in spans]
    if not spans or not lines:
        return False
    ends = [p for n, b in enumerate(spans) for p in (b[0], b[-1])
            if not any(min(np.hypot(*(o[0] - p)), np.hypot(*(o[-1] - p))) < 0.75 for m, o in enumerate(spans) if m != n)]
    for c in roadsmod._crossings(spans, list(lines)):
        if all(np.hypot(*(np.asarray(c[2]) - e)) >= APPROACH_MIN_CROSS_M for e in ends):
            return True
    return False


def bridge_chain(line, all_bridges):
    """The bridge parts joined to line through their ends (within 0.75 m), line itself excluded. Pure."""
    line = np.asarray(line, np.float64)[:, :2]
    parts = [np.asarray(b, np.float64)[:, :2] for b in all_bridges]
    parts = [b for b in parts if not (len(b) == len(line) and np.array_equal(b, line))]
    if not parts:
        return []
    first = np.array([b[0] for b in parts])
    last = np.array([b[-1] for b in parts])
    out, frontier, used = [], [line[0], line[-1]], np.zeros(len(parts), bool)
    while frontier:
        e = frontier.pop()
        d = np.minimum(np.hypot(first[:, 0] - e[0], first[:, 1] - e[1]), np.hypot(last[:, 0] - e[0], last[:, 1] - e[1]))
        for k in np.flatnonzero((d < 0.75) & ~used):    # (in the parts' order, as a loop over them would)
            used[k] = True
            out.append(parts[k])
            frontier += [parts[k][0], parts[k][-1]]
    return out


def bridge_lifts(line, all_bridges, ground_lines, z_at, chain=None, rail_lines=(), sunk_lines=(), end_z=(None, None)):
    """[(end xy (2,), lift m)] of a bridge part (line (k, 2) ENU m): at each of its ends that no other bridge part
    continues from (an abutment), how far the ground there must be raised for the deck to clear every road crossing
    under it (roads.DECK_ROAD_CLEAR + APPROACH_MARGIN_M over the road, full height within APPROACH_FLAT_M of the end,
    then falling at roads.DECK_GRADE), within [APPROACH_MIN_LIFT, APPROACH_MAX_LIFT]. Roads crossing within
    APPROACH_MIN_CROSS_M of either end meet it at grade and do not count; railways (rail_lines) always count (a track
    never meets a road deck at grade: the deck ended 3 m past one at Vitebsk, GitHub #324); roads that give way
    (sunk_lines: a town motorway's trench, gives_way) count UNDER_MAX_DROP_M lower than their ground, the dip they
    get under the deck (_lower_underpasses), so the ramps raise only what the dip cannot give. z_at(xy (n, 2)) ->
    ground m; end_z: the ground at the line's (first, last) end as the approaches' plans have it (abutment_height),
    None for z_at's. Pure given z_at."""
    line = np.asarray(line, np.float64)[:, :2]
    if len(line) < 2:
        return []
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(line, axis=0).T))]
    # (a short part, or one no road crosses under, still raises its free end for the roads under the rest of its chain:
    # the deck carried a few metres onto the road joined at its end (roads._skew_decks) left that end unraised and the
    # whole deck ~4.5 m low over BR-343 at Teresina, GitHub #306)
    if s[-1] < 2 * APPROACH_MIN_CROSS_M and not chain:
        return []
    cr = roadsmod._crossings([line], ground_lines)
    nr = len(cr)
    # (only under the short spans the dip is made for (_lower_underpasses: UNDER_MAX_BRIDGE_M over the part and the
    # chain it is in, each part once); under a long deck the road gives nothing, and the ramps rise for it as for any
    # road)
    seen = {_span_key(line)}
    chain_m = 0.0
    for part in (chain or ()):
        k = _span_key(part)
        if k not in seen:
            seen.add(k)
            chain_m += float(np.hypot(*np.diff(np.asarray(part, np.float64)[:, :2], axis=0).T).sum())
    if s[-1] + chain_m > UNDER_MAX_BRIDGE_M:
        ground_lines, sunk_lines = list(ground_lines) + list(sunk_lines), ()
        cr = roadsmod._crossings([line], ground_lines)
        nr = len(cr)
    if sunk_lines:
        cr = cr + roadsmod._crossings([line], list(sunk_lines))
    ns = len(cr)
    if rail_lines:
        cr = cr + roadsmod._crossings([line], list(rail_lines))
    if not cr and not chain:
        return []
    zu = z_at(np.array([c[2] for c in cr], np.float64).reshape(-1, 2)) if cr else np.zeros(0)
    zu = zu - np.where((np.arange(len(cr)) >= nr) & (np.arange(len(cr)) < ns), UNDER_MAX_DROP_M, 0.0)
    arcs = np.array([c[1] for c in cr], np.float64)

    def joined_at(end, own=None):
        own = line if own is None else own
        for other in all_bridges:
            if other is own or (len(other) == len(own) and np.array_equal(np.asarray(other)[:, :2], own[:, :2])):
                continue
            o = np.asarray(other, np.float64)
            if min(np.hypot(*(o[0, :2] - end)), np.hypot(*(o[-1, :2] - end))) < 0.75:
                return True
        return False
    joined = (joined_at(line[0]), joined_at(line[-1]))
    # (not a junction at an abutment; at a joint with the chain's next part the crossing is mid-bridge: Commerce
    # Street's bridge over I-95 in Springfield VA, split 2 m from where the motorway passes under, GitHub #138)
    rail = np.arange(len(cr)) >= ns
    under = ((arcs >= APPROACH_MIN_CROSS_M) | joined[0]) & ((arcs <= s[-1] - APPROACH_MIN_CROSS_M) | joined[1]) | rail
    zu, arcs, rail = zu[under], arcs[under], rail[under]
    pts = np.array([c[2] for c in cr], np.float64)[under].reshape(-1, 2)
    # (the crossings under the rest of its chain, by their distance from the abutment: Overture splits a bridge where
    # other ways meet it, and the road under it may pass under the next part, GitHub #138)
    # (a road meeting a part's free end is a junction at grade there, as for the line itself: the trunk's own next
    # carriageway at the far abutment of its chain counted as a road under it and raised a residential bridge joined
    # to the chain 9.7 m, the trunk deck climbing 10 m in 20 m to it, US-24 near Buena Vista CO, 2026-10-27)
    more, drop = [], []
    for part in (chain or ()):
        part = np.asarray(part, np.float64)[:, :2]
        sp = float(np.hypot(*np.diff(part, axis=0).T).sum())
        pj = (joined_at(part[0], part), joined_at(part[-1], part))
        for lines_, dr in ((ground_lines, 0.0), (sunk_lines, UNDER_MAX_DROP_M)):
            for c in roadsmod._crossings([part], list(lines_)) if lines_ else ():
                a = float(c[1])
                if (a >= APPROACH_MIN_CROSS_M or pj[0]) and (a <= sp - APPROACH_MIN_CROSS_M or pj[1]):
                    more.append(np.asarray(c[2], np.float64))
                    drop.append(dr)
        for c in roadsmod._crossings([part], list(rail_lines)) if rail_lines else ():
            more.append(np.asarray(c[2], np.float64))
            drop.append(0.0)
    if more:
        mp = np.array(more, np.float64)
        zu = np.r_[zu, z_at(mp) - np.array(drop, np.float64)]
        pts = np.r_[pts, mp]
        rail = np.r_[rail, np.zeros(len(mp), bool)]
    if not len(zu):
        return []
    out = []
    for (end, at, ze), jn in zip(((line[0], 0.0, end_z[0]), (line[-1], s[-1], end_z[1])), joined):
        if jn:
            continue
        d = np.r_[np.abs(arcs - at), np.hypot(*(pts[len(arcs):] - end).T)] if more else np.abs(arcs - at)
        far = (d >= APPROACH_MIN_CROSS_M) | rail     # (a road meeting the abutment itself: a junction at grade)
        if not far.any():
            continue
        d, zu_e = d[far], zu[far]
        need = zu_e + roadsmod.DECK_ROAD_CLEAR + APPROACH_MARGIN_M \
            - roadsmod.DECK_GRADE * np.maximum(d - APPROACH_FLAT_M, 0.0)
        lift = float(need.max() - (z_at(end[None])[0] if ze is None else ze))
        if lift >= APPROACH_MIN_LIFT:
            out.append((np.asarray(end, np.float64), min(lift, APPROACH_MAX_LIFT)))
    return out


def fill_abutment(z, at_end, step=PROFILE_STEP, wide=False):
    """The raw profile z (a sample every step m) of a road leading onto a bridge at its first (at_end False) or last
    sample, its last ABUT_NEAR_M filled up to the straight grade of the stretch from there to ABUT_FAR_M where they
    fall at least ABUT_STEEP more steeply than it toward the bridge (ABUT_FILL_ON); wide (the deck spans a road or a
    track): a dip at the end up to ABUT_SLOPE_MAX_M wide filled to its rim too (_fill_gap). Returns z itself when
    nothing is filled. Pure."""
    z = np.asarray(z, np.float64)
    q = _fill_near(z, at_end, step)
    if not wide:
        return q
    p = z[::-1] if at_end else z
    g = _fill_gap(p, np.arange(len(p)) * step, step)
    if g is None:
        return q
    g = g[::-1] if at_end else g
    return np.maximum(q, g)


def _fill_gap(p, d, step):
    """p (the profile from the bridge end outward, at d m) with the dip at its start filled to the dip's rim: the first
    ABUT_FAR_M - ABUT_NEAR_M stretch from ABUT_NEAR_M on (to ABUT_SLOPE_MAX_M) with no sample ABUT_STEEP_WIDE over its
    own grade and a slope ABUT_STEEP_WIDE over that grade before it, that grade carried back to the end over what lies
    under it, ABUT_MAX_M at most; None when there is none or the end is not ABUT_GAP_MIN_M under it. Pure."""
    span = ABUT_FAR_M - ABUT_NEAR_M
    rise = np.diff(p) / step                    # (outward: the ground climbing away from the bridge)
    for s0 in np.arange(ABUT_NEAR_M, ABUT_SLOPE_MAX_M + 1e-9, step):
        w = (d >= s0) & (d <= s0 + span)
        if w.sum() < 4:
            return None
        b, a = np.polyfit(d[w], p[w], 1)
        if (rise[w[1:] & w[:-1]] > b + ABUT_STEEP_WIDE).any():
            continue                            # (still on the slope)
        m = d < s0
        if (rise[:int(m.sum())] - b).max() < ABUT_STEEP_WIDE:
            continue                            # (no slope before it: the dip's rim is farther, if there is one)
        e = a + b * d[m]
        if e[0] - p[0] < ABUT_GAP_MIN_M:
            return None
        q = p.copy()
        q[m] = np.minimum(np.maximum(p[m], e), p[m] + ABUT_MAX_M)
        return q
    return None


def _fill_near(z, at_end, step):
    """fill_abutment's last ABUT_NEAR_M. Pure."""
    p = z[::-1] if at_end else z
    d = np.arange(len(p)) * step
    far = (d >= ABUT_NEAR_M) & (d <= ABUT_FAR_M)
    if far.sum() < 4:
        return z
    b, a = np.polyfit(d[far], p[far], 1)
    # (the stretch's own samples still on the cutting's slope left out and the grade fitted again on the rest: the
    # DEM smooths a city trench's wall into a 1:3 slope, and a 22 m street part ending on 11th Avenue's bridge over
    # Brooklyn's Prospect Expressway had the stretch on the slope's top and fitted the slope, no fill: the street
    # sagged 4 m into the trench at the abutment, 2026-10-29)
    steep = np.zeros(len(p), bool)
    steep[1:] = np.diff(p) / step > b + ABUT_STEEP
    k = np.flatnonzero(steep & far)
    if len(k):
        rest = far & (d > d[k.max()])
        if rest.sum() >= 4:
            b, a = np.polyfit(d[rest], p[rest], 1)
    n = int((d < ABUT_NEAR_M).sum())
    e = a + b * d[:n]
    if e[0] - p[0] < ABUT_MIN_M or (np.diff(p[:n + 1]) / step - b).max() < ABUT_STEEP:
        return z
    q = p.copy()
    q[:n] = np.minimum(np.maximum(p[:n], e), p[:n] + ABUT_MAX_M)
    return q[::-1] if at_end else q


def _goes_on(line, at_end, lines, reach):
    """The first reach m of the road that goes on from line's end (at_end: its last point, else its first) among lines
    (k, 2): one whose end is within CHAIN_JOIN_M of it, turning at most CHAIN_TURN_DEG there (as the chain extensions,
    Grader._extensions, take it), from the node outward; None when none does. Pure."""
    line = np.asarray(line, np.float64)[:, :2]
    inward = line[::-1] if at_end else line
    node = inward[0]
    d0 = node - _point_along(inward, CHAIN_LOOK_M)      # (the way the road goes on out of this end)
    best, best_turn = None, CHAIN_TURN_DEG
    for other in lines:
        other = np.asarray(other, np.float64)[:, :2]
        if len(other) < 2 or (len(other) == len(line) and np.array_equal(other, line)):
            continue
        for out in (other, other[::-1]):
            if np.hypot(*(out[0] - node)) > CHAIN_JOIN_M:
                continue
            d1 = _point_along(out, CHAIN_LOOK_M) - out[0]
            cosang = float(np.dot(d0, d1)) / max(float(np.hypot(*d0) * np.hypot(*d1)), 1e-9)
            turn = math.degrees(math.acos(max(-1.0, min(1.0, cosang))))
            if turn <= best_turn:
                best, best_turn = out, turn
    if best is None:
        return None
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(best, axis=0).T))]
    k = int(np.searchsorted(s, reach))
    if k < len(s):
        p = np.array([np.interp(reach, s, best[:, 0]), np.interp(reach, s, best[:, 1])])
        return np.vstack([best[:k], p[None]])
    return best


def abutment_height(end, approaches, z_at, lines=(), wide=False):
    """m: the ground at a bridge end (xy (2,)) as the plans of the roads leading onto it (approaches: lines (k, 2),
    an end within APPROACH_TOUCH_M of it) have it: the DEM there or, higher, the fill of the approach's last
    ABUT_NEAR_M (fill_abutment, as plan_roads applies it to the part with the road it goes on in past its other end:
    lines, the ground roads around, _goes_on). The DEM at a bridge's end is the cutting's slope where the map's
    bridge range stops short of the real abutment, or the DEM smooths the trench's wall into a slope (11th Avenue
    over Brooklyn's Prospect Expressway: 31.6 m at the bridge's end where the street is 38.7 m, 2026-10-29): on it
    the ramps lifted a street already filled, 7 m into a hump, and the underpass dip dug the expressway 5 m deeper
    than the deck needed. wide: the deck spans a road or a track (fill_abutment). z_at(xy (n, 2)) -> ground m. Pure
    given z_at."""
    end = np.asarray(end, np.float64)[:2]
    best = float(z_at(end[None])[0])
    if not ABUT_FILL_ON:
        return best
    for line in approaches:
        line = np.asarray(line, np.float64)[:, :2]
        if len(line) < 2:
            continue
        at_end = bool(np.hypot(*(line[-1] - end)) <= np.hypot(*(line[0] - end)))
        on = _goes_on(line, not at_end, lines, ABUT_LOOK_M if wide else ABUT_FAR_M + PROFILE_STEP) if lines else None
        # (on runs out of the part's far end, the one away from the bridge: before its first point when the bridge is
        # at its last. Joined the other way round, past the bridge end, the profile's end sample was the next road's
        # ground ~40 m off: Fort Hamilton Parkway's abutment over Brooklyn's Prospect Expressway read 16.7 m where the
        # street's plan has 18.6, so the trench under it was dug ~2 m deeper than its deck needs, 2026-10-29)
        if on is not None:
            line = np.vstack([on[1:][::-1], line]) if at_end else np.vstack([line, on[1:]])
        length = float(np.hypot(*np.diff(line, axis=0).T).sum())
        if length <= 0:
            continue
        step = length / max(1, math.ceil(length / PROFILE_STEP - 1e-9)) * (1.0 + 1e-9)     # (plan_roads' step)
        xy, _ = resample(line, step)
        if len(xy) < 2:
            continue
        z = fill_abutment(np.asarray(z_at(xy), np.float64), at_end, step, wide)
        best = max(best, float(z[-1] if at_end else z[0]))
    return best


def underpass_offsets(lines, requirements, queries=None):
    """Smooth underpass dips along joined ground roads, never across space (earthfast.underpass_offsets; after
    wyo-backroads' PR #374): m (<= 0) to add to the roads' heights.

    lines: the ground roads [(k, 2)]; requirements [(line, arc m, depth m)] of the overpasses they cross under.
    queries: [(line, points (k, 2) on it)] where the offsets are asked (default: every line's own points); one
    array per query (per line by default). Roads join where their ends meet; a node shared by several roads takes
    one offset in each, the eighth norm combines nearby dips without summing their depths, loose ends stay pinned
    and only joined ground junctions carry a dip past the road under the bridge. Pure."""
    import fast
    fast.require(85, "underpass_offsets")
    f64 = np.float64
    pts = [np.asarray(ln, f64)[:, :2] for ln in lines]
    if queries is None:
        queries = list(enumerate(pts))
    if not queries:
        return []
    qs = [np.asarray(q, f64).reshape(-1, 2) for _l, q in queries]
    if not pts or not requirements:
        return [np.zeros(len(q)) for q in qs]
    starts = np.r_[0, np.cumsum([len(p) for p in pts])].astype(np.int64)
    xy = np.ascontiguousarray(np.vstack(pts)) if len(starts) and starts[-1] else np.zeros((0, 2))
    req = np.ascontiguousarray(np.asarray(requirements, f64).reshape(-1, 3))
    qxy = np.ascontiguousarray(np.vstack(qs)) if sum(len(q) for q in qs) else np.zeros((0, 2))
    qline = np.repeat(np.array([int(l) for l, _q in queries], np.int64), [len(q) for q in qs])
    prm = np.array([UNDER_FLAT_M, UNDER_GRADE, UNDER_MAX_GRADE, UNDER_MAX_DROP_M, UNDER_JOINT_FLAT_M,
                    UNDER_JOINT_FADE_M, 0.5], f64)
    if not UNDER_JOINT_GENTLE_ON:
        out = fast._ef.underpass_offsets(xy, starts, req, qxy, qline, prm)
        return np.split(out, np.cumsum([len(q) for q in qs])[:-1])
    # (and every queried line whole, every UNDER_GENTLE_STEP_M, for _gentle_joints: the same in every cell)
    lids = sorted({int(l) for l, _q in queries})
    whole = [resample(pts[l], UNDER_GENTLE_STEP_M) for l in lids]
    nq = len(qxy)
    allq = np.ascontiguousarray(np.vstack([qxy] + [w for w, _s in whole]))
    allline = np.r_[qline, np.repeat(np.array(lids, np.int64), [len(w) for w, _s in whole])].astype(np.int64)
    got = fast._ef.underpass_offsets(xy, starts, req, allq, allline, prm)
    out = np.array(got[:nq], f64)
    k = nq
    fix = {}
    for l, (w, sw) in zip(lids, whole):
        off = np.asarray(got[k:k + len(w)], f64)
        k += len(w)
        d = _gentle_joints(sw, off)
        if d is not None:
            fix[l] = (pts[l], sw, d)
    for n, (l, q) in enumerate(queries):
        f = fix.get(int(l))
        if f is None:
            continue
        a = int(sum(len(x) for x in qs[:n]))
        out[a:a + len(qs[n])] += np.interp(_arc_of(f[0], qs[n]), f[1], f[2])
    return np.split(out, np.cumsum([len(q) for q in qs])[:-1])


def _arc_of(line, pts):
    """m along line (k, 2) of the nearest point of it to each of pts (m, 2). Pure."""
    a, b = line[:-1], line[1:]
    ab = b - a
    L2 = np.maximum((ab * ab).sum(1), 1e-12)
    t = np.clip(((pts[:, None, :] - a[None]) * ab[None]).sum(2) / L2[None], 0.0, 1.0)
    d = np.hypot(*(pts[:, None, :] - (a[None] + t[..., None] * ab[None])).transpose(2, 0, 1))
    j = np.argmin(d, axis=1)
    s = np.r_[0.0, np.cumsum(np.sqrt(L2))]
    return s[j] + t[np.arange(len(pts)), j] * (s[j + 1] - s[j])


def _gentle_joints(s, off):
    """The change (<= 0, n,) to a whole line's underpass offsets off (n,) at its arcs s (n,) that makes them rise no
    steeper than UNDER_GRADE (deeper only: the clearance under a deck never less), its corners rounded
    (UNDER_GENTLE_SIGMA_M) and fading in over UNDER_GENTLE_END_M from its ends (their offsets, which the roads joined
    there share, kept); None when nothing changes. Pure."""
    if len(off) < 3 or not np.any(off < -1e-6):
        return None
    g = UNDER_GRADE             # (min over j of off[j] + g |s - s[j]|: from either side, a running minimum)
    lim = np.minimum(np.minimum.accumulate(off - g * s) + g * s,
                     np.minimum.accumulate((off + g * s)[::-1])[::-1] - g * s)
    ds = np.diff(s)
    if np.all(lim >= off - 1e-6):
        return None
    sm = smooth_profile(lim, float(np.mean(ds)), UNDER_GENTLE_SIGMA_M)
    cand = np.minimum(off, np.minimum(sm, 0.0))
    u = np.clip(np.minimum(s, s[-1] - s) / UNDER_GENTLE_END_M, 0.0, 1.0)
    d = (cand - off) * (u * u * (3.0 - 2.0 * u))
    return d if np.any(d < -1e-6) else None


def under_dip(d, depth, room=(math.inf, math.inf)):
    """m added (<= 0) to a road's heights d m (along it, signed) from where it crosses under an overpass, for a dip
    depth m deep: flat under the deck (UNDER_FLAT_M either way), then back up on a cosine ramp whose steepest grade is
    UNDER_GRADE (a V of straight UNDER_GRADE ramps was a 6 % kink at each end and at the bottom: at motorway speed a
    jolt). room: m from the crossing to the road part's (start, end): a ramp that would run past it is made shorter,
    up to UNDER_MAX_GRADE, so the dip ends inside the part (its next part, past a junction, is not lowered: a step).
    None when that is too steep. Pure."""
    d = np.asarray(d, np.float64)
    run = depth * math.pi / (2 * UNDER_GRADE)
    runs = []
    for r in room:
        avail = r - UNDER_FLAT_M
        if avail >= run:
            runs.append(run)
        elif avail > 0 and depth * math.pi / (2 * avail) <= UNDER_MAX_GRADE:
            runs.append(avail)
        else:
            return None
    rr = np.where(d < 0, runs[0], runs[1])
    u = np.clip((np.abs(d) - UNDER_FLAT_M) / np.maximum(rr, 1e-9), 0.0, 1.0)
    return -depth * 0.5 * (1.0 + np.cos(math.pi * u))


def approach_add(xy, at_end, lift, half, others=()):
    """m to add to a road plan's vertices (xy (n, 2)) rising to `lift` at its first (at_end False) or last vertex (a
    bridge's abutment), falling off along the road (arc length) on a cosine ramp whose steepest grade is APPROACH_GRADE;
    at most what keeps the fill slope (SLOPE_RUN) clear of the other roads [(line (k, 2), half width)] beside it, with
    APPROACH_GAP_M to spare. None when nothing is raised. Pure."""
    xy = np.asarray(xy, np.float64)[:, :2]
    if len(xy) < 2 or lift < APPROACH_MIN_LIFT:
        return None
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    d = (s[-1] - s) if at_end else s
    run = float(np.clip(lift * math.pi / (2 * APPROACH_GRADE), APPROACH_MIN_RUN, APPROACH_MAX_RUN))
    u = np.clip(d / run, 0.0, 1.0)
    add = lift * 0.5 * (1.0 + np.cos(math.pi * u))
    on = add > 1e-3
    if not on.any():
        return None
    q0 = xy[on]
    lo_q, hi_q = q0.min(0), q0.max(0)
    for line, oh in others:
        line = np.asarray(line, np.float64)[:, :2]
        if len(line) < 2:
            continue
        reach = half + oh + APPROACH_GAP_M + SLOPE_RUN * lift
        if (line.max(0) < lo_q - reach).any() or (line.min(0) > hi_q + reach).any():
            continue                    # too far to matter
        q = xy[on]
        a, b = line[:-1], line[1:]
        ab = b - a
        L2 = np.maximum((ab * ab).sum(1), 1e-12)
        t = np.clip(((q[:, None, :] - a[None]) * ab[None]).sum(2) / L2[None], 0.0, 1.0)
        dist = np.hypot(*(q[:, None, :] - (a[None] + t[..., None] * ab[None])).transpose(2, 0, 1)).min(1)
        cap = np.maximum(0.0, (dist - half - oh - APPROACH_GAP_M) / SLOPE_RUN)
        add[on] = np.minimum(add[on], cap)
    # a cap lowers the ramp before it at APPROACH_CAP_GRADE at most, instead of a step at the cap
    for i in range(1, len(add)):
        add[i] = min(add[i], add[i - 1] + APPROACH_CAP_GRADE * (s[i] - s[i - 1]))
    for i in range(len(add) - 2, -1, -1):
        add[i] = min(add[i], add[i + 1] + APPROACH_CAP_GRADE * (s[i + 1] - s[i]))
    if add.max() < 1e-3:
        return None
    return add


def _is_link(spec):
    return bool(getattr(spec, "link", False))


def _never_raised(spec):
    """A road whose approaches are never raised: a motorway or trunk (APPROACH_SKIP), not one of their links. A link
    leaving a motorway's bridge runs down a fill to the street: kept at the ground, its deck fell 11 m within 90 m and
    dropped beside its mainline while they still overlapped, and an AI car drove into the mainline's parapet trying to
    take the exit (US-101's exit 1E over the Los Angeles River, 2026-10-27)."""
    return spec.cls in APPROACH_SKIP and not _is_link(spec)


def gives_way(spec):
    """A ground road lowered under a street's bridge before the street's ramps are raised (TOWN_CUT_ON): a motorway or
    trunk, or one of their links (the ramps in the same trench: a deck clearing the mainline still rose for them), in
    town, where its cutting is walled in concrete to the full depth (_town_walls, WALL_MAX_H_CITY_M), with the
    underpass dips on (UNDER_ON, UNDER_ANY_ON: the dip is theirs; without them the ramps rise as before)."""
    return (TOWN_CUT_ON and UNDER_ON and UNDER_ANY_ON and spec is not None and spec.cls in APPROACH_SKIP
            and bool(spec.urban))


class GradedDem:
    """dem.Dem with sample() graded under the roads; everything else passes through."""

    def __init__(self, dem, grader):
        self._dem, self._grader = dem, grader

    def sample(self, lat, lon, zoom, progress=None):
        base = self._dem.sample(lat, lon, zoom, progress) if progress is not None else self._dem.sample(lat, lon, zoom)
        return self._grader.apply(lat, lon, base)

    def __getattr__(self, name):
        return getattr(self._dem, name)
