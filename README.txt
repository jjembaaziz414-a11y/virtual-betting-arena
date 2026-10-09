JJ VIRTUAL BETTING ARENA + AVIATOR INTEGRATION

WHAT WAS ADDED
- Added Aviator as a website game at /aviator and a Play Aviator link on the portal.
- Aviator uses the same website device account and balance as Virtual Football Arena.
- Two Aviator bet panels are available per player.
- Aviator history, player Aviator state, house balance, and net profit/loss are saved in the same SQLite database used by the website.
- Added /aviator/admin, restricted to the same registered admin device as Football Admin.
- The existing admin PIN is 4422. The first device that successfully claims the admin panel remains the registered admin device; other devices are blocked.
- Added an Aviator Admin link to the existing admin page.

DEPLOY
1. Back up your current persistent database before replacing app.py.
2. Copy app.py, requirements.txt, and render.yaml into the root of the GitHub repository connected to Render.
3. Commit and push the changes, then wait for Render to deploy.
4. Open your website's home page and choose Play Aviator.
5. To manage Aviator, open the normal Admin Panel on the already-registered admin device using PIN 4422, then choose OPEN AVIATOR ADMIN.

IMPORTANT NOTES
- This patch uses the existing persistent SQLite database path configured by DATA_DIR. A persistent disk must be mounted at /var/data for the Render deployment to preserve data across restarts. Do not remove the disk or delete game_state.db.
- Aviator is a separate game, but it shares each device's balance with football. Bets are charged at take-off; a crash loses the stake, and cash-out credits the payout.
- Syntax compilation passed. A full Flask runtime test could not be run in this environment because Flask is not installed here; Render installs dependencies from requirements.txt during deployment. Check the Render logs and test with a low demo stake after deploying.
- The current website has no player registration/login flow; this integration uses the website's existing device identity model.
