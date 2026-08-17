import { useState } from "react";

type LastPost = {
  path: string;
  status: number;
  body: string;
};

async function postLoadgen(
  path: string,
  body?: Record<string, number>,
): Promise<LastPost> {
  const init: RequestInit = { method: "POST" };
  if (body !== undefined) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  const response = await fetch(`/loadgen${path}`, init);
  const text = await response.text();
  return { path, status: response.status, body: text };
}

export function ControlPage() {
  const [mult, setMult] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [last, setLast] = useState<LastPost | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = async (path: string, body?: Record<string, number>) => {
    setBusy(path);
    setError(null);
    try {
      const result = await postLoadgen(path, body);
      setLast(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : "loadgen POST failed");
    } finally {
      setBusy(null);
    }
  };

  const rush = () => {
    const trimmed = mult.trim();
    if (trimmed === "") {
      void run("/scenario/rush");
      return;
    }
    const value = Number(trimmed);
    if (!Number.isFinite(value) || value <= 0) {
      setError("mult must be a number > 0");
      return;
    }
    void run("/scenario/rush", { mult: value });
  };

  return (
    <main className="page">
      <h1>Control</h1>
      <p className="lede">
        Click a beat, then watch the other tab. This page does not share React
        state with Watch — each polls on its own. Curls to :8090 are the same
        endpoints.
      </p>
      {error ? <p className="error">{error}</p> : null}

      <section className="pane">
        <h2>Pre-demo</h2>
        <p className="pane-intro">
          Calibrate H on this machine (mix stays on). New cohort resets
          snapshot counts; H is kept.
        </p>
        <div className="control-row">
          <button
            type="button"
            disabled={busy !== null}
            onClick={() => void run("/calibrate")}
          >
            Calibrate
          </button>
          <button
            type="button"
            disabled={busy !== null}
            onClick={() => void run("/cohort/new")}
          >
            New cohort
          </button>
        </div>
      </section>

      <section className="pane">
        <h2>Load</h2>
        <p className="pane-intro">
          Steady is 0.4×H. Rush is 60s at 1.5×H×mult, then drain back to
          0.4×H — it does not replay a baseline minute. Stop ends arrivals.
        </p>
        <div className="control-row">
          <button
            type="button"
            disabled={busy !== null}
            onClick={() => void run("/scenario/steady")}
          >
            Steady
          </button>
          <label className="mult">
            mult
            <input
              value={mult}
              onChange={(event) => setMult(event.target.value)}
              placeholder="optional"
              inputMode="decimal"
              spellCheck={false}
              autoComplete="off"
              disabled={busy !== null}
            />
          </label>
          <button type="button" disabled={busy !== null} onClick={rush}>
            Rush
          </button>
          <button
            type="button"
            disabled={busy !== null}
            onClick={() => void run("/stop")}
          >
            Stop
          </button>
        </div>
      </section>

      <p className="hint last-post">
        {busy ? `posting ${busy}…` : null}
        {!busy && last
          ? `${last.path} → ${last.status} ${last.body.slice(0, 400)}`
          : null}
        {!busy && !last ? "Last POST status shows here." : null}
      </p>
    </main>
  );
}
