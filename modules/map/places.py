"""! @file
@brief Offline place names for GPS positions, and the location: search token.

resolve() turns (lat, lon) pairs into {city, admin2, admin1, cc, country,
continent} with reverse_geocoder (GeoNames cities with population > 1000, a
k-d tree held in memory, no network) and pycountry for country names. Both are
optional: without them available() is False and the module skips place work.

location_clause() builds the SQL for `location:<text>`: the text (quotes and
underscores allowed for spaces; "city, region, country" or "city / region /
country" to narrow down; * and ? wildcards) is matched
case-insensitively against every place column, after expanding user aliases,
US postal codes and country codes / names.
"""

import threading

from optional_deps import optional_import

from . import continents

rg, _HAVE_RG = optional_import("reverse_geocoder")
pycountry, _HAVE_PYCOUNTRY = optional_import("pycountry")

_rg_lock = threading.Lock()

## @brief Aliases a fresh install starts with (alias -> expansion).
DEFAULT_ALIASES = [
    {"alias": "NYC", "expansion": "New York City"},
    {"alias": "LA", "expansion": "Los Angeles"},
    {"alias": "SF", "expansion": "San Francisco"},
    {"alias": "DC", "expansion": "Washington, D.C."},
    {"alias": "UK", "expansion": "GB"},
    {"alias": "Britain", "expansion": "GB"},
    {"alias": "Russia", "expansion": "RU"},
    {"alias": "USA", "expansion": "US"},
    {"alias": "America", "expansion": "US"},
    {"alias": "UAE", "expansion": "AE"},
    {"alias": "Holland", "expansion": "NL"},
    {"alias": "Korea", "expansion": "KR"},
    {"alias": "Vietnam", "expansion": "VN"},
    {"alias": "Vegas", "expansion": "Las Vegas"},
    {"alias": "Philly", "expansion": "Philadelphia"},
    {"alias": "Rio", "expansion": "Rio de Janeiro"},
    {"alias": "HK", "expansion": "Hong Kong"},
]

## @brief Columns of the places table a location: term is compared with.
_TEXT_COLS = ("city", "admin2", "admin1", "country", "continent")


def available():
    """! @brief True when the offline geocoder is installed."""
    return _HAVE_RG


def country_name(cc):
    """! @brief A country's everyday name ("South Korea", not "Korea, Republic of")."""
    cc = str(cc or "").upper()
    if not cc:
        return ""
    if cc in continents.EXTRA_COUNTRY_NAMES:
        return continents.EXTRA_COUNTRY_NAMES[cc]
    if _HAVE_PYCOUNTRY:
        c = pycountry.countries.get(alpha_2=cc)
        if c is not None:
            return getattr(c, "common_name", None) or c.name
    return cc


def _country_code(term):
    """! @brief The alpha-2 code a country name / code names ("usa", "Germany"), or ''."""
    t = term.strip()
    if not t:
        return ""
    if len(t) == 2 and t.upper() in continents.CONTINENT_OF:
        return t.upper()
    if not _HAVE_PYCOUNTRY:
        return ""
    try:
        return pycountry.countries.lookup(t).alpha_2
    except LookupError:
        return ""


def resolve(coords):
    """! @brief Place names for a list of (lat, lon).
    @return one dict per coordinate: city, admin2, admin1, cc, country, continent
            (empty strings when unknown); [] when the geocoder is missing.
    """
    coords = [(float(a), float(b)) for a, b in coords]
    if not coords or not _HAVE_RG:
        return []
    with _rg_lock:
        hits = rg.search(coords, mode=1, verbose=False)
    out = []
    for h in hits:
        cc = str(h.get("cc") or "").upper()
        out.append({"city": h.get("name") or "", "admin2": h.get("admin2") or "",
                    "admin1": h.get("admin1") or "", "cc": cc,
                    "country": country_name(cc), "continent": continents.continent(cc)})
    return out


def merge(auto, existing):
    """! @brief A file's place: what the user wrote into the file wins, the geocoder fills the rest.
    @param auto      resolve() output for the file's position.
    @param existing  {city, state, country, cc} read from the file.
    @return (row dict, source) with source "file" when any user value was used, else "gps".
    """
    row = dict(auto)
    used = False
    if existing.get("city"):
        row["city"], used = existing["city"], True
    if existing.get("state"):
        row["admin1"], used = existing["state"], True
    if existing.get("cc"):
        row["cc"], used = existing["cc"].strip().upper(), True
        row["country"] = country_name(row["cc"])
        row["continent"] = continents.continent(row["cc"])
    if existing.get("country"):
        row["country"], used = existing["country"], True
        if not existing.get("cc"):
            cc = _country_code(existing["country"])
            if cc:
                row["cc"], row["continent"] = cc, continents.continent(cc)
    return row, ("file" if used else "gps")


def fill_patch(auto, existing):
    """! @brief XMP patch filling only the place fields the file leaves empty."""
    want = {"photoshop.City": ("city", auto.get("city")),
            "photoshop.State": ("state", auto.get("admin1")),
            "photoshop.Country": ("country", auto.get("country")),
            "iptcCore.CountryCode": ("cc", auto.get("cc"))}
    return {tok: val for tok, (key, val) in want.items() if val and not existing.get(key)}


def clean_aliases(rows):
    """! @brief Validate the alias rows setting: [{alias, expansion}] with both set."""
    out, seen = [], set()
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        a = str(r.get("alias") or "").strip()
        e = str(r.get("expansion") or "").strip()
        if a and e and a.lower() not in seen:
            seen.add(a.lower())
            out.append({"alias": a, "expansion": e})
    return out


def _unquote(value):
    """! @brief A token value without its quotes, underscores read as spaces."""
    v = str(value or "").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v.replace('"', "").replace("_", " ").strip()


def expand(term, aliases):
    """! @brief Every spelling one location term stands for.
    @return (names, codes, us_states): lower-case names to compare with the text
            columns, country codes, and US state names (for postal codes like "nc").
    """
    t = " ".join(term.split())
    if not t:
        return set(), set(), set()
    amap = {a["alias"].lower(): a["expansion"] for a in clean_aliases(aliases)}
    # an alias replaces the term, so "LA" means Los Angeles and not Laos / Louisiana
    v = amap.get(t.lower(), t)
    names, codes, states = {v, v.lower()}, set(), set()
    if v.lower().endswith(" county"):
        names.add(v.lower()[:-7].strip())
    cc = _country_code(v)
    if cc:
        codes.add(cc)
    if len(v) == 2 and v.upper() in continents.US_STATES:
        states.add(continents.US_STATES[v.upper()].lower())
    return names, codes, states


def _wild(term):
    """! @brief * and ? wildcards as LIKE patterns."""
    return term.replace("*", "%").replace("?", "_")


def term_clause(term, aliases):
    """! @brief SQL over places for one term (any column matches), or ('', [])."""
    if "*" in term or "?" in term:
        like = _wild(term.strip())
        ors = " OR ".join(f"{c} LIKE ?" for c in _TEXT_COLS)
        return f"({ors})", [like] * len(_TEXT_COLS)
    names, codes, states = expand(term, aliases)
    if not names:
        return "", []
    ors, params = [], []
    marks = ",".join("?" * len(names))
    for c in _TEXT_COLS:
        ors.append(f"{c} IN ({marks})")
        params += sorted(names)
    # "Wake" finds "Wake County"
    ors.append(f"admin2 IN ({marks})")
    params += sorted(n + " county" for n in names)
    if codes:
        ors.append(f"cc IN ({','.join('?' * len(codes))})")
        params += sorted(codes)
    if states:
        ors.append(f"(cc = 'US' AND admin1 IN ({','.join('?' * len(states))}))")
        params += sorted(states)
    return "(" + " OR ".join(ors) + ")", params


def location_clause(value, aliases):
    """! @brief SQL on files for `location:<value>`; "a, b, c" (or "a / b / c", which
    keeps names with commas whole) needs every part to match.
    @return (sql, params) or ('', []) for an empty value.
    """
    whole = _unquote(value)
    parts = [p.strip() for p in whole.split("/" if "/" in whole else ",") if p.strip()]
    if not parts:
        return "", []
    # "Washington, D.C." is one place: an alias / whole-value match comes first
    amap = {a["alias"].lower() for a in clean_aliases(aliases)}
    if len(parts) > 1 and whole.lower() in amap:
        parts = [whole]
    clauses, params = [], []
    for p in parts:
        c, pp = term_clause(p, aliases)
        if c:
            clauses.append(c)
            params += pp
    if not clauses:
        return "", []
    whole_c, whole_p = term_clause(whole, aliases) if len(parts) > 1 else ("", [])
    where = " AND ".join(clauses)
    if whole_c:
        where = f"({where}) OR {whole_c}"
        params += whole_p
    return f"rel_path IN (SELECT rel_path FROM places WHERE {where})", params
