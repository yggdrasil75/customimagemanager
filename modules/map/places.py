"""! @file
@brief Offline place names for GPS positions, and the location: search token.

resolve() turns (lat, lon) pairs into {city, admin2, admin1, cc, country,
continent} with reverse_geocode (GeoNames cities with population > 1000 bundled
as reverse_geocode/geocode.gz, a scipy k-d tree held in memory, no network) and
the package's countries.csv for country names. It is optional: without it, or
without its data file (the package would download it), available() is False
and the module skips place work. forward() reuses the same loaded city list.

location_clause() builds the SQL for `location:<text>`: the text (quotes and
underscores allowed for spaces; "city, region, country" or "city / region /
country" to narrow down; * and ? wildcards) is matched
case-insensitively against every place column, after expanding user aliases,
US postal codes and country codes / names.
"""

import csv
import functools
import gzip
import json
import math
import os
import threading

from optional_deps import optional_import

from . import continents

rgc, _HAVE_RGC = optional_import("reverse_geocode")

## @brief The package folder holding its bundled data (geocode.gz, countries.csv).
_DATA_DIR = os.path.dirname(os.path.abspath(rgc.__file__)) if _HAVE_RGC else ""
_GEOCODE_GZ = os.path.join(_DATA_DIR, "geocode.gz") if _DATA_DIR else ""
_COUNTRIES_CSV = os.path.join(_DATA_DIR, "countries.csv") if _DATA_DIR else ""
## @brief countries.csv rows that are regions, not countries.
_PSEUDO_CODES = {"AP", "EU"}

# guards the package's lazily built singleton (and its query, which edits shared rows)
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

## @brief Two candidate cities farther apart than this (km) make a name ambiguous.
AMBIGUOUS_KM = 100.0

## @brief Columns of the places table a location: term is compared with.
_TEXT_COLS = ("city", "admin2", "admin1", "country", "continent")


def available():
    """! @brief True when the offline geocoder and its bundled data file are installed."""
    return bool(_HAVE_RGC and os.path.isfile(_GEOCODE_GZ))


@functools.lru_cache(maxsize=1)
def country_table():
    """! @brief {alpha-2 code: name} from reverse_geocode's countries.csv ({} without it).
    The region pseudo-codes (AP, EU) are left out. Shared: do not modify.
    """
    out = {}
    if not (_COUNTRIES_CSV and os.path.isfile(_COUNTRIES_CSV)):
        return out
    with open(_COUNTRIES_CSV, encoding="utf-8", newline="") as fh:
        for row in csv.reader(fh):
            if len(row) < 2:
                continue
            cc = row[0].strip().upper()
            if len(cc) == 2 and cc not in _PSEUDO_CODES and row[1].strip():
                out[cc] = row[1].strip()
    return out


@functools.lru_cache(maxsize=1)
def _code_by_name():
    """! @brief Lower-case country name -> code: the table's names and the everyday ones."""
    out = {name.lower(): cc for cc, name in country_table().items()}
    out.update({name.lower(): cc for cc, name in continents.EXTRA_COUNTRY_NAMES.items()})
    return out


def country_name(cc):
    """! @brief A country's everyday name ("South Korea", not "Korea, Republic of")."""
    cc = str(cc or "").upper()
    if not cc:
        return ""
    if cc in continents.EXTRA_COUNTRY_NAMES:
        return continents.EXTRA_COUNTRY_NAMES[cc]
    return country_table().get(cc, cc)


def _country_code(term):
    """! @brief The alpha-2 code a country name / code names ("usa", "Germany"), or ''."""
    t = term.strip()
    if not t:
        return ""
    if len(t) == 2 and t.upper() in continents.CONTINENT_OF:
        return t.upper()
    return _code_by_name().get(" ".join(t.lower().split()), "")


def resolve(coords):
    """! @brief Place names for a list of (lat, lon).
    @return one dict per coordinate: city, admin2, admin1, cc, country, continent
            (empty strings when unknown); [] when the geocoder is missing.
    """
    coords = [(float(a), float(b)) for a, b in coords]
    if not coords or not available():
        return []
    with _rg_lock:
        hits = rgc.search(coords)
    return [_place(h) for h in hits]


def _place(loc):
    """! @brief A reverse_geocode row as a place dict (state -> admin1, county -> admin2)."""
    cc = str(loc.get("country_code") or "").upper()
    return {"city": loc.get("city") or "", "admin2": loc.get("county") or "",
            "admin1": loc.get("state") or "", "cc": cc,
            "country": country_name(cc), "continent": continents.continent(cc)}


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


def _locations():
    """! @brief The GeoNames city rows reverse_geocode holds in memory ({country_code, city,
    latitude, longitude, population, state?, county?}); loads them on first use.
    The rows are shared with the geocoder: read only.
    """
    with _rg_lock:
        # GeocodeData(0) is the singleton instance search() builds and queries
        data = rgc.GeocodeData(0)
    locs = getattr(data, "_locations", None)
    if locs is None:  # a release that no longer keeps them: read the same bundled file
        with gzip.open(_GEOCODE_GZ) as gz:
            locs = json.loads(gz.read())
    return locs


def forward_available():
    """! @brief True when the offline city table is there for forward lookups."""
    return available()


def _km(a, b):
    """! @brief Rough great-circle distance in km between two (lat, lon)."""
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


def _state_names(state):
    """! @brief Lower-case spellings a typed state / region may match ("NC" -> north carolina)."""
    s = str(state or "").strip()
    out = {s.lower()} if s else set()
    if len(s) == 2 and s.upper() in continents.US_STATES:
        out.add(continents.US_STATES[s.upper()].lower())
    return out


def forward(wants):
    """! @brief Approximate positions for typed place names, offline (one pass over the table).
    @param wants  [{city, state, country, cc}] as read from files (geo.read_places).
    @return one entry per want: {lat, lon, city, admin2, admin1, cc, country, continent}
            for an unambiguous city match (the country / state narrow it down), else None.
    """
    out = [None] * len(wants)
    if not wants or not forward_available():
        return out
    keys = []
    for w in wants:
        cc = str(w.get("cc") or "").strip().upper() or _country_code(str(w.get("country") or ""))
        keys.append((str(w.get("city") or "").strip().lower(), cc, _state_names(w.get("state"))))
    names = {k[0] for k in keys if k[0]}
    if not names:
        return out
    found = {}
    for loc in _locations():
        n = (loc.get("city") or "").lower()
        if n in names:
            found.setdefault(n, []).append(loc)
    for i, (name, cc, states) in enumerate(keys):
        cands = found.get(name) or []
        if cc:
            cands = [r for r in cands if (r.get("country_code") or "").upper() == cc]
        if states:
            narrowed = [r for r in cands if (r.get("state") or "").lower() in states]
            cands = narrowed or cands
        if not cands:
            continue
        pts = [(float(r["latitude"]), float(r["longitude"])) for r in cands]
        if any(_km(pts[0], p) > AMBIGUOUS_KM for p in pts[1:]):
            continue  # Springfield: several far-apart cities, no way to tell
        out[i] = dict(_place(cands[0]), lat=pts[0][0], lon=pts[0][1])
    return out


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
