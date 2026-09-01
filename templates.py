
import os

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

def _read(name):
    with open(os.path.join(WEB_DIR, name), "r", encoding="utf-8") as f:
        return f.read()

# Loaded at import; the page shell references /static/*.css and /static/*.js.
HTML = _read("app.html")