/* Tailwind config shared by the in-browser JIT (loaded right after /tailwind)
 * and the standalone CLI build (manager.py passes -c static/tailwind.config.js).
 *
 * Every colour family the markup uses for a role, plus gray / white / black,
 * resolves to a theming variable with the stock colour as its fallback:
 *
 *   blue -> --cim-accent-*   indigo -> --cim-accent2-*   sky   -> --cim-accent3-*
 *   red  -> --cim-danger-*   green  -> --cim-ok-*        amber -> --cim-warn-*
 *   gray -> --cim-gray-*     white  -> --cim-white       black -> --cim-black
 *
 * So Tailwind itself emits `background-color: <var>` for bg-blue-700,
 * hover:bg-gray-800, text-white/80, ring-sky-500 ... and a palette or colour
 * scheme only has to set variables. With nothing set the fallbacks are the
 * stock Tailwind colours, so the default dark look is unchanged.
 * Opacity modifiers (/50, bg-opacity-*) go through color-mix, which keeps the
 * variables plain hex colours. */
(function () {
  const SHADES = [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950];
  const STOCK = {
    blue:   ["#eff6ff", "#dbeafe", "#bfdbfe", "#93c5fd", "#60a5fa", "#3b82f6", "#2563eb", "#1d4ed8", "#1e40af", "#1e3a8a", "#172554"],
    indigo: ["#eef2ff", "#e0e7ff", "#c7d2fe", "#a5b4fc", "#818cf8", "#6366f1", "#4f46e5", "#4338ca", "#3730a3", "#312e81", "#1e1b4b"],
    sky:    ["#f0f9ff", "#e0f2fe", "#bae6fd", "#7dd3fc", "#38bdf8", "#0ea5e9", "#0284c7", "#0369a1", "#075985", "#0c4a6e", "#082f49"],
    red:    ["#fef2f2", "#fee2e2", "#fecaca", "#fca5a5", "#f87171", "#ef4444", "#dc2626", "#b91c1c", "#991b1b", "#7f1d1d", "#450a0a"],
    green:  ["#f0fdf4", "#dcfce7", "#bbf7d0", "#86efac", "#4ade80", "#22c55e", "#16a34a", "#15803d", "#166534", "#14532d", "#052e16"],
    amber:  ["#fffbeb", "#fef3c7", "#fde68a", "#fcd34d", "#fbbf24", "#f59e0b", "#d97706", "#b45309", "#92400e", "#78350f", "#451a03"],
    gray:   ["#f9fafb", "#f3f4f6", "#e5e7eb", "#d1d5db", "#9ca3af", "#6b7280", "#4b5563", "#374151", "#1f2937", "#111827", "#030712"],
  };
  const VAR = { blue: "accent", indigo: "accent2", sky: "accent3", red: "danger",
                green: "ok", amber: "warn", gray: "gray" };

  /** @brief A colour value Tailwind can apply an opacity to. */
  function color(variable, stock) {
    return `color-mix(in srgb, var(${variable}, ${stock}) calc(<alpha-value> * 100%), transparent)`;
  }

  const colors = {
    white: color("--cim-white", "#fff"),
    black: color("--cim-black", "#000"),
  };
  for (const fam in STOCK) {
    colors[fam] = {};
    SHADES.forEach((sh, i) => { colors[fam][sh] = color(`--cim-${VAR[fam]}-${sh}`, STOCK[fam][i]); });
    colors[fam].DEFAULT = colors[fam][500];
  }

  const config = { content: [], theme: { extend: { colors } } };
  if (typeof module !== "undefined" && module.exports) module.exports = config;
  else if (typeof window !== "undefined" && window.tailwind) window.tailwind.config = config;
})();
