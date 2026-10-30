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
  tests.test_surface tests.test_osmtags tests.test_blockworker tests.test_tunnels tests.test_underpass_profiles`
  (371 tests, ~15 s, all pass in both road modes below; on Linux with `PYTHONPATH=tests`, as `test_grading` imports
  `test_roads` bare). `test_roads.TrackerTest`'s `test_an_away_camera_gets_its_tiles_after_the_cars_and_keeps_them`
  and `test_other_vehicles_foci_get_their_tiles_after_the_cars_and_keep_them` are flaky (one fails about every third
  run on the unchanged tree, more under load, 2026-10-29: tile order by time). The town-cut test was flaky too (a
  race, fixed: `_bridge_reach`).
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
| The abutment of a deck ending on the DEM's trench slope (the 10-30 m DEM smooths the Prospect Expwy's walls into 1:3 banks; Overture's range ends ~10 m inside them: 31.6 m at 11th Ave's end where the street is 38.7 m): the ramps (`bridge_lifts`), the dip's deck height (`_lower_underpasses`) and the street's plan all take the *filled* height (`abutment_height` = `fill_abutment` on the approach with the road it goes on in past its far end, `_goes_on`), and `fill_abutment` drops the stretch's samples still on the slope before fitting the grade (a 22 m part had its 14-40 m stretch on the slope's top: no fill, a 4 m sag into the trench). `plan_roads` fills with the extension in the profile | `grading.py` `abutment_height`, `_goes_on`, `fill_abutment`, `plan_roads`, `bridge_lifts(end_z=)` | same test; `approach_check.py` (2026-10-29, after the fixes below): 11th Ave decks 5a6d064d 38.23 off → 37.76 m on, cc59f431 39.47, 729d02a3 39.30; the street -0.2 to +0.2 m against the deck 12 m on (the commit's "36.8-37.6 m" did not reproduce) |
| `abutment_height` joined the road going on past the approach's far end onto its *bridge* end (the two `vstack` branches swapped), so the abutment read that road's ground 40-50 m off: Fort Hamilton Pkwy's 16.7 m where the street's plan has 18.6 m, and the trench under every such deck was dug ~2 m deeper than its deck needs (3ec1b883's dip -1.32 → -0.40 m, 59a150c6's -2.79 → -0.83) | `grading.py` `abutment_height` | `test_grading.TownCutTest.test_abutment_height_reads_the_road_going_on_past_the_far_end` |
| A part's chain extension that runs onto a bridge's abutment is filled there as the part leading onto it is (`ext_abut`); raw, the DEM's trench slope was in the neighbour's smoothing only: Seeley St (ad9c961c) planned 1.4 m under the DEM at its node with 18th St, 12 m from the d3a5ba33 deck, where the stub and 18th St met the DEM | `grading.py` `plan_roads(ext_abut=)`, `Grader._ext_abuts`, `_make_cell` | `test_grading.ExtensionFillTest` |
| A road joined at a dipped junction rises back no steeper than the dip's own ramps (`UNDER_GRADE`, deeper only, corners rounded, line ends kept): the kernel blends a junction's roads back over a fixed 20 m whatever the depth, 12-18 % on the exit at Fort Hamilton Pkwy (e8451d88) once the town cut dug the trench, a 0.11 m jolt. In Python (`earthfast` is prebuilt for Windows too), per whole line, the same in every cell. `BNE_UNDER_JOINT_GENTLE=off` for the old blend | `grading.py` `underpass_offsets`, `_gentle_joints`, `_arc_of`, `UNDER_JOINT_GENTLE_ON` | `test_grading.JunctionDipTest` |
| `roads._bridge_reach` put a placeholder (`over` 0) in its memo while it worked; another thread planning the next cell read it, and `_skew_decks` kept the street joined to the bridge unlengthened for good. The deck's end and the dip under it changed from run to run (`TownCutTest.test_a_bridge_range_ending_inside_the_cutting`: 2.71 or 3.10 m, failed 3 runs in 8). Now one thread makes it, the others wait (an RLock on the cell's features) | `roads.py` `_bridge_reach`, `_bridge_reach_calc`, `_reach_done` | `test_roads.BridgeReachTest`; the town-cut test passes 30/30 |
| A feature's level ranges are its *home* copy's in every cell (the cell its bbox centre lies in, Overture's `cx`/`cy`, the one that builds it). Each cell's copy lengthened its decks (`_skew_decks`) over the roads and bridges *that cell* has, and copies disagreed by metres where a deck ends near a cell's edge: 27 of the 265 features with ranges in 2+ cells along the BQE (`b76c613e`, the exit at Kent Ave, 40.69604,-73.96365: one cell's ground part began 9 m short, down the DEM's slope under the deck: a 2.4 m wall; the BQE's own decks 69f07220, b0ad0591, e9342e78, 8fba762c, c9d3429d too). Now 0 | `roads.py` `_home`, `_home_key`, `level_ranges`, `raw_level_ranges`, `_cell_ranges` (a cell's all-at-once build makes only the features it is home to); `mapdata.py` (`_bnge_home` hook on the roads layer) | `test_roads.HomeCopyTest` |
| A short underpass's portal is carried out of a major road's graded ground as the grading has it (a town road's width with its parking and shoulder, not the bare rural carriageway). Tried for city streets too (tertiary .. living street over it): it cleared East Burnside Ave's underpass under the Grand Concourse's service roads (40.85240,-73.90360, a 1.7 m dip in them), but carried East 175th St's (40.84658,-73.90969) 8 m back over its own approach, where its ground part climbs the DEM's smoothed wall: a 3.9 m wall, and Tremont Ave's grew 0.4 → 1.2 m. Kept for the major roads only; Burnside's service roads keep a 0.9 m wall (remaining) | `roads.py` `_clear_end` | `test_tunnels.PortalClearTest.test_end_under_a_major_town_road_carried_past_its_graded_ground` |
| A tunnel's open sections and mouths are walled as the road's own ground parts are: the cuttings' sharp walls (`_add_cuts`), its class's town wall height, its carriageway's half (not the shoulder's). They were bare plans graded out on an earth slope: the BQE's Williamsburg trench, mapped as a run of short tunnels under the cross streets, pulled Division Ave's abutment (ec9a1bb2, 40.70719,-73.95767) from 15.5 to 12.9 m 7 m from a mouth | `grading.py` `_tunnel_walls`, `_mouth_plans` (`road_half`), `_make_cell` | `test_grading.TunnelWallTest` |
| A deck reaches (`_bridge_reach`) over a road whose piece under it is a short tunnel (≤ `UNDERPASS_MAX_M`) as over one on the ground: Division Ave's deck ended 6.4 m from the BQE's centre line, over its carriageway (the BQE is a tunnel there), so its street sank into the trench. Now 0.223-0.510 → 0.142-0.614 and the street meets it at 15.5 m | `roads.py` `_raw_short_tunnels`, `_bridge_reach` | `test_roads.TunnelUnderDeckTest` |
| A deck reaches over a drawn railway's track under it too (its bed's 3 m half width): 4th Ave's deck over the LIRR's Bay Ridge cut (b0b795ce, 40.65315,-74.00572) ended 4 m off the track, its abutment on the cut's graded slope; once the gap fill held the street at its level to the deck, the cut's plan beside it made a 2.8 m drop (main: 0.9). Now 0.137 → 0.033 and the street meets the deck flat | `roads.py` `_bridge_reach`, `SKEW_RAIL_HALF_M` | `test_roads.RailUnderDeckTest` |
| Where a deck spans a road or a track, a dip in the DEM at its end up to 50 m wide and 2 m deep or more is filled to its rim (`fill_abutment(wide=)`, `_fill_gap`): the 10 m DEM spreads an embankment's end over 30-40 m. The BQE's 37 m deck over Kent Ave (c9d3429d, 40.6999,-73.9617) ended at the bottom of the DEM's 100 m wide gap (15 m of street level, then a 10-15 % slope up to the embankment 34 m out): no fill, the deck at the street's 3.5 m, and Kent Ave and Hewes St dug 7 m under it to -4.5 m. Now the decks sit at 9.4 m and Kent Ave at its 3.0 m. The street's plan, the ramps and the dip read the same flag (`Grader._end_wide`, `spans_a_road`) | `grading.py` `fill_abutment`, `_fill_gap`, `_fill_near`, `ABUT_SLOPE_MAX_M`, `ABUT_GAP_MIN_M`, `ABUT_STEEP_WIDE`, `abutment_height(wide=)`, `plan_roads` (abut 2), `_ext_abuts`, `_end_wide` | `test_grading.AbutmentGapTest` |
| ... only where one ground road leads onto the deck's end, and the parts leading onto it and their extensions read `ABUT_LOOK_M` of road, so every plan there sees the same rim: at the Washington Bridge's Bronx end (40.84511,-73.92253) a 5 m stub (be9b3e6d) and the street it meets (97824669) filled differently, 8 m apart, a 2.0 m wall (new against main on the Cross Bronx corridor). `_ground_table` keyed its parts by their first point and point count, so two 2-point segments from one node were one road (found by the junction test): now by both ends | `grading.py` `_end_wide_calc`, `ABUT_LOOK_M`, `_extensions(long=)`, `_make_cell`, `abutment_height`, `_ground_table` | `test_grading.AbutmentGapTest.test_the_street_under_a_motorway_on_its_embankment_stays` |
| An abandoned railway's bridge pieces no longer make the roads under them dip (it is still drawn, and road decks over its track still clear it): Brooklyn's Jay Street Connecting Railroad in DUMBO has a 117 m bridge piece whose ends' ground is the street's, and Adams and York Streets (40.70189,-73.98860) were dug 7 m under it | `grading.py` `_cell_enu_calc` | `test_grading.AbandonedRailBridgeTest` |

Not yet tested in game (user testing as of 2026-10-28; none of the 2026-10-29/30 fixes yet: see NEXT, "Where to drive").

`depth_check.py` on 2026-10-29 (cell 40.6476,-73.9770, both 11th Ave decks and the ramp bridge): every deck fits with
the ends level with the street. Two chains in the cell log `bridge clearance below 4.9 m target`: Seeley Street
(`d3a5ba33`, 4.54 m) and `0d19bc63` (4.79 m) — the deck solver's own abutment/grade limits, a semi (4.1 m) still passes.

## NEXT: humps over the Prospect Expressway, then the corridors (2026-10-29/30)

### Findings: why the town cut missed Seeley St and half-fixed Fort Hamilton Pkwy

`approach_check.py` reproduced the review's numbers first (Seeley ad9c961c end 1: 3.8 / 2.9 / 1.8 both off and on;
Fort Hamilton Pkwy ddcfd6f2: 6.0 / 3.0 / -0.3 off, 3.5 / 1.3 / -0.3 on).

- **Seeley St ad9c961c does not cross the expressway.** Its deck (45 m, 40.6545,-73.9764) spans "Prospect Avenue"
  (26162ade, tertiary) beside the IND Culver tunnel, in a cut of its own in the DEM (Seeley 32.5 m both sides, Prospect
  Ave 28.0-28.3 m under it on a steady 7 % grade). The only ground road under it is that tertiary, `gives_way` False:
  the town cut was never meant to reach it. Open problem 7.
- **Fort Hamilton Pkwy ddcfd6f2 is no ramp.** Both ground parts under it (3ec1b883, 59a150c6) give way, and
  `bridge_lifts` raises neither end. Its "lift at 12 m" is `fill_abutment` carrying the street's own ~4 % rise over the
  DEM's trench slope, which reaches 16 m back from Overture's bridge end there (DEM 17.8 m at 20 m, 16.8 m at 12 m, 14.4
  m at the end). The graded street climbs steadily 16.5 → 18.5 m to the deck and on to 19.9 m, as the DEM does on the
  two sides. approach_check's lift at 12 m reads the slope as the street; connect_check's `hump` (the deck end over the
  line of the street's own ground 20-60 m past it) is the measure for it: under 1 m with the cut on, 2.9 m off.
- **Open problem 6 is not the cause:** `town_check.py`'s 9 carriageways all get the wall, and every Prospect Expwy part
  under a street deck in the corridor gives way.
- The checks compare deck heights with the ground in the frame's up (the earth's curvature off: 7 cm 930 m from the
  frame's origin), as the decks have them; approach_check's "street - deck" did not (small near `--at`).

### approach_check.py, final (2026-10-30, cell 40.6476,-73.9770, town cut on)

Lift over the DEM at 2 / 12 / 40 m past each free deck end, and the street - deck at 12 m:

| Street (fid) | lift 2 / 12 / 40 m | street - deck at 12 m |
|---|---|---|
| 11th Ave (5a6d064d) | 5.2 / -0.1 / -0.0 (deck 37.76 m, 38.23 off) | -0.2 |
| 11th Ave (cc59f431, 729d02a3) | 4.1 / 0.2 / -0.0; 3.8 / 0.0 / 0.0 | 0.0; 0.2 |
| Fort Hamilton Pkwy (ddcfd6f2) | 3.5 / 1.3 / -0.3 (the fill over the DEM's slope, above); other end -0.0 / -0.0 / -0.1 | -0.4; -0.0 |
| Seeley St (d3a5ba33, over the expressway) | 0.0 / 0.0 / -0.1; 1.7 / -0.1 / 0.5 | -0.1; -0.0 |
| Caton Ave (0d19bc63) | 0.0 / 0.0 / 0.2; 0.0 / -0.2 / -0.3 | 0.1; -0.2 |
| Seeley St (ad9c961c, over Prospect Ave, not the expressway) | 3.6 / 2.6 / 1.6; 2.9 / 2.9 / 2.0 | open problem 7 |

Prospect Expwy ground parts: max grade 7.6 % (c496c742; off 7.1), max curvature over 20 m 0.0023 /m (6df52be8), lowest
1.27-2.51 m under the DEM; dips under the street decks 0.27-0.91 m (on - off; 1.32-2.79 m before `abutment_height`'s
fix).

**Done criteria:** streets over the Prospect Expwy lift at most 0.2 m at 12 m past a free deck end, but for Fort
Hamilton Pkwy's 1.3 m, which is the fill over the DEM's 16 m smoothed slope (the street rises steadily to the deck, its
hump over its own line under 1 m: connect_check finds none on the corridor with the cut on, 2.9 m off); the expressway
within 7.7 % and 0.003 /m; connect_check's Prospect corridor: no wall, step, deck, gap or hump (main: 2 walls, 4 steps, 3
humps), one hill (Seeley ad9c961c's ramp, open problem 7). Other cities and corridors below.

### Other cities: town cut off vs on, and against main (connect_check, 2026-10-29/30)

- **Commerce St over I-95, Springfield VA** (38.7806,-77.1777): the cut changes one link only, Shirley Hwy's b5a0b58e
  beside its mainline 0.87 → 0.49 m; Commerce St keeps its ramps (5.8 / 4.7 / 2.8 m: the motorway under it is not
  urban there, so no dip it doesn't need). Against main: walls 20 → 22, steps 4 → 9: two 0.33-0.43 m walls and five
  0.10-0.19 m steps on Franconia Rd's unnamed carriageways (38dd52e7, 75115583, 38.7784,-77.1790), beside its main
  carriageways that the gap fill now carries to their decks over Loisdale Rd. Pre-existing and left: Franconia Rd's
  secondary carriageways 7-8 m under the DEM (38.7779,-77.1754): its twin carriageways' decks over Commerce St /
  Loisdale Rd sit at the street's level, each one's ramps held down by the other carriageway beside it
  (`approach_add`'s fill-slope cap), so the dip under them took 7 m and spread through the junctions.
- **I-95 by Canton, Baltimore** (39.2830,-76.5600): the cut fixes Eastern Ave's 1.6 m hump (6f0e90b2) and adds none.
  Against main: walls 25 → 22; one new 0.37 m wall (Grundy St 9dea1205 beside a service road) and two 0.18 m steps on
  O'Donnell St beside the rail bed. Pre-existing: the dips under Eastern Ave's decks over the rail yard spread hundreds of
  metres through the street grid (South Macon St 3.3 m under the DEM, 39.2817,-76.5573), the same joined-dip spread.

### Corridors (connect_check --corridor, roads within 80 m of the named road; main → branch, 2026-10-30)

| corridor | wall main → branch | step main → branch | deck main → branch | gap main → branch | hump main → branch | hill main → branch | new walls/steps/decks/gaps | gone |
|---|---|---|---|---|---|---|---|---|
| bqe | 67 → 44 | 25 → 24 | 7 → 0 | 0 → 0 | 29 → 2 | 16 → 5 | 15 | 46 |
| gowanus | 15 → 7 | 9 → 4 | 0 → 0 | 0 → 0 | 4 → 5 | 1 → 1 | 4 | 17 |
| prospect | 2 → 0 | 4 → 0 | 0 → 0 | 0 → 0 | 3 → 0 | 1 → 1 | 0 | 6 |
| flatbush | 2 → 2 | 2 → 1 | 0 → 0 | 6 → 0 | 1 → 1 | 0 → 0 | 0 | 7 |
| 4thave | 19 → 3 | 1 → 0 | 0 → 0 | 1 → 0 | 0 → 0 | 1 → 1 | 0 | 18 |
| oceanpkwy | 0 → 0 | 3 → 0 | 0 → 0 | 0 → 0 | 1 → 0 | 0 → 0 | 0 | 3 |
| fdr | 20 → 16 | 5 → 8 | 0 → 0 | 0 → 0 | 0 → 0 | 0 → 0 | 3 | 4 |
| broadway | 22 → 22 | 6 → 6 | 0 → 0 | 1 → 0 | 4 → 1 | 6 → 4 | 4 | 5 |
| harlemriver | 13 → 10 | 11 → 9 | 0 → 0 | 0 → 0 | 7 → 5 | 3 → 3 | 2 | 7 |
| crossbronx | 38 → 12 | 16 → 10 | 11 → 3 | 0 → 0 | 23 → 11 | 15 → 6 | 5 | 45 |
| concourse | 36 → 30 | 16 → 15 | 0 → 0 | 0 → 0 | 3 → 3 | 15 → 13 | 7 | 14 |
| lie | 34 → 15 | 33 → 17 | 8 → 0 | 0 → 0 | 9 → 4 | 4 → 1 | 7 | 50 |
| queensblvd | 23 → 19 | 12 → 13 | 0 → 0 | 0 → 0 | 5 → 2 | 3 → 2 | 3 | 6 |
| gcp | 35 → 32 | 25 → 17 | 1 → 0 | 0 → 0 | 24 → 10 | 6 → 1 | 10 | 22 |
| siexpwy | 12 → 1 | 8 → 3 | 6 → 0 | 0 → 0 | 4 → 0 | 0 → 0 | 3 | 25 |

"new": walls, steps, deck and gap failures on the branch with no failure of the same kind on the same road within 40 m
on main (most are a main failure moved or a small one: 0.3-0.9 m, beside another road's plan). The worst new one: a
2.1 m wall on the GCP's flyover link at Flushing Meadows (132cfdea, 40.75845,-73.85301), beside the parkway (88c90ac9)
dipping 2.5 m under its deck: two roads side by side at different levels (1 below). Deck failures left: East 175th St's
deck at 40.84349,-73.89447 has a free end no road leads onto, and two 0.05-0.06 m steps onto decks (Cross Bronx Expwy
service road 40.84502,-73.90481, Weeks Ave 40.84516,-73.90754). Gaps: none (the 6 on Flatbush Ave and Rector St's on
Broadway were parts under construction, below). Junction spreads, ramps and divided pairs are listed by connect_check
but not ranked here: they are slopes, not steps.

Walls and steps left, by what they are up against (connect_check names the plan beside each):

1. **Two roads side by side at different levels whose carriageways overlap** (most of what is left): a street on the
   rim of a trench beside the motorway in it or a road on a viaduct's ramp beside the one under it, their centre lines
   closer than their two half widths in the map (Columbia Heights, Furman St and the BQE's triple cantilever,
   40.6935,-73.9993, which the map has as ground, layer 1, over Furman St; Hicks St on the BQE's Red Hook trench,
   40.6813,-74.0025; the RFK Bridge's Queens approach and 26th St / Hoyt Ave, 40.7727,-73.9203, 3.35 m, on main too;
   Borden Ave, 40.7292,-73.9106). The heightfield can't hold both: one gets a step at the other's wall. Needs a stacked
   or walled-in pair handled in grade_points (the Rust kernel), or decks for the upper one.
2. **Elevated railways mapped as short bridges over each street with ground between**: the DEM has no viaduct, the rail
   decks are taken straight between ends at the street's level, and the streets under them dip 6-8 m (East Harlem and
   Mott Haven under Metro-North, 40.799-40.812,-73.937; the GCP beside the LIRR at Flushing, 91029092, 40.7500,-73.8509,
   2.57 m, on main too). The road version of this is fixed (the gap fill); rails need the same in railways.py.
3. **Underpasses under the Grand Concourse**: its service roads cross the cross streets' underpass mouths (Burnside
   40.8524,-73.9037, Tremont 40.8494,-73.9060, Bedford Park 40.8721,-73.8879, 161st 40.8268,-73.9227), 0.6-1.2 m.
   Carrying the portals past them helped Burnside and broke East 175th St (Done table): the street's own ground part
   climbs the DEM's smoothed wall at the portal, which wants a mirrored `fill_abutment` (a cut kept to the portal).
4. **Junction spreads** (hundreds per corridor, most 0.05-0.4 m): at a crossroads two roads' plans are smoothed apart
   (a flat avenue and a cross street climbing out of it: 4th Ave / 17th St 0.41 m, 40.66492,-73.99329); the ground
   blends them into a slope. Needs one height per node that every cell computes the same.
5. **Flatbush Ave's 6 "gaps"** (40.6775-40.6849,-73.9727..-73.9779) on main and the branch alike were parts under
   construction (`highway=construction`), not drawn by design (GitHub #184); connect_check skips them now.

Where to drive (the fixes): Seeley St / 18th St (40.65408,-73.97760), the exit at Fort Hamilton Pkwy (40.64932,
-73.97512), Kent Ave and Hewes St under the BQE (40.7006,-73.9626), Division Ave (40.70719,-73.95767), the BQE exit to
Kent Ave (40.69604,-73.96365), Adams St in DUMBO (40.70189,-73.98860), East Burnside Ave and the Concourse
(40.85240,-73.90360), 4th Ave over the Bay Ridge cut (40.65315,-74.00572), the Washington Bridge's Bronx end
(40.84511,-73.92253), and the Prospect Expwy as before (11th Ave 40.6476,-73.9770).

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
8. **Roads side by side at different levels with overlapping carriageways** (a street on a trench's rim, a ramp beside
   the road it climbs from, the BQE's triple cantilever over Furman St mapped as ground): the heightfield holds one of
   them, the other gets a step. The biggest walls left on the corridors (NEXT, walls left, 1). Needs the pair walled
   apart at the carriageways' edges in grade_points (Rust), or decks for the upper one.
9. **Elevated railways mapped as short bridges with ground between** (Metro-North in East Harlem and Mott Haven, the
   LIRR by Flushing): the rail decks are straight between ends at the DEM's street level, so the streets under them dip
   6-8 m. The roads' version is fixed (`_fill_gap`); railways.py's decks and grade plans need the same.
10. **A dip spreads through a street grid**: the kernel carries a junction's dip along every joined road at half grade
   (up to ~420 m for 8 m), so a deep dip sinks the blocks around it while alleys and lots beside stay at the DEM
   (Baltimore's South Macon St, 3.3 m, 39.2817,-76.5573). And a divided road's decks: each carriageway's ramps are held
   down by the other carriageway beside it (`approach_add`), so the road under takes the whole clearance (Springfield's
   Franconia Rd, 7-8 m, 38.7779,-77.1754).

## Repro spots

- Prospect Expwy trench: 40.66556,-73.99318 and 40.6476,-73.9770 (11th Ave / Caton Ave decks)
- Culver Line open cut: 40.64780,-73.97558
- Mobil at Caton Ave (gas pad): near 40.6476,-73.9770
- Park Slope grid (paint, corners): 40.6720,-73.9790
