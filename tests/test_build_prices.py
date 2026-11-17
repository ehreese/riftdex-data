"""Unit tests for scripts/build_prices.py. No network.

Run from the repository root with:

    python3 -I -m unittest discover -s tests
"""

import gzip
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build_prices as bp  # noqa: E402

SOURCE_TS = "2026-11-16T20:05:58Z"
NOW = datetime(2026, 11, 16, 20, 31, 7, tzinfo=timezone.utc)


def price(product_id, finish, market):
    return {"productId": product_id, "lowPrice": None, "midPrice": None, "highPrice": None,
            "marketPrice": market, "directLowPrice": None, "subTypeName": finish}


def envelope(results, success=True, errors=()):
    return json.dumps({"success": success, "errors": list(errors), "results": results}).encode()


class BuildRowsTests(unittest.TestCase):

    def test_normal_only(self):
        rows = bp.build_rows({1: [price(10, "Normal", 0.25)]})
        self.assertEqual(rows, [[10, 0.25, None]])

    def test_foil_only(self):
        rows = bp.build_rows({1: [price(10, "Foil", 3.74)]})
        self.assertEqual(rows, [[10, None, 3.74]])

    def test_normal_and_foil(self):
        rows = bp.build_rows({1: [price(10, "Foil", 3.74), price(10, "Normal", 0.25)]})
        self.assertEqual(rows, [[10, 0.25, 3.74]])

    def test_null_market_price_on_one_finish(self):
        rows = bp.build_rows({1: [price(10, "Normal", None), price(10, "Foil", 0.33)]})
        self.assertEqual(rows, [[10, None, 0.33]])

    def test_no_market_price_at_all_means_no_row(self):
        rows = bp.build_rows({1: [price(10, "Normal", None), price(10, "Foil", None),
                                  price(11, "Normal", 1.0)]})
        self.assertEqual(rows, [[11, 1.0, None]])

    def test_duplicate_product_across_groups_with_same_price(self):
        rows = bp.build_rows({1: [price(10, "Normal", 0.25)],
                              2: [price(10, "Normal", 0.25), price(10, "Foil", 2.0)]})
        self.assertEqual(rows, [[10, 0.25, 2.0]])

    def test_duplicate_product_with_null_in_one_group_keeps_the_price(self):
        rows = bp.build_rows({1: [price(10, "Normal", None)], 2: [price(10, "Normal", 0.25)]})
        self.assertEqual(rows, [[10, 0.25, None]])

    def test_conflicting_duplicate_is_an_error(self):
        with self.assertRaisesRegex(bp.BuildError, "product 10 has two Normal prices"):
            bp.build_rows({1: [price(10, "Normal", 0.25)], 2: [price(10, "Normal", 0.30)]})

    def test_conflict_within_one_group_is_an_error(self):
        with self.assertRaises(bp.BuildError):
            bp.build_rows({1: [price(10, "Foil", 1.0), price(10, "Foil", 1.5)]})

    def test_rows_sorted_by_product_id(self):
        rows = bp.build_rows({2: [price(30, "Normal", 3.0), price(5, "Normal", 0.5)],
                              1: [price(20, "Foil", 2.0)]})
        self.assertEqual([row[0] for row in rows], [5, 20, 30])

    def test_rounding_to_two_decimals(self):
        rows = bp.build_rows({1: [price(1, "Normal", 0.125), price(2, "Normal", 1.005),
                                  price(3, "Normal", 2.994), price(4, "Normal", 7),
                                  price(5, "Normal", 0.1 + 0.2)]})
        self.assertEqual([row[1] for row in rows], [0.13, 1.01, 2.99, 7.0, 0.3])

    def test_unknown_finish_is_an_error(self):
        with self.assertRaisesRegex(bp.BuildError, "unknown subTypeName"):
            bp.build_rows({1: [price(10, "Holofoil", 1.0)]})

    def test_bad_values_are_errors(self):
        for bad in ("1.00", True, -1.0, float("nan"), float("inf")):
            with self.subTest(bad=bad), self.assertRaises(bp.BuildError):
                bp.build_rows({1: [price(10, "Normal", bad)]})
        for bad_id in (None, "10", 0, True):
            with self.subTest(bad_id=bad_id), self.assertRaises(bp.BuildError):
                bp.build_rows({1: [price(bad_id, "Normal", 1.0)]})


class ParseTests(unittest.TestCase):

    def test_source_timestamp_formats(self):
        for text in (b"2026-11-16T20:05:58+0000\n", b"2026-11-16T20:05:58+00:00",
                     b"2026-11-16T20:05:58Z", b"2026-11-16T15:05:58-05:00",
                     b"2026-11-16T20:05:58.123456+00:00"):
            with self.subTest(text=text):
                self.assertEqual(bp.parse_source_timestamp(text), SOURCE_TS)

    def test_source_timestamp_rejects_garbage_and_naive_times(self):
        for text in (b"", b"<html>", b"2026-11-16T20:05:58", b"\xff\xfe"):
            with self.subTest(text=text), self.assertRaises(bp.BuildError):
                bp.parse_source_timestamp(text)

    def test_results_envelope(self):
        self.assertEqual(bp.parse_results(envelope([1, 2]), "x"), [1, 2])
        for body in (b"not json", b"[]", envelope([], success=False, errors=["boom"]),
                     json.dumps({"success": True, "results": None}).encode(),
                     json.dumps({"results": []}).encode()):
            with self.subTest(body=body), self.assertRaises(bp.BuildError):
                bp.parse_results(body, "x")

    def test_group_ids(self):
        self.assertEqual(bp.group_ids([{"groupId": 24344}, {"groupId": 24439}]), [24344, 24439])
        for groups in ([], [{"name": "no id"}], [{"groupId": "1"}]):
            with self.subTest(groups=groups), self.assertRaises(bp.BuildError):
                bp.group_ids(groups)


class RenderTests(unittest.TestCase):

    ROWS = [[653002, None, 0.33], [653069, 0.25, 3.74]]

    def test_matches_documented_example(self):
        text = bp.render(self.ROWS, SOURCE_TS, "2026-11-16T20:31:07Z")
        self.assertEqual(text, (
            '{\n'
            '  "version": 1,\n'
            '  "currency": "USD",\n'
            '  "source": "TCGplayer market prices via TCGCSV",\n'
            '  "sourceUpdatedAt": "2026-11-16T20:05:58Z",\n'
            '  "generatedAt": "2026-11-16T20:31:07Z",\n'
            '  "prices": [\n'
            '    [653002, null, 0.33],\n'
            '    [653069, 0.25, 3.74]\n'
            '  ]\n'
            '}\n'))
        self.assertEqual(json.loads(text)["prices"], self.ROWS)

    def test_deterministic(self):
        groups_a = {1: [price(653069, "Foil", 3.74), price(653002, "Foil", 0.33)],
                    2: [price(653069, "Normal", 0.25)]}
        groups_b = {2: [price(653069, "Normal", 0.25)],
                    1: [price(653002, "Foil", 0.33), price(653069, "Foil", 3.74)]}
        text_a = bp.render(bp.build_rows(groups_a), SOURCE_TS, "2026-11-16T20:31:07Z")
        text_b = bp.render(bp.build_rows(groups_b), SOURCE_TS, "2026-11-16T20:31:07Z")
        self.assertEqual(text_a, text_b)
        self.assertEqual(bp.build_rows(groups_a), self.ROWS)

    def test_empty_rows_still_valid_json(self):
        self.assertEqual(json.loads(bp.render([], SOURCE_TS, SOURCE_TS))["prices"], [])


class ShrinkGuardTests(unittest.TestCase):

    def test_ninety_percent_is_allowed(self):
        bp.check_shrink(90, 100, allow_shrink=False)
        bp.check_shrink(1625, 1805, allow_shrink=False)  # 90.03% of the first real file
        bp.check_shrink(2000, 1805, allow_shrink=False)  # growing is always fine

    def test_eighty_nine_percent_is_refused(self):
        with self.assertRaisesRegex(bp.BuildError, "fewer than 90%"):
            bp.check_shrink(89, 100, allow_shrink=False)
        with self.assertRaises(bp.BuildError):
            bp.check_shrink(1624, 1805, allow_shrink=False)  # 89.97%

    def test_override(self):
        bp.check_shrink(89, 100, allow_shrink=True)
        bp.check_shrink(1, 100, allow_shrink=True)

    def test_no_existing_file(self):
        bp.check_shrink(1, 0, allow_shrink=False)


class FakeSource:
    """Serves canned responses by path and records what was asked for."""

    def __init__(self, responses):
        self.responses = responses
        self.asked = []
        self.requests = 0

    def get(self, path):
        self.asked.append(path)
        self.requests += 1
        if path not in self.responses:
            raise bp.BuildError(f"no response for {path}")
        return self.responses[path]


def standard_responses(last_updated=b"2026-11-16T20:05:58+0000"):
    return {
        "last-updated.txt": last_updated,
        "tcgplayer/89/groups": envelope([{"groupId": 1, "name": "Origins"},
                                         {"groupId": 2, "name": "Spiritforged"}]),
        "tcgplayer/89/1/prices": envelope([price(653069, "Normal", 0.25),
                                           price(653069, "Foil", 3.74)]),
        "tcgplayer/89/2/prices": envelope([price(653002, "Foil", 0.33),
                                           price(653003, "Normal", None)]),
    }


class RunTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.output = Path(self.temp.name) / "prices.json"
        self.logs = []

    def tearDown(self):
        self.temp.cleanup()

    def run_build(self, responses, **kwargs):
        source = FakeSource(responses)
        self.warnings = []
        written = bp.run(source, self.output, now=NOW, log=self.logs.append,
                         warn=self.warnings.append, **kwargs)
        return written, source

    def write_existing(self, source_updated_at, rows):
        self.output.write_text(bp.render(rows, source_updated_at, "2026-11-15T20:31:07Z"))
        return self.output.read_bytes()

    def test_writes_file_when_none_exists(self):
        written, source = self.run_build(standard_responses())
        self.assertTrue(written)
        document = json.loads(self.output.read_text())
        self.assertEqual(document["sourceUpdatedAt"], SOURCE_TS)
        self.assertEqual(document["generatedAt"], "2026-11-16T20:31:07Z")
        self.assertEqual(document["prices"], [[653002, None, 0.33], [653069, 0.25, 3.74]])
        self.assertEqual(source.asked, ["last-updated.txt", "tcgplayer/89/groups",
                                        "tcgplayer/89/1/prices", "tcgplayer/89/2/prices"])
        self.assertEqual(list(Path(self.temp.name).iterdir()), [self.output])  # no temp left

    def test_unchanged_source_short_circuits(self):
        before = self.write_existing(SOURCE_TS, [[1, 1.0, None]])
        written, source = self.run_build(standard_responses())
        self.assertFalse(written)
        self.assertEqual(source.asked, ["last-updated.txt"])
        self.assertEqual(self.output.read_bytes(), before)
        self.assertIn("nothing to do", self.logs[-1])

    def test_force_rebuilds_unchanged_source(self):
        self.write_existing(SOURCE_TS, [[1, 1.0, None]])
        written, _ = self.run_build(standard_responses(), force=True)
        self.assertTrue(written)
        self.assertEqual(len(json.loads(self.output.read_text())["prices"]), 2)

    def test_changed_source_rebuilds(self):
        self.write_existing("2026-11-15T20:05:51Z", [[1, 1.0, None]])
        written, _ = self.run_build(standard_responses())
        self.assertTrue(written)

    def test_unchanged_prices_give_identical_prices_array(self):
        self.run_build(standard_responses(b"2026-11-15T20:05:51+0000"))
        first = json.loads(self.output.read_text())
        self.run_build(standard_responses())
        second = json.loads(self.output.read_text())
        self.assertEqual(json.dumps(first["prices"]), json.dumps(second["prices"]))
        self.assertNotEqual(first["sourceUpdatedAt"], second["sourceUpdatedAt"])

    def test_shrink_guard_leaves_file_untouched(self):
        # The fixtures give 2 rows: 2 of 3 existing rows is 67%, below 90%.
        before = self.write_existing("2026-11-15T20:05:51Z", [[i, 1.0, None] for i in range(1, 4)])
        with self.assertRaisesRegex(bp.BuildError, "fewer than 90%"):
            self.run_build(standard_responses())
        self.assertEqual(self.output.read_bytes(), before)
        written, _ = self.run_build(standard_responses(), allow_shrink=True)
        self.assertTrue(written)

    def test_empty_group_is_skipped_with_a_warning(self):
        responses = standard_responses()
        responses["tcgplayer/89/groups"] = envelope([
            {"groupId": 1, "name": "Origins"}, {"groupId": 2, "name": "Spiritforged"},
            {"groupId": 3, "name": "Next Set"}])
        responses["tcgplayer/89/3/prices"] = envelope([])
        written, source = self.run_build(responses)
        self.assertTrue(written)
        self.assertIn("tcgplayer/89/3/prices", source.asked)
        self.assertEqual(self.warnings,
                         ["warning: group 3 (Next Set) has no prices; skipped"])
        self.assertEqual(json.loads(self.output.read_text())["prices"],
                         [[653002, None, 0.33], [653069, 0.25, 3.74]])
        self.assertIn("from 2 groups", self.logs[-1])

    def test_no_warning_when_every_group_has_prices(self):
        self.run_build(standard_responses())
        self.assertEqual(self.warnings, [])

    def test_group_that_loses_its_prices_is_caught_by_the_shrink_guard(self):
        # Group 1 used to supply most rows; now it is empty.
        self.write_existing("2026-11-15T20:05:51Z", [[i, 1.0, None] for i in range(1, 11)])
        responses = standard_responses()
        responses["tcgplayer/89/1/prices"] = envelope([price(n, "Normal", 1.0)
                                                       for n in range(100, 109)])
        self.run_build(responses)  # 10 rows: 100% of the existing 10
        before = self.output.read_bytes()
        responses = standard_responses(b"2026-11-17T20:05:58+0000")
        responses["tcgplayer/89/1/prices"] = envelope([])
        with self.assertRaisesRegex(bp.BuildError, "fewer than 90%"):
            self.run_build(responses)
        self.assertEqual(self.output.read_bytes(), before)

    def assert_fails_untouched(self, responses, message):
        before = self.write_existing("2026-11-15T20:05:51Z", [[1, 1.0, None], [2, 2.0, None]])
        with self.assertRaisesRegex(bp.BuildError, message):
            self.run_build(responses)
        self.assertEqual(self.output.read_bytes(), before)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [self.output])

    def test_failures_leave_file_untouched(self):
        cases = {
            "missing response": ("tcgplayer/89/2/prices", None, "no response"),
            "success false": ("tcgplayer/89/1/prices", envelope([], success=False), "success"),
            "not json": ("tcgplayer/89/groups", b"<html>busy</html>", "not valid JSON"),
            "empty group list": ("tcgplayer/89/groups", envelope([]), "no Riftbound groups"),
            "conflict": ("tcgplayer/89/2/prices", envelope([price(653069, "Foil", 9.99)]),
                         "two Foil prices"),
            "bad last-updated": ("last-updated.txt", b"oops", "not a timestamp"),
        }
        for name, (path, body, message) in cases.items():
            with self.subTest(name):
                responses = standard_responses()
                if body is None:
                    del responses[path]
                else:
                    responses[path] = body
                self.assert_fails_untouched(responses, message)

    def test_no_priced_product_is_an_error(self):
        responses = standard_responses()
        responses["tcgplayer/89/1/prices"] = envelope([price(1, "Normal", None)])
        responses["tcgplayer/89/2/prices"] = envelope([])
        self.assert_fails_untouched(responses, "no product has a market price")

    def test_corrupt_existing_file_is_an_error(self):
        self.output.write_text("{not json")
        with self.assertRaisesRegex(bp.BuildError, "not valid JSON"):
            self.run_build(standard_responses())
        self.assertEqual(self.output.read_text(), "{not json")


class OfflineAndMainTests(unittest.TestCase):

    def test_offline_round_trip_through_main(self):
        with tempfile.TemporaryDirectory() as temp:
            raw = Path(temp) / "raw"
            for path, body in standard_responses().items():
                target = raw / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(body)
            output = Path(temp) / "prices.json"
            args = ["--offline", str(raw), "--output", str(output)]
            with redirect_stdout():
                self.assertEqual(bp.main(args), 0)
                self.assertEqual(json.loads(output.read_text())["prices"][0], [653002, None, 0.33])
                before = output.read_bytes()
                self.assertEqual(bp.main(args), 0)  # unchanged: short-circuits
                self.assertEqual(output.read_bytes(), before)
                (raw / "tcgplayer/89/1/prices").unlink()
                self.assertEqual(bp.main(args + ["--force"]), 1)
                self.assertEqual(output.read_bytes(), before)


class redirect_stdout:
    """Silence stdout and stderr while main() runs."""

    def __enter__(self):
        self.saved = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = io.StringIO()

    def __exit__(self, *exc):
        sys.stdout, sys.stderr = self.saved


class FakeResponse:

    def __init__(self, body, encoding=None):
        self.body = body
        self.headers = {"Content-Encoding": encoding} if encoding else {}

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


class NetworkSourceTests(unittest.TestCase):

    def make(self, replies, save_dir=None):
        self.clock = FakeClock()
        self.requests = []

        def opener(request, timeout):
            self.requests.append((request, timeout))
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        return bp.NetworkSource(base_url="https://example.invalid", opener=opener,
                                sleep=self.clock.sleep, clock=self.clock.clock,
                                save_dir=save_dir, log=lambda message: None)

    def test_headers_timeout_and_gzip(self):
        source = self.make([FakeResponse(gzip.compress(b"hello"), "gzip")])
        self.assertEqual(source.get("last-updated.txt"), b"hello")
        request, timeout = self.requests[0]
        self.assertEqual(request.full_url, "https://example.invalid/last-updated.txt")
        self.assertEqual(request.get_header("User-agent"), bp.USER_AGENT)
        self.assertEqual(request.get_header("Accept-encoding"), "gzip")
        self.assertEqual(timeout, bp.TIMEOUT)

    def test_plain_and_unlabelled_gzip_bodies(self):
        source = self.make([FakeResponse(b"plain"), FakeResponse(gzip.compress(b"zipped"))])
        self.assertEqual(source.get("a"), b"plain")
        self.assertEqual(source.get("b"), b"zipped")

    def test_brotli_is_an_error(self):
        source = self.make([FakeResponse(b"\x0b\x02\x80", "br")])
        with self.assertRaisesRegex(bp.BuildError, "Content-Encoding"):
            source.get("a")
        self.assertEqual(source.requests, 1)

    def test_requests_are_spaced(self):
        source = self.make([FakeResponse(b"1"), FakeResponse(b"2")])
        source.get("a")
        source.get("b")
        self.assertEqual(self.clock.sleeps, [bp.MIN_INTERVAL])
        self.assertGreaterEqual(bp.MIN_INTERVAL, 0.15)

    def test_one_polite_retry_then_success(self):
        source = self.make([urllib.error.URLError("reset"), FakeResponse(b"ok")])
        self.assertEqual(source.get("a"), b"ok")
        self.assertEqual(source.requests, 2)
        self.assertIn(bp.RETRY_PAUSE, self.clock.sleeps)

    def test_fails_after_second_failure(self):
        error = urllib.error.HTTPError("u", 503, "busy", {}, None)
        source = self.make([error, urllib.error.HTTPError("u", 503, "busy", {}, None)])
        with self.assertRaisesRegex(bp.BuildError, "failed twice"):
            source.get("a")
        self.assertEqual(source.requests, 2)

    def test_no_retry_on_not_found(self):
        source = self.make([urllib.error.HTTPError("u", 404, "missing", {}, None)])
        with self.assertRaisesRegex(bp.BuildError, "HTTP 404"):
            source.get("a")
        self.assertEqual(source.requests, 1)

    def test_save_raw(self):
        with tempfile.TemporaryDirectory() as temp:
            source = self.make([FakeResponse(gzip.compress(b"body"), "gzip")], save_dir=temp)
            source.get("tcgplayer/89/groups")
            self.assertEqual((Path(temp) / "tcgplayer/89/groups").read_bytes(), b"body")


if __name__ == "__main__":
    unittest.main()
