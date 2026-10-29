#!/usr/bin/env bash
# BeamNG.Earth: start the world server with the recommended options.
# Run it, then start BeamNG.drive. Keep this terminal open while you play.
cd "$(dirname "$0")" || exit 1
# local: the carriageways as crisp asphalt meshes under the lane lines, not painted into the 2 m ground (wavy edges)
export BNGE_ROADS_IN_GROUND="${BNGE_ROADS_IN_GROUND:-0}"
if [ ! -x .venv/bin/python ]; then
    echo "The server is not installed yet. Running the installer first..."
    python3 install.py || exit 1
fi
exec .venv/bin/python start.py "$@"
