#!/usr/bin/env python3
"""Build prices.json from TCGCSV's Riftbound price files.

The contract for the output is docs/format.md. Standard library only.

The script is split in two halves:

* Fetching (NetworkSource, OfflineSource): turns a path such as
  "tcgplayer/89/groups" into the raw bytes of the response. Only this half
  touches the network.
* Transformation (everything from parse_source_timestamp to render): pure
  functions from raw responses to rows to the text of the file. The unit
  tests cover this half without any network.

run() ties the two together and applies the safety checks. On any problem
it raises BuildError, main() prints it and exits non-zero, and the existing
prices.json is never touched.
"""

import argparse
import gzip
import http.client
import json
import math
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zlib
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

BASE_URL = "https://tcgcsv.com"
CATEGORY_ID = 89  # Riftbound
USER_AGENT = "riftdex-data/1.0 (+https://github.com/ehreese/riftdex-data)"

MIN_INTERVAL = 0.2  # seconds between the start of two requests (TCGCSV asks for >= 0.15)
TIMEOUT = 30  # seconds per request
RETRY_PAUSE = 5  # seconds before the single polite retry

FORMAT_VERSION = 1
CURRENCY = "USD"
SOURCE = "TCGplayer market prices via TCGCSV"

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "prices.json"

LAST_UPDATED_PATH = "last-updated.txt"
GROUPS_PATH = f"tcgplayer/{CATEGORY_ID}/groups"

# A new file must keep at least this share of the published file's rows. One
# main set is 14-20% of all rows, so losing one stops the run.
MIN_KEPT_PERCENT = 90

FINISHES = {"Normal": 0, "Foil": 1}  # subTypeName -> index in (normal, foil)
CENT = Decimal("0.01")


class BuildError(Exception):
    """Anything that must stop the run and leave prices.json untouched."""


def prices_path(group_id):
    return f"tcgplayer/{CATEGORY_ID}/{group_id}/prices"


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def decode_body(body, content_encoding):
    """Undo the transfer encoding of a response body.

    We only ever ask for gzip. Some servers compress anyway, so also accept a
    body that starts with the gzip magic number. Brotli cannot be decoded with
    the standard library, so it is an error.
    """
    encoding = (content_encoding or "").strip().lower()
    if encoding in ("", "identity"):
        if body[:2] == b"\x1f\x8b":
            return gzip.decompress(body)
        return body
    if encoding in ("gzip", "x-gzip"):
        return gzip.decompress(body)
    raise BuildError(f"unsupported Content-Encoding {content_encoding!r}")


class NetworkSource:
    """Fetches from TCGCSV one request at a time, politely."""

    def __init__(self, base_url=BASE_URL, min_interval=MIN_INTERVAL, timeout=TIMEOUT,
                 retry_pause=RETRY_PAUSE, opener=urllib.request.urlopen,
                 sleep=time.sleep, clock=time.monotonic, save_dir=None, log=print):
        self.base_url = base_url.rstrip("/")
        self.min_interval = min_interval
        self.timeout = timeout
        self.retry_pause = retry_pause
        self.opener = opener
        self.sleep = sleep
        self.clock = clock
        self.save_dir = Path(save_dir) if save_dir else None
        self.log = log
        self.requests = 0
        self._last_start = None

    def _wait_turn(self):
        if self._last_start is not None:
            remaining = self.min_interval - (self.clock() - self._last_start)
            if remaining > 0:
                self.sleep(remaining)
        self._last_start = self.clock()

    def _request_once(self, url):
        self._wait_turn()
        self.requests += 1
        request = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "gzip",
        })
        with self.opener(request, timeout=self.timeout) as response:
            body = response.read()
            return decode_body(body, response.headers.get("Content-Encoding"))

    def get(self, path):
        url = f"{self.base_url}/{path}"
        try:
            body = self._request_once(url)
        except TRANSIENT_ERRORS as first:
            if isinstance(first, urllib.error.HTTPError) and not _worth_retrying(first.code):
                raise BuildError(f"GET {url} failed: HTTP {first.code}") from first
            self.log(f"GET {url} failed ({_describe(first)}); retrying once in {self.retry_pause} s")
            self.sleep(self.retry_pause)
            try:
                body = self._request_once(url)
            except TRANSIENT_ERRORS as second:
                raise BuildError(f"GET {url} failed twice: {_describe(second)}") from second
        if self.save_dir is not None:
            target = self.save_dir / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        return body


# Network and decoding failures that earn one polite retry. URLError (and its
# HTTPError) and timeouts are OSErrors; a truncated gzip body raises EOFError,
# BadGzipFile or zlib.error; a dropped connection mid-body raises IncompleteRead.
TRANSIENT_ERRORS = (OSError, EOFError, zlib.error, http.client.HTTPException)


def _worth_retrying(status):
    return status == 429 or status >= 500


def _describe(error):
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code}"
    return f"{type(error).__name__}: {error}"


class OfflineSource:
    """Reads responses saved by --save-raw, laid out like the URL paths."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.requests = 0

    def get(self, path):
        target = self.directory / path
        try:
            return target.read_bytes()
        except OSError as error:
            raise BuildError(f"offline: cannot read {path}: {error.strerror}") from error


# ---------------------------------------------------------------------------
# Transformation (pure)
# ---------------------------------------------------------------------------


def format_timestamp(moment):
    """UTC, whole seconds, with a Z: 2026-11-16T20:05:58Z."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_source_timestamp(body):
    """Turn the body of last-updated.txt into the sourceUpdatedAt value."""
    try:
        text = body.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise BuildError("last-updated.txt is not UTF-8 text") from error
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        moment = datetime.fromisoformat(candidate)
    except ValueError as error:
        raise BuildError(f"last-updated.txt is not a timestamp: {text[:80]!r}") from error
    if moment.tzinfo is None:
        raise BuildError(f"last-updated.txt has no time zone: {text!r}")
    return format_timestamp(moment)


def parse_results(body, label):
    """Check a TCGCSV JSON envelope and return its results list."""
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise BuildError(f"{label}: response is not valid JSON") from error
    if not isinstance(document, dict):
        raise BuildError(f"{label}: response is not a JSON object")
    if document.get("success") is not True:
        raise BuildError(f"{label}: success is {document.get('success')!r}, "
                         f"errors: {document.get('errors')!r}")
    results = document.get("results")
    if not isinstance(results, list):
        raise BuildError(f"{label}: results is not a list")
    return results


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def group_ids(groups):
    """The groupId of every group, in the order TCGCSV lists them."""
    if not groups:
        raise BuildError("groups: no Riftbound groups listed")
    ids = []
    for group in groups:
        group_id = group.get("groupId") if isinstance(group, dict) else None
        if not _is_int(group_id) or group_id <= 0:
            raise BuildError(f"groups: bad groupId in {group!r}")
        if group_id not in ids:
            ids.append(group_id)
    return ids


def round_price(value, where):
    """A marketPrice as a float with at most two decimals, or None."""
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise BuildError(f"{where}: marketPrice is not a number: {value!r}")
    if not math.isfinite(value) or value < 0:
        raise BuildError(f"{where}: marketPrice out of range: {value!r}")
    # repr() gives the shortest text that round-trips, so 0.125 rounds to 0.13
    # rather than suffering from its binary representation.
    return float(Decimal(repr(value)).quantize(CENT, rounding=ROUND_HALF_UP))


def build_rows(prices_by_group):
    """Merge the price results of every group into sorted output rows.

    prices_by_group maps groupId -> results list. Returns a list of
    [productId, normal, foil] rows, sorted by productId, one per product
    that has at least one market price.
    """
    merged = {}  # productId -> [normal, foil]
    seen_from = {}  # (productId, finish index) -> groupId that set it
    for group_id in sorted(prices_by_group):
        for entry in prices_by_group[group_id]:
            where = f"group {group_id}"
            if not isinstance(entry, dict):
                raise BuildError(f"{where}: price entry is not an object: {entry!r}")
            product_id = entry.get("productId")
            if not _is_int(product_id) or product_id <= 0:
                raise BuildError(f"{where}: bad productId in {entry!r}")
            where = f"group {group_id}, product {product_id}"
            finish = entry.get("subTypeName")
            if finish not in FINISHES:
                raise BuildError(f"{where}: unknown subTypeName {finish!r}")
            price = round_price(entry.get("marketPrice"), where)
            if price is None:
                continue
            index = FINISHES[finish]
            slot = merged.setdefault(product_id, [None, None])
            if slot[index] is None:
                slot[index] = price
                seen_from[(product_id, index)] = group_id
            elif slot[index] != price:
                raise BuildError(
                    f"product {product_id} has two {finish} prices: {slot[index]} "
                    f"(group {seen_from[(product_id, index)]}) and {price} (group {group_id})")
    return [[product_id, normal, foil]
            for product_id, (normal, foil) in sorted(merged.items())]


def render(rows, source_updated_at, generated_at):
    """The full text of prices.json, one price row per line."""
    header = {
        "version": FORMAT_VERSION,
        "currency": CURRENCY,
        "source": SOURCE,
        "sourceUpdatedAt": source_updated_at,
        "generatedAt": generated_at,
    }
    lines = ["{"]
    for key, value in header.items():
        lines.append(f"  {json.dumps(key)}: {json.dumps(value)},")
    if rows:
        lines.append('  "prices": [')
        body = [f"    {json.dumps(row, separators=(', ', ': '))}" for row in rows]
        lines.append(",\n".join(body))
        lines.append("  ]")
    else:
        lines.append('  "prices": []')
    lines.append("}")
    return "\n".join(lines) + "\n"


def check_shrink(new_count, old_count, allow_shrink):
    """Refuse a file with fewer than MIN_KEPT_PERCENT of the existing rows."""
    if old_count and new_count * 100 < old_count * MIN_KEPT_PERCENT and not allow_shrink:
        raise BuildError(
            f"new file would have {new_count} rows, fewer than {MIN_KEPT_PERCENT}% of the "
            f"existing {old_count}; rerun with --allow-shrink if this is expected")


def read_existing(path):
    """The existing prices.json as a dict, or None if there is none."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise BuildError(f"cannot read existing {path}: {error.strerror}") from error
    try:
        document = json.loads(text)
    except ValueError as error:
        raise BuildError(f"existing {path} is not valid JSON; delete it to start over") from error
    if not isinstance(document, dict) or not isinstance(document.get("prices"), list):
        raise BuildError(f"existing {path} does not look like a prices file; "
                         f"delete it to start over")
    return document


def write_atomically(path, text):
    """Write to a temporary file next to path, then rename over it."""
    path = Path(path)
    fd, temp_name = tempfile.mkstemp(prefix=".prices-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o644)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run(source, output, force=False, allow_shrink=False, now=None, log=print, warn=None):
    """Build and write prices.json. Returns True if the file was written."""
    warn = warn or log
    output = Path(output)
    existing = read_existing(output)

    source_updated_at = parse_source_timestamp(source.get(LAST_UPDATED_PATH))
    if existing is not None and existing.get("sourceUpdatedAt") == source_updated_at and not force:
        log(f"TCGCSV data unchanged since {source_updated_at}; nothing to do")
        return False

    groups = parse_results(source.get(GROUPS_PATH), "groups")
    ids = group_ids(groups)
    names = {group["groupId"]: group.get("name") for group in groups}
    prices_by_group = {}
    for group_id in ids:
        results = parse_results(source.get(prices_path(group_id)), f"prices for group {group_id}")
        if not results:
            # A newly listed group can have no products yet. Skip it; the
            # shrink guard catches a group that loses its prices.
            warn(f"warning: group {group_id} ({names.get(group_id)}) has no prices; skipped")
            continue
        prices_by_group[group_id] = results

    rows = build_rows(prices_by_group)
    if not rows:
        raise BuildError("no product has a market price")
    old_count = len(existing["prices"]) if existing is not None else 0
    check_shrink(len(rows), old_count, allow_shrink)

    generated_at = format_timestamp(now or datetime.now(timezone.utc))
    write_atomically(output, render(rows, source_updated_at, generated_at))
    log(f"wrote {output.name}: {len(rows)} rows from {len(prices_by_group)} groups, "
        f"source updated {source_updated_at}")
    return True


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Build prices.json from TCGCSV.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT),
                        help="where to write the file (default: prices.json at the repo root)")
    parser.add_argument("--force", action="store_true",
                        help="rebuild even if TCGCSV has not updated since the existing file")
    parser.add_argument("--allow-shrink", action="store_true",
                        help=f"accept a file with fewer than {MIN_KEPT_PERCENT}%% of the rows "
                             f"of the existing one")
    parser.add_argument("--offline", metavar="DIR",
                        help="read responses saved by --save-raw instead of the network")
    parser.add_argument("--save-raw", metavar="DIR",
                        help="also save every decoded response under DIR")
    return parser.parse_args(argv)


def print_warning(message):
    """Print a warning; inside GitHub Actions also annotate the run with it."""
    print(message, file=sys.stderr)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning::{message.removeprefix('warning: ')}", flush=True)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.offline:
        source = OfflineSource(args.offline)
    else:
        source = NetworkSource(save_dir=args.save_raw)
    try:
        run(source, args.output, force=args.force, allow_shrink=args.allow_shrink,
            warn=print_warning)
    except BuildError as error:
        print(f"error: {error}", file=sys.stderr)
        print("prices.json was not changed", file=sys.stderr)
        return 1
    finally:
        if not args.offline:
            print(f"requests made: {source.requests}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
