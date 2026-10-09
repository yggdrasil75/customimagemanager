"""! @file
@brief Offline lookup tables for place search: ISO 3166-1 alpha-2 country code to
continent, and the US state / territory postal codes.

Transcontinental countries sit where they are usually filed (RU, TR, KZ, GE, AZ,
AM, CY in their customary continent; EG in Africa). Island territories go with
their region (UM, AS in Oceania; IO, CX, CC in Asia; TF, HM, BV, GS in
Antarctica). XK (Kosovo) is a user-assigned code GeoNames uses.
"""

## @brief Continent names, keyed by the two-letter continent code GeoNames uses.
CONTINENT_NAMES = {
    "AF": "Africa", "AN": "Antarctica", "AS": "Asia", "EU": "Europe",
    "NA": "North America", "OC": "Oceania", "SA": "South America",
}

_BY_CONTINENT = {
    "AF": "DZ AO BJ BW BF BI CV CM CF TD KM CG CD CI DJ EG GQ ER SZ ET GA GM GH GN GW "
          "KE LS LR LY MG MW ML MR MU YT MA MZ NA NE NG RE RW SH ST SN SC SL SO ZA SS "
          "SD TZ TG TN UG EH ZM ZW",
    "AN": "AQ BV GS HM TF",
    "AS": "AF AM AZ BH BD BT BN KH CN CY GE HK IN ID IR IQ IL JP JO KZ KW KG LA LB MO "
          "MY MV MN MM NP KP OM PK PS PH QA SA SG KR LK SY TW TJ TH TL TR TM AE UZ VN "
          "YE IO CX CC",
    "EU": "AX AL AD AT BY BE BA BG HR CZ DK EE FO FI FR DE GI GR GG HU IS IE IM IT JE "
          "XK LV LI LT LU MT MD MC ME NL MK NO PL PT RO RU SM RS SK SI ES SJ SE CH UA "
          "GB VA",
    "NA": "AI AG AW BS BB BZ BM BQ VG CA KY CR CU CW DM DO SV GL GD GP GT HT HN JM MQ "
          "MX MS NI PA PR BL KN LC MF PM VC SX TT TC US VI",
    "OC": "AS AU CK FJ PF GU KI MH FM NR NC NZ NU NF MP PW PG PN WS SB TK TO TV VU WF UM",
    "SA": "AR BO BR CL CO EC FK GF GY PY PE SR UY VE",
}

## @brief Country code -> continent name.
CONTINENT_OF = {cc: CONTINENT_NAMES[k] for k, codes in _BY_CONTINENT.items()
                for cc in codes.split()}

## @brief Everyday country names that win over the country table's formal ones
# ("Korea, Republic of"), plus codes a table may not carry. Both spellings
# match a location: search.
EXTRA_COUNTRY_NAMES = {
    "XK": "Kosovo", "KR": "South Korea", "KP": "North Korea", "RU": "Russia",
    "IR": "Iran", "SY": "Syria", "LA": "Laos", "MD": "Moldova", "TZ": "Tanzania",
    "BN": "Brunei", "FM": "Micronesia", "VA": "Vatican City", "CZ": "Czechia",
    "MK": "North Macedonia", "SZ": "Eswatini", "PS": "Palestine",
    "CD": "DR Congo", "CG": "Republic of the Congo", "CI": "Ivory Coast",
    "VE": "Venezuela", "BO": "Bolivia",
}

## @brief US postal codes -> state / district / territory name (GeoNames admin1).
US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "Washington, D.C.", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
}


def continent(cc):
    """! @brief Continent name of a country code, or ''."""
    return CONTINENT_OF.get(str(cc or "").upper(), "")
