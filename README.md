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

## What this is not

- No card images or card art.
- No application code.
- Not a general-purpose API. The format can change; consumers should check its `version`.

## Credits and legal

- Prices come from TCGplayer by way of TCGCSV. This project is not affiliated with either.
- Riftdex is an unofficial fan project. It is not endorsed by Riot Games and does not reflect the views or opinions of Riot Games or anyone officially involved in producing or managing Riot Games properties. Riftbound and League of Legends are trademarks or registered trademarks of Riot Games, Inc.
