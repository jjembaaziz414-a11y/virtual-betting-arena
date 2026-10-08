JJ Virtual Betting Arena - Game Lobby Render deployment

IMPORTANT: Put these files directly in the GitHub repository ROOT:
  app.py
  requirements.txt
  render.yaml
  .python-version

Render build command:
  pip install -r requirements.txt

Render start command:
  gunicorn app:app

The database is initialized when Gunicorn imports app.py.
