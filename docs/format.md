# `prices.json` format

Version 1. This document is the contract between the daily job and the Riftdex app.

## Example

```json
{
  "version": 1,
  "currency": "USD",
  "source": "TCGplayer market prices via TCGCSV",
  "sourceUpdatedAt": "2026-11-16T20:05:58Z",
  "generatedAt": "2026-11-16T20:31:07Z",
  "prices": [
    [653002, null, 0.33],
    [653069, 0.25, 3.74]
  ]
}
```

## Fields

| Field | Meaning |
|---|---|
| `version` | Integer. Changes whenever the shape of this file changes. A consumer that does not know the version must ignore the file. |
| `currency` | Always `"USD"`. |
| `source` | Human-readable credit. |
| `sourceUpdatedAt` | When TCGCSV last updated its data, from its `last-updated.txt`, as a UTC timestamp. This is the "prices are from" date. |
| `generatedAt` | When the job wrote this file, UTC. |
| `prices` | One row per TCGplayer product that has at least one market price. |

## Price rows

Each row is `[productId, normal, foil]`:

- `productId` — TCGplayer product id (integer). The same id the Riftdex catalogue uses for a printing.
- `normal` — market price of the Normal finish, or `null` if there is none.
- `foil` — market price of the Foil finish, or `null` if there is none.

Rules:

- Prices are decimal numbers in dollars with at most two decimal places.
- Rows are sorted by `productId`, ascending. A product appears at most once.
- A product with no market price for either finish has no row.
- Every product in TCGCSV's Riftbound price files is included, sealed products too. Consumers ignore ids they do not know.
- The file is written deterministically: unchanged source data produces a byte-identical `prices` array.

## For consumers

- Fetch at most once a day, with `If-None-Match` so an unchanged file costs almost nothing.
- Treat prices as stale when `sourceUpdatedAt` is more than 36 hours old.
- Never fall back to contacting TCGCSV directly.
