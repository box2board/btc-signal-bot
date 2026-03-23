# btc-signal-bot

A beginner-friendly FastAPI app for Railway that watches Kalshi's public **KXBTC15M** Bitcoin 15-minute markets and publishes simple trading **signals only**.

> This project does **not** place live trades. It only reads public market data, keeps small in-memory state, and now includes a paper trading layer that simulates entries and exits.

## What this bot does

- Polls Kalshi public market data for the `KXBTC15M` series.
- Picks the open market that is closing soonest while still leaving enough time to act.
- Reads the public orderbook using public market data only.
- Parses sparse or oddly-shaped orderbook payloads more defensively.
- Logs a safe summary of the top of book so skipped signals are easier to debug.
- Produces clearer skip reasons such as:
  - `missing orderbook`
  - `missing yes side`
  - `missing no side`
  - `no usable bid levels`
  - `synthetic price invalid`
  - `spread too wide`
  - `too close to market close`
  - `no entry in range`
- Simulates paper entries for `BUY_YES` and `BUY_NO` signals.
- Simulates paper exits using the existing take profit, stop loss, and force-exit rules.
- Tracks in-memory paper trade stats and the last 10 closed paper trades.
- Returns one of these actions:
  - `BUY_YES`
  - `BUY_NO`
  - `HOLD`
  - `EXIT`
  - `SKIP`

Because the state is stored only in memory, restarting the Railway app resets the open paper position, paper stats, and recent signal history.

## Required files in this repo

Your repo should contain these setup files:

- `app.py`
- `requirements.txt`
- `railway.json`
- `README.md`
- `.gitignore`

## Repo structure

```text
app.py          FastAPI app and background polling bot
requirements.txt Python packages Railway installs
railway.json    Railway start command
README.md       Setup guide for beginners
.gitignore      Python and local environment ignores
```

## Railway deployment steps

These steps are written for someone using GitHub and Railway on an iPad:

1. Push this repository to GitHub.
2. Open [Railway](https://railway.app/) in Safari.
3. Sign in and choose **New Project**.
4. Choose **Deploy from GitHub repo**.
5. Select your `btc-signal-bot` repository.
6. Railway should detect the Python project automatically.
7. Before the first deploy finishes, open your project and go to **Variables**.
8. Add the environment variables listed below.
9. Redeploy if Railway asks you to.
10. After deploy finishes, open the generated Railway public URL.

Railway uses `railway.json` to start the app with Uvicorn.

## Environment variables to add in Railway

Add these exact variable names in Railway.

| Variable | Example value | What it means |
| --- | --- | --- |
| `SERIES_TICKER` | `KXBTC15M` | Kalshi series to watch |
| `POLL_SECONDS` | `10` | How often the background bot polls |
| `ENTRY_MIN` | `35` | Lowest entry price allowed |
| `ENTRY_MAX` | `65` | Highest entry price allowed |
| `TAKE_PROFIT_PCT` | `8` | Exit if unrealized gain reaches this percent |
| `STOP_LOSS_PCT` | `5` | Exit if unrealized loss reaches this percent |
| `MIN_SECONDS_LEFT` | `180` | Minimum time left before a new entry is allowed |
| `FORCE_EXIT_SECONDS` | `45` | Exit before market close when this little time remains |
| `MAX_SPREAD` | `8` | Maximum synthetic spread allowed |

A simple starter set is:

```text
SERIES_TICKER=KXBTC15M
POLL_SECONDS=10
ENTRY_MIN=35
ENTRY_MAX=65
TAKE_PROFIT_PCT=8
STOP_LOSS_PCT=5
MIN_SECONDS_LEFT=180
FORCE_EXIT_SECONDS=45
MAX_SPREAD=8
```

## Endpoints

After Railway deploys the app, you will get a public URL that looks something like this:

```text
https://your-app-name.up.railway.app
```

Then open:

- Home page: `https://your-app-name.up.railway.app/`
- Status page: `https://your-app-name.up.railway.app/status`
- Paper trading page: `https://your-app-name.up.railway.app/paper`

On an iPad, you can paste those URLs directly into Safari.

## What `/status` now includes

`/status` still keeps the main runtime information, but now also includes:

- `paper_trading_enabled`
- `paper_stats`
- `open_paper_position`
- `recent_closed_paper_trades`
- `last_skip_reason`
- `last_diagnostics`
- `market_snapshot.orderbook_diagnostics`

The diagnostics are designed to stay readable while still showing the important top-of-book structure and why a signal was skipped.

## What `/paper` returns

`/paper` is a cleaner endpoint that returns only paper-trading information:

- whether paper trading is enabled
- cumulative in-memory paper stats
- the currently open paper position, if any
- the last 10 closed paper trades

This keeps paper results easy to inspect without the rest of the runtime bot status.

## Notes about the signal logic

This app is intentionally simple and readable:

- It only uses public Kalshi data.
- It does not use API keys.
- It does not sign requests.
- It does not place live orders.
- It does not do authenticated order placement.
- It keeps only recent data in memory.
- It exits paper positions with take profit, stop loss, or a forced exit before close.

## Important limitation of Railway restarts

Paper trading state is **in memory only**.

That means a Railway restart, redeploy, or crash will clear:

- the open paper position
- cumulative paper stats
- recent closed paper trades
- recent signal history

If you want longer-lived paper tracking later, the next step would be adding a small database or a file-backed log.

## Assumptions behind this version

- Kalshi's public orderbook can sometimes be sparse, partially missing, or shaped as either dictionaries or price/size arrays.
- Paper P/L is tracked in simple contract-price dollars and percentages so the bot stays beginner-friendly.
- The app should remain signal-only for now, with no live trading and no authenticated private API flow.
