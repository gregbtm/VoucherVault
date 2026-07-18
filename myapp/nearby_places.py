import logging
import re

import requests
from django.core.cache import cache

from .models import SiteConfiguration
from .utils import levenshtein_distance

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 12
OVERPASS_QUERY_TIMEOUT = 10
CACHE_SECONDS = 300
DEFAULT_OVERPASS_URL = 'https://overpass-api.de/api/interpreter'

# Retail/food/finance categories worth matching against a vault of gift
# cards, vouchers, coupons, and loyalty cards - deliberately narrow so a
# "nearby" query doesn't also surface parks, schools, or bus stops.
_AMENITY_VALUES = ('bank', 'cafe', 'fast_food', 'restaurant', 'pharmacy', 'fuel', 'cinema')

_NON_ALNUM_RE = re.compile(r'[^a-z0-9\s]')
_WHITESPACE_RE = re.compile(r'\s+')


def nearby_places_enabled() -> bool:
    return SiteConfiguration.load().nearby_places_enabled


def _normalize(name: str) -> str:
    name = _NON_ALNUM_RE.sub('', name.lower())
    return _WHITESPACE_RE.sub(' ', name).strip()


def _names_match(issuer_norm: str, poi_name: str) -> bool:
    """
    True if a POI's name plausibly refers to the same merchant as an
    already-normalized issuer name. Substring containment handles the
    common chain-branch case ("Tesco" vs. "Tesco Express"/"Tesco
    Superstore"), which a plain edit-distance check would score as very
    different strings despite being the same brand. The edit-distance
    fallback catches minor spelling/formatting differences for names that
    don't contain one another, scaled by length so short names aren't
    over-matched and long ones aren't under-matched.
    """
    poi_norm = _normalize(poi_name)
    if not issuer_norm or not poi_norm:
        return False
    if issuer_norm == poi_norm:
        return True
    if issuer_norm in poi_norm or poi_norm in issuer_norm:
        return True
    distance = levenshtein_distance(issuer_norm, poi_norm)
    return distance <= max(2, min(len(issuer_norm), len(poi_norm)) // 4)


def _build_overpass_query(lat: float, lon: float, radius_m: int) -> str:
    amenity_pattern = '|'.join(_AMENITY_VALUES)
    around = f'around:{radius_m},{lat},{lon}'
    return (
        f'[out:json][timeout:{OVERPASS_QUERY_TIMEOUT}];'
        f'('
        f'node["shop"]({around});'
        f'way["shop"]({around});'
        f'node["amenity"~"^({amenity_pattern})$"]({around});'
        f'way["amenity"~"^({amenity_pattern})$"]({around});'
        f');'
        f'out tags;'
    )


def _query_overpass(lat: float, lon: float, radius_m: int):
    """Returns a list of POI names near (lat, lon), or None on any request
    failure - callers treat None as "don't cache this, try again next time"
    versus an empty list, which is a genuine (and cacheable) no-results answer."""
    config = SiteConfiguration.load()
    url = config.overpass_api_url or DEFAULT_OVERPASS_URL
    query = _build_overpass_query(lat, lon, radius_m)
    try:
        response = requests.post(url, data={'data': query}, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        elements = response.json().get('elements', [])
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Overpass nearby-places lookup failed: %s", exc)
        return None
    return [
        element['tags']['name']
        for element in elements
        if element.get('tags', {}).get('name')
    ]


def _cache_key(lat: float, lon: float, radius_m: int) -> str:
    # Rounded to 4 decimal places (~11m) so repeated opens from roughly the
    # same spot hit the cache instead of re-querying Overpass every time.
    return f'nearby_places:{round(lat, 4)}:{round(lon, 4)}:{radius_m}'


def _nearby_poi_names(lat: float, lon: float, radius_m: int) -> list:
    key = _cache_key(lat, lon, radius_m)
    cached = cache.get(key)
    if cached is not None:
        return cached
    names = _query_overpass(lat, lon, radius_m)
    if names is None:
        return []
    cache.set(key, names, timeout=CACHE_SECONDS)
    return names


def find_nearby_issuer_matches(lat: float, lon: float, radius_m: int, issuers) -> list:
    """
    Given a coordinate and a list of item issuer names, returns the subset
    (case-preserved, deduplicated) whose name fuzzy-matches a shop/amenity
    within radius_m metres, per OpenStreetMap. Best-effort: any lookup
    failure or nothing configured/enabled returns an empty list rather than
    raising, since this only ever powers a dismissible suggestion.
    """
    if not nearby_places_enabled():
        return []
    unique_issuers = {i.strip(): _normalize(i.strip()) for i in issuers if i and i.strip()}
    if not unique_issuers:
        return []

    poi_names = _nearby_poi_names(lat, lon, radius_m)
    if not poi_names:
        return []

    matched = {
        issuer for issuer, issuer_norm in unique_issuers.items()
        if any(_names_match(issuer_norm, poi_name) for poi_name in poi_names)
    }
    return sorted(matched)
