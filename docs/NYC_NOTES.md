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
| `abutment_height` joined the road going on past the approach's far end onto its *bridge* end (the two `vstack` branches swapped), so the abutment read that road's ground 40-50 m off: Fort Hamilton Pkwy's 16.7 m where the street's plan has 18.6 m, and the trench under every such deck was dug ~2 m deeper than its deck needs (3ec1b883's dip -1.32 → -0.40 m, 59a150c6's -2.79 → -0.83) | `grading.py` `abutment_height` | `test_grading.TownCutTest.test_abutment_height_reads_the_road_going_on_past_the_far_end` |
| A part's chain extension that runs onto a bridge's abutment is filled there as the part leading onto it is (`ext_abut`); raw, the DEM's trench slope was in the neighbour's smoothing only: Seeley St (ad9c961c) planned 1.4 m under the DEM at its node with 18th St, 12 m from the d3a5ba33 deck, where the stub and 18th St met the DEM | `grading.py` `plan_roads(ext_abut=)`, `Grader._ext_abuts`, `_make_cell` | `test_grading.ExtensionFillTest` |
| A road joined at a dipped junction rises back no steeper than the dip's own ramps (`UNDER_GRADE`, deeper only, corners rounded, line ends kept): the kernel blends a junction's roads back over a fixed 20 m whatever the depth, 12-18 % on the exit at Fort Hamilton Pkwy (e8451d88) once the town cut dug the trench, a 0.11 m jolt. In Python (`earthfast` is prebuilt for Windows too), per whole line, the same in every cell. `BNE_UNDER_JOINT_GENTLE=off` for the old blend | `grading.py` `underpass_offsets`, `_gentle_joints`, `_arc_of`, `UNDER_JOINT_GENTLE_ON` | `test_grading.JunctionDipTest` |
| `roads._bridge_reach` put a placeholder (`over` 0) in its memo while it worked; another thread planning the next cell read it, and `_skew_decks` kept the street joined to the bridge unlengthened for good. The deck's end and the dip under it changed from run to run (`TownCutTest.test_a_bridge_range_ending_inside_the_cutting`: 2.71 or 3.10 m, failed 3 runs in 8). Now one thread makes it, the others wait (an RLock on the cell's features) | `roads.py` `_bridge_reach`, `_bridge_reach_calc`, `_reach_done` | `test_roads.BridgeReachTest`; the town-cut test passes 30/30 |
| A feature's level ranges are its *home* copy's in every cell (the cell its bbox centre lies in, Overture's `cx`/`cy`, the one that builds it). Each cell's copy lengthened its decks (`_skew_decks`) over the roads and bridges *that cell* has, and copies disagreed by metres where a deck ends near a cell's edge: 27 of the 265 features with ranges in 2+ cells along the BQE (`b76c613e`, the exit at Kent Ave, 40.69604,-73.96365: one cell's ground part began 9 m short, down the DEM's slope under the deck: a 2.4 m wall; the BQE's own decks 69f07220, b0ad0591, e9342e78, 8fba762c, c9d3429d too). Now 0 | `roads.py` `_home`, `_home_key`, `level_ranges`, `raw_level_ranges`, `_cell_ranges` (a cell's all-at-once build makes only the features it is home to); `mapdata.py` (`_bnge_home` hook on the roads layer) | `test_roads.HomeCopyTest` |
| A short underpass's portal is carried past a *city street* over it too (tertiary, residential, unclassified, living street, not only motorway..secondary), and out of the street's graded ground as the grading has it (a town street's width with its parking and shoulder, not the bare rural carriageway). East Burnside Ave's underpass under the Grand Concourse (40.85240,-73.90360) ends ~10 m past the Concourse's service roads: its floor at the portals was the blended 25.7 m over the cut's 24 m, 3.8 m under the Concourse, so it was graded as an open road and dug the service roads 1.7 m down where they cross it. Now 56 → 68 m, a low arch, the service roads flat at 29.4-29.5 m | `roads.py` `PORTAL_CLASSES`, `_clear_end` | `test_tunnels.PortalClearTest.test_end_under_a_town_street_carried_past_its_graded_ground` |
| ... but for a city street over it, never past the tunnel road's own end (`PORTAL_ON_CLASSES` keep the old carry onto the road beyond): East 175th St's underpass is mapped to its node with Morris Ave (40.84658,-73.90969), which turns away; carried straight on under the Concourse's service road, the tube left the road 8 m into the block: a 3.9 m wall and a gap (connect_check's gap and wall, new against main, 2026-10-29) | `roads.py` `PORTAL_ON_CLASSES`, `_clear_end` | `test_tunnels.PortalClearTest.test_not_carried_past_its_own_end_for_a_city_street` |
| A tunnel's open sections and mouths are walled as the road's own ground parts are: the cuttings' sharp walls (`_add_cuts`), its class's town wall height, its carriageway's half (not the shoulder's). They were bare plans graded out on an earth slope: the BQE's Williamsburg trench, mapped as a run of short tunnels under the cross streets, pulled Division Ave's abutment (ec9a1bb2, 40.70719,-73.95767) from 15.5 to 12.9 m 7 m from a mouth | `grading.py` `_tunnel_walls`, `_mouth_plans` (`road_half`), `_make_cell` | `test_grading.TunnelWallTest` |
| A deck reaches (`_bridge_reach`) over a road whose piece under it is a short tunnel (≤ `UNDERPASS_MAX_M`) as over one on the ground: Division Ave's deck ended 6.4 m from the BQE's centre line, over its carriageway (the BQE is a tunnel there), so its street sank into the trench. Now 0.223-0.510 → 0.142-0.614 and the street meets it at 15.5 m | `roads.py` `_raw_short_tunnels`, `_bridge_reach` | `test_roads.TunnelUnderDeckTest` |
| Where a deck spans a road or a track, a dip in the DEM at its end up to 50 m wide and 2 m deep or more is filled to its rim (`fill_abutment(wide=)`, `_fill_gap`): the 10 m DEM spreads an embankment's end over 30-40 m. The BQE's 37 m deck over Kent Ave (c9d3429d, 40.6999,-73.9617) ended at the bottom of the DEM's 100 m wide gap (15 m of street level, then a 10-15 % slope up to the embankment 34 m out): no fill, the deck at the street's 3.5 m, and Kent Ave and Hewes St dug 7 m under it to -4.5 m. Now the decks sit at 9.4 m and Kent Ave at its 3.0 m. The street's plan, the ramps and the dip read the same flag (`Grader._end_wide`, `spans_a_road`) | `grading.py` `fill_abutment`, `_fill_gap`, `_fill_near`, `ABUT_SLOPE_MAX_M`, `ABUT_GAP_MIN_M`, `ABUT_STEEP_WIDE`, `abutment_height(wide=)`, `plan_roads` (abut 2), `_ext_abuts`, `_end_wide` | `test_grading.AbutmentGapTest` |
| ... only where one ground road leads onto the deck's end, and the parts leading onto it and their extensions read `ABUT_LOOK_M` of road, so every plan there sees the same rim: at the Washington Bridge's Bronx end (40.84511,-73.92253) a 5 m stub (be9b3e6d) and the street it meets (97824669) filled differently, 8 m apart, a 2.0 m wall (new against main on the Cross Bronx corridor). `_ground_table` keyed its parts by their first point and point count, so two 2-point segments from one node were one road (found by the junction test): now by both ends | `grading.py` `_end_wide_calc`, `ABUT_LOOK_M`, `_extensions(long=)`, `_make_cell`, `abutment_height`, `_ground_table` | `test_grading.AbutmentGapTest.test_the_street_under_a_motorway_on_its_embankment_stays` |
| An abandoned railway's bridge pieces no longer make the roads under them dip (it is still drawn, and road decks over its track still clear it): Brooklyn's Jay Street Connecting Railroad in DUMBO has a 117 m bridge piece whose ends' ground is the street's, and Adams and York Streets (40.70189,-73.98860) were dug 7 m under it | `grading.py` `_cell_enu_calc` | `test_grading.AbandonedRailBridgeTest` |

Not yet tested in game (user testing as of 2026-10-28; the town-cut fixes of 2026-10-29 not yet).

`depth_check.py` on 2026-10-29 (cell 40.6476,-73.9770, both 11th Ave decks and the ramp bridge): every deck fits with
the ends level with the street. Two chains in the cell log `bridge clearance below 4.9 m target`: Seeley Street
(`d3a5ba33`, 4.54 m) and `0d19bc63` (4.79 m) — the deck solver's own abutment/grade limits, a semi (4.1 m) still passes.

## NEXT: humps over the Prospect Expressway — reopened after review (2026-10-29)

### Findings, second pass (2026-10-29)

`approach_check.py` reproduced the review's numbers first (Seeley ad9c961c end 1: 3.8 / 2.9 / 1.8 both off and on;
Fort Hamilton Pkwy ddcfd6f2: 6.0 / 3.0 / -0.3 off, 3.5 / 1.3 / -0.3 on).

- **Seeley St ad9c961c does not cross the expressway.** Its deck (45 m, 40.6545,-73.9764) spans "Prospect Avenue"
  (26162ade, tertiary) beside the IND Culver tunnel, in a cut of its own in the DEM (Seeley 32.5 m both sides, Prospect
  Ave 28.0-28.3 m under it on a steady 7 % grade). The only ground road under it is that tertiary, `gives_way` False:
  the town cut was never meant to reach it, and its ramps are the clearance over a city street (7.25 m deck over the
  road under, 4.3 m in the DEM). Open problem 7 below.
- **Fort Hamilton Pkwy ddcfd6f2 is no ramp.** Both ground parts under it (3ec1b883, 59a150c6) give way (`gives_way`
  True), and `bridge_lifts` raises neither end. Its "lift at 12 m" is `fill_abutment` carrying the street's own ~4 %
  rise over the DEM's trench slope, which reaches 16 m back from Overture's bridge end there (DEM 17.8 m at 20 m, 16.8 m
  at 12 m, 14.4 m at the end). The graded street climbs steadily 16.5 → 18.5 m to the deck and on to 19.9 m, as the DEM
  does on the two sides. approach_check's lift at 12 m reads the slope as the street; `connect_check.py`'s `hump`
  (the deck end over the line of the street's own ground 20-60 m past it) is the measure for it: under 1 m on, 2.9 m off.
- **Open problem 6 is not the cause here:** `town_check.py`'s 9 carriageways all get the wall, and every Prospect Expwy
  part under a street deck in the corridor gives way.
- Found on the way, fixed (Done table): `abutment_height`'s swapped join (the trench dug ~2 m too deep), the chain
  extension's raw trench slope (1.4 m between two plans at Seeley / 18th St), the junction blend (12-18 % on the Fort
  Hamilton exit), the `_bridge_reach` race (decks ending at random from run to run).
- `connect_check.py` and approach_check compare deck heights with the ground in the frame's up (the earth's curvature
  off: 7 cm 930 m from the frame's origin), as the decks have them.

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
   (2026-10-29: every Prospect Expwy part under a street deck gives way in the corridor; see NEXT.)
7. **A city street's bridge over another city street** where the DEM has less room than the deck needs: Seeley St
   (ad9c961c, 40.6545,-73.9764) over Prospect Ave, 4.3 m between the two in the DEM, 7.25 m from the deck's top to the
   road under (4.9 m headroom + 2.2 m of slab and girders + margin): Seeley rises ~3 m on 80 m ramps (5 %). Neither
   road is a town motorway, so neither gives way first.

## Repro spots

- Prospect Expwy trench: 40.66556,-73.99318 and 40.6476,-73.9770 (11th Ave / Caton Ave decks)
- Culver Line open cut: 40.64780,-73.97558
- Mobil at Caton Ave (gas pad): near 40.6476,-73.9770
- Park Slope grid (paint, corners): 40.6720,-73.9790
