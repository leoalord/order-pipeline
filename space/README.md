---
title: Order Pipeline
emoji: 🍜
colorFrom: yellow
colorTo: red
sdk: docker
app_port: 7860
suggested_hardware: cpu-upgrade
---

# Order Pipeline

Live demo of the food-delivery order pipeline. The board is the whole app:
**Presenter controls** in the header runs Calibrate, Steady, Rush, and the
fault beats.

This Space is **one container** (Postgres + API + two sims + two workers +
loadgen + the dashboard). Hugging Face does not run `docker compose`.

## Demo

1. Wait for the Space to finish building (cold start after sleep rebuilds Postgres).
2. Open **Presenter controls**.
3. **Calibrate**, then **New cohort**, then **01 Normal**.
4. Watch one ticket walk placed → delivered. Then Rush / Outage / Courier failure.

Worker crash (`docker kill`) is a local-Compose beat. There is no Docker socket
here — skip that card, or use **05 Courier failure** + **Redrive** instead.

Use **CPU Upgrade** (or larger). Free CPU Basic will likely thrash under
calibrate / rush.
