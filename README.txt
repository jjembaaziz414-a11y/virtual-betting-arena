JJ VIRTUAL BETTING ARENA — MULTI-GAME UPDATE

This update keeps the existing Virtual Football, Aviator, and Rugby pages and adds browser-native versions of four uploaded games:
- Velocity Car Racing
- Chicken Clash
- Hot 7 Fruit
- Fortune Slots

All seven games are linked from the main Game Lobby. The four added games use the website's existing player session and shared UGX balance, and they use the existing SQLite device balance synchronization. The original standalone Pygame/HTTP servers are not started on Render because those servers would conflict with the Flask website and are not suitable for the hosted browser app; their game concepts have been adapted into website pages.

DEPLOY
1. Back up your current GitHub files and any database export you have.
2. Extract this ZIP.
3. Replace app.py, requirements.txt, and render.yaml at the ROOT of your connected GitHub repository with these files.
4. Commit the changes and wait for Render to redeploy.
5. Keep the Render persistent disk mounted at /var/data. Do not delete the persistent disk or game_state.db if you need to preserve balances and game history.

IMPORTANT
- This is a simulated/demo game implementation, not a real-money payment integration.
- Test each new game after deployment before relying on game outcomes or balances.
- The existing admin PIN remains 4422 and the existing one-device admin lock is preserved.
- Syntax validation was performed, but a full Flask runtime/browser test was not possible in the current environment because Flask is not installed here.
