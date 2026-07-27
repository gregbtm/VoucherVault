"""
Enrichment helpers for OCR extractions using embedded data resources.

Provides fast lookups and suggestions using pre-embedded data:
- UK railway station code/name resolution
- Travel pass route validation
"""
import json
import logging
import os

logger = logging.getLogger(__name__)

_DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
_UK_STATIONS = None


def _load_uk_stations():
    """Lazy-load the UK stations database on first use."""
    global _UK_STATIONS
    if _UK_STATIONS is not None:
        return _UK_STATIONS

    stations_file = os.path.join(_DATA_DIR, 'uk_stations.json')
    try:
        with open(stations_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
            stations = {}
            for station in data.get('stations', []):
                # Index by code and name for fast lookups
                code = station.get('code')
                name = station.get('name')
                if code:
                    stations[code.upper()] = station
                if name:
                    stations[name.upper()] = station
                # Index aliases (e.g. "London" -> "LON")
                for alias in station.get('aliases', []):
                    stations[alias.upper()] = station
            _UK_STATIONS = stations
            return stations
    except Exception as e:
        logger.warning(f"Failed to load UK stations database: {e}")
        return {}


def normalize_station(location_str: str) -> dict | None:
    """
    Takes a raw station name/code from vision model and attempts to resolve
    it to a known UK railway station. Returns station dict with code/name/region,
    or None if no match found.

    Supports partial matching: "London" → "LON", "KGX" → full King's Cross record.
    """
    if not location_str or not isinstance(location_str, str):
        return None

    stations = _load_uk_stations()
    if not stations:
        return None

    location_key = location_str.strip().upper()

    # Exact match first (fast path)
    if location_key in stations:
        return stations[location_key]

    # Substring search for partial matches (e.g. "manchester" -> "Manchester Piccadilly")
    for key, station in stations.items():
        if location_key in key or key in location_key:
            return station

    return None


def is_valid_travel_pass(extraction: dict) -> bool:
    """
    Checks if an extraction that claims to be a travel pass has the required
    fields populated. Returns True only if journey_origin, journey_destination,
    and ideally a redeem code are all present.
    """
    required_fields = ['journey_origin', 'journey_destination', 'code']
    return all(extraction.get(field) for field in required_fields)
