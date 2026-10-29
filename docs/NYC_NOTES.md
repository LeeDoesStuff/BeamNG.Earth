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
  tests.test_surface tests.test_osmtags tests.test_blockworker` (323 tests, ~13 s, all pass in both
  road modes below).
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

Not yet tested in game (user testing as of 2026-10-28).

## Open problems (ranked by how much they hurt driving)

1. **Humps on streets over the Prospect Expressway.** `depth_check.py`: the DEM (bare earth, interpolated
   under bridges) says the trench is only -0.1 to 2.4 m deep at the 11th Ave crossings, so a deck that
   must clear 7.25 m stands 4.8-7.3 m over the street. Where the DEM is right (6.5 m) the hump is 0.8 m.
   Ideas: when a ground road under a deck is a motorway in a town cut, lower *its* profile (cut the trench
   to the clearance, walls already 10 m) instead of raising the deck; or take bridge/trench heights from
   lidar (USGS 3DEP class 17, upstream's stated next step). See `roads.BridgeProfiles`, `grading.py`,
   `docs/BRIDGE_CLEARANCE.md`.
2. **Open-cut subways not modelled.** IND Culver Line by 40.64780,-73.97558: `osm_railway=subway` is excluded
   by `railways.drawn()` (`OSM_RAILWAY_NOT`), so the cut is raw DEM — dirt banks and humped streets over it.
   Needs: draw/grade subway segments that are not `tunnel=yes`, a sharp cut + `retaining_walls` for rail
   plans, decks for the street bridges over them. `tools/nyc/rail_check.py` lists what is there.
3. **Wavy/lumpy street edges** left in grading on the 2 m grid near cuts (T_MIN 5 / T_MAX 12 transitions).
4. **Road paint coverage.** Lines by class: `CENTRE_CLASSES`, `EDGE_LINE_CLASSES`, `LANE_CLASSES` in `roads.py`.
   NYC specifics not handled: double-yellow on avenues vs none on one-way side streets (check), bike lanes
   (green paint, OSM `cycleway=lane`), bus lanes (red, `busway`/`bus:lanes`), crosswalk style (NYC ladder),
   parking lane lines.
5. **Crosswalks only where both corners have sidewalks** (`junctions.find`); T-junctions through-road side
   gets none (`walk/angle` rejections at 180/270°) — real NYC has them.

## Repro spots

- Prospect Expwy trench: 40.66556,-73.99318 and 40.6476,-73.9770 (11th Ave / Caton Ave decks)
- Culver Line open cut: 40.64780,-73.97558
- Mobil at Caton Ave (gas pad): near 40.6476,-73.9770
- Park Slope grid (paint, corners): 40.6720,-73.9790
