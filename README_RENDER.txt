JJ Virtual Betting Arena - Render deployment

Upload this folder's contents to your GitHub repository root (do not put them inside another folder).
Required root files:
  app.py
  requirements.txt
  render.yaml

Render will use:
  Build: pip install -r requirements.txt
  Start: gunicorn app:app

The app uses render_template_string, so an index.html file is NOT required.
