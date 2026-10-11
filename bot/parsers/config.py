"""All tunable criteria and constants live here. Edit freely."""

# ---------------------------------------------------------------- criteria
CITY_NAME = "Wrocław"
ROOMS_MIN = 1
ROOMS_MAX = 3
PRICE_MIN_PLN = None          # None = no limit
PRICE_MAX_PLN = None
AREA_MIN_M2 = None
AREA_MAX_M2 = None

# Canonical building types (see filters.normalize_building_type).
BUILDING_ALLOWED = {"blok", "plomba", "szeregowiec", "apartamentowiec"}
BUILDING_EXCLUDED = {"kamienica", "dom_wolnostojacy", "loft"}
# Anything else ("pozostale"/"other") or missing is allowed:
ALLOW_UNKNOWN_BUILDING = True
ALLOW_UNKNOWN_ROOMS = True

# Only listings first created within this window count as "new".
# Bumped/refreshed old listings (created earlier) are ignored.
NEW_MAX_AGE_HOURS = 48

TIMEZONE = "Europe/Warsaw"      # created_at / refreshed_at are emitted in this zone

# ---------------------------------------------------------------- polling
POLL_INTERVAL_S = 300          # fixed 5 min, used by --loop and the future bot
MAX_PAGES = 5                  # hard cap per fetch_new() call
# OLX orders by last refresh, not by creation. Even when page 1 holds nothing unseen, keep paging while the
# page still reaches back less than this many hours: covers listings that become visible late (moderation)
# and bursts of bumps that push a fresh listing off page 1 between two polls. Usually = 2 pages.
OLX_MIN_LOOKBACK_HOURS = 6
# A listing whose parsing crashes is retried on the next polls, then marked seen after this many failures.
MAX_ITEM_PARSE_FAILURES = 3
REQUEST_TIMEOUT_S = 30
CONNECT_TIMEOUT_S = 10
RETRIES = 3                    # per request
BACKOFF_BASE_S = 2.0           # 2, 4, 8 ... + jitter
BACKOFF_BLOCKED_S = 20.0       # extra wait on 403/429 before the retry
REQUEST_DELAY_S = (1.2, 2.5)   # polite random sleep between any two requests
OTODOM_MAX_DETAILS_PER_RUN = 80  # detail pages fetched per fetch_new() call

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
CURL_CFFI_IMPERSONATE = "chrome"
ACCEPT_LANGUAGE = "pl-PL,pl;q=0.9,en-US;q=0.6,en;q=0.5"

# ---------------------------------------------------------------- OLX
OLX_CATEGORY_ID = 15           # Nieruchomości > Mieszkania > Wynajem (verified; 11 = Stancje i pokoje)
OLX_CITY_ID = 19701            # Wrocław
OLX_PAGE_LIMIT = 40
OLX_API = "https://www.olx.pl/api/v1/offers/"
# filter_enum_rooms is applied server-side; we still re-check locally.
OLX_ROOMS_ENUM = {"one": 1, "two": 2, "three": 3, "four": 4}
# OLX photo template is ".../image;s={width}x{height}". OLX fits the original
# into this box (never upscales), so a big box = original resolution.
OLX_PHOTO_SIZE = "4000x4000"
# OLX imports from these partners (partner.code) are reposts from other portals.
OLX_PARTNER_IMPORTS = {"otodom_pl"}

# ---------------------------------------------------------------- Otodom
OTODOM_SEARCH = ("https://www.otodom.pl/pl/wyniki/wynajem/mieszkanie/dolnoslaskie/"
                 "wroclaw/wroclaw/wroclaw")
# isPrivateOwner=true IS honoured server-side (verified 2026-10-09: 146/146 private,
# shows up in filteringQueryParams). ownerTypeSingleSelect only exists for SELL.
OTODOM_SEARCH_PARAMS = {
    "roomsNumber": "[ONE,TWO,THREE]",
    "isPrivateOwner": "true",
    "by": "LATEST",
    "direction": "DESC",
    "limit": "72",
}
OTODOM_AD_URL = "https://www.otodom.pl/pl/oferta/{slug}"

# ---------------------------------------------------------------- agent heuristics
# Regexes (case-insensitive) that suggest an agency. A match is ignored when it is
# negated nearby (see filters.NEGATION_BEFORE / NEGATION_AFTER).
AGENT_PATTERNS = [
    r"biur\w* nieruchomo\w*",
    r"biur\w* obrotu",
    r"oferta biura",
    r"agencj\w* nieruchomo\w*",
    r"\bagencj\w*",
    r"po[sś]rednik\w*",
    r"po[sś]rednictw\w*",
    r"prowizj\w*",
    r"licencj\w* (?:zawodow\w*|nr|pfrn)",
    r"\bnr licencji",
    r"\bpfrn\b",
    r"nieruchomo\w* sp\.? ?z ?o\.? ?o",
    r"real estate",
    r"umow\w* po[sś]rednictwa",
    r"wynagrodzeni\w* (?:biura|agencji|po[sś]rednika)",
    r"op[lł]at\w* (?:za )?po[sś]rednictw\w*",
    # well-known agency brands that sign listings posted from "private" accounts (review 2026-10-09:
    # an OLX listing signed "Elżbieta Śliwka / Metrohouse" passed every other check)
    r"\bmetrohouse\b", r"\bre/?max\b", r"\bhome ?broker\b", r"\bemmerson\b", r"\bnieruchomo[sś]ci\s+sp\b",
    r"\bw obrocie nieruchomo\w*",
]
# Weak agency hints: recorded as signals ("weak_agent:...") but never reject alone.
# (Private owners on Otodom copy the "art. 66 Kodeksu cywilnego" disclaimer too.)
AGENT_WEAK_PATTERNS = [
    r"nie stanowi oferty (?:handlowej|w rozumieniu)",
    r"art\.? ?66",
    r"\bnumer oferty\b|\bnr oferty\b|\bid oferty\b",
    r"wystawiamy faktur\w*|nie pobieramy prowizji",
]
# Strong agency phrases that are NOT neutralised by negation ("bez prowizji od najemcy"
# is what agencies write when the owner pays them).
AGENT_STRONG_PATTERNS = [
    r"(?:bez|brak|nie pobier\w*|0\s*%|zero)\s+(?:\w+\s+)?prowizj\w*\s+(?:od|dla)\s+najemc\w*",
    r"prowizj\w*[^.\n]{0,30}(?:p[lł]aci|pokrywa)\w*\s+w[lł]a[sś]ciciel\w*",
    r"\bbiuro\b(?=[^.\n]{0,40}prowizj)",
    r"\bnasz\w* (?:biur\w*|agencj\w*|ofert\w*)\b",
    r"\bagent\w* (?:prowadz\w*|odpowiedzialn\w*|nieruchomo\w*)",
]
# Phrases that indicate a private owner (only reported as signals; they also neutralise
# the matching agent keyword through the negation logic).
PRIVATE_PATTERNS = [
    r"bez prowizji", r"bez po[sś]rednik\w*", r"bez agencji", r"bez biur\w*",
    r"oferta prywatna", r"\bprywatn\w*", r"bezpo[sś]redni\w*", r"od w[lł]a[sś]ciciela",
    r"w[lł]a[sś]ciciel\w*", r"nie jestem po[sś]rednikiem",
    r"(?:agencj\w*|biur\w*|po[sś]rednik\w*) (?:prosz\w* o nie|dzi[eę]kuj\w*)",
]

# Seller display names that look like a company (checked for non-import listings).
SELLER_NAME_AGENT_PATTERNS = [
    r"(?<![\w.])[a-ząćęłńóśźż\-]{4,}\.(?:pl|com|eu|net)\b",   # "luxholiday.pl" (not "rom1978.pl")
    r"nieruchomo", r"sp\.? ?z ?o", r"\bs\.?a\.?$", r"\bestate\b",
    r"\binvest", r"\bdevelop", r"\brent(?:al)?s?\b", r"\bapartament(?:y|s)\b", r"\bhome[s]?\b",
    r"\bbiuro\b", r"\bagencj", r"\bgroup\b", r"\bholding\b", r"\bpartner(?:s|zy)?\b",
]
