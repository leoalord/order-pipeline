import { useEffect, useRef, useState } from "react";
import { Navigate } from "react-router-dom";

import { useFocusTrap } from "./focusTrap";
import {
  redriveWorkItem,
  stockLine,
  type LoadgenStatus,
  type MenuStock,
  type ParkedRow,
} from "./snapshot";

export type ScenarioId =
  | "ready"
  | "normal"
  | "rush"
  | "outage"
  | "worker_crash"
  | "courier_failure";

type LastPost = {
  path: string;
  status: number;
  body: string;
};

type PresenterRailProps = {
  activeScenario: ScenarioId;
  courierFaultActive: boolean;
  loadgen: LoadgenStatus | null;
  menuStock: MenuStock | null;
  parkedCourierJobs: ParkedRow[];
  readyOrders: number;
  onClose: () => void;
  onMutation: () => void;
  onScenarioChange: (scenario: ScenarioId) => void;
};

type CourierCapacity = {
  fleet_size: number;
  boot_fleet_size: number;
  min_fleet_size: number;
  max_fleet_size: number;
};

type KitchenCapacity = {
  kitchen_pans: number;
  boot_kitchen_pans: number;
  min_kitchen_pans: number;
  max_kitchen_pans: number;
};

async function post(
  path: string,
  body?: Record<string, string | number>,
): Promise<LastPost> {
  const init: RequestInit = { method: "POST" };
  if (body !== undefined) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  const response = await fetch(path, init);
  const text = await response.text();
  return { path, status: response.status, body: text };
}

export function scenarioLabel(scenario: ScenarioId): string {
  return {
    ready: "Ready",
    normal: "Normal",
    rush: "Rush",
    outage: "Outage",
    worker_crash: "Worker crash",
    courier_failure: "Courier failure",
  }[scenario];
}

export function PresenterRail({
  activeScenario,
  courierFaultActive,
  loadgen,
  menuStock,
  parkedCourierJobs,
  readyOrders,
  onClose,
  onMutation,
  onScenarioChange,
}: PresenterRailProps) {
  const [mult, setMult] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [last, setLast] = useState<LastPost | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [capacity, setCapacity] = useState<CourierCapacity | null>(null);
  const [capacityDraft, setCapacityDraft] = useState(8);
  const [kitchen, setKitchen] = useState<KitchenCapacity | null>(null);
  const [kitchenDraft, setKitchenDraft] = useState(20);
  const railRef = useRef<HTMLElement>(null);

  useFocusTrap(railRef, true);

  useEffect(() => {
    const controller = new AbortController();
    const loadCapacity = async () => {
      try {
        const [courierResponse, kitchenResponse] = await Promise.all([
          fetch("/csim/admin/capacity", { signal: controller.signal }),
          fetch("/rsim/admin/capacity", { signal: controller.signal }),
        ]);
        if (!courierResponse.ok) {
          throw new Error(`GET /csim/admin/capacity ${courierResponse.status}`);
        }
        if (!kitchenResponse.ok) {
          throw new Error(`GET /rsim/admin/capacity ${kitchenResponse.status}`);
        }
        const courierBody = (await courierResponse.json()) as CourierCapacity;
        const kitchenBody = (await kitchenResponse.json()) as KitchenCapacity;
        setCapacity(courierBody);
        setCapacityDraft(courierBody.fleet_size);
        setKitchen(kitchenBody);
        setKitchenDraft(kitchenBody.kitchen_pans);
      } catch (err) {
        if (!controller.signal.aborted) {
          setError(
            err instanceof Error ? err.message : "Capacity controls unavailable",
          );
        }
      }
    };
    void loadCapacity();
    return () => controller.abort();
  }, []);

  const run = async (
    path: string,
    body?: Record<string, string | number>,
    scenario?: ScenarioId,
  ) => {
    setBusy(path);
    setError(null);
    setNotice(null);
    try {
      const result = await post(path, body);
      setLast(result);
      if (result.status < 200 || result.status >= 300) {
        throw new Error(
          `${path} returned ${result.status}: ${result.body.slice(0, 160)}`,
        );
      }
      if (scenario) {
        onScenarioChange(scenario);
      }
      onMutation();
      if (scenario) {
        onClose();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Scenario action failed");
    } finally {
      setBusy(null);
    }
  };

  const rush = () => {
    const trimmed = mult.trim();
    if (trimmed === "") {
      void run("/loadgen/scenario/rush", undefined, "rush");
      return;
    }
    const value = Number(trimmed);
    if (!Number.isFinite(value) || value <= 0) {
      setError("Rush multiplier must be a number greater than 0.");
      return;
    }
    void run("/loadgen/scenario/rush", { mult: value }, "rush");
  };

  const applyKitchenCapacity = async () => {
    const path = "/rsim/admin/capacity";
    setBusy(path);
    setError(null);
    setNotice(null);
    try {
      const result = await post(path, { kitchen_pans: kitchenDraft });
      setLast(result);
      if (result.status < 200 || result.status >= 300) {
        throw new Error(
          `${path} returned ${result.status}: ${result.body.slice(0, 160)}`,
        );
      }
      const body = JSON.parse(result.body) as KitchenCapacity;
      setKitchen(body);
      setKitchenDraft(body.kitchen_pans);
      onMutation();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Capacity change failed");
    } finally {
      setBusy(null);
    }
  };

  const applyCourierCapacity = async () => {
    const path = "/csim/admin/capacity";
    setBusy(path);
    setError(null);
    setNotice(null);
    try {
      const result = await post(path, { fleet_size: capacityDraft });
      setLast(result);
      if (result.status < 200 || result.status >= 300) {
        throw new Error(
          `${path} returned ${result.status}: ${result.body.slice(0, 160)}`,
        );
      }
      const body = JSON.parse(result.body) as CourierCapacity;
      setCapacity(body);
      setCapacityDraft(body.fleet_size);
      onMutation();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Capacity change failed");
    } finally {
      setBusy(null);
    }
  };

  const redriveAllCourierJobs = async () => {
    const jobs = [...parkedCourierJobs];
    setBusy("redrive-courier");
    setError(null);
    setNotice(null);
    let succeeded = 0;
    let failed = 0;
    try {
      for (let start = 0; start < jobs.length; start += 8) {
        const outcomes = await Promise.allSettled(
          jobs.slice(start, start + 8).map((job) => redriveWorkItem(job.id)),
        );
        for (const outcome of outcomes) {
          if (outcome.status === "fulfilled") {
            succeeded += 1;
          } else {
            failed += 1;
          }
        }
      }
      if (succeeded === 0 && failed > 0) {
        setError(`No courier jobs were redriven; ${failed} failed.`);
      } else {
        setNotice(
          failed > 0
            ? `Redrove ${succeeded} courier jobs; ${failed} could not be redriven.`
            : `Redrove ${succeeded} parked courier jobs.`,
        );
      }
      onMutation();
    } finally {
      setBusy(null);
    }
  };

  const reset = async () => {
    const steps: Array<
      [string, Record<string, string | number> | undefined]
    > = [
      ["/loadgen/stop", undefined],
      ["/rsim/admin/faults", { mode: "clear" }],
      ["/csim/admin/faults", { mode: "clear" }],
      [
        "/rsim/admin/capacity",
        { kitchen_pans: kitchen?.boot_kitchen_pans ?? 20 },
      ],
      [
        "/csim/admin/capacity",
        { fleet_size: capacity?.boot_fleet_size ?? 8 },
      ],
      ["/rsim/admin/stock", { item: "burrito", count: 200 }],
      ["/loadgen/cohort/new", undefined],
    ];
    setBusy("reset");
    setError(null);
    setNotice(null);
    try {
      for (const [path, body] of steps) {
        const result = await post(path, body);
        setLast(result);
        if (result.status < 200 || result.status >= 300) {
          throw new Error(
            `${path} returned ${result.status}: ${result.body.slice(0, 160)}`,
          );
        }
      }
      onScenarioChange("ready");
      onMutation();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Reset failed");
    } finally {
      setBusy(null);
    }
  };

  const disabled = busy !== null;
  // Boot ships a conservative fallback H, so a positive H is not a measurement.
  // Normal walks fine on the fallback; Rush sizes its peak off H and needs a real one.
  const hasBaseline = (loadgen?.h ?? 0) > 0;
  const calibrated =
    hasBaseline && (loadgen?.calibrated ?? loadgen?.h_source === "calibrated");

  return (
    <aside
      className="side-panel presenter-rail"
      id="presenter-rail"
      aria-label="Presenter controls"
      role="dialog"
      aria-modal="true"
      tabIndex={-1}
      ref={railRef}
    >
      <div className="panel-heading">
        <div>
          <p className="eyebrow">Demo sequence</p>
          <h2>Presenter controls</h2>
        </div>
        <button
          className="hide-controls-button"
          type="button"
          onClick={onClose}
          aria-label="Hide presenter controls"
        >
          <span aria-hidden="true">‹</span>
          Hide controls
        </button>
      </div>

      <div className="rail-body">
      <p className="panel-summary">
        Active: <strong>{scenarioLabel(activeScenario)}</strong>. Actions use
        the existing scenario endpoints.
      </p>

      <section
        className={`calibration-card ${calibrated ? "ready" : "required"}`}
        aria-label="Capacity calibration"
      >
        <div>
          <span>Capacity baseline</span>
          <strong>
            {calibrated
              ? `Calibrated · H ${(loadgen?.h ?? 0).toFixed(2)} orders / sec`
              : `Fallback baseline · H ${(loadgen?.h ?? 0).toFixed(2)} · unverified`}
          </strong>
          <small>
            {calibrated
              ? "Recalibrate after changing the worker or dependency topology."
              : "Normal runs on the fallback. Rush needs a measured H. Calibrate finds the fastest rate that keeps up before kitchen, courier, or the door say busy."}
          </small>
        </div>
        <button
          type="button"
          disabled={disabled}
          onClick={() => void run("/loadgen/calibrate")}
        >
          {busy === "/loadgen/calibrate"
            ? "Calibrating…"
            : calibrated
              ? "Recalibrate"
              : "Calibrate first"}
        </button>
      </section>

      <div className="scenario-list">
        <section
          className={
            activeScenario === "normal"
              ? "scenario-card active"
              : "scenario-card"
          }
        >
          <span className="scenario-number">01</span>
          <div>
            <h3>Normal</h3>
            <p>Steady arrivals; follow a ticket through every lifecycle stage.</p>
            <button
              type="button"
              disabled={disabled || !hasBaseline}
              onClick={() =>
                void run("/loadgen/scenario/steady", undefined, "normal")
              }
            >
              Start normal
            </button>
          </div>
        </section>

        <section
          className={
            activeScenario === "rush"
              ? "scenario-card active warning"
              : "scenario-card warning"
          }
        >
          <span className="scenario-number">02</span>
          <div>
            <h3>Rush</h3>
            <p>Pressure rises, promises stretch, then the same pipeline drains.</p>
            <label className="mult">
              Multiplier
              <input
                value={mult}
                onChange={(event) => setMult(event.target.value)}
                placeholder="1.0"
                inputMode="decimal"
                disabled={disabled}
              />
            </label>
            <button
              type="button"
              disabled={disabled || !calibrated}
              onClick={rush}
            >
              Start rush
            </button>
            {!calibrated ? (
              <small className="scenario-warning">
                Calibrate first. On the fallback baseline the rush peak can sit
                below this host's capacity and produce no visible pressure.
              </small>
            ) : null}
          </div>
        </section>

        <section
          className={
            activeScenario === "outage"
              ? "scenario-card active danger"
              : "scenario-card danger"
          }
        >
          <span className="scenario-number">03</span>
          <div>
            <h3>Outage</h3>
            <p>
              Tag doomed confirms, then black out the Restaurant lane for 60s.
            </p>
            <div className="button-pair">
              <button
                type="button"
                disabled={disabled}
                onClick={() =>
                  void run("/loadgen/beat/doom-confirm", undefined, "outage")
                }
              >
                1 · Doom confirms
              </button>
              <button
                type="button"
                disabled={disabled}
                onClick={() =>
                  void run(
                    "/rsim/admin/faults",
                    { mode: "blackout", seconds: 60 },
                    "outage",
                  )
                }
              >
                2 · Blackout
              </button>
              <button
                className="secondary-button"
                type="button"
                disabled={disabled}
                onClick={() =>
                  void run("/rsim/admin/faults", { mode: "clear" })
                }
              >
                Recover restaurant
              </button>
            </div>
          </div>
        </section>

        <section
          className={
            activeScenario === "worker_crash"
              ? "scenario-card active danger"
              : "scenario-card danger"
          }
        >
          <span className="scenario-number">04</span>
          <div>
            <h3>Worker crash</h3>
            <p>
              Arm a readable lease, then stop the matching owner from the Docker
              terminal.
            </p>
            <button
              type="button"
              disabled={disabled}
              onClick={() =>
                void run(
                  "/rsim/admin/faults",
                  { mode: "blackout", seconds: 60 },
                  "worker_crash",
                )
              }
            >
              Arm visible lease
            </button>
          </div>
        </section>

        <section
          className={
            activeScenario === "courier_failure"
              ? "scenario-card active danger"
              : "scenario-card danger"
          }
        >
          <span className="scenario-number">05</span>
          <div>
            <h3>Courier failure</h3>
            <p>
              Exhaust dispatch, park the same work item, recover, then Redrive.
            </p>
            <button
              type="button"
              disabled={disabled}
              onClick={() =>
                void run(
                  "/csim/admin/faults",
                  { mode: "blackout", seconds: 30 },
                  "courier_failure",
                )
              }
            >
              Blackout courier · 30s
            </button>
            <button
              className="secondary-button"
              type="button"
              disabled={disabled}
              onClick={() =>
                void run("/csim/admin/faults", { mode: "clear" })
              }
            >
              Recover courier
            </button>
          </div>
        </section>
      </div>

      <section className="capacity-card kitchen" aria-label="Cooking capacity">
        <div className="capacity-heading">
          <div>
            <span>Cooking capacity</span>
            <strong>Meals at once · {kitchenDraft}</strong>
          </div>
          <output aria-live="polite">
            {kitchen ? `Live ${kitchen.kitchen_pans}` : "Connecting…"}
          </output>
        </div>
        <label htmlFor="kitchen-capacity">
          <span>Fewer</span>
          <input
            id="kitchen-capacity"
            type="range"
            min={kitchen?.min_kitchen_pans ?? 1}
            max={kitchen?.max_kitchen_pans ?? 64}
            step={1}
            value={kitchenDraft}
            aria-label="Meal cooking capacity"
            aria-valuetext={`${kitchenDraft} meals at once`}
            disabled={disabled || kitchen === null}
            onChange={(event) => setKitchenDraft(Number(event.target.value))}
          />
          <span>More</span>
        </label>
        <p>
          How many meals the kitchen can prepare at the same time. New
          confirms use the new limit; tickets already cooking keep their quote.
        </p>
        <div className="capacity-actions">
          <button
            type="button"
            disabled={
              disabled || kitchen === null || kitchenDraft === kitchen.kitchen_pans
            }
            onClick={() => void applyKitchenCapacity()}
          >
            {busy === "/rsim/admin/capacity"
              ? "Applying…"
              : `Apply ${kitchenDraft} meals at once`}
          </button>
        </div>
      </section>

      <section className="capacity-card" aria-label="Courier capacity">
        <div className="capacity-heading">
          <div>
            <span>Courier capacity</span>
            <strong>Fleet capacity · {capacityDraft} couriers</strong>
          </div>
          <output aria-live="polite">
            {capacity ? `Live ${capacity.fleet_size}` : "Connecting…"}
          </output>
        </div>
        <label htmlFor="courier-capacity">
          <span>Fewer</span>
          <input
            id="courier-capacity"
            type="range"
            min={capacity?.min_fleet_size ?? 4}
            max={capacity?.max_fleet_size ?? 32}
            step={1}
            value={capacityDraft}
            aria-label="Courier fleet capacity"
            aria-valuetext={`${capacityDraft} courier fleet capacity`}
            disabled={disabled || capacity === null}
            onChange={(event) => setCapacityDraft(Number(event.target.value))}
          />
          <span>More</span>
        </label>
        <div className="capacity-context">
          <span>{readyOrders} Ready orders</span>
          <span>{parkedCourierJobs.length} parked courier jobs</span>
        </div>
        <p>
          Scaling affects new dispatches. Redrive parked courier jobs after the
          Delivery service recovers.
        </p>
        <div className="capacity-actions">
          <button
            type="button"
            disabled={
              disabled || capacity === null || capacityDraft === capacity.fleet_size
            }
            onClick={() => void applyCourierCapacity()}
          >
            {busy === "/csim/admin/capacity"
              ? "Applying…"
              : `Apply ${capacityDraft} couriers`}
          </button>
          <button
            className="redrive-all-button"
            type="button"
            disabled={
              disabled || courierFaultActive || parkedCourierJobs.length === 0
            }
            onClick={() => void redriveAllCourierJobs()}
          >
            {busy === "redrive-courier"
              ? "Redriving courier jobs…"
              : `Redrive ${parkedCourierJobs.length} parked courier jobs`}
          </button>
        </div>
        {courierFaultActive ? (
          <small className="capacity-warning">
            Recover the courier service before redriving parked jobs.
          </small>
        ) : null}
      </section>

      <details className="rail-setup">
        <summary>Setup & bonus beats</summary>
        <p
          className={
            menuStock?.burrito === 0 ? "stock-line stock-empty" : "stock-line"
          }
        >
          Kitchen inventory ·{" "}
          {menuStock ? stockLine(menuStock) : "unavailable"}
        </p>
        <div className="button-pair">
          <button
            type="button"
            disabled={disabled}
            onClick={() => void run("/loadgen/cohort/new")}
          >
            New cohort
          </button>
          <button
            type="button"
            disabled={disabled}
            title="Rehearsal only — confirm is milliseconds, so a live click can land after being prepared and return 409. The pytest that holds confirm in-flight is the proof."
            onClick={() => void run("/loadgen/beat/cancel-race")}
          >
            Cancel race (rehearsal)
          </button>
          <button
            type="button"
            disabled={disabled}
            onClick={() =>
              void run("/rsim/admin/faults", { mode: "fail_void" })
            }
          >
            Fail void
          </button>
          <button
            type="button"
            disabled={disabled}
            onClick={() =>
              void run("/rsim/admin/stock", { item: "burrito", count: 0 })
            }
          >
            Out of stock
          </button>
          <button
            type="button"
            disabled={disabled}
            onClick={() =>
              void run("/loadgen/beat/place", { item: "burrito" })
            }
          >
            Place burrito
          </button>
          <button
            type="button"
            disabled={disabled}
            onClick={() =>
              void run("/rsim/admin/stock", {
                item: "burrito",
                count: 200,
              })
            }
          >
            Restore stock
          </button>
        </div>
        <p className="rail-note">
          Cancel race is a rehearsal: confirm completes in milliseconds, so a
          live click can land after being prepared, return 409, and increment
          invalid transitions. The in-flight pytest is the proof.
        </p>
      </details>
      </div>

      <div className="rail-footer">
        <button
          className="reset-button"
          type="button"
          disabled={disabled}
          onClick={() => void reset()}
        >
          Reset demo
        </button>
        <p
          className={error ? "action-status error" : "action-status"}
          role="status"
        >
          {busy
            ? `Running ${busy}…`
            : (error ??
              notice ??
              (last ? `${last.path} → ${last.status}` : "Ready for the next beat."))}
        </p>
      </div>
    </aside>
  );
}

/** Old bookmarks land on the unified presentation surface. */
export function ControlPage() {
  return <Navigate to="/" replace />;
}
