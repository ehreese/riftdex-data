#!/usr/bin/env python3
"""One-off backfill of history/ from TCGCSV's daily price archives.

Not part of the daily job. Run by hand on a developer's machine; see
"Rebuilding the history" in README.md. The format is docs/format.md.

Three stages, each working from the cache left by the one before:

1. Download. TCGCSV keeps one archive per day at
   https://tcgcsv.com/archive/tcgplayer/prices-YYYY-MM-DD.ppmd.7z. Each is
   fetched once, one at a time, at least PAUSE seconds apart, into
   <cache>/archives/. A file already there is never requested again. A day
   TCGCSV answers 404 for is recorded in <cache>/not-found.txt and not asked
   for again. Any other failure stops the run: no retries.
2. Extract. The archives are 7z with PPMd compression, which the standard
   library cannot read, so this stage runs bsdtar (libarchive). Only the
   Riftbound members (YYYY-MM-DD/89/...) are extracted, into
   <cache>/extracted/YYYY-MM-DD/89/. A day already extracted is not
   extracted again.
3. Build. Every extracted day is read with the same row rules as
   build_prices.py, and history/ is written: one file per month and the
   index. Same cache in, byte-identical files out.

--offline skips stage 1. --extracted DIR builds from a folder laid out like
<cache>/extracted (YYYY-MM-DD/89/<groupId>/prices) and needs neither the
network nor bsdtar.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_prices as bp  # noqa: E402
import price_history as ph  # noqa: E402

ARCHIVE_URL = "https://tcgcsv.com/archive/tcgplayer/prices-{day}.ppmd.7z"
FIRST_DAY = "2025-10-31"  # Riftbound's release
PAUSE = 1.5  # default seconds between the end of one request and the start of the next
MIN_PAUSE = 1.0  # TCGCSV asks for a pause between archive downloads
TIMEOUT = 120  # seconds per archive
SEVEN_ZIP_MAGIC = b"7z\xbc\xaf\x27\x1c"
BSDTAR = "/usr/bin/bsdtar"

DEFAULT_CACHE = bp.REPO_ROOT / ".cache" / "history"
DEFAULT_OUTPUT = bp.REPO_ROOT / "history"


class BackfillError(Exception):
    """Anything that must stop the backfill."""


def days_between(first, last):
    current, end = date.fromisoformat(first), date.fromisoformat(last)
    while current <= end:
        yield current.isoformat()
        current += timedelta(days=1)


def archive_name(day):
    return f"prices-{day}.ppmd.7z"


# ---------------------------------------------------------------------------
# 1. Download
# ---------------------------------------------------------------------------


class Downloader:
    """Fetches each missing archive once, sequentially, with a pause."""

    def __init__(self, cache, opener=urllib.request.urlopen, sleep=time.sleep,
                 clock=time.monotonic, log=print, pause=PAUSE):
        if pause < MIN_PAUSE:
            raise BackfillError(f"pause must be at least {MIN_PAUSE} s")
        self.pause = pause
        self.archives = Path(cache) / "archives"
        self.not_found_file = Path(cache) / "not-found.txt"
        self.opener = opener
        self.sleep = sleep
        self.clock = clock
        self.log = log
        self.requests = 0
        self.downloaded = []  # (day, bytes)
        self._last_end = None

    def not_found(self):
        try:
            text = self.not_found_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return set()
        return {line.strip() for line in text.splitlines() if line.strip()}

    def _record_not_found(self, day):
        days = self.not_found() | {day}
        self.not_found_file.write_text("".join(f"{d}\n" for d in sorted(days)), encoding="utf-8")

    def _wait_turn(self):
        if self._last_end is not None:
            remaining = self.pause - (self.clock() - self._last_end)
            if remaining > 0:
                self.sleep(remaining)

    def fetch(self, day, last_day):
        """Download one day's archive unless it is cached or known missing.

        Returns "cached", "downloaded", "not-found" or "not-yet" (404 on the
        last day, which may simply not be published yet; not recorded).
        """
        target = self.archives / archive_name(day)
        if target.exists():
            return "cached"
        if day in self.not_found():
            return "not-found"
        self.archives.mkdir(parents=True, exist_ok=True)
        url = ARCHIVE_URL.format(day=day)
        request = urllib.request.Request(url, headers={
            "User-Agent": bp.USER_AGENT,
            "Accept-Encoding": "identity",
        })
        self._wait_turn()
        self.requests += 1
        started = self.clock()
        try:
            with self.opener(request, timeout=TIMEOUT) as response:
                body = response.read()
                length = response.headers.get("Content-Length")
        except urllib.error.HTTPError as error:
            self._last_end = self.clock()
            if error.code == 404:
                if day == last_day:
                    self.log(f"{day}: no archive yet (HTTP 404); not recorded")
                    return "not-yet"
                self._record_not_found(day)
                self.log(f"{day}: no archive (HTTP 404); recorded in {self.not_found_file.name}")
                return "not-found"
            raise BackfillError(f"GET {url} failed: HTTP {error.code}; stopping") from error
        except (OSError, EOFError) as error:
            raise BackfillError(f"GET {url} failed: {type(error).__name__}: {error}; "
                                f"stopping") from error
        self._last_end = self.clock()
        if length is not None and int(length) != len(body):
            raise BackfillError(f"GET {url}: got {len(body)} bytes, expected {length}; stopping")
        if not body.startswith(SEVEN_ZIP_MAGIC):
            raise BackfillError(f"GET {url}: response is not a 7z archive; stopping")
        partial = target.with_name(target.name + ".part")
        partial.write_bytes(body)
        os.replace(partial, target)
        self.downloaded.append((day, len(body)))
        self.log(f"{day}: downloaded {len(body):,} bytes in {self._last_end - started:.1f} s")
        return "downloaded"


def download(cache, first, last, log=print, downloader=None, pause=PAUSE):
    downloader = downloader or Downloader(cache, log=log, pause=pause)
    results = {}
    for day in days_between(first, last):
        results[day] = downloader.fetch(day, last)
    return downloader, results


# ---------------------------------------------------------------------------
# 2. Extract
# ---------------------------------------------------------------------------


def run_bsdtar(args):
    completed = subprocess.run([BSDTAR, *args], capture_output=True, check=False)
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", "replace").strip()
        raise BackfillError(f"bsdtar {' '.join(args)} failed: {message}")
    return completed.stdout


def extract(cache, day, log=print):
    """Extract day's Riftbound members into <cache>/extracted/<day>/89/.

    The extracted folder mirrors the archive: <day>/89/<groupId>/prices. A day
    whose archive has no Riftbound members gets an empty <day> folder, so the
    archive is not opened again. Returns True if the day has Riftbound members.
    """
    archive = Path(cache) / "archives" / archive_name(day)
    extracted = Path(cache) / "extracted"
    done = extracted / day
    if done.exists():
        return (done / str(bp.CATEGORY_ID)).exists()
    members = run_bsdtar(["-t", "-f", str(archive)]).decode("utf-8").splitlines()
    tops = {member.split("/", 1)[0] for member in members}
    if tops != {day}:
        raise BackfillError(f"{archive.name}: unexpected top-level entries {sorted(tops)[:5]}")
    prefix = f"{day}/{bp.CATEGORY_ID}/"
    wanted = [member for member in members if member.startswith(prefix)]
    temp = extracted / f".{day}.tmp"
    if temp.exists():
        shutil.rmtree(temp)
    temp.mkdir(parents=True)
    if wanted:
        # Member names come from the archive. bsdtar refuses absolute paths and
        # ".." unless told otherwise, and only the Riftbound subtree is asked for.
        run_bsdtar(["-x", "-f", str(archive), "-C", str(temp), "--no-same-owner",
                    f"{day}/{bp.CATEGORY_ID}"])
        os.replace(temp / day, done)
        temp.rmdir()
    else:
        os.replace(temp, done)
        log(f"{day}: archive has no Riftbound (category {bp.CATEGORY_ID}) members")
    return bool(wanted)


# ---------------------------------------------------------------------------
# 3. Build
# ---------------------------------------------------------------------------


def read_day(extracted, day, warn=print):
    """Rows for one extracted day, or [] when it has no Riftbound prices.

    extracted holds <day>/89/<groupId>/prices. Validation is build_prices.py's:
    the TCGCSV envelope, the row shape, rounding, and no conflicting prices.
    """
    category = Path(extracted) / day / str(bp.CATEGORY_ID)
    if not category.is_dir():
        return []
    prices_by_group = {}
    for entry in sorted(category.iterdir(), key=lambda p: p.name):
        if not entry.name.isdigit() or not entry.is_dir():
            raise BackfillError(f"{day}: unexpected entry {entry.name!r} in category folder")
        prices_file = entry / "prices"
        if not prices_file.is_file():
            warn(f"warning: {day}: group {entry.name} has no prices file; skipped")
            continue
        others = sorted(p.name for p in entry.iterdir() if p.name != "prices")
        if others:
            raise BackfillError(f"{day}: group {entry.name} has unexpected files {others}")
        results = bp.parse_results(prices_file.read_bytes(), f"{day} group {entry.name}")
        if not results:
            warn(f"warning: {day}: group {entry.name} has no prices; skipped")
            continue
        prices_by_group[int(entry.name)] = results
    return bp.build_rows(prices_by_group)


def read_all(extracted, days, warn=print):
    """{day: rows} for every day in days that has an extracted folder."""
    columns = {}
    for day in days:
        if not (Path(extracted) / day).is_dir():
            continue
        try:
            columns[day] = read_day(extracted, day, warn=warn)
        except bp.BuildError as error:
            raise BackfillError(f"{day}: {error}") from error
    return columns


def write_history(output, files, log=print):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    for name in sorted(files):
        path = output / name
        try:
            if path.read_text(encoding="utf-8") == files[name]:
                continue
        except FileNotFoundError:
            pass
        bp.write_atomically(path, files[name])
        log(f"wrote {output.name}/{name}")
    stale = sorted(p.name for p in output.glob("*.json") if p.name not in files)
    for name in stale:
        log(f"warning: history/{name} is not part of the rebuilt history; left in place")


def seam_report(published_path, columns, log=print):
    """Compare the published prices.json with the backfilled day it falls on."""
    existing = bp.read_existing(published_path)
    if existing is None:
        log("seam: no published prices.json to compare with")
        return
    day = ph.day_of_timestamp(existing["sourceUpdatedAt"])
    rows = existing["prices"]
    if day not in columns:
        through = max(d for d, r in columns.items() if r)
        log(f"seam: published prices.json is for {day}, after the last backfilled day "
            f"{through}; the days between are not in this history")
        return
    if columns[day] == rows:
        log(f"seam: backfilled {day} matches the published prices.json "
            f"({existing['sourceUpdatedAt']}, {len(rows)} rows)")
    else:
        backfilled = {row[0]: row for row in columns[day]}
        published = {row[0]: row for row in rows}
        differ = sorted(pid for pid in backfilled.keys() | published.keys()
                        if backfilled.get(pid) != published.get(pid))
        raise BackfillError(f"seam: backfilled {day} differs from the published prices.json in "
                            f"{len(differ)} products, e.g. {differ[:5]}")


def summarize(columns, first, last, log=print):
    days = list(days_between(first, last))
    with_data = [day for day in days if columns.get(day)]
    missing = [day for day in days if not columns.get(day)]
    log(f"days {first}..{last}: {len(days)}; with Riftbound prices: {len(with_data)}; "
        f"missing: {len(missing)}")
    if missing:
        log("missing days: " + ", ".join(missing))


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Backfill history/ from TCGCSV's archives.")
    parser.add_argument("--first", default=FIRST_DAY, help=f"first day (default {FIRST_DAY})")
    parser.add_argument("--last", help="last day (default: the day of the published prices.json)")
    parser.add_argument("--cache", default=str(DEFAULT_CACHE),
                        help="cache folder (default .cache/history)")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT),
                        help="history folder to write (default history/)")
    parser.add_argument("--offline", action="store_true",
                        help="never touch the network; use the archives already cached")
    parser.add_argument("--pause", type=float, default=PAUSE,
                        help=f"seconds between downloads (default {PAUSE}, at least {MIN_PAUSE})")
    parser.add_argument("--download-only", action="store_true",
                        help="download (and nothing else)")
    parser.add_argument("--extracted", metavar="DIR",
                        help="build from this extracted folder; no network, no bsdtar")
    parser.add_argument("--published", default=str(bp.DEFAULT_OUTPUT),
                        help="prices.json to check the seam against (default prices.json)")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        last = args.last
        if last is None:
            existing = bp.read_existing(args.published)
            if existing is None:
                raise BackfillError("no published prices.json; give --last")
            last = ph.day_of_timestamp(existing["sourceUpdatedAt"])
        cache = Path(args.cache)
        days = list(days_between(args.first, last))
        if args.extracted:
            extracted = Path(args.extracted)
        else:
            if not args.offline:
                started = time.monotonic()
                downloader, _ = download(cache, args.first, last, pause=args.pause)
                total = sum(size for _, size in downloader.downloaded)
                print(f"download: {downloader.requests} requests, "
                      f"{len(downloader.downloaded)} archives, {total:,} bytes, "
                      f"{time.monotonic() - started:.0f} s")
                if args.download_only:
                    return 0
            for day in days:
                if (cache / "archives" / archive_name(day)).exists():
                    extract(cache, day)
            extracted = cache / "extracted"
        columns = read_all(extracted, days, warn=bp.print_warning)
        summarize(columns, args.first, last)
        files = ph.build_history(columns)
        write_history(args.output, files)
        seam_report(args.published, columns)
    except (BackfillError, bp.BuildError, ph.HistoryError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
