# NYC driving notes (fork: LeeDoesStuff/BeamNG.Earth)

Goal: Brooklyn / NYC as good a *driving* experience as the generator can give — straight, well-graded
streets with real road paint, clean lots, no junk in the lanes. A faithful visual copy of the city is
out of scope; drivable and believable is the bar.

Upstream: TecnaGamer/BeamNG.Earth, forked at `0bf942c` (1.7.0-beta). Keep fixes small and in upstream's
style (see the comments in `server/`: every constant says why, with the place it was seen) so they can
go back as PRs.

## Running

- Tests: `python -m unittest discover -s server/tests` from the repo root (the full suite is slow, 25 min+).
  For road work, run the modules: `cd server && python -m unittest tests.test_roads tests.test_roadmesh
  tests.test_grading tests.test_bridge_clearance tests.test_gasstations tests.test_vegetation
  tests.test_surface tests.test_osmtags tests.test_blockworker` (325 tests, ~13 s, all pass in both
  road modes below; on Linux with `PYTHONPATH=tests`, as `test_grading` imports `test_roads` bare).
  `test_roads.TrackerTest.test_an_away_camera_gets_its_tiles_after_the_cars_and_keeps_them` is flaky
  (fails about every other run on the unchanged tree, 2026-10-29: tile order by time).
- Real-data checks: `tools/nyc/*.py`. They fetch the same map cells / DEM / land cover the server does
  (cache `~/.cache/beamng-earth`, network on first run) and print numbers for known trouble spots.
  Cloud sessions can run these; they can't run the game.
- Game: `run_server.bat` / `run_server.sh`, then BeamNG.drive with the mod. Only the user can test in game.

## Done (commit "NYC: ...", on main)

| Fix | Where | Check |
|---|---|---|
| Street trees cleared from the *drawn* road width (was a per-class guess, ~7.8 m off a 20 m avenue's centre: trees in the lanes) | `vegetation.py` `_road_specs`, `_half_width`, `_mask_roads`; `earthd.py` passes `urban_fn` / `widths`; VERSION 15 | `test_vegetation.test_street_tree_clears_the_drawn_width` |
| Gas station pad paved out to the curbs/walls (margin 6 → 20 m, clipped in two passes by buildings and carriageways; grass in the lots) | `gasstations.py` `PAD_MARGIN_M`, `forecourt_pad` | `test_gasstations.test_city_lot_paved_to_its_curbs` |
| Town trench walls: major roads (`CONCRETE_WALL_CLASSES`) in built-up land get the 10 m concrete wall in grading, not 4 m + dirt slope (Prospect Expwy dirt banks) | `grading.py` `Grader(urban=)`, `_town_walls`; `blockworker.py`, `earthd.py`, `plancache.py` (key has the urban bit) | `test_grading.test_grader_plans_town_walls`; `tools/nyc/town_check.py` (all 9 Prospect Expwy carriageways get it) |
| Bridge headroom 6.0 → 4.9 m (16 ft Interstate minimum; env `BNE_BRIDGE_HEADROOM`). Deck top over the road under it 8.35 → 7.25 m: smaller humps on streets over the expressway | `roads.py` `MIN_BRIDGE_HEADROOM_M` | `test_bridge_clearance`; `tools/nyc/depth_check.py` |
| `sidewalk=separate` (NYC tags nearly every street so) now falls to the town rule instead of "no sidewalk". Footways are never drawn, so those streets had no curbs, corners, crosswalks or stop lines. Park Slope tile: corners 7 → 31, crosswalks 2 → 13 | `roads.py` `SIDEWALK_TAG` | `test_roads.test_sidewalks_follow_the_map_tag`; `tools/nyc/paint_check.py` |
| Launchers default `BNGE_ROADS_IN_GROUND=0`: carriageways as asphalt meshes under the lane lines, not painted into the 2 m ground grid (wavy edges). Upstream moved to in-ground for mountain hairpins; flat city doesn't need it | `run_server.bat`, `run_server.sh` | boots clean; both modes pass the tests |
| Streets over a town motorway stay at street level: the trench is cut to the clearance under the deck (the underpass dip, 8 m at most) *before* the street's ramps are raised, which now lift only what the dip can't give. `gives_way`: motorway/trunk and their links in built-up land (the ones with the 10 m concrete wall; a link is never urban in `road_specs`, so `_town_walls` reads the land cover at its points), underpasses on. Was: ramps first (up to 10 m), the dip for the rest, so the DEM's interpolated trench floor under each deck (-0.1 to 2.4 m deep at 11th Ave) became a 5-7 m hump. `BNE_TOWN_CUT=off` for the old ramps | `grading.py` `TOWN_CUT_ON`, `gives_way`, `bridge_lifts(sunk_lines=)`, `_cell_enu_calc` (town specs, `gives_way` per ground part), `_ground_table`, `_end_lift_calc`, `_town_walls` | `test_grading.TownCutTest`; `tools/nyc/depth_check.py` now also prints the solved deck (`BridgeProfiles` on the graded ground), its clearance over the graded expressway and its ends against the street |
| A short deck's chain is one span for the dip: `roads._skew_decks` carries every short deck a few metres onto the streets it joins (11th Ave: a 5 m piece + two 33 m forks), and `_lower_underpasses` took "joined" as "no span" — no dip at all under most street decks; `bridge_lifts`' give-way likewise. Now parts joined at their ends, ≤ 120 m in all, are one span with the abutments at the chain's free ends | `grading.py` `_lower_underpasses` (union-find over `uniq`, `free`, `chain_ends`), `_span_key`, `bridge_lifts` | `test_grading.TownCutTest.test_a_bridge_range_ending_inside_the_cutting` |
| The abutment of a deck ending on the DEM's trench slope (the 10-30 m DEM smooths the Prospect Expwy's walls into 1:3 banks; Overture's range ends ~10 m inside them: 31.6 m at 11th Ave's end where the street is 38.7 m): the ramps (`bridge_lifts`), the dip's deck height (`_lower_underpasses`) and the street's plan all take the *filled* height (`abutment_height` = `fill_abutment` on the approach with the road it goes on in past its far end, `_goes_on`), and `fill_abutment` drops the stretch's samples still on the slope before fitting the grade (a 22 m part had its 14-40 m stretch on the slope's top: no fill, a 4 m sag into the trench). `plan_roads` fills with the extension in the profile | `grading.py` `abutment_height`, `_goes_on`, `fill_abutment`, `plan_roads`, `bridge_lifts(end_z=)` | same test; `depth_check.py`: 11th Ave decks 37.9 → 36.8-37.6 m, -0.2 m against the street 12 m on, 8.6-9.2 m clear |

Not yet tested in game (user testing as of 2026-10-28; the town-cut fixes of 2026-10-29 not yet).

`depth_check.py` on 2026-10-29 (cell 40.6476,-73.9770, both 11th Ave decks and the ramp bridge): every deck fits with
the ends level with the street. Two chains in the cell log `bridge clearance below 4.9 m target`: Seeley Street
(`d3a5ba33`, 4.54 m) and `0d19bc63` (4.79 m) — the deck solver's own abutment/grade limits, a semi (4.1 m) still passes.

## NEXT: humps over the Prospect Expressway — reopened after review (2026-10-29)

Review of commit `2430373` on real data (Windows, same cache layout): tests pass (391, both road modes); the expressway
drives fine (max grade 7.7 %, off 7.0 %; curvature over 20 m <= 0.0025 /m, ~0.12 g at 80 km/h). But the street ramps
are only partly gone, and the numbers in the table above / the commit message did not reproduce (11th Ave decks
37.8-39.5 m, not 36.8-37.6). Keep this branch unmerged and commit here.

**Measure with `tools/nyc/approach_check.py`**, not `depth_check.py`: depth_check's "ends against the street 12 m on"
takes a joint between two deck parts for an end, and its DEM-only depth is not what the game builds (the "5-7 m humps on
11th Ave" in the old problem 1 was that estimate: graded, 11th Ave was already level on main). approach_check prints,
for every free deck end, the lift (graded street minus raw DEM) 2/12/40/80/120 m past it, town cut off and on, and the
expressway's profile. Lift at 2 m alone is the abutment fill over the DEM's smoothed wall; at 12 m and on, a ramp.
Second anchor: `--at 40.66556,-73.99318 --bbox 40.655,-74.005,40.675,-73.982` (there: only motorway links and service
roads lift, as expected).

Lift at 2 / 12 / 40 m, main -> this branch:

| Street (fid) | main | branch |
|---|---|---|
| 11th Ave (5a6d064d, 729d02a3, cc59f431) | ~0 from 12 m | same, decks 0.4-0.7 m lower |
| Seeley St (d3a5ba33, west end) | sag, street 8 m over the deck | fixed |
| Seeley St (ad9c961c, end 1) | 5.2 / 4.3 / 3.6 | 3.8 / 2.9 / 1.8 — **identical with the town cut off**: not reached by it |
| Seeley St (ad9c961c, end 2) | 2.9 / 2.9 / 2.0 | unchanged |
| Fort Hamilton Pkwy (ddcfd6f2) | 5.6 / 2.8 / -0.4 | 3.5 / 1.3 / -0.3 |
| Caton Ave (0d19bc63) | ~0 | ~0 |

To do, in order:
1. Find *why* the town cut misses Seeley St (ad9c961c) and only half-fixes Fort Hamilton Pkwy — check `gives_way` of the
   ground parts under them, don't assume. Likely open problem 6 (the parts beside Prospect Park count as rural): then fix
   the urban classification, not the dip.
2. Other cities: `gives_way` applies to every urban motorway/trunk. Run approach_check (`--at`, `--bbox`, `--motorway`)
   at one or two non-NYC urban motorways with street overpasses (Commerce St over I-95, Springfield VA, upstream #138;
   I-95 by Canton, Baltimore): grade, curvature, dip depth and street lifts with the cut off vs on. No dip a motorway
   didn't need.
3. **Roads connect and make sense** (the user, 2026-10-29: "ensure that roads connect properly and make sense"). Every
   fix here moves road heights, so check the whole network around it, not only the deck you fixed. Write
   `tools/nyc/connect_check.py` (graded ground and solved decks as approach_check builds them, same `--at` / `--bbox`)
   and report, off vs on:
   - **junctions:** at every node two or more ground roads share (`junctions.NODE_TOL`), the spread of their graded
     heights: no step at an intersection (target <= 5 cm);
   - **deck to street:** the step where a deck meets its approach (approach_check's street - deck at 2 m: <= 5 cm), and
     no deck end left in the air or buried (a free end with no ground road within `APPROACH_TOUCH_M`);
   - **ramps:** motorway links meet their mainline at its height and grade (no step, no kink over 2 %);
   - **divided roads:** paired one-way carriageways (and a street's two halves around a median) at about the same height
     side by side, unless the DEM says otherwise;
   - **made-up hills:** street grades the DEM doesn't have (graded grade over the DEM's own + 2 %), and city-street
     curvature over 20 m above 0.01 /m (~0.12 g at 40 km/h);
   - **nothing dropped:** every map road's ground part still drawn in the road tiles (`roads.build_tile` carriageways vs
     `roads.ground_parts`), joined where the map joins them (chunk ends within 0.5 m), no road through a building or
     under the trench wall.
   List each failure with the street names and lat/lon so the user can drive there.
4. Put approach_check's and connect_check's real output in this file; drop the numbers that didn't reproduce.

Done when: no city street (residential .. primary) over the Prospect Expressway lifts more than 1 m at 12 m past a free
deck end; the expressway stays within 7.7 % and 0.003 /m; connect_check finds no new failure against main (and the old
ones are listed); no regression in the other cities; tests pass.

## Open problems (ranked by how much they hurt driving)

1. **Open-cut subways not modelled.** IND Culver Line by 40.64780,-73.97558: `osm_railway=subway` is excluded
   by `railways.drawn()` (`OSM_RAILWAY_NOT`), so the cut is raw DEM — dirt banks and humped streets over it.
   Needs: draw/grade subway segments that are not `tunnel=yes`, a sharp cut + `retaining_walls` for rail
   plans, decks for the street bridges over them. `tools/nyc/rail_check.py` lists what is there.
2. **Wavy/lumpy street edges** left in grading on the 2 m grid near cuts (T_MIN 5 / T_MAX 12 transitions).
3. **Road paint coverage.** Lines by class: `CENTRE_CLASSES`, `EDGE_LINE_CLASSES`, `LANE_CLASSES` in `roads.py`.
   NYC specifics not handled: double-yellow on avenues vs none on one-way side streets (check), bike lanes
   (green paint, OSM `cycleway=lane`), bus lanes (red, `busway`/`bus:lanes`), crosswalk style (NYC ladder),
   parking lane lines. Mapillary's detections (CC-BY-SA, open API: crosswalks, lane lines, bike-lane paint,
   signs, with positions) could fill in where OSM has no tag; Google Street View can't be used (its terms
   forbid deriving map data, and there is no bulk API for a server).
4. **Crosswalks only where both corners have sidewalks** (`junctions.find`); T-junctions through-road side
   gets none (`walk/angle` rejections at 180/270°) — real NYC has them.
5. **Real bridge and trench heights.** Deck heights are still the abutments' (filled) DEM plus the clearance
   rule, and the Prospect Expwy trench floor under each street deck is now the dip's (deck minus 7.25 m,
   ~28.5 m at 11th Ave where the DEM's floor is 30.9 m), 6.5-7.5 m deep from the DEM between the decks:
   believable, not surveyed; the trench walls are the DEM's 1:3 banks behind the 10 m concrete wall. Lidar
   would give the real ones: USGS 3DEP point clouds (class 17 "bridge deck", upstream's stated next step)
   or NYC's own 2017 1-ft lidar DEM. Street-level photos don't help here (nobody measures a trench depth
   from a photo). Seeley Street's deck (`d3a5ba33`) is 0.36 m short of the 4.9 m headroom.
6. **Expressway parts along Prospect Park are rural** for grading (`URBAN_SHARE` of their vertices built-up,
   the park is tree cover): no town wall, and the street decks over them rise on ramps as in the country
   (`depth_check` neighbour at ENU (-506, 1110) of 40.6476,-73.9770: lifts 6.7-9.2 m). A motorway in a city
   with buildings on one side should count as urban (`URBAN_BESIDE_SHARE`/`URBAN_BOTH_SHARE` in `roads.py`).

## Repro spots

- Prospect Expwy trench: 40.66556,-73.99318 and 40.6476,-73.9770 (11th Ave / Caton Ave decks)
- Culver Line open cut: 40.64780,-73.97558
- Mobil at Caton Ave (gas pad): near 40.6476,-73.9770
- Park Slope grid (paint, corners): 40.6720,-73.9790
