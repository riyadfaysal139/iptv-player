"""Map provider category names onto a two-level (group -> subcategory) tree.

The rules here were derived by measuring a real Xtream catalog: the provider
prefixes almost every VOD category with "VOD |", so splitting on the pipe alone
yields one useless top-level group. What matters is the *remainder*, which is
what these rules classify.

Everything is data-driven so a provider renaming a category is a one-line edit,
and every classifier falls back to UNCATEGORIZED rather than dropping items.
"""

from __future__ import annotations

import re

UNCATEGORIZED = "Uncategorized"

# --------------------------------------------------------------------------
# shared vocabulary
# --------------------------------------------------------------------------

LANGUAGES = [
    "English", "Hindi", "Telugu", "Tamil", "Malayalam", "Kannada",
    "Punjabi", "Bangla", "Marathi", "Gujarati", "Pakistani",
]

MOVIE_GENRES = {
    "Adventure", "Action", "Comedy", "Crime", "Documentary", "Drama",
    "Family", "Fantasy", "History", "Music", "Mystery", "Romance",
    "Science Fiction", "Thriller", "TV Movie", "War", "Western", "Horror",
}

# Live category prefixes that are region codes rather than content types.
COUNTRY_CODES = {
    "US": "United States",
    "UK": "United Kingdom",
    "CA": "Canada",
    "INT": "International",
    "INR": "India",
    "IN": "India",
    "PK": "Pakistan",
    "AR": "Arabic",
    "AF": "Africa",
    "IT": "Italy",
    "IE": "Ireland",
    "NL": "Netherlands",
    "BE": "Belgium",
    "PT": "Portugal",
    "KU": "Kurdish",
    "IN/PK": "India / Pakistan",
    "DSTV": "DSTV Africa",
    "SC": "Scotland",
}

# Live prefixes that describe sport/event content, folded into one group so the
# ~48 "LIVE |" and "SPORTS |" categories stop competing for top-level space.
SPORTS_PREFIXES = {"LIVE", "SPORTS"}
SPORTS_GROUP = "Sports & Events"

# Series groupings.
SERIES_PROVIDERS = [
    "Netflix", "Amazon Prime", "Zee5", "Hotstar", "HotStar", "JioCinema",
    "SonyLiV", "DisneyPlus", "Disney+", "Apple TV", "Peacock", "Hulu", "HBO",
    "Max", "Britbox", "Stan", "Showtime", "Starz", "Paramount", "Acorn",
    "Epix", "Cinemax", "Shudder", "MXPlayer", "MX Player", "ULLU",
    "ALTBalaji", "Voot", "Hoichoi", "Chaupal", "Shemaroo",
]
SERIES_GENRES = {
    "Comedy", "Crime", "Documentary", "Reality", "Sci-Fi & Fantasy",
    "Fitness", "Reality Shows",
}
SERIES_REGIONS = [
    "Hindi", "Tamil", "Telugu", "Malayalam", "Kannada", "Punjabi", "Bangla",
    "Marathi", "Gujrati", "Gujarati", "Pakistani", "Indian", "Turkish",
    "Afrikaans", "Asian", "SouthAfrica",
]
SERIES_KIDS = [
    "Cartoon", "Cbeebies", "Kids", "Animaion", "Animation", "Adul Swim",
    "Adult Swim", "Freeform", "Abc Family",
]

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def split_prefix(name: str) -> tuple[str | None, str]:
    """Split "VOD | English 2026" into ("VOD", "English 2026")."""
    if "|" in name:
        prefix, rest = name.split("|", 1)
        return prefix.strip(), rest.strip()
    return None, name.strip()


_YEAR_RANGE = re.compile(r"(\d{4})\s*-\s*(\d{4})")
_YEAR = re.compile(r"(19|20)\d{2}")
_DECADE = re.compile(r"^(\d{2})s$")


def era_sort_key(sub_name: str) -> tuple[int, int]:
    """Sort era-style subcategories newest-first.

    "English 2026" must come before "English 2000-2009"; plain alphabetical
    sorting would invert that. Non-era names keep their natural order after
    the dated ones.
    """
    text = sub_name

    rng = _YEAR_RANGE.search(text)
    if rng:
        return (0, -int(rng.group(2)))

    year = _YEAR.search(text)
    if year:
        return (0, -int(year.group(0)))

    for token in text.split():
        dec = _DECADE.match(token)
        if dec:
            two = int(dec.group(1))
            full = 1900 + two if two >= 30 else 2000 + two
            return (0, -full)

    # Undated buckets (Trending, Modern, Classic, Old is Gold) come after.
    weights = {"Trending": 1, "Modern": 2, "Classic": 3, "Old is Gold": 4}
    for word, weight in weights.items():
        if word in text:
            return (1, weight)
    return (2, 0)


# --------------------------------------------------------------------------
# classifiers
# --------------------------------------------------------------------------


def classify_movie(category_name: str) -> tuple[str, str]:
    """Return (group, subcategory) for a VOD category name."""
    prefix, rest = split_prefix(category_name)

    if prefix and prefix.upper().startswith("4K"):
        return ("4K", rest or category_name)

    sub = rest or category_name

    if sub in MOVIE_GENRES:
        return ("Genres", sub)
    if sub == "Kids":
        return ("Kids", sub)
    if "FIFA" in sub or "Sports Replay" in sub:
        return ("Sports", sub)

    for language in LANGUAGES:
        if sub.startswith(language):
            return (language, sub)

    # "Hollywood Modern"/"Hollywood Classic" are English-language buckets.
    if sub.startswith("Hollywood"):
        return ("English", sub)

    # Anything left is a franchise/collection (007, X-Men, Harry Potter...).
    return ("Collections", sub)


def classify_live(category_name: str) -> tuple[str, str]:
    """Return (group, subcategory) for a live TV category name."""
    prefix, rest = split_prefix(category_name)

    if prefix is None:
        return ("Other Regions", category_name)

    upper = prefix.upper()
    if upper in SPORTS_PREFIXES:
        return (SPORTS_GROUP, rest or category_name)

    group = COUNTRY_CODES.get(upper, COUNTRY_CODES.get(prefix, prefix))
    return (group, rest or category_name)


def classify_series(category_name: str) -> tuple[str, str]:
    """Return (group, subcategory) for a series category name."""
    lowered = category_name.lower()

    if any(k.lower() in lowered for k in SERIES_KIDS):
        return ("Kids & Animation", category_name)
    if category_name in SERIES_GENRES:
        return ("Genres", category_name)
    for provider in SERIES_PROVIDERS:
        if provider.lower() in lowered:
            return ("Streaming Services", category_name)
    for region in SERIES_REGIONS:
        if region.lower() in lowered:
            return ("Regional / Language", category_name)
    return ("TV Networks", category_name)


CLASSIFIERS = {
    "live": classify_live,
    "movie": classify_movie,
    "series": classify_series,
}


def classify(kind: str, category_name: str) -> tuple[str, str]:
    """Classify a category for the given kind, never raising on odd input."""
    fn = CLASSIFIERS.get(kind)
    if fn is None or not category_name:
        return (UNCATEGORIZED, category_name or UNCATEGORIZED)
    try:
        group, sub = fn(category_name)
    except Exception:
        return (UNCATEGORIZED, category_name)
    return (group or UNCATEGORIZED, sub or category_name)


def group_sort_key(kind: str, group: str, item_count: int) -> tuple:
    """Order groups by size, but always pin Uncategorized last."""
    return (1 if group == UNCATEGORIZED else 0, -item_count, group.lower())
