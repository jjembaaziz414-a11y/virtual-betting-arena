JJ Virtual Betting Arena - persistent season fix

Upload app.py, requirements.txt, and render.yaml to the ROOT of the GitHub repository connected to Render.
Commit and push the changes, then wait for Render to deploy.

IMPORTANT: render.yaml configures a 1 GB Render persistent disk mounted at /var/data. Render persistent disks require a paid web service. If you use a free Render service, this disk configuration will not provide persistence there; use a supported persistent database/storage plan instead.

The app stores game_state.db and session_secret.key in /var/data when the persistent disk is mounted. This preserves the match timeline, device balances, bets, profits, return pool, house vault, payout records, winning history, browser device identity, and locked match results across ordinary restarts/deploys.

Do not delete the persistent disk or game_state.db if you want to keep the existing season. Back up the database before major changes.
