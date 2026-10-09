VIRTUAL BETTING ARENA — SHARED WALLET + DOG RACING

This package adds Dog Racing to the existing browser game lobby.

Included:
- One player account and shared wallet across the existing games and Dog Racing.
- Six greyhound runners, named odds, 15-second betting window, live race phase,
  result phase, new race IDs, rotating track names, and refreshed odds each round.
- Multiple runner selections per race; the same runner can only be selected once
  per player per race.
- Stakes deducted from the shared wallet and winning payouts credited to it.
- Existing games and their routes are retained.

Deploy on Render:
1. Replace the app files with the contents of this ZIP.
2. Ensure requirements.txt and render.yaml are included.
3. Deploy with the persistent disk mounted at /var/data.

Important:
- This is a browser adaptation of the supplied Pydroid 3 game, not a pixel-perfect
  copy of the Android UI. Existing game code has been retained and Dog Racing
  has been integrated with the website's account wallet.
- The provided Dog Racing code's original house-balance/reserve accounting is
  adapted to the site's shared wallet and round settlement. This is a demo, not
  a production-ready real-money betting platform.
- Mobile-money deposits are still manual requests unless a payment provider is
  separately integrated.

Round-cycle fix (October 2026):
- Added a state-polling watchdog so overdue betting/action/result phases advance
  even if the background daemon loop is delayed or not running.
- Fixed Dog Racing to enter the RACING phase (instead of the chicken BATTLE phase),
  allowing its track animation and race progress to update.
- The round loops still run normally; the watchdog is a recovery path.
