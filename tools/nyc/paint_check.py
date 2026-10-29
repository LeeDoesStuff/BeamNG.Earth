"""Real-data check: a Park Slope road tile's chunks by material, as built in the current road mode."""
import collections, os, sys
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "server"))
import dem as demmod, geodesy, mapdata, netfetch, roads, surface
CACHE = os.path.join(os.path.expanduser("~"), ".cache", "beamng-earth")
net = netfetch.Fetcher(offline=False)
md = mapdata.MapData(CACHE, fetcher=net)
dem = demmod.Dem(os.path.join(CACHE, "dem"), fetcher=net)
sf = surface.Surface(md)
lat, lon = 40.6720, -73.9790          # 7th Ave / 8th St, Park Slope
frame = geodesy.EnuFrame(lat, lon, 0.0)
sf.classes(lat, lon)
elev = lambda la, lo: np.asarray(dem.sample(np.atleast_1d(la), np.atleast_1d(lo), 16), float)
tx, ty = 0, 0
cells = [md.cell(k, layers=("roads",), kind="demand").layers.get("roads") for k in roads.cells_of_tile(frame, tx, ty)]
for k in roads.cells_of_tile(frame, tx, ty):
    md.cell(k, layers=("landcover",), kind="demand")
ch = roads.build_tile(frame, tx, ty, cells, elev, sf.urban, walks=[])
c = collections.Counter(x.get("m") for x in ch)
print("ROADS_IN_GROUND", roads.ROADS_IN_GROUND, "chunks", len(ch))
for m, n in c.most_common():
    print(f"  {n:5d} {m}")
import junctions
_find = junctions.find
dbg = []
def find(*a, **k):
    k["debug"] = dbg
    r = _find(*a, **k)
    box = roads.tile_bbox(tx, ty)
    inb = lambda p: box[0] <= p[0] < box[2] and box[1] <= p[1] < box[3]
    print("corners", len(r[0]), "crosswalks+stops", len(r[2]), "in tile:", sum(inb(0.5 * (q["a"] + q["b"])) for q in r[2]))
    return r
junctions.find = find
roads.build_tile(frame, tx, ty, cells, elev, sf.urban, walks=[])
print(collections.Counter(d[0] for d in dbg)); print(collections.Counter(d[1:4] for d in dbg))
print(roads.tile_bbox(tx, ty))
sp = [s for f in cells for s in roads.road_specs(f, sf.urban, None)]
print(collections.Counter((s.cls, s.urban, tuple(s.walk) if hasattr(s.walk, '__iter__') else s.walk) for s in sp if s is not None))
print("sides", collections.Counter((s.cls, getattr(s, "sides", "NA") if getattr(s, "sides", None) is None else tuple(s.sides), roads.has_sidewalks(s)) for s in sp if s is not None))
