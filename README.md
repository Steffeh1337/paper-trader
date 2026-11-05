# paper-trader

A paper-trading simulation of the [day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill)
(MIT, copied into `.claude/skills/`), funded with EUR 200 a month and compared with putting
the same money into SPY. Results: [`sim/RESULTS.md`](sim/RESULTS.md).

Each trading day after the US close, `python3 sim/daily_alpaca.py` replays the day on Alpaca 5-minute bars with the skill.s Time Machine and the same engine as the backtest (`sim/engine.py`, rules in `sim/rules.json`)
(clock pinned, no peeking at later prices): opening-range breakouts at 9:50 ET, ranked by the Time
Machine, 2R target, 5% of the account at risk per trade, max 3 trades, flat by 15:55.
Fills are simulated from real 5-minute bars. No broker, paper only, not financial advice.
