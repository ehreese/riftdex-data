"""Monthly price history files: build, render, read and append.

The contract for the files is the "Price history" part of docs/format.md.
Standard library only, and pure: nothing here touches the network or writes
files. scripts/backfill_history.py builds whole months from TCGCSV's daily
archives; scripts/build_prices.py appends one day after it writes
prices.json. Both go through month_document() and render_month(), so a
month built in one go and a month built a day at a time are byte-identical.

A day's prices ("a column") are given as prices.json rows:
[productId, normal, foil], sorted by productId, each with at least one price.
"""

import json
import math
import re
from datetime import date
from decimal import Decimal, InvalidOperation

HISTORY_VERSION = 1
CURRENCY = "USD"
SOURCE = "TCGplayer market prices via TCGCSV"

INDEX_NAME = "index.json"

_MONTH = re.compile(r"\d{4}-\d{2}")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


class HistoryError(Exception):
    """A history file, or a day to add to one, breaks the contract."""


def month_of(day):
    """"2026-10-07" -> "2026-10"."""
    return day[:7]


def month_file_name(month):
    return f"{month}.json"


def check_day(day):
    if not isinstance(day, str) or not _DAY.fullmatch(day):
        raise HistoryError(f"not a date: {day!r}")
    try:
        date.fromisoformat(day)
    except ValueError as error:
        raise HistoryError(f"not a date: {day!r}") from error
    return day


def check_month(month):
    if not isinstance(month, str) or not _MONTH.fullmatch(month):
        raise HistoryError(f"not a month: {month!r}")
    check_day(month + "-01")
    return month


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _check_price(value, where):
    """A price is null or a non-negative number with at most two decimals."""
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise HistoryError(f"{where}: price is not a number: {value!r}")
    if not math.isfinite(value) or value < 0:
        raise HistoryError(f"{where}: price out of range: {value!r}")
    try:
        exponent = Decimal(repr(value)).as_tuple().exponent
    except InvalidOperation as error:
        raise HistoryError(f"{where}: price is not a number: {value!r}") from error
    if exponent < -2:
        raise HistoryError(f"{where}: price has more than two decimals: {value!r}")
    return float(value)


def check_column(day, rows):
    """Validate one day's rows; return them as a list of (id, normal, foil)."""
    column = []
    previous = 0
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != 3:
            raise HistoryError(f"{day}: bad row {row!r}")
        product_id, normal, foil = row
        if not _is_int(product_id) or product_id <= previous:
            raise HistoryError(f"{day}: product ids must be positive and ascending, got {row!r}")
        previous = product_id
        where = f"{day}, product {product_id}"
        normal, foil = _check_price(normal, where), _check_price(foil, where)
        if normal is None and foil is None:
            raise HistoryError(f"{where}: no price for either finish")
        column.append((product_id, normal, foil))
    return column


# ---------------------------------------------------------------------------
# Month files
# ---------------------------------------------------------------------------


def month_document(month, columns):
    """Build a month file's document from {day: rows}.

    Every day must fall in month and have at least one price; a day with no
    prices is a missing day and must simply be left out of columns.
    """
    check_month(month)
    if not columns:
        raise HistoryError(f"{month}: a month file needs at least one day")
    days = sorted(columns)
    for day in days:
        check_day(day)
        if month_of(day) != month:
            raise HistoryError(f"{day} is not in {month}")
        if not columns[day]:
            raise HistoryError(f"{day}: a day with no prices is a missing day, not a day")
    width = len(days)
    by_product = {}
    for index, day in enumerate(days):
        for product_id, normal, foil in check_column(day, columns[day]):
            series = by_product.get(product_id)
            if series is None:
                series = by_product[product_id] = ([None] * width, [None] * width)
            series[0][index] = normal
            series[1][index] = foil
    prices = []
    for product_id in sorted(by_product):
        normal, foil = by_product[product_id]
        prices.append([product_id,
                       normal if any(v is not None for v in normal) else None,
                       foil if any(v is not None for v in foil) else None])
    return {
        "version": HISTORY_VERSION,
        "month": month,
        "currency": CURRENCY,
        "source": SOURCE,
        "days": days,
        "prices": prices,
    }


def _compact(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def render_month(document):
    """The text of a month file: compact JSON, one price row per line."""
    header = ",".join(f"{_compact(key)}:{_compact(document[key])}"
                      for key in ("version", "month", "currency", "source"))
    lines = ["{" + header + ",",
             '"days":' + _compact(document["days"]) + ",",
             '"prices":[',
             ",\n".join(_compact(row) for row in document["prices"]),
             "]}"]
    return "\n".join(lines) + "\n"


def parse_month(text, month):
    """Read a month file and return its {day: rows}. Checks the contract."""
    check_month(month)
    where = month_file_name(month)
    try:
        document = json.loads(text)
    except ValueError as error:
        raise HistoryError(f"{where} is not valid JSON") from error
    if not isinstance(document, dict):
        raise HistoryError(f"{where} is not a JSON object")
    if document.get("version") != HISTORY_VERSION:
        raise HistoryError(f"{where}: unknown version {document.get('version')!r}")
    if document.get("month") != month:
        raise HistoryError(f"{where}: month is {document.get('month')!r}")
    days = document.get("days")
    if not isinstance(days, list) or not days:
        raise HistoryError(f"{where}: days is not a non-empty list")
    for day in days:
        check_day(day)
        if month_of(day) != month:
            raise HistoryError(f"{where}: day {day} is not in {month}")
    if days != sorted(set(days)):
        raise HistoryError(f"{where}: days are not ascending and unique")
    prices = document.get("prices")
    if not isinstance(prices, list):
        raise HistoryError(f"{where}: prices is not a list")
    columns = {day: [] for day in days}
    previous = 0
    for row in prices:
        if not isinstance(row, list) or len(row) != 3 or not _is_int(row[0]):
            raise HistoryError(f"{where}: bad row {str(row)[:80]}")
        product_id = row[0]
        if product_id <= previous:
            raise HistoryError(f"{where}: rows are not sorted by product id at {product_id}")
        previous = product_id
        series = []
        for series_value in row[1:]:
            if series_value is None:
                series.append([None] * len(days))
                continue
            if not isinstance(series_value, list) or len(series_value) != len(days):
                raise HistoryError(f"{where}: product {product_id} is not aligned with days")
            if all(value is None for value in series_value):
                raise HistoryError(f"{where}: product {product_id} has an all-null series")
            series.append(series_value)
        if row[1] is None and row[2] is None:
            raise HistoryError(f"{where}: product {product_id} has no prices")
        for index, day in enumerate(days):
            normal = _check_price(series[0][index], f"{where}, {day}, product {product_id}")
            foil = _check_price(series[1][index], f"{where}, {day}, product {product_id}")
            if normal is not None or foil is not None:
                columns[day].append([product_id, normal, foil])
    for day, rows in columns.items():
        if not rows:
            raise HistoryError(f"{where}: day {day} has no prices")
    return columns


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


def index_document(months, through):
    months = sorted(months)
    for month in months:
        check_month(month)
    check_day(through)
    if not months or month_of(through) != months[-1]:
        raise HistoryError(f"index: through {through} is not in the last month {months[-1:]}")
    return {"version": HISTORY_VERSION, "months": months, "through": through}


def render_index(document):
    return _compact(document) + "\n"


def parse_index(text):
    try:
        document = json.loads(text)
    except ValueError as error:
        raise HistoryError(f"{INDEX_NAME} is not valid JSON") from error
    if not isinstance(document, dict) or document.get("version") != HISTORY_VERSION:
        raise HistoryError(f"{INDEX_NAME}: not a version {HISTORY_VERSION} index")
    months = document.get("months")
    if not isinstance(months, list) or not months or months != sorted(set(months)):
        raise HistoryError(f"{INDEX_NAME}: months is not a non-empty ascending list")
    return index_document(months, document.get("through"))


# ---------------------------------------------------------------------------
# Whole history and the daily append
# ---------------------------------------------------------------------------


def build_history(columns):
    """All files for a history from {day: rows}: {file name: text}.

    Days with no rows are missing days and are left out.
    """
    by_month = {}
    for day, rows in columns.items():
        check_day(day)
        if rows:
            by_month.setdefault(month_of(day), {})[day] = rows
    if not by_month:
        raise HistoryError("no day has any prices")
    files = {}
    for month in sorted(by_month):
        files[month_file_name(month)] = render_month(month_document(month, by_month[month]))
    through = max(max(days) for days in by_month.values())
    files[INDEX_NAME] = render_index(index_document(list(by_month), through))
    return files


def append_day(index_text, month_text, day, rows):
    """Add one day to the history; return {file name: new text}.

    index_text is the current index.json. month_text is the current file for
    day's month, or None if there is none yet. The result holds the month file
    and the index, whether or not they changed; the caller writes the ones
    that differ.

    * A day after the index's "through" is appended, starting a new month
      file when it is the first day of a new month.
    * The day equal to "through" is replaced, so adding the same day twice
      gives byte-identical files and a second TCGCSV update on one UTC date
      replaces the first.
    * An earlier day is refused: finished days and months never change.
    """
    check_day(day)
    if not rows:
        raise HistoryError(f"{day}: no prices to add")
    index = parse_index(index_text)
    through = index["through"]
    month = month_of(day)
    last_month = index["months"][-1]
    if day < through:
        raise HistoryError(f"{day} is before the end of the history ({through}); "
                           f"past days never change")
    if month == last_month:
        if month_text is None:
            raise HistoryError(f"{month_file_name(month)} is listed in the index but missing")
        columns = parse_month(month_text, month)
        if max(columns) != through:
            raise HistoryError(f"{month_file_name(month)} ends on {max(columns)}, "
                               f"the index says {through}")
        months = index["months"]
    else:
        if month_text is not None:
            raise HistoryError(f"{month_file_name(month)} exists but is not in the index")
        columns = {}
        months = index["months"] + [month]
    columns[day] = rows
    return {
        month_file_name(month): render_month(month_document(month, columns)),
        INDEX_NAME: render_index(index_document(months, day)),
    }


def day_of_timestamp(timestamp):
    """"2026-11-18T20:05:56Z" -> "2026-11-18". The timestamp must be UTC."""
    if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
        raise HistoryError(f"not a UTC timestamp: {timestamp!r}")
    return check_day(timestamp[:10])
