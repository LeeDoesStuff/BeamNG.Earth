# Fork notes

This fork works on NYC (Brooklyn) driving quality. Read `docs/NYC_NOTES.md` first: what is done, the open
problems ranked, repro coordinates, and the real-data checks in `tools/nyc/`.

- Python server in `server/`, BeamNG mod (Lua/Vue) in `mod/`. Architecture: `docs/ARCHITECTURE.md`.
- Fast road tests: `cd server && python -m unittest tests.test_roads tests.test_roadmesh tests.test_grading tests.test_bridge_clearance tests.test_gasstations tests.test_vegetation tests.test_surface tests.test_osmtags tests.test_blockworker`
- Match upstream style: comments say why, with the place and date it was seen; a test per fix.
- The game can't run here: check fixes with tests and `tools/nyc/*.py`, and leave in-game checks to the user.
- Roads must connect and make sense after every change: no steps at junctions or deck ends, links meet their mainline,
  no made-up hills, nothing dropped. Check the network around a fix, not just the fixed spot (`docs/NYC_NOTES.md` NEXT).
- Update `docs/NYC_NOTES.md` (Done table, Open problems) with every fix.
