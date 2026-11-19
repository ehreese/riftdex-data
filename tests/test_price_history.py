"""Unit tests for scripts/price_history.py. No network.

Run from the repository root with:

    python3 -I -m unittest discover -s tests
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import price_history as ph  # noqa: E402

# Three invented days in October with a gap on the 3rd, and two in November.
OCT_1 = [[10, 0.25, 3.74], [20, 1.0, None]]
OCT_2 = [[10, 0.3, 3.5], [20, 1.1, 2.0], [30, 5.0, None]]   # 20 gains a foil; 30 is new
OCT_4 = [[10, 0.31, None], [30, 5.0, None]]                 # 20 has no price at all
NOV_1 = [[10, 0.4, 3.0], [40, None, 7.25]]
NOV_2 = [[10, 0.41, 3.1], [40, None, 7.5]]

COLUMNS = {"2026-10-01": OCT_1, "2026-10-02": OCT_2, "2026-10-04": OCT_4,
           "2026-11-01": NOV_1, "2026-11-02": NOV_2}


def month_columns(month):
    return {day: rows for day, rows in COLUMNS.items() if day.startswith(month)}


class MonthDocumentTests(unittest.TestCase):

    def setUp(self):
        self.document = ph.month_document("2026-10", month_columns("2026-10"))

    def row(self, product_id):
        return next(row for row in self.document["prices"] if row[0] == product_id)

    def test_header(self):
        self.assertEqual({k: v for k, v in self.document.items() if k != "prices"}, {
            "version": 1, "month": "2026-10", "currency": "USD",
            "source": "TCGplayer market prices via TCGCSV",
            "days": ["2026-10-01", "2026-10-02", "2026-10-04"]})

    def test_missing_day_is_not_a_day(self):
        self.assertNotIn("2026-10-03", self.document["days"])
        for row in self.document["prices"]:
            for series in row[1:]:
                if series is not None:
                    self.assertEqual(len(series), 3)

    def test_series_are_aligned_with_days(self):
        self.assertEqual(self.row(10), [10, [0.25, 0.3, 0.31], [3.74, 3.5, None]])

    def test_product_that_appears_mid_month(self):
        self.assertEqual(self.row(30), [30, [None, 5.0, 5.0], None])

    def test_finish_that_appears_mid_month_and_a_day_without_prices(self):
        self.assertEqual(self.row(20), [20, [1.0, 1.1, None], [None, 2.0, None]])

    def test_finish_without_any_price_is_null(self):
        self.assertIsNone(self.row(30)[2])

    def test_rows_sorted_by_product_id(self):
        self.assertEqual([row[0] for row in self.document["prices"]], [10, 20, 30])

    def test_product_without_prices_has_no_row(self):
        self.assertNotIn(40, [row[0] for row in self.document["prices"]])

    def test_order_of_input_does_not_matter(self):
        reversed_columns = dict(reversed(list(month_columns("2026-10").items())))
        self.assertEqual(ph.month_document("2026-10", reversed_columns), self.document)

    def test_refusals(self):
        cases = {
            "day outside the month": ("2026-10", {"2026-11-01": NOV_1}),
            "day without prices": ("2026-10", {"2026-10-01": []}),
            "no days": ("2026-10", {}),
            "unsorted rows": ("2026-10", {"2026-10-01": [[20, 1.0, None], [10, 1.0, None]]}),
            "row without prices": ("2026-10", {"2026-10-01": [[10, None, None]]}),
            "three decimals": ("2026-10", {"2026-10-01": [[10, 0.125, None]]}),
            "negative": ("2026-10", {"2026-10-01": [[10, -1.0, None]]}),
            "not a date": ("2026-10", {"2026-10-32": OCT_1}),
            "bad month": ("2026-13", {"2026-13-01": OCT_1}),
        }
        for name, (month, columns) in cases.items():
            with self.subTest(name), self.assertRaises(ph.HistoryError):
                ph.month_document(month, columns)


class RenderAndParseTests(unittest.TestCase):

    def test_render_is_compact_with_one_row_per_line(self):
        text = ph.render_month(ph.month_document("2026-10", {"2026-10-01": OCT_1}))
        self.assertEqual(text, (
            '{"version":1,"month":"2026-10","currency":"USD",'
            '"source":"TCGplayer market prices via TCGCSV",\n'
            '"days":["2026-10-01"],\n'
            '"prices":[\n'
            '[10,[0.25],[3.74]],\n'
            '[20,[1.0],null]\n'
            ']}\n'))

    def test_round_trip(self):
        columns = month_columns("2026-10")
        text = ph.render_month(ph.month_document("2026-10", columns))
        self.assertEqual(ph.parse_month(text, "2026-10"),
                         {day: [list(row) for row in rows] for day, rows in columns.items()})

    def test_deterministic(self):
        first = ph.build_history(dict(COLUMNS))
        second = ph.build_history(dict(reversed(list(COLUMNS.items()))))
        self.assertEqual(first, second)

    def test_parse_refuses_broken_files(self):
        good = json.loads(ph.render_month(ph.month_document("2026-10", month_columns("2026-10"))))
        broken = {
            "wrong month": dict(good, month="2026-11"),
            "wrong version": dict(good, version=2),
            "misaligned": dict(good, prices=[[10, [0.25, 0.3], None]]),
            "all-null series": dict(good, prices=[[10, [None, None, None], [1.0, 1.0, 1.0]]]),
            "unsorted": dict(good, prices=[[20, [1.0, 1.0, 1.0], None], [10, [1.0, 1.0, 1.0], None]]),
            "unsorted days": dict(good, days=["2026-10-02", "2026-10-01", "2026-10-04"]),
            "empty day": dict(good, prices=[[10, [1.0, None, 1.0], None]]),
        }
        for name, document in broken.items():
            with self.subTest(name), self.assertRaises(ph.HistoryError):
                ph.parse_month(json.dumps(document), "2026-10")
        with self.assertRaises(ph.HistoryError):
            ph.parse_month("{not json", "2026-10")


class BuildHistoryTests(unittest.TestCase):

    def test_month_rollover_and_index(self):
        files = ph.build_history(dict(COLUMNS))
        self.assertEqual(sorted(files), ["2026-10.json", "2026-11.json", "index.json"])
        self.assertEqual(files["index.json"],
                         '{"version":1,"months":["2026-10","2026-11"],"through":"2026-11-02"}\n')
        november = json.loads(files["2026-11.json"])
        self.assertEqual(november["days"], ["2026-11-01", "2026-11-02"])
        self.assertEqual(november["prices"], [[10, [0.4, 0.41], [3.0, 3.1]],
                                              [40, None, [7.25, 7.5]]])

    def test_day_without_rows_is_left_out(self):
        files = ph.build_history({"2026-10-01": OCT_1, "2026-10-02": [], "2026-11-01": []})
        self.assertEqual(sorted(files), ["2026-10.json", "index.json"])
        self.assertEqual(json.loads(files["index.json"])["through"], "2026-10-01")
        self.assertEqual(json.loads(files["2026-10.json"])["days"], ["2026-10-01"])

    def test_nothing_at_all_is_an_error(self):
        with self.assertRaises(ph.HistoryError):
            ph.build_history({"2026-10-01": []})


class AppendDayTests(unittest.TestCase):

    def history_through(self, last_day):
        return ph.build_history({d: r for d, r in COLUMNS.items() if d <= last_day})

    def append(self, files, day, rows):
        month_name = ph.month_file_name(ph.month_of(day))
        result = ph.append_day(files["index.json"], files.get(month_name), day, rows)
        return dict(files, **result)

    def test_appending_day_by_day_equals_building_at_once(self):
        days = sorted(COLUMNS)
        files = ph.build_history({days[0]: COLUMNS[days[0]]})
        for day in days[1:]:
            files = self.append(files, day, COLUMNS[day])
        self.assertEqual(files, ph.build_history(dict(COLUMNS)))

    def test_missing_day_leaves_a_gap(self):
        files = self.append(self.history_through("2026-10-02"), "2026-10-04", OCT_4)
        self.assertEqual(json.loads(files["2026-10.json"])["days"],
                         ["2026-10-01", "2026-10-02", "2026-10-04"])

    def test_same_day_twice_changes_nothing(self):
        files = self.history_through("2026-10-04")
        again = self.append(files, "2026-10-04", OCT_4)
        self.assertEqual(again, files)

    def test_second_update_on_the_same_day_replaces_it(self):
        files = self.history_through("2026-10-04")
        replaced = self.append(files, "2026-10-04", [[10, 0.5, None]])
        october = json.loads(replaced["2026-10.json"])
        self.assertEqual(october["days"], ["2026-10-01", "2026-10-02", "2026-10-04"])
        self.assertEqual(october["prices"], [[10, [0.25, 0.3, 0.5], [3.74, 3.5, None]],
                                             [20, [1.0, 1.1, None], [None, 2.0, None]],
                                             [30, [None, 5.0, None], None]])
        self.assertEqual(replaced["index.json"], files["index.json"])

    def test_new_month_starts_a_new_file(self):
        files = self.history_through("2026-10-04")
        result = ph.append_day(files["index.json"], None, "2026-11-01", NOV_1)
        self.assertEqual(sorted(result), ["2026-11.json", "index.json"])
        self.assertEqual(json.loads(result["index.json"]),
                         {"version": 1, "months": ["2026-10", "2026-11"], "through": "2026-11-01"})
        self.assertEqual(json.loads(result["2026-11.json"])["days"], ["2026-11-01"])

    def test_past_days_never_change(self):
        files = self.history_through("2026-11-01")
        with self.assertRaisesRegex(ph.HistoryError, "past days never change"):
            ph.append_day(files["index.json"], files["2026-10.json"], "2026-10-04", OCT_4)
        with self.assertRaisesRegex(ph.HistoryError, "past days never change"):
            ph.append_day(files["index.json"], files["2026-10.json"], "2026-10-31", OCT_4)

    def test_inconsistent_files_are_refused(self):
        files = self.history_through("2026-10-04")
        with self.assertRaisesRegex(ph.HistoryError, "missing"):
            ph.append_day(files["index.json"], None, "2026-10-05", OCT_4)
        with self.assertRaisesRegex(ph.HistoryError, "not in the index"):
            ph.append_day(files["index.json"], files["2026-10.json"], "2026-11-01", NOV_1)
        stale_index = ph.render_index(ph.index_document(["2026-10"], "2026-10-02"))
        with self.assertRaisesRegex(ph.HistoryError, "ends on 2026-10-04"):
            ph.append_day(stale_index, files["2026-10.json"], "2026-10-05", OCT_4)
        with self.assertRaises(ph.HistoryError):
            ph.append_day("{}", files["2026-10.json"], "2026-10-05", OCT_4)
        with self.assertRaises(ph.HistoryError):
            ph.append_day(files["index.json"], files["2026-10.json"], "2026-10-05", [])

    def test_day_of_timestamp(self):
        self.assertEqual(ph.day_of_timestamp("2026-11-18T20:05:56Z"), "2026-11-18")
        self.assertEqual(ph.day_of_timestamp("2026-11-30T23:59:59Z"), "2026-11-30")
        for bad in ("2026-11-18T20:05:56+01:00", "2026-11-18", None):
            with self.subTest(bad=bad), self.assertRaises(ph.HistoryError):
                ph.day_of_timestamp(bad)


if __name__ == "__main__":
    unittest.main()
