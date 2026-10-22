# Kalshi Picks Bot

A Python command-line tool that scans open [Kalshi](https://kalshi.com) prediction markets, pulls recent news for each one, asks Claude to estimate the real probability, and prints a short list of suggested trades: single picks, 2 to 3 leg combos, or nothing at all when there's no edge.

> **Research tool, not financial advice.** It never places orders. Prediction markets already price in most public news, so expect "no recommended trades" often. That's the bot being honest, not broken.

## How it works

```
Kalshi public API ──► filter to liquid markets closing soon (5%–95% priced)
                          │
Google News RSS ────► attach recent headlines to each market
                          │
Claude API ─────────► estimate P(YES) + confidence + reasoning for each market
                          │
picks.py ───────────► edge = bot's win chance − price − Kalshi fee
                      keep picks above --min-edge, rank them, build combos
```

| File | What it does |
|---|---|
| `main.py` | Command-line entry point and output formatting |
| `kalshi_bot/kalshi.py` | Fetches and filters markets from Kalshi's public API (no account needed) |
| `kalshi_bot/news.py` | Gets recent headlines from Google News RSS (free, no key) |
| `kalshi_bot/analyst.py` | Sends markets + news to Claude and gets structured estimates back |
| `kalshi_bot/picks.py` | Edge math, fees, ranking, combos |
| `tests/test_picks.py` | Unit tests that run offline against `sample_data/events.json` |

## Setup (VS Code, first time)

You need Python 3.10 or newer. Check with `python --version` (on Mac it may be `python3`).

1. Put the `kalshi-bot` folder somewhere on your computer and open it in VS Code (**File → Open Folder**).
2. Open the terminal in VS Code (**Terminal → New Terminal**) and create a virtual environment so the packages stay inside this project:

   ```bash
   # Mac / Linux
   python3 -m venv .venv
   source .venv/bin/activate

   # Windows (PowerShell)
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   ```

   You'll see `(.venv)` at the start of your terminal prompt when it's active. Activate it again each time you open a new terminal.
3. Install the packages:

   ```bash
   pip install -r requirements.txt
   ```
4. Get a Claude API key at [console.anthropic.com](https://console.anthropic.com/) (it's pay-as-you-go; one run of 12 markets is a few cents).
5. Copy `.env.example` to a new file named `.env` and paste your key in:

   ```
   ANTHROPIC_API_KEY=sk-ant-...
   ```
   `.env` is in `.gitignore`, so your key won't end up on GitHub.

## Running it

```bash
python main.py --sample --no-ai   # check your setup: offline sample data, no API calls
python -m pytest                  # run the tests
python main.py --no-ai            # live Kalshi markets + news, no Claude call (free)
python main.py                    # the full thing
```

Useful options:

```bash
python main.py --category economics      # only one category (economics, sports, politics, crypto...)
python main.py --markets 8 --days 7      # fewer markets, closing within a week
python main.py --min-edge 0.03           # lower the bar (more picks, less conviction)
python main.py --save                    # also write results to output/picks-<time>.json
python main.py --help                    # everything else
```

Example output (from the sample data with made-up estimates):

```
SINGLE TRADES

1. BUY YES @ $0.64  |  Fed decision in October 2026: Cut 25bps
   Market says 63% YES, bot says 78% YES (high confidence)
   Expected profit per contract: $+0.12 after fees  |  pays 56% if right
   Why: Recent Fed speakers signaled a cut; market lags.

COMBOS

1. 2-leg combo  |  est. win chance 56%, est. cost $0.38 per $1 payout
   ...
```

## Reading the output

- **Edge** is the expected profit on one $1 contract: the bot's chance of winning minus what you pay minus Kalshi's taker fee (7% × P × (1−P), rounded up per contract). An edge of $0.05 means "if the bot's estimate is right, you'd make 5 cents per contract on average."
- **Low-confidence** picks must clear twice the edge bar, and they're left out of combos.
- **Combos** multiply the legs' odds, which assumes the outcomes are independent. Related markets (a Fed cut and a low CPI print) usually aren't, so treat combo numbers as rough, and check Kalshi's real combo price.

## Pushing it to GitHub

```bash
git init
git add .
git commit -m "Kalshi picks bot v1"
```
Then create an empty repo on github.com (no README), and run the two `git remote add origin ...` / `git push -u origin main` lines GitHub shows you. Double-check `.env` is **not** in `git status` before your first commit.

## Ideas for v2

- Let Claude search the web itself (Claude's web search tool) instead of relying on RSS headlines.
- Track every pick in a CSV and score it after the market settles, so you can measure whether the bot actually beats the market (calibration / Brier score). This is the most valuable addition for a portfolio.
- Run it on a schedule and send picks to Discord or email.
- Paper-trade mode, then (much later, carefully) order placement via Kalshi's authenticated API.

## Notes

- Kalshi's market data endpoints are public; this project only reads them. API docs: [docs.kalshi.com](https://docs.kalshi.com).
- The model defaults to `claude-opus-5-5` at `medium` effort. Change `CLAUDE_MODEL` / `CLAUDE_EFFORT` in `.env` (e.g. `claude-sonnet-5-5` is cheaper).
- Market links go to `kalshi.com/markets/<event>`; if one doesn't open the right page, search the ticker on Kalshi.
