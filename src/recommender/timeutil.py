"""Time utility — produces UTC datetimes uniformly.

Why (fixes review #7):
  utcnow() is deprecated in Py 3.12+, and it returns a naive datetime
  (without tzinfo). Naive datetimes cause pitfalls in cross-timezone comparisons / writing to TIMESTAMPTZ.
  This project runs Py 3.14, so keeping it would keep emitting DeprecationWarning.

  Use this utcnow() uniformly; it returns a naive UTC datetime.

  Note (bug fix — surfaced during testing):
  The columns defined by the DB migration are TIMESTAMP WITHOUT TIME ZONE (sa.DateTime()),
  and asyncpg does not allow writing a timezone-aware datetime into a naive TIMESTAMP column
  ("can't subtract offset-naive and offset-aware datetimes").
  Hence we keep naive UTC: datetime.now(UTC).replace(tzinfo=None).
  If TIMESTAMPTZ is needed in the future, add a migration to change the columns to DateTime(timezone=True),
  then change this back to datetime.now(UTC).
"""
from datetime import UTC, datetime


def utcnow() -> datetime:
    """Current UTC time (naive, compatible with the TIMESTAMP WITHOUT TIME ZONE DB schema)."""
    return datetime.now(UTC).replace(tzinfo=None)
