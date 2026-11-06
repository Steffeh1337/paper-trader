# Trading Desk (local live dashboard)

A dashboard that runs on your own computer, for manual day trading with the
opening-range breakout rule (variant B):

- **Watchlist:** live prices (refreshed every 5 seconds), each stock's opening range,
  breakout status (waiting, in trade, target hit, stopped out), and the entry, stop
  and 2R target worked out for you.
- **5-minute chart:** today's candles with the range, entry, stop and target drawn in.
- **Earnings today:** companies to avoid on their earnings day.
- **Biggest movers:** among the 600 liquid stocks.
- **News:** headlines for your watchlist.
- **Paper trader:** the automatic simulation's latest results.

Information only. It never places orders. Data: Alpaca's free IEX feed (US stocks only).

## One-time setup on Windows

Open **PowerShell** (Start menu, type "PowerShell"). Run each command, accepting the defaults.

1. Install Python and Git:
   ```
   winget install -e --id Python.Python.3.12
   winget install -e --id Git.Git
   ```
   Close and reopen PowerShell afterwards.

2. Download the repo (skip if you already have it):
   ```
   New-Item -ItemType Directory -Force D:\DayTrading
   cd D:\DayTrading
   git clone https://github.com/Steffeh1337/paper-trader.git
   ```

3. Store your Alpaca **paper** keys on your PC (type them yourself; never share them):
   ```
   setx APCA_API_KEY_ID "your-key-id"
   setx APCA_API_SECRET_KEY "your-secret-key"
   ```
   Close and reopen PowerShell so they take effect.

## Every day

Double-click `D:\DayTrading\paper-trader\local\start.bat`. It fetches the latest
paper-trader results and opens http://localhost:8050 in your browser. Keep the black
window open while you use the dashboard; close it to stop.

Or from PowerShell:
```
cd D:\DayTrading\paper-trader
py local\app.py --open
```

## Using it

- Type your watchlist in the box (comma separated, US tickers) and press **Update list**.
  It is remembered in your browser.
- Click a row to see its chart.
- Times: the opening range is 9:30-9:45 New York = 16:30-16:45 in Romania; the market
  closes at 16:00 New York = 23:00 in Romania.
- If SPY is below its open, the header says so: the routine says no new trades.
