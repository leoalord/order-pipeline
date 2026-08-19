# Stale — do not cite these as current baselines

The five `*-1440x810.jpg` files in this directory were captured before the
presentation-layer corrections landed. They show UI that no longer exists:
numbered stage headings, the retired two-tab `/control` copy, `Ready · H 0.25`
on the calibration card, a `courier 5xx` headline on the courier-failure beat,
and health chips driven by error counts rather than armed fault state.

Recapture all five against HEAD **after** the presentation-sizing pass (type
floor and rail scroll container), so the images are not invalidated a second
time by the same work.

To recapture, at 1440×810 with the stack up (`docker compose up -d`):

1. Presenter controls → **Reset demo**, then **Calibrate** (Rush stays disabled
   until this finishes — the fallback H is not a measurement).
2. **01 Normal** — wait for the focused ticket to reach Ready, then capture
   `normal-1440x810.jpg`.
3. **02 Rush** — capture while backlog and ETA stretch are climbing:
   `rush-1440x810.jpg`.
4. **03 Outage** — `1 · Doom confirms`, then `2 · Blackout`; capture while the
   Restaurant chip shows its countdown: `outage-1440x810.jpg`.
5. **04 Worker crash** — `Arm visible lease`, read the owner from the Workers
   drawer, `docker kill` that worker, capture `worker-crash-1440x810.jpg`, then
   `Recover restaurant` and `docker compose up -d worker`.
6. **05 Courier failure** — `Blackout courier · 30s`; capture with parked
   dispatch rows and their disabled Redrive buttons visible:
   `courier-failure-1440x810.jpg`.
7. Presenter controls → **Reset demo**.

Delete this file once the images are current.
