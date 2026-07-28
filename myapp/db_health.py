"""
Detects the exact class of failure behind the myapp_itemrecommendation
outage: a migration silently rolls back (or a table is otherwise missing)
and every page that touches it 500s on every request, with nothing
surfacing the problem until a user sends a screenshot.

Two pieces:
- missing_tables(): a full introspection sweep across every installed
  app's models, for the periodic integrity-check task.
- tolerate_missing_table(): a narrow, request-time guard for wrapping one
  specific query a view depends on, so a missing table degrades that one
  feature instead of 500ing the whole page.
"""
import logging
import re
from contextlib import contextmanager

from django.apps import apps
from django.core.cache import cache
from django.db import connection, OperationalError, ProgrammingError

logger = logging.getLogger(__name__)

# SQLite raises "no such table: X"; Postgres raises 'relation "x" does not
# exist'. Matching on this narrows the guard to genuinely missing-table
# errors so it never silently swallows an unrelated database problem
# (a real query bug, a lock timeout, a connection failure).
_MISSING_TABLE_RE = re.compile(r'no such table|relation .* does not exist', re.IGNORECASE)


def _is_missing_table_error(exc: Exception) -> bool:
    return bool(_MISSING_TABLE_RE.search(str(exc)))


def missing_tables():
    """
    Returns a list of (app_label.ModelName, db_table) for every concrete,
    managed Django model whose table doesn't actually exist in the
    database - the same drift that left myapp_itemrecommendation missing
    despite the model, and its migration, both existing in the codebase.
    """
    existing = set(connection.introspection.table_names())
    missing = []
    for model in apps.get_models():
        meta = model._meta
        if meta.proxy or meta.abstract or not meta.managed:
            continue
        if meta.db_table not in existing:
            missing.append((f"{meta.app_label}.{model.__name__}", meta.db_table))
    return missing


@contextmanager
def tolerate_missing_table(feature_name: str):
    """
    Wrap one query/block that depends on a table which might be missing
    due to a migration that never actually committed. On a genuine
    missing-table error, logs once per 10 minutes per feature (so a
    broken table doesn't spam the log on every single request) and lets
    the caller's `except` branch handle the degraded state. Any other
    exception (a real bug, a connection error) is never touched.

    Usage:
        with tolerate_missing_table('smart_suggestions') as ok:
            if ok:
                recommendations = list(get_user_recommendations(user))
        if not ok:
            recommendations = []
    """
    class _Result:
        available = True

    result = _Result()
    try:
        yield result
    except (OperationalError, ProgrammingError) as exc:
        if not _is_missing_table_error(exc):
            raise
        result.available = False
        cache_key = f'db_health:missing_table_logged:{feature_name}'
        if cache.add(cache_key, True, timeout=600):
            logger.error(
                "Feature %r degraded - its backing table appears to be missing "
                "(likely a migration that never committed): %s", feature_name, exc,
            )
