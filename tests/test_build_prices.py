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
from datetime import datetime, timedelta, timezone
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build_prices as bp  # noqa: E402
import price_history as ph  # noqa: E402

SOURCE_TS = "2026-11-16T20:05:58Z"
NOW = datetime(2026, 11, 16, 20, 31, 7, tzinfo=timezone.utc)
# The next day's hourly run after the update window opened (SOURCE_TS + 23 h 34 min).
NEXT_DAY = datetime(2026, 11, 17, 19, 40, 0, tzinfo=timezone.utc)


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
        kwargs.setdefault("now", NOW)
        written = bp.run(source, self.output, log=self.logs.append,
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
        written, source = self.run_build(standard_responses(), now=NEXT_DAY)
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
            self.run_build(responses, now=NEXT_DAY)
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


class UpdateDueTests(unittest.TestCase):
    """The hourly rule: no request at all until 23 hours after the last update."""

    UPDATED = datetime(2026, 11, 18, 20, 5, 56, tzinfo=timezone.utc)
    EXISTING = {"sourceUpdatedAt": "2026-11-18T20:05:56Z", "prices": []}

    def due(self, existing, now):
        return bp.update_due(existing, now)[0]

    def test_just_under_23_hours_is_not_due(self):
        self.assertFalse(self.due(self.EXISTING, self.UPDATED + timedelta(hours=23, seconds=-1)))
        self.assertFalse(self.due(self.EXISTING, self.UPDATED + timedelta(minutes=1)))
        self.assertFalse(self.due(self.EXISTING, self.UPDATED))  # the very moment of the update

    def test_23_hours_and_over_is_due(self):
        self.assertTrue(self.due(self.EXISTING, self.UPDATED + timedelta(hours=23)))
        self.assertTrue(self.due(self.EXISTING, self.UPDATED + timedelta(hours=23, seconds=1)))
        self.assertTrue(self.due(self.EXISTING, self.UPDATED + timedelta(days=5)))

    def test_no_published_file_is_always_due(self):
        self.assertTrue(self.due(None, self.UPDATED))

    def test_future_source_timestamp_is_due(self):
        self.assertTrue(self.due(self.EXISTING, self.UPDATED - timedelta(seconds=1)))
        self.assertTrue(self.due(self.EXISTING, self.UPDATED - timedelta(days=400)))
        reason = bp.update_due(self.EXISTING, self.UPDATED - timedelta(hours=1))[1]
        self.assertIn("in the future", reason)

    def test_unreadable_source_timestamp_is_due(self):
        for value in (None, 20261118, "", "yesterday", "2026-11-18T20:05:56"):
            with self.subTest(value):
                self.assertTrue(self.due({"sourceUpdatedAt": value, "prices": []}, self.UPDATED))
        self.assertTrue(self.due({"prices": []}, self.UPDATED))

    def test_other_time_zones_are_compared_in_utc(self):
        existing = {"sourceUpdatedAt": "2026-11-18T15:05:56-05:00", "prices": []}
        self.assertFalse(self.due(existing, self.UPDATED + timedelta(hours=22, minutes=59)))
        self.assertTrue(self.due(existing, self.UPDATED + timedelta(hours=23)))

    def test_reason_names_when_the_next_check_is_due(self):
        due, reason = bp.update_due(self.EXISTING, self.UPDATED + timedelta(hours=19, minutes=3))
        self.assertFalse(due)
        self.assertEqual(reason, "prices.json is from 2026-11-18T20:05:56Z, 19 h 03 min old; "
                                 "no update is due before 2026-11-19T19:05:56Z")


class HourlyRunTests(unittest.TestCase):
    """What run() and main() ask TCGCSV for, depending on the age of the file."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.output = Path(self.temp.name) / "prices.json"
        self.output.write_text(bp.render([[653069, 0.24, 3.7]], SOURCE_TS, "2026-11-16T20:31:07Z"))
        self.before = self.output.read_bytes()
        self.logs = []

    def tearDown(self):
        self.temp.cleanup()

    def network(self):
        """A real NetworkSource whose opener records every request it is asked to make."""
        self.opened = []
        responses = standard_responses()

        def opener(request, timeout):
            self.opened.append(request.full_url)
            path = request.full_url.removeprefix("https://example.invalid/")
            return FakeResponse(responses[path])

        return bp.NetworkSource(base_url="https://example.invalid", opener=opener,
                                sleep=lambda s: None, log=lambda m: None)

    def run_at(self, now, **kwargs):
        source = self.network()
        written = bp.run(source, self.output, now=now, log=self.logs.append, **kwargs)
        return written, source

    def test_not_due_run_makes_zero_requests(self):
        for minutes in (0, 25, 60 * 12, 60 * 23 - 1):
            with self.subTest(minutes=minutes):
                now = datetime(2026, 11, 16, 20, 5, 58, tzinfo=timezone.utc) + timedelta(
                    minutes=minutes)
                written, source = self.run_at(now)
                self.assertFalse(written)
                self.assertEqual(self.opened, [])
                self.assertEqual(source.requests, 0)
                self.assertEqual(self.output.read_bytes(), self.before)
                self.assertIn("TCGCSV not contacted", self.logs[-1])

    def test_due_run_with_no_new_update_reads_only_last_updated(self):
        written, source = self.run_at(NEXT_DAY)
        self.assertFalse(written)
        self.assertEqual(self.opened, ["https://example.invalid/last-updated.txt"])
        self.assertEqual(self.output.read_bytes(), self.before)

    def test_manual_check_reads_last_updated_whatever_the_age(self):
        written, source = self.run_at(NOW, check_now=True)
        self.assertFalse(written)  # TCGCSV still says SOURCE_TS
        self.assertEqual(self.opened, ["https://example.invalid/last-updated.txt"])
        self.assertEqual(self.output.read_bytes(), self.before)

    def test_manual_check_rebuilds_when_tcgcsv_has_updated(self):
        self.output.write_text(bp.render([[653069, 0.24, 3.7]], "2026-11-16T19:30:00Z",
                                         "2026-11-16T19:40:00Z"))
        written, _ = self.run_at(NOW)  # 1 h after that file: not due
        self.assertFalse(written)
        self.assertEqual(self.opened, [])
        written, _ = self.run_at(NOW, check_now=True)
        self.assertTrue(written)
        self.assertEqual(len(self.opened), 4)
        self.assertEqual(json.loads(self.output.read_text())["sourceUpdatedAt"], SOURCE_TS)

    def test_force_still_rebuilds_a_fresh_file(self):
        written, _ = self.run_at(NOW, force=True)
        self.assertTrue(written)
        self.assertEqual(len(self.opened), 4)

    def test_future_source_timestamp_neither_crashes_nor_loops(self):
        self.output.write_text(bp.render([[653069, 0.24, 3.7]], "2026-11-20T20:05:58Z",
                                         "2026-11-20T20:31:07Z"))
        written, _ = self.run_at(NOW)
        # Due, so TCGCSV is asked; it names another update, so the usual build
        # runs once: each URL fetched once, no repeat.
        self.assertTrue(written)
        self.assertEqual(self.opened, [
            "https://example.invalid/last-updated.txt",
            "https://example.invalid/tcgplayer/89/groups",
            "https://example.invalid/tcgplayer/89/1/prices",
            "https://example.invalid/tcgplayer/89/2/prices"])

    def test_future_source_timestamp_matching_tcgcsv_stops_after_one_request(self):
        future = "2026-11-20T20:05:58Z"
        self.output.write_text(bp.render([[653069, 0.24, 3.7]], future, "2026-11-20T20:31:07Z"))
        before = self.output.read_bytes()
        self.opened = []
        responses = standard_responses(future.encode())
        source = bp.NetworkSource(
            base_url="https://example.invalid", sleep=lambda s: None, log=lambda m: None,
            opener=lambda request, timeout: (self.opened.append(request.full_url),
                                             FakeResponse(responses["last-updated.txt"]))[1])
        self.assertFalse(bp.run(source, self.output, now=NOW, log=self.logs.append))
        self.assertEqual(len(self.opened), 1)
        self.assertEqual(self.output.read_bytes(), before)
        self.assertIn("unchanged", self.logs[-1])

    def main_with_fake_network(self, args):
        """main() with its NetworkSource swapped for one that cannot reach the network."""
        original = bp.NetworkSource
        self.opened = []
        stamp = self.source_stamp.encode()

        def opener(request, timeout):
            self.opened.append(request.full_url)
            return FakeResponse(stamp)

        def fake_network(save_dir=None):
            return original(base_url="https://example.invalid", opener=opener,
                            sleep=lambda s: None, log=lambda m: None, save_dir=save_dir)

        out = io.StringIO()
        with mock.patch.object(bp, "NetworkSource", fake_network), \
                mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", out):
            code = bp.main(["--output", str(self.output), "--no-history"] + args)
        return code, out.getvalue()

    def fresh_file(self):
        # main() reads the real clock, so date the file one hour before it.
        moment = datetime.now(timezone.utc) - timedelta(hours=1)
        self.source_stamp = bp.format_timestamp(moment)
        self.output.write_text(bp.render([[653069, 0.24, 3.7]], self.source_stamp,
                                         self.source_stamp))
        return self.output.read_bytes()

    def test_main_scheduled_run_on_a_fresh_file_contacts_nothing(self):
        before = self.fresh_file()
        code, out = self.main_with_fake_network([])
        self.assertEqual(code, 0)
        self.assertEqual(self.opened, [])
        self.assertIn("no update is due", out)
        self.assertIn("requests made: 0", out)
        self.assertEqual(self.output.read_bytes(), before)

    def test_main_check_now_reads_last_updated(self):
        before = self.fresh_file()
        code, out = self.main_with_fake_network(["--check-now"])
        self.assertEqual(code, 0)
        self.assertEqual(self.opened, ["https://example.invalid/last-updated.txt"])
        self.assertIn("requests made: 1", out)
        self.assertEqual(self.output.read_bytes(), before)


def write_history(folder, columns):
    """A history as the backfill leaves it, from {day: rows}."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in ph.build_history(columns).items():
        (folder / name).write_text(text)


def snapshot(folder):
    return {p.relative_to(folder).as_posix(): p.read_bytes()
            for p in sorted(Path(folder).rglob("*")) if p.is_file()}


STANDARD_ROWS = [[653002, None, 0.33], [653069, 0.25, 3.74]]


class HistoryAppendTests(unittest.TestCase):
    """The daily job adds each new prices.json day to history/."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.output = self.root / "prices.json"
        self.history = self.root / "history"
        # The backfill ends on the 15th.
        write_history(self.history, {"2026-11-14": [[653069, 0.2, 3.5]],
                                     "2026-11-15": [[653069, 0.24, 3.7]]})
        self.output.write_text(bp.render([[653069, 0.24, 3.7]], "2026-11-15T20:05:51Z",
                                         "2026-11-15T20:31:07Z"))

    def tearDown(self):
        self.temp.cleanup()

    def build(self, responses, **kwargs):
        kwargs.setdefault("now", NOW)
        return bp.run(FakeSource(responses), self.output, log=lambda m: None,
                      warn=lambda m: None, history=self.history, **kwargs)

    def november(self):
        return json.loads((self.history / "2026-11.json").read_text())

    def index(self):
        return json.loads((self.history / "index.json").read_text())

    def test_new_day_is_appended(self):
        self.assertTrue(self.build(standard_responses()))
        self.assertEqual(self.november()["days"], ["2026-11-14", "2026-11-15", "2026-11-16"])
        self.assertEqual(self.november()["prices"], [
            [653002, None, [None, None, 0.33]],
            [653069, [0.2, 0.24, 0.25], [3.5, 3.7, 3.74]]])
        self.assertEqual(self.index(), {"version": 1, "months": ["2026-11"],
                                        "through": "2026-11-16"})

    def test_matches_a_history_built_in_one_go(self):
        self.build(standard_responses())
        expected = self.root / "expected"
        write_history(expected, {"2026-11-14": [[653069, 0.2, 3.5]],
                                 "2026-11-15": [[653069, 0.24, 3.7]],
                                 "2026-11-16": STANDARD_ROWS})
        self.assertEqual(snapshot(self.history), snapshot(expected))

    def test_same_source_twice_changes_nothing(self):
        self.build(standard_responses())
        before = snapshot(self.root)
        self.assertFalse(self.build(standard_responses()))  # short-circuits
        self.assertEqual(snapshot(self.root), before)
        self.assertTrue(self.build(standard_responses(), force=True))  # rebuilds the same day
        after = snapshot(self.root)
        self.assertEqual({k: v for k, v in after.items() if k.startswith("history/")},
                         {k: v for k, v in before.items() if k.startswith("history/")})

    def test_the_seam_with_the_backfill(self):
        # The backfill's last day and the published prices.json are the same
        # TCGCSV update: rebuilding that day leaves the history as it was.
        before = snapshot(self.history)
        responses = standard_responses(b"2026-11-15T20:05:51+0000")
        responses["tcgplayer/89/1/prices"] = envelope([price(653069, "Normal", 0.24),
                                                       price(653069, "Foil", 3.7)])
        responses["tcgplayer/89/2/prices"] = envelope([])
        self.assertTrue(self.build(responses, force=True))
        self.assertEqual(snapshot(self.history), before)
        # The next day continues the same file.
        self.build(standard_responses())
        self.assertEqual(self.november()["days"], ["2026-11-14", "2026-11-15", "2026-11-16"])

    def test_new_month_starts_a_new_file(self):
        self.build(standard_responses())
        november = (self.history / "2026-11.json").read_bytes()
        self.build(standard_responses(b"2026-12-01T20:05:12+0000"),
                   now=datetime(2026, 12, 1, 20, 40, tzinfo=timezone.utc))
        self.assertEqual((self.history / "2026-11.json").read_bytes(), november)
        december = json.loads((self.history / "2026-12.json").read_text())
        self.assertEqual(december["days"], ["2026-12-01"])
        self.assertEqual(december["prices"], [[653002, None, [0.33]],
                                              [653069, [0.25], [3.74]]])
        self.assertEqual(self.index(), {"version": 1, "months": ["2026-11", "2026-12"],
                                        "through": "2026-12-01"})

    def test_day_is_the_utc_date_of_the_source_update(self):
        self.build(standard_responses(b"2026-11-16T23:30:00-05:00"))  # 04:30 on the 17th, UTC
        self.assertEqual(self.index()["through"], "2026-11-17")

    def assert_nothing_written(self, responses, message, **kwargs):
        before = snapshot(self.root)
        with self.assertRaisesRegex(bp.BuildError, message):
            self.build(responses, **kwargs)
        self.assertEqual(snapshot(self.root), before)

    def test_history_problems_publish_nothing(self):
        self.assert_nothing_written(standard_responses(b"2026-11-14T20:05:00+0000"),
                                    "past days never change")
        (self.history / "2026-11.json").write_text("{broken")
        self.assert_nothing_written(standard_responses(), "not valid JSON")
        (self.history / "index.json").unlink()
        self.assert_nothing_written(standard_responses(), "index.json is missing")

    def test_build_problems_leave_the_history_alone(self):
        responses = standard_responses()
        responses["tcgplayer/89/2/prices"] = envelope([price(653069, "Foil", 9.99)])
        self.assert_nothing_written(responses, "two Foil prices")

    def test_without_history_nothing_is_appended(self):
        before = snapshot(self.history)
        bp.run(FakeSource(standard_responses()), self.output, now=NOW, log=lambda m: None)
        self.assertEqual(snapshot(self.history), before)


class OfflineAndMainTests(unittest.TestCase):

    def test_offline_round_trip_through_main(self):
        with tempfile.TemporaryDirectory() as temp:
            raw = Path(temp) / "raw"
            for path, body in standard_responses().items():
                target = raw / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(body)
            output = Path(temp) / "prices.json"
            history = Path(temp) / "history"
            write_history(history, {"2026-11-15": [[653069, 0.24, 3.7]]})
            args = ["--offline", str(raw), "--output", str(output)]
            with redirect_stdout():
                self.assertEqual(bp.main(args), 0)
                self.assertEqual(json.loads(output.read_text())["prices"][0], [653002, None, 0.33])
                self.assertEqual(json.loads((history / "index.json").read_text())["through"],
                                 "2026-11-16")
                before = output.read_bytes(), snapshot(history)
                self.assertEqual(bp.main(args), 0)  # unchanged: short-circuits
                self.assertEqual((output.read_bytes(), snapshot(history)), before)
                (raw / "tcgplayer/89/1/prices").unlink()
                self.assertEqual(bp.main(args + ["--force"]), 1)
                self.assertEqual((output.read_bytes(), snapshot(history)), before)

    def test_no_history_option(self):
        with tempfile.TemporaryDirectory() as temp:
            raw = Path(temp) / "raw"
            for path, body in standard_responses().items():
                target = raw / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(body)
            output = Path(temp) / "prices.json"
            with redirect_stdout():
                self.assertEqual(bp.main(["--offline", str(raw), "--output", str(output)]), 1)
                self.assertFalse(output.exists())
                self.assertEqual(bp.main(["--offline", str(raw), "--output", str(output),
                                          "--no-history"]), 0)
            self.assertTrue(output.exists())
            self.assertFalse((Path(temp) / "history").exists())


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
