This project researches one MOEX share and trades virtual funds through the T-Invest sandbox only.
Read README.md before changes. There must be no production order endpoint or production trading mode.
Use Python standard library, Decimal for money, timezone-aware candles, completed bars, and no future data in signals.
Backtests execute a close signal at the next available open; include both-side costs and lot rounding.
Sandbox submissions must be opt-in, reconcile uncertain orders, and persist state before network submission.
Validate with `python -m unittest discover -s tests -v` and the README demo command.
