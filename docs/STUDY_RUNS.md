# Study run order (METHODS §8, variant B: DeepSeek V4.1 Flash only)

Seed 20261026. Run in this order. Each run: `uv run agentbisect record --task tasks/<task>.yaml --model flash`,
then a **new** Alan Code conversation on the printed `workdir` folder. If a run must be redone, redo it at once.

| # | task | run folder | status |
|---|---|---|---|
| 1 | t10 | runs/t10-flash-20261026-132009 | done (green, 10 steps); first try resumed old chat, set aside |
| 2 | t06 | runs/t06-flash-20261026-132352 | done (green, 18 steps) |
| 3 | t08 | runs/t08-flash-20261026-133042 | done (green, 18 steps) |
| 4 | t04 | | |
| 5 | s03 (sabotage) | runs/s03-flash-20261026-135147 | prepared (moved ahead of run 4 to get a broken trace early) |
| 6 | t05 | | |
| 7 | t07 | | |
| 8 | t01 | runs/t01-flash-20261026-095102 | done (green) |
| 9 | t02 | | |
| 10 | s02 (sabotage) | | |
| 11 | t09 | | |
| 12 | t07 (2nd) | | |
| 13 | t08 (2nd) | | |
| 14 | t03 | | |
| 15 | s01 (sabotage) | | |
| 16 | t02 (2nd) | | |
| 17 | t09 (2nd) | | |
| 18 | t05 (2nd) | | |
| 19 | t04 (2nd) | | |
