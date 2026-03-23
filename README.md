# btc-signal-bot

A beginner-friendly FastAPI app for Railway that watches Kalshi's public **KXBTC15M** Bitcoin 15-minute markets and publishes simple trading **signals only**.

> This project does **not** place trades. It only reads public market data, keeps a small in-memory state, and shows what the bot would do.

## What this bot does

- Polls Kalshi public market data for the `KXBTC15M` series.
- Picks the open market that is closing soonest while still leaving enough time to act.
- Reads the public orderbook.
- Estimates YES and NO buy prices using the reciprocal orderbook idea.
- Tracks a simple in-memory position for demo signal logic.
- Returns one of these actions:
  - `BUY_YES`
  - `BUY_NO`
  - `HOLD`
  - `EXIT`
  - `SKIP`

Because the state is stored in memory, restarting the Railway app resets the current demo position and recent signal history.

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

## How to open `/` and `/status`

After Railway deploys the app, you will get a public URL that looks something like this:

```text
https://your-app-name.up.railway.app
```

Then open:

- Home page: `https://your-app-name.up.railway.app/`
- Status page: `https://your-app-name.up.railway.app/status`

On an iPad, you can paste those URLs directly into Safari.

## How to confirm the app is working

Open `/` first. You should see JSON with items such as:

- app name
- current status
- watched series ticker
- last signal
- current market ticker

Then open `/status`. You should see more details, including:

- current config values
- last poll time
- current market snapshot
- current demo position if one exists
- recent signals kept in memory

If you see a `last_error` value, the bot is running but had trouble reading the API or parsing market data. In that case, wait for the next poll or review the Railway logs.

## Notes about the signal logic

This app is intentionally simple and readable:

- It only uses public Kalshi data.
- It does not use API keys.
- It does not sign requests.
- It does not place orders.
- It keeps only recent signals in memory.
- It exits demo positions with take profit, stop loss, or a forced exit before close.

## Next step for future demo trading

A good next step is to add a separate **paper trading** mode that writes fake fills to a small database or log file so you can review performance over time without touching real money.

After that, you could add charts, richer signal rules, or notifications before thinking about any private trading integration.
