"""! @file
@brief Generate modules/theming/static/theming.css - the palette contract.

    python3 modules/theming/gen_theming_css.py

The app's markup uses exactly six colour families besides gray:

    role      Tailwind family   variables                    who sets them
    accent    blue              --cim-accent-50  .. 950      palette modules
    accent2   indigo            --cim-accent2-50 .. 950      palette modules
    accent3   sky               --cim-accent3-50 .. 950      palette modules
    danger    red               --cim-danger-50  .. 950      optional (stock red)
    ok        green             --cim-ok-50      .. 950      optional (stock green)
    warn      amber             --cim-warn-50    .. 950      optional (stock amber)

Every utility class of those families (bg / text / border / ring / accent,
their hover / focus /
group-hover variants, and the usual opacity steps: bg /10 .. /80, text and
border /40 /60 /80)
is mapped onto its variable whenever a palette is active, with the stock hex
as the fallback. A palette module only sets variables; no markup, module CSS
or JS ever needs a palette-specific branch. Other Tailwind hues (purple, teal,
emerald, rose, ...) are not used: pick the role, not a colour.

The cim-btn classes are the core button: `cimButton()` in static/globals.js
emits them, so every module button follows the palette the same way.
"""
import os

STOCK = {
    "blue":   ["#eff6ff", "#dbeafe", "#bfdbfe", "#93c5fd", "#60a5fa", "#3b82f6", "#2563eb", "#1d4ed8", "#1e40af", "#1e3a8a", "#172554"],
    "indigo": ["#eef2ff", "#e0e7ff", "#c7d2fe", "#a5b4fc", "#818cf8", "#6366f1", "#4f46e5", "#4338ca", "#3730a3", "#312e81", "#1e1b4b"],
    "sky":    ["#f0f9ff", "#e0f2fe", "#bae6fd", "#7dd3fc", "#38bdf8", "#0ea5e9", "#0284c7", "#0369a1", "#075985", "#0c4a6e", "#082f49"],
    "red":    ["#fef2f2", "#fee2e2", "#fecaca", "#fca5a5", "#f87171", "#ef4444", "#dc2626", "#b91c1c", "#991b1b", "#7f1d1d", "#450a0a"],
    "green":  ["#f0fdf4", "#dcfce7", "#bbf7d0", "#86efac", "#4ade80", "#22c55e", "#16a34a", "#15803d", "#166534", "#14532d", "#052e16"],
    "amber":  ["#fffbeb", "#fef3c7", "#fde68a", "#fcd34d", "#fbbf24", "#f59e0b", "#d97706", "#b45309", "#92400e", "#78350f", "#451a03"],
}
SHADES = [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950]
ROLES = {"blue": "accent", "indigo": "accent2", "sky": "accent3",
         "red": "danger", "green": "ok", "amber": "warn"}
BG_ALPHAS = [10, 20, 30, 40, 50, 60, 80]          # translucent fills: shades 400..950
LINE_ALPHAS = [40, 60, 80]                         # translucent text / borders: 200..900
P = "body[data-palette]"


def var(fam, shade):
    return f"var(--cim-{ROLES[fam]}-{shade}, {STOCK[fam][SHADES.index(shade)]})"


def esc(cls):
    return cls.replace(":", "\\:").replace("/", "\\/")


def sel(cls, states):
    out = [f"{P} .{esc(cls)}"]
    for st in states:
        if st == "group-hover":
            out.append(f"{P} .group:hover .{esc('group-hover:' + cls)}")
        else:
            out.append(f"{P} .{esc(st + ':' + cls)}:{st}")
    return ", ".join(out)


def build():
    doc = __doc__.split("@brief", 1)[-1].strip()
    L = [doc.replace("*/", "* /").join(("/* ", " */")), ""]
    L.append("/* -- utility classes -> palette variables (only while a palette is active) -- */")
    for fam in STOCK:
        for sh in SHADES:
            v = var(fam, sh)
            st = ["hover", "focus", "group-hover"]
            L.append(f"{sel(f'bg-{fam}-{sh}', st)} {{ background-color: {v}; }}")
            L.append(f"{sel(f'text-{fam}-{sh}', st)} {{ color: {v}; }}")
            L.append(f"{sel(f'border-{fam}-{sh}', st)} {{ border-color: {v}; }}")
            L.append(f"{sel(f'ring-{fam}-{sh}', ['focus'])} {{ --tw-ring-color: {v}; }}")
            L.append(f"{P} .accent-{fam}-{sh} {{ accent-color: {v}; }}")
    L.append("")
    L.append("/* -- opacity steps (bg-x-900/40 ...) -- */")
    for fam in STOCK:
        for sh in SHADES:
            v = var(fam, sh)
            for a in BG_ALPHAS if sh >= 400 else ():
                mix = f"color-mix(in srgb, {v} {a}%, transparent)"
                L.append(f"{sel(f'bg-{fam}-{sh}/{a}', ['hover'])} {{ background-color: {mix}; }}")
            for a in LINE_ALPHAS if 200 <= sh <= 900 else ():
                mix = f"color-mix(in srgb, {v} {a}%, transparent)"
                L.append(f"{P} .{esc(f'text-{fam}-{sh}/{a}')} {{ color: {mix}; }} "
                         f"{P} .{esc(f'border-{fam}-{sh}/{a}')} {{ border-color: {mix}; }}")
    L.append("")
    L.append("""/* -- core buttons: cimButton() / class="cim-btn cim-btn-<variant> cim-btn-<size>" --
   Always on (palette or not); the colour comes from the same variables. */
.cim-btn { font-weight: 700; border-radius: .25rem; color: #fff; cursor: pointer; white-space: nowrap;
           transition: background-color .1s; display: inline-flex; align-items: center; justify-content: center; gap: .25rem; }
.cim-btn:disabled { opacity: .5; cursor: default; }
.cim-btn-xs    { font-size: 10px; line-height: 14px; padding: .125rem .5rem; }
.cim-btn-sm    { font-size: .75rem; line-height: 1rem; padding: .375rem .75rem; }
.cim-btn-block { display: flex; width: 100%; font-size: .875rem; line-height: 1.25rem; padding: .375rem .5rem; }""")
    for name, fam in (("primary", "blue"), ("secondary", "indigo"), ("tertiary", "sky"),
                      ("ok", "green"), ("warn", "amber"), ("danger", "red")):
        L.append(f".cim-btn-{name} {{ background-color: {var(fam, 700)}; }} "
                 f".cim-btn-{name}:hover:not(:disabled) {{ background-color: {var(fam, 600)}; }}")
    L.append(".cim-btn-neutral { background-color: #374151; color: #e5e7eb; } "
             ".cim-btn-neutral:hover:not(:disabled) { background-color: #4b5563; }")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "theming.css")
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(build())
    print(f"wrote {out} ({os.path.getsize(out) // 1024} KB)")
