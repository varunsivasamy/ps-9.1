import { useEffect, useState } from "react";
import { getCalibration } from "../api";
import type { ApiError, CalibrationEntry } from "../types";

interface Props {
  refreshTick: number;
  intervalMs?: number;
}

interface State {
  data: Record<string, CalibrationEntry>;
  loadedAt: Date | null;
  error: string | null;
  loading: boolean;
}

export function CalibrationPanel({ refreshTick, intervalMs = 15000 }: Props) {
  const [state, setState] = useState<State>({
    data: {},
    loadedAt: null,
    error: null,
    loading: false,
  });

  useEffect(() => {
    let cancelled = false;

    function load() {
      setState((s) => ({ ...s, loading: true }));
      getCalibration()
        .then((res) => {
          if (cancelled) return;
          setState({ data: res.calibration, loadedAt: new Date(), error: null, loading: false });
        })
        .catch((err: ApiError) => {
          if (cancelled) return;
          setState((s) => ({ ...s, error: err.message, loading: false }));
        });
    }

    load();
    const id = window.setInterval(load, intervalMs);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [refreshTick, intervalMs]);

  const entries = Object.entries(state.data).sort(([a], [b]) => a.localeCompare(b));

  return (
    <div className="tui-panel tui-panel--calibration">
      <div className="tui-panel__title">
        <span>[4] CALIBRATION</span>
        <span className="tui-panel__meta">
          {state.loading ? "refreshing…" :
           state.error   ? "error" :
           entries.length === 0 ? "empty" :
                                  `${entries.length} action_type${entries.length === 1 ? "" : "s"}`}
        </span>
      </div>

      {state.error && (
        <div className="tui-calib__empty">// {state.error}</div>
      )}

      {!state.error && entries.length === 0 && (
        <div className="tui-calib__empty">
          No calibration signals yet. Confirm or reject actions to teach the router
          which action_types can relax over time and which need tighter control.
        </div>
      )}

      {!state.error && entries.length > 0 && (
        <div className="tui-calib">
          <table className="tui-calib__table">
            <thead>
              <tr>
                <th>ACTION_TYPE</th>
                <th className="tui-calib__num">✓</th>
                <th className="tui-calib__num">✗</th>
                <th className="tui-calib__off">OFFSET</th>
              </tr>
            </thead>
            <tbody>
              {entries.map(([k, v]) => (
                <tr key={k}>
                  <td className="tui-calib__type">{k}</td>
                  <td className="tui-calib__num">{v.confirms_without_modification}</td>
                  <td className="tui-calib__num">{v.rejects_or_modifications}</td>
                  <td className={`tui-calib__off ${offsetClass(v.band_offset)}`}>
                    {formatOffset(v.band_offset)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="tui-calib__foot">
        <span>// ✓ relaxes routing · ✗ tightens it</span>
        <span>{state.loadedAt ? `t:${state.loadedAt.toLocaleTimeString()}` : "—"}</span>
      </div>
    </div>
  );
}

function offsetClass(offset: number): string {
  if (offset > 0)  return "tui-calib__off--pos";
  if (offset < 0)  return "tui-calib__off--neg";
  return "tui-calib__off--zero";
}

function formatOffset(offset: number): string {
  if (offset === 0)     return "0";
  const sign = offset > 0 ? "+" : "";
  return `${sign}${offset.toFixed(1)}`;
}
