"""Unit tests for scripts/backfill_history.py. No network, no bsdtar.

The archives are 7z with PPMd compression, which the standard library cannot
write, so these tests start from extracted folders: tests/fixtures/extracted
is laid out as the backfill extracts an archive (YYYY-MM-DD/89/<groupId>/prices)
and tests/fixtures/history is what the backfill must build from it. All of
the data is invented.

Run from the repository root with:

    python3 -I -m unittest discover -s tests
"""

import io
import json
import shutil
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import backfill_history as bh  # noqa: E402
import build_prices as bp  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXTRACTED = FIXTURES / "extracted"
EXPECTED = FIXTURES / "history"
FIRST, LAST = "2026-01-30", "2026-02-03"


def quiet(*args, **kwargs):
    pass


def price(product_id, finish, market):
    return {"productId": product_id, "marketPrice": market, "subTypeName": finish}


def write_group(root, day, group, results, success=True):
    path = Path(root) / day / "89" / str(group) / "prices"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"success": success, "errors": [], "results": results}))


class ReadDayTests(unittest.TestCase):

    def test_reads_only_category_89(self):
        # The fixture also has 2026-01-30/1/89/prices: category 1, group 89.
        self.assertEqual(bh.read_day(EXTRACTED, "2026-01-30", warn=quiet),
                         [[1001, 0.25, 3.74], [1002, 1.0, None]])

    def test_merges_groups_and_rounds_like_build_prices(self):
        self.assertEqual(bh.read_day(EXTRACTED, "2026-02-03", warn=quiet),
                         [[1001, 0.31, None], [1002, None, 2.1], [1003, 5.0, None]])

    def test_day_without_prices_is_empty(self):
        warnings = []
        self.assertEqual(bh.read_day(EXTRACTED, "2026-01-31", warn=warnings.append), [])
        self.assertEqual(warnings, ["warning: 2026-01-31: group 101 has no prices; skipped"])
        self.assertEqual(bh.read_day(EXTRACTED, "2026-02-02", warn=quiet), [])  # no category

    def test_bad_data_stops_the_backfill(self):
        with tempfile.TemporaryDirectory() as temp:
            cases = {
                "conflict": [price(1, "Normal", 1.0), price(1, "Normal", 2.0)],
                "unknown finish": [price(1, "Holofoil", 1.0)],
                "bad price": [price(1, "Normal", "1.00")],
            }
            for name, results in cases.items():
                with self.subTest(name):
                    shutil.rmtree(Path(temp) / "2026-01-01", ignore_errors=True)
                    write_group(temp, "2026-01-01", 5, results)
                    with self.assertRaises(bp.BuildError):
                        bh.read_day(temp, "2026-01-01", warn=quiet)
            shutil.rmtree(Path(temp) / "2026-01-01")
            write_group(temp, "2026-01-01", 5, [], success=False)
            with self.assertRaisesRegex(bp.BuildError, "success"):
                bh.read_day(temp, "2026-01-01", warn=quiet)
            with self.assertRaisesRegex(bh.BackfillError, "2026-01-01"):
                bh.read_all(temp, ["2026-01-01"], warn=quiet)


class BuildFromFixturesTests(unittest.TestCase):

    def run_main(self, output, published=None):
        published = published or Path(output).parent / "no-prices.json"
        args = ["--extracted", str(EXTRACTED), "--first", FIRST, "--last", LAST,
                "--output", str(output), "--published", str(published)]
        stdout, stderr = io.StringIO(), io.StringIO()
        saved = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = stdout, stderr
        try:
            code = bh.main(args)
        finally:
            sys.stdout, sys.stderr = saved
        return code, stdout.getvalue(), stderr.getvalue()

    def test_builds_the_expected_files(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "history"
            code, out, _ = self.run_main(output)
            self.assertEqual(code, 0)
            built = {p.name: p.read_bytes() for p in output.iterdir()}
            expected = {p.name: p.read_bytes() for p in EXPECTED.iterdir()}
            self.assertEqual(built, expected)
            self.assertIn("missing days: 2026-01-31, 2026-02-02", out)

    def test_rebuild_is_byte_identical_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "history"
            self.run_main(output)
            first = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in output.iterdir()}
            _, out, _ = self.run_main(output)
            second = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in output.iterdir()}
            self.assertEqual(first, second)
            self.assertNotIn("wrote", out)

    def test_seam_with_the_published_file(self):
        with tempfile.TemporaryDirectory() as temp:
            published = Path(temp) / "prices.json"
            rows = [[1001, 0.31, None], [1002, None, 2.1], [1003, 5.0, None]]
            published.write_text(bp.render(rows, "2026-02-03T20:05:56Z", "2026-02-03T20:40:00Z"))
            code, out, _ = self.run_main(Path(temp) / "history", published)
            self.assertEqual(code, 0)
            self.assertIn("seam: backfilled 2026-02-03 matches the published prices.json", out)

            published.write_text(bp.render(rows[:2], "2026-02-03T20:05:56Z", "2026-02-03T20:40:00Z"))
            code, _, err = self.run_main(Path(temp) / "history", published)
            self.assertEqual(code, 1)
            self.assertIn("differs from the published prices.json in 1 products", err)

            published.write_text(bp.render(rows, "2026-02-04T20:05:56Z", "2026-02-04T20:40:00Z"))
            code, out, _ = self.run_main(Path(temp) / "history", published)
            self.assertEqual(code, 0)
            self.assertIn("published prices.json is for 2026-02-04, after the last backfilled "
                          "day 2026-02-03; the days between are not in this history", out)


class FakeResponse:

    def __init__(self, body):
        self.body = body
        self.headers = {"Content-Length": str(len(body))}

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeClock:

    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 3))
        self.now += seconds


ARCHIVE = bh.SEVEN_ZIP_MAGIC + b"invented archive body"


class DownloaderTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cache = Path(self.temp.name)
        self.clock = FakeClock()
        self.requests = []

    def tearDown(self):
        self.temp.cleanup()

    def make(self, replies):
        def opener(request, timeout):
            self.requests.append(request)
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        return bh.Downloader(self.cache, opener=opener, sleep=self.clock.sleep,
                             clock=self.clock.clock, log=quiet)

    def test_downloads_each_day_once_politely(self):
        downloader = self.make([FakeResponse(ARCHIVE), FakeResponse(ARCHIVE)])
        bh.download(self.cache, "2026-01-30", "2026-01-31", downloader=downloader)
        self.assertEqual([r.full_url for r in self.requests], [
            "https://tcgcsv.com/archive/tcgplayer/prices-2026-01-30.ppmd.7z",
            "https://tcgcsv.com/archive/tcgplayer/prices-2026-01-31.ppmd.7z"])
        self.assertEqual(self.requests[0].get_header("User-agent"), bp.USER_AGENT)
        self.assertEqual(self.clock.sleeps, [bh.PAUSE])
        self.assertGreaterEqual(bh.PAUSE, 1)
        self.assertEqual(sorted(p.name for p in (self.cache / "archives").iterdir()),
                         ["prices-2026-01-30.ppmd.7z", "prices-2026-01-31.ppmd.7z"])
        # A second run asks for nothing.
        again = self.make([])
        _, results = bh.download(self.cache, "2026-01-30", "2026-01-31", downloader=again)
        self.assertEqual(again.requests, 0)
        self.assertEqual(set(results.values()), {"cached"})

    def test_pause_can_be_longer_but_not_shorter(self):
        downloader = bh.Downloader(self.cache, opener=None, sleep=self.clock.sleep,
                                   clock=self.clock.clock, log=quiet, pause=10)
        self.assertEqual(downloader.pause, 10)
        with self.assertRaises(bh.BackfillError):
            bh.Downloader(self.cache, pause=0.5)

    def test_resumes_after_the_last_cached_day(self):
        (self.cache / "archives").mkdir()
        (self.cache / "archives" / "prices-2026-01-30.ppmd.7z").write_bytes(ARCHIVE)
        downloader = self.make([FakeResponse(ARCHIVE)])
        bh.download(self.cache, "2026-01-30", "2026-01-31", downloader=downloader)
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(self.requests[0].full_url.endswith("prices-2026-01-31.ppmd.7z"))

    def test_not_found_is_recorded_and_not_asked_again(self):
        missing = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        downloader = self.make([missing, FakeResponse(ARCHIVE)])
        _, results = bh.download(self.cache, "2026-01-30", "2026-01-31", downloader=downloader)
        self.assertEqual(results, {"2026-01-30": "not-found", "2026-01-31": "downloaded"})
        self.assertEqual((self.cache / "not-found.txt").read_text(), "2026-01-30\n")
        again = self.make([])
        bh.download(self.cache, "2026-01-30", "2026-01-31", downloader=again)
        self.assertEqual(again.requests, 0)

    def test_not_found_on_the_last_day_is_not_recorded(self):
        missing = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        downloader = self.make([missing])
        _, results = bh.download(self.cache, "2026-01-31", "2026-01-31", downloader=downloader)
        self.assertEqual(results, {"2026-01-31": "not-yet"})
        self.assertFalse((self.cache / "not-found.txt").exists())

    def test_any_other_failure_stops_without_retrying(self):
        for name, reply in {
                "server error": urllib.error.HTTPError("u", 503, "busy", {}, None),
                "throttled": urllib.error.HTTPError("u", 429, "slow down", {}, None),
                "network": urllib.error.URLError("reset"),
                "not an archive": FakeResponse(b"<html>busy</html>"),
        }.items():
            with self.subTest(name):
                self.requests = []
                downloader = self.make([reply, FakeResponse(ARCHIVE)])
                with self.assertRaisesRegex(bh.BackfillError, "stopping"):
                    bh.download(self.cache, "2026-01-30", "2026-01-31", downloader=downloader)
                self.assertEqual(len(self.requests), 1)
                self.assertFalse((self.cache / "archives").exists()
                                 and any((self.cache / "archives").iterdir()))

    def test_truncated_body_is_refused(self):
        response = FakeResponse(ARCHIVE)
        response.headers["Content-Length"] = str(len(ARCHIVE) + 10)
        downloader = self.make([response])
        with self.assertRaisesRegex(bh.BackfillError, "expected"):
            bh.download(self.cache, "2026-01-30", "2026-01-30", downloader=downloader)
        self.assertEqual(list((self.cache / "archives").iterdir()), [])


class ExtractTests(unittest.TestCase):
    """The bsdtar calls, with bsdtar replaced by a stand-in."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cache = Path(self.temp.name)
        self.calls = []
        self.saved = bh.run_bsdtar

    def tearDown(self):
        bh.run_bsdtar = self.saved
        self.temp.cleanup()

    def fake(self, members):
        def run_bsdtar(args):
            self.calls.append(args)
            if args[0] == "-t":
                return "".join(f"{m}\n" for m in members).encode()
            target = Path(args[args.index("-C") + 1])
            write_group(target, "2026-01-30", 101, [price(1001, "Normal", 0.25)])
            return b""
        bh.run_bsdtar = run_bsdtar

    def test_extracts_only_category_89(self):
        self.fake(["2026-01-30/1/89/prices", "2026-01-30/89/101/prices", "2026-01-30/3/1/prices"])
        self.assertTrue(bh.extract(self.cache, "2026-01-30", log=quiet))
        self.assertEqual(self.calls[1][-1], "2026-01-30/89")
        self.assertIn("--no-same-owner", self.calls[1])
        self.assertEqual(bh.read_day(self.cache / "extracted", "2026-01-30", warn=quiet),
                         [[1001, 0.25, None]])
        # Already extracted: bsdtar is not run again.
        self.assertTrue(bh.extract(self.cache, "2026-01-30", log=quiet))
        self.assertEqual(len(self.calls), 2)
        self.assertEqual([p.name for p in (self.cache / "extracted").iterdir()], ["2026-01-30"])

    def test_archive_without_riftbound_is_a_missing_day(self):
        self.fake(["2026-01-30/1/89/prices"])
        self.assertFalse(bh.extract(self.cache, "2026-01-30", log=quiet))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(bh.read_day(self.cache / "extracted", "2026-01-30", warn=quiet), [])

    def test_unexpected_layout_stops(self):
        self.fake(["2026-01-29/89/101/prices"])
        with self.assertRaisesRegex(bh.BackfillError, "unexpected top-level"):
            bh.extract(self.cache, "2026-01-30", log=quiet)


if __name__ == "__main__":
    unittest.main()
