# riftdex-data

Daily market prices for Riftbound trading cards, published as one small JSON file for the Riftdex app.

## What this is

A scheduled job runs once a day, reads the Riftbound price files from [TCGCSV](https://tcgcsv.com) (a daily export of TCGplayer's catalogue), and writes `prices.json` to this repository. The Riftdex app downloads that one file at most once a day.

The job exists so that phones never contact TCGCSV directly: TCGCSV asks that requests not be made from user-facing applications, and that clients sync at most once every 24 hours.

## The file

`prices.json` is described in [docs/format.md](docs/format.md). In short: TCGplayer market prices in US dollars, keyed by TCGplayer product id, with a Normal and a Foil price where each exists.

Fetch it from:

```
https://raw.githubusercontent.com/ehreese/riftdex-data/main/prices.json
```

## How the job works

The workflow [`.github/workflows/prices.yml`](.github/workflows/prices.yml) runs at 20:40 UTC, shortly after TCGCSV's daily update at about 20:00 UTC, and again at 23:40 UTC as a catch-up. It can also be started by hand from the Actions tab ("Run workflow").

Each run:

1. Runs the unit tests.
2. Runs [`scripts/build_prices.py`](scripts/build_prices.py), which:
   - reads `https://tcgcsv.com/last-updated.txt` and stops, successfully and without writing anything, if it matches the `sourceUpdatedAt` of the published `prices.json`;
   - otherwise fetches the list of Riftbound groups (TCGplayer category 89) and then each group's price file, one request at a time, at least 150 ms apart, with a 30 second timeout and a User-Agent naming this repository;
   - builds the rows described in [docs/format.md](docs/format.md) and writes `prices.json` atomically (a temporary file renamed into place).
3. If `prices.json` changed, commits it to `main` as `github-actions[bot]` with the message `Prices as of <sourceUpdatedAt>`. If nothing changed, the run ends green with no commit.

A full run makes 2 requests plus one per group (17 in November 2026). A run on a day TCGCSV has not updated makes 1.

The workflow has `contents: write` permission and nothing else, uses no secrets, times out after 15 minutes, and never runs twice at once. A manual run started on a branch other than `main` builds and tests but does not commit.

## Safety checks

The script stops with a non-zero exit, leaves the published `prices.json` exactly as it was, and so turns the workflow run red, if:

- any request fails after one polite retry (the retry happens only for network errors, timeouts, HTTP 429 and 5xx, after a 5 second pause);
- a response is not valid JSON, has `success` other than `true`, or arrives brotli-encoded (only gzip is requested and supported);
- `last-updated.txt` is not a timestamp with a time zone;
- the group list is empty, or any group's price file has no results;
- a price row has an unexpected shape: a missing or non-integer `productId`, a `subTypeName` other than `Normal` or `Foil`, or a `marketPrice` that is neither null nor a non-negative number;
- the same product has two different prices for the same finish (after rounding to cents), for example in two groups;
- no product has a price at all;
- the new file would have fewer than half the rows of the published one;
- the published `prices.json` is not readable as a prices file.

## Running it locally

Python 3.11 or later, standard library only. From the repository root:

```sh
# Unit tests. No network.
python3 -I -m unittest discover -s tests

# Build prices.json from TCGCSV. Does nothing if TCGCSV has not updated.
python3 -I scripts/build_prices.py

# Same, and keep every raw response to inspect or replay later.
python3 -I scripts/build_prices.py --save-raw .cache/raw

# Rebuild from saved responses without touching the network.
python3 -I scripts/build_prices.py --offline .cache/raw --force
```

Other options: `--force` rebuilds even when TCGCSV has not updated, `--allow-shrink` accepts a file with fewer than half the rows of the existing one, and `--output PATH` writes somewhere other than `prices.json`. `.cache/` is ignored by git.

Please run against the network sparingly: TCGCSV is a one-person hobby project and the scheduled job already fetches everything once a day. Use `--save-raw` once and `--offline` after that.

## When a run fails

A failed run publishes nothing, so the app keeps the previous prices and shows them as stale once they are 36 hours old. There is no emergency, but look before the next day's run.

1. Open the failed run in the Actions tab and read the `Build prices.json` step. Its last lines start with `error:` and say what was wrong.
2. Decide from the message:
   - **A request failed, timed out or returned bad JSON.** Usually TCGCSV or the network had a bad moment. Let the catch-up run try, or start the workflow by hand once, later. Do not start it repeatedly.
   - **A group's price file has no results.** Look at that group on [tcgcsv.com](https://tcgcsv.com). If TCGCSV published an incomplete update, its next update will fix it. If an empty group is now normal (for example a newly listed set with no products yet), the script needs a change, through a pull request.
   - **Two different prices for one product, or an unknown `subTypeName`.** TCGCSV's data changed shape. Look at the product named in the message. The script needs a change, and if the file the app reads has to change too, so do [docs/format.md](docs/format.md) and its `version`.
   - **Fewer than half the rows.** Check whether TCGCSV really lost products. If the drop is genuine, run the script locally with `--allow-shrink`, check the result, and land the new `prices.json` through a pull request. The workflow never passes `--allow-shrink`.
   - **The unit tests failed.** Something merged to `main` broke them. Fix it through a pull request.
   - **The push to `main` failed.** Another commit landed on `main` during the run. The next run will publish normally.
3. To reproduce a failure locally, run `python3 -I scripts/build_prices.py --save-raw .cache/raw` once, then iterate with `--offline .cache/raw --force`.

## What this is not

- No card images or card art.
- No application code.
- Not a general-purpose API. The format can change; consumers should check its `version`.

## Credits and legal

- Prices come from TCGplayer by way of TCGCSV. This project is not affiliated with either.
- Riftdex is an unofficial fan project. It is not endorsed by Riot Games and does not reflect the views or opinions of Riot Games or anyone officially involved in producing or managing Riot Games properties. Riftbound and League of Legends are trademarks or registered trademarks of Riot Games, Inc.
