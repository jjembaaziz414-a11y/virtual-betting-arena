JJ VIRTUAL BETTING ARENA — AVIATOR ROUND FIX + RUGBY GAME

Files in this folder:
- app.py: updated Flask website, including Virtual Football, Aviator, and Rugby
- requirements.txt: Python dependencies
- render.yaml: Render service configuration (single Gunicorn worker + persistent disk)

Deploy:
1. Back up the current GitHub files and confirm your Render persistent disk is still mounted at /var/data.
2. Copy these three files to the ROOT of the GitHub repository, replacing the matching app.py, requirements.txt, and render.yaml.
3. Commit/push. Wait for Render to finish deploying.
4. Open the site lobby and select Play Aviator or Play Rugby.

Aviator changes:
- Adds the missing json import used by saved Aviator state.
- Makes the round loop recover from a malformed saved bet/state rather than permanently dying.
- Normalizes saved player bet fields, shows flight multiplier updates, and gives players a 5-second betting phase.

Rugby:
- New Rugby selection from the lobby.
- Simulated rugby match, score, 1X2 winner odds, 15-second betting window, and shared player balance.

Important:
- Keep the existing Render persistent disk and /var/data mount. Do not delete game_state.db if you need existing balances.
- The local environment used to prepare this patch did not have Flask installed, so syntax compilation was checked but a full live Flask/Render runtime test was not possible here. Verify after deployment before relying on balances or results.
- This is a demo game; test with small demo stakes first.
