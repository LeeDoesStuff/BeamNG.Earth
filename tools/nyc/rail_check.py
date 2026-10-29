"""Real-data check: the rail lines and bridges by the Culver Line spot (40.64780, -73.97558), as the server reads them."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "server"))
import mapcells
import mapdata
import netfetch
import railways
import roads

CACHE = os.path.join(os.path.expanduser("~"), ".cache", "beamng-earth")
md = mapdata.MapData(CACHE, fetcher=netfetch.Fetcher(offline=False))
lat, lon = 40.64780, -73.97558
key = mapcells.cells_for_bbox(lat - 1e-4, lon - 1e-4, lat + 1e-4, lon + 1e-4)[0]
f = md.cell(key, layers=("roads",), kind="demand").layers.get("roads")
a = f.attrs
col = lambda n, i: (str(a[n][i]) if n in a and a[n][i] is not None else "")
bs = roads.bridge_specs(f)
near = []
for i in range(len(f.kind)):
    p = f.part_off[i]
    ll = f.lonlat[f.coord_off[f.ring_off[p]]:f.coord_off[f.ring_off[f.part_off[i + 1]]]]
    if not len(ll):
        continue
    d = np.hypot((ll[:, 0] - lon) * 84500, (ll[:, 1] - lat) * 111000).min()
    if d < 150 and (col("subtype", i) == "rail" or bs[i] is not None):
        near.append((round(d), col("subtype", i), col("class", i), col("osm_railway", i), col("name", i),
                     bs[i] is not None, railways.drawn(f, i)))
for r in sorted(set(near)):
    print("%4d m  sub=%-5s class=%-15s osm_railway=%-8s name=%-28s bridge=%s drawn=%s" % r)
