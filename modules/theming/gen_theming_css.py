"""! @file
@brief Generates static/theming.css, the palette contract. Run:
    python3 modules/theming/gen_theming_css.py

Markup uses six colour families besides gray, one per role:
    accent  blue    --cim-accent-50..950    set by palettes
    accent2 indigo  --cim-accent2-50..950   set by palettes
    accent3 sky     --cim-accent3-50..950   set by palettes
    danger  red     --cim-danger-50..950    optional
    ok      green   --cim-ok-50..950        optional
    warn    amber   --cim-warn-50..950      optional
While a palette is active every bg / text / border / ring / accent class of
those families (hover, focus, group-hover, common opacity steps) maps to its
variable, stock colour as fallback. The cim-btn classes (cimButton() in
static/globals.js) use the same variables.

The gray ramp and white / black are the colour scheme's (scheme.css): while
body[data-scheme] is set every gray-50..950 / white / black utility (bg, text,
border, ring, divide, placeholder; hover, focus, group-hover; opacity steps)
maps to --cim-gray-50..950 / --cim-white / --cim-black, stock as fallback, so
the dark scheme (no variables) looks exactly like plain Tailwind.
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
BG_ALPHAS = [10, 20, 30, 40, 50, 60, 80]  # translucent fills: shades 400..950
LINE_ALPHAS = [40, 60, 80]  # translucent text / borders: shades 200..900
P = "body[data-palette]"
GRAY = ["#f9fafb", "#f3f4f6", "#e5e7eb", "#d1d5db", "#9ca3af", "#6b7280", "#4b5563", "#374151", "#1f2937", "#111827", "#030712"]
MONO = {"white": "#fff", "black": "#000"}
NEUTRAL_ALPHAS = [5, 10, 20, 25, 30, 40, 50, 60, 70, 75, 80, 90, 95]  # Tailwind's opacity steps
SCH = "body[data-scheme]"


def var(fam, shade):
    return f"var(--cim-{ROLES[fam]}-{shade}, {STOCK[fam][SHADES.index(shade)]})"


def esc(cls):
    return cls.replace(":", "\\:").replace("/", "\\/")


def sel(cls, states, pre=P):
    """! @brief Selector list for a utility class and its state variants under `pre`."""
    out = [f"{pre} .{esc(cls)}"]
    for st in states:
        if st == "group-hover":
            out.append(f"{pre} .group:hover .{esc('group-hover:' + cls)}")
        else:
            out.append(f"{pre} .{esc(st + ':' + cls)}:{st}")
    return ", ".join(out)


def neutrals():
    """! @brief (class suffix, css value) for gray-50..950, white and black."""
    out = [(f"gray-{sh}", f"var(--cim-gray-{sh}, {GRAY[i]})") for i, sh in enumerate(SHADES)]
    return out + [(k, f"var(--cim-{k}, {v})") for k, v in MONO.items()]


def build_neutrals():
    """! @brief Rules mapping gray / white / black utilities onto the scheme variables."""
    L = ["/* -- gray / white / black utilities -> scheme variables (while body[data-scheme] is set) -- */"]
    st = ["hover", "focus", "group-hover"]
    for name, v in neutrals():
        # body itself carries bg-gray-900 text-white: match it too (SCH.cls)
        L.append(f"{SCH}.bg-{name}, {sel(f'bg-{name}', st, SCH)} {{ background-color: {v}; }}")
        L.append(f"{SCH}.text-{name}, {sel(f'text-{name}', st, SCH)} {{ color: {v}; }}")
        L.append(f"{sel(f'border-{name}', st, SCH)} {{ border-color: {v}; }}")
        L.append(f"{sel(f'ring-{name}', ['focus'], SCH)} {{ --tw-ring-color: {v}; }}")
        L.append(f"{SCH} .{esc(f'divide-{name}')} > :not([hidden]) ~ :not([hidden]) {{ border-color: {v}; }}")
        L.append(f"{SCH} .{esc(f'placeholder-{name}')}::placeholder {{ color: {v}; }}")
    for name, v in neutrals():
        for a in NEUTRAL_ALPHAS:
            mix = f"color-mix(in srgb, {v} {a}%, transparent)"
            L.append(f"{sel(f'bg-{name}/{a}', ['hover'], SCH)} {{ background-color: {mix}; }}")
            L.append(f"{sel(f'text-{name}/{a}', ['hover'], SCH)} {{ color: {mix}; }} "
                     f"{SCH} .{esc(f'border-{name}/{a}')} {{ border-color: {mix}; }}")
    return L


def build():
    doc = __doc__.split("@brief", 1)[-1].strip()
    L = [doc.replace("*/", "* /").join(("/* ", " */")), ""]
    # neutrals first: where markup stacks gray and a role colour the role wins, as in Tailwind
    L += build_neutrals() + [""]
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
