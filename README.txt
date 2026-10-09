VIRTUAL BETTING ARENA — SHARED PLAYER WALLET UPDATE

Included games
- Virtual Football
- Aviator
- Rugby
- Velocity Car Racing
- Chicken Clash
- Hot 7 Fruit
- Fortune Slots

Player account and wallet
- Players register once with a username and password, then log in on any device.
- All seven games use the same persistent wallet for that account.
- A stake is deducted from that wallet. A confirmed payout is credited back to the same wallet.
- The default demo starting credit remains UGX 10,000.
- Existing device balances can be retained when the player creates the account in the same browser session that held that old wallet.

Game flow
- Aviator continues automatic betting/flight/crash/next-round cycles.
- Velocity Car Racing now has shared automatic rounds with a 10-second betting window and a common finish order for all players.
- Chicken Clash now has shared automatic rounds with a 15-second betting window and a common winner for all players.
- Multiple different car/fighter selections may be placed during a round, once per selection.
- Football and Rugby continue their shared automatic rounds.
- Hot 7 Fruit and Fortune Slots spin when the player presses the spin button.
- The portal uses a mobile-friendly sportsbook-style game selector inspired by common sportsbook layouts; it is not a copy of BetPawa's proprietary site.

Deposit handling
- This package has a deposit-request form for amount, payment method, and transaction reference.
- Requests are credited only after an admin verifies the transaction and approves it in the admin panel under Deposit Requests.
- This package does NOT connect to an automatic MTN Mobile Money, Airtel Money, card, or bank payment gateway. A provider account/API integration is required for automatic deposits and real withdrawals.
- The starting UGX 10,000 is demo credit, not a cash deposit.
- Before accepting real-money wagers, ensure the service has the required legal approvals, payment-provider permissions, responsible-gambling controls, and security review for the jurisdictions served.

Deploy to Render
1. Extract this ZIP.
2. Replace app.py, requirements.txt, and render.yaml in the root of the GitHub repository connected to Render.
3. Commit and push the changes.
4. Confirm Render keeps the persistent disk mounted at /var/data; do not remove it, because it stores account wallets and game state.
5. Wait for deployment to finish.
6. Open the site, choose Create account, then log in with that same account on any device to use the same wallet.
7. To access owner tools, enter the configured admin PIN from the lobby. The admin panel remains locked to one registered wallet/device identity.

Testing notes
- Python syntax compilation passed.
- Jinja templates compiled.
- SQLite CREATE TABLE statements passed against an in-memory database.
- JavaScript syntax checks passed for all inline scripts.
- Round outcome determinism and 200 fruit/slot sample spins passed basic assertions.
- A live Flask test-client/browser test could not be run in this build environment because Flask is not installed here and package download access is unavailable. Render will install requirements.txt during deployment; test account creation, deposits, and each game after redeployment.
