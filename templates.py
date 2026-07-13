"""templates.py — loads the UI from real .html/.css/.js files in web/.

The HTML shells (templates/app.html, web/training.html) link their stylesheet and
script via <link href="/templates/app.css"> and <script src="/templates/app.js">. Those
static assets are served by the /web/<file> route registered in manager.py.

`HTML` and `TRAINING_HTML` are read once at import so index()/training_portal()
can keep doing render_template_string(HTML) unchanged. Editing the .html/.css/.js
files and restarting picks up the changes (or call reload() in a dev reloader).
"""
import os

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

def _read(name):
    with open(os.path.join(WEB_DIR, name), "r", encoding="utf-8") as f:
        return f.read()

# Loaded at import; the page shells reference /web/*.css and /web/*.js.
HTML = _read("app.html")
TRAINING_HTML = _read("training.html")