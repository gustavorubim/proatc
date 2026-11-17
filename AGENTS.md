# For agents working on Pro ATC

Read [docs/testing.md](docs/testing.md) before changing anything. It says how every kind of change is verified, and what "verified" does not cover.

The essentials:

- **Before a pull request:** `uvx ruff@0.17.5 format --check . && uvx ruff@0.17.5 check .`, `uv run pytest -q`, `npm run check`. Packaging changes also need `npm run pack:dir && npm run smoke`.
- **A change to what a controller says** will fail a golden flight (`tests/golden/`). Read the diff, regenerate with `PROATC_UPDATE_GOLDEN=1`, read the new transcript, commit it.
- **A bug from a real flight** comes with a saved log: `uv run proatc replay <log>.json` shows the answers the engine gives now.
- **Every run of the app writes a journal** to `logs/journal/` in its data folder (Windows: `%APPDATA%\Pro ATC\engine`): events, plus a trace of position and station every twenty seconds. Read it before guessing at a reported fault; `proatc replay` takes it too.
- **Whole flights are the test that matters.** `tests/test_world.py` flies flights across a made-up world on every change. With X-Plane installed, `uv run proatc verify-flights --flights 300` flies real ones; run 1,000 before a release. Faults found in flight have all been in how the station is found from the tuned frequency, which unit tests that pass the station in cannot see.
- **With X-Plane installed,** `uv run proatc verify-data` runs the engine over hundreds of real airports.
- **Nothing in the test suite flies the sim.** Say in the pull request what was and was not tried in a real flight.
- **Releases:** publish the draft only after every build job has finished and its files are attached.
- **The repository is public:** no keys, no personal identifiers, no planning notes. Stage files by name.
- **The architecture rule:** the language model turns speech into an intent (`intents.py`). The rules engine (`engine.py`, `ifr.py`, `vfr.py`) decides every reply from X-Plane's own data. Do not let the model issue a clearance.

Where things are: [docs/development.md](docs/development.md). What the controllers do: [docs/how-it-works.md](docs/how-it-works.md).
