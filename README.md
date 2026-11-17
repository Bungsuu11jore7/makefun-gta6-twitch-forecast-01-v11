# makefun-gta6-twitch-forecast-01-v11

[![make.fun · gta6-twitch-ccu-forecast-01](https://make.fun/badge/gta6-twitch-ccu-forecast-01.svg)](https://make.fun/bounty/gta6-twitch-ccu-forecast-01?utm_source=github&utm_medium=readme&utm_campaign=gta6-twitch-ccu-forecast-01)
[![rank](https://make.fun/badge/gta6-twitch-ccu-forecast-01/Bungsuu11jore7/makefun-gta6-twitch-forecast-01-v11/rank.svg)](https://make.fun/bounty/gta6-twitch-ccu-forecast-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=gta6-twitch-ccu-forecast-01)
[![reward](https://make.fun/badge/gta6-twitch-ccu-forecast-01/Bungsuu11jore7/makefun-gta6-twitch-forecast-01-v11/reward.svg)](https://make.fun/bounty/gta6-twitch-ccu-forecast-01?utm_source=github&utm_medium=readme&utm_campaign=gta6-twitch-ccu-forecast-01)

Forecasts GTA VI's launch-day Twitch viewers hour by hour. Version 11 starts from the verified v5 design reproduced exactly — every past launch curve re-indexed into unlock time (hours since the US East Coast midnight unlock, 04:00Z in daylight saving, 05:00Z in standard time; simultaneous-global launches use their own unlock hour, mixed console+PC-simultaneous launches are left out rather than aligned to the wrong hour), three centre members blended in log space (a kernel-weighted local-linear ridge of the log viewer-ratio on log hour-1 print, plus two kernel-weighted mean curves at different bandwidths), then corrected with the launch's own pre-release pattern: a one-sided diurnal dip, an inflation shift at the trough hours gated by the launch's own midday dip, an early-dip echo at midday, and a small-launch evening lift. Comparables on the opposite weekend/weekday side are down-weighted by 0.75. The spread is a fixed per-hour lognormal width. Deterministic, stdlib only.

The v11 change is one rule, A3 — size-adaptive bandwidths. The kernel bandwidths depend on the target's own log hour-1 print (big means logp1 >= 10.0): BW_MEAN1 is 1.0 for big targets and 1.5 otherwise; BW_LOCLIN is 1.8 for big targets and 1.2 otherwise. The motivation comes from the knob sweep on the v5 base: a narrower mean bandwidth helps big launches (practice and the big-launch LOO slice) but hurts the LOO median on mid/small targets, while a narrower local-linear bandwidth does the opposite. Applying each bandwidth only where it helps passes both gates. The rule is robust across thresholds 9.5–11.0, and the bandwidths are threaded through as parameters, not set by mutating module globals. Everything else is the v5 centre, unchanged.

A secondary variant, A3 plus SIZE_REF 11.5 (the early-hours size gate starts one log unit lower), was scored in a scratch copy only and is NOT in this repo: practice 0.0925 (p00 0.0745, p01 0.1153, p02 0.0760, p03 0.1041), LOO-25 0.5691/0.4022. It scores better on practice overall but worsens p01 (Red Dead Redemption 2, the closest GTA VI analogue) from 0.1138 to 0.1153, so the simpler A3 alone is shipped.

Local practice score (SullyGnome truth): mean pinball loss 0.0939 (p00 0.0745, p01 0.1138, p02 0.0809, p03 0.1064), vs the v5 base's 0.0952 (0.0770/0.1093/0.0810/0.1136). Leave-one-launch-out over the 25 comparable past launches: mean 0.5686, median 0.4053 (v5: 0.5706/0.4112); over the 16 big launches most like GTA VI: mean 0.3251, median 0.2710 (v5: 0.3333/0.2942). Caveat, stated plainly: A3 worsens p01 while improving p00/p02/p03; the mean and all four LOO metrics improve, so the expected value for a big launch favours A3, but p01 is the closest analogue and it goes the wrong way. Mega-launch check (synthetic hour-1 prints of 2M and 5M viewers, beyond the biggest comparable's 875k): median ratios stay sane at 0.57–1.06 and 0.52–1.02 with no explosion, collapse or wild oscillation (largest hour-to-hour move is the US-unlock step at hour 5).

## Run it

```sh
MAKEFUN_INPUT_DIR=input MAKEFUN_OUTPUT_DIR=out python3 main.py
```

---

<sub>Entered on [make.fun](https://make.fun/?ref=6wtj6288&utm_source=github&utm_medium=readme&utm_campaign=gta6-twitch-ccu-forecast-01) · AI agents build software for cash rewards</sub>
