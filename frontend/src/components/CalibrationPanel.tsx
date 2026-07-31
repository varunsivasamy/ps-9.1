import type { CalibrationEntry } from "../types";

interface CalibrationPanelProps {
  table: Record<string, CalibrationEntry>;
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
}

/** Matches MIN_SIGNALS_FOR_SHIFT in calibration.py — the bar a type must clear. */
const MIN_SIGNALS_FOR_SHIFT = 10;

function humanise(actionType: string): string {
  return actionType.replace(/_/g, " ");
}

/**
 * What the engine has learned from human decisions, per action_type.
 *
 * The counters move on every confirm and reject, but nothing shifts until the
 * net clears MIN_SIGNALS_FOR_SHIFT — so the useful thing to show is not the raw
 * numbers but the distance still to go. A progress meter answers "why has this
 * not changed yet?" in one glance, which the raw JSON does not.
 */
export function CalibrationPanel({ table, loading, error, onRefresh }: CalibrationPanelProps) {
  const entries = Object.entries(table);

  return (
    <div className="panel">
      <div className="panel__header">
        <h2>Adaptive calibration</h2>
        <button
          type="button"
          className="button button--ghost button--sm"
          onClick={onRefresh}
          disabled={loading}
        >
          {loading ? "…" : "Refresh"}
        </button>
      </div>
      <p className="panel__subtitle">
        Routing learned from past human decisions. A type needs a net{" "}
        {MIN_SIGNALS_FOR_SHIFT} signals before its band moves, and calibration can never
        override the blast-radius floor.
      </p>

      {error && <p className="form-error">{error}</p>}

      {!error && entries.length === 0 && (
        <p className="panel__empty">
          Nothing learned yet. Confirm or reject a few actions and the counters will
          appear here.
        </p>
      )}

      {entries.length > 0 && (
        <ul className="calibration-list">
          {entries.map(([actionType, entry]) => {
            const confirms = entry.confirms_without_modification;
            const rejects = entry.rejects_or_modifications;
            const net = confirms - rejects;
            const progress = Math.min(Math.abs(net) / MIN_SIGNALS_FOR_SHIFT, 1);
            const shifted = entry.band_offset !== 0;
            const direction = net > 0 ? "relaxing" : net < 0 ? "tightening" : "neutral";

            return (
              <li key={actionType} className="calibration-row">
                <div className="calibration-row__top">
                  <span className="calibration-row__name">{humanise(actionType)}</span>
                  {shifted ? (
                    <span
                      className="calibration-row__badge"
                      data-direction={entry.band_offset < 0 ? "down" : "up"}
                    >
                      band {entry.band_offset < 0 ? "−1" : "+1"}
                    </span>
                  ) : (
                    <span className="calibration-row__badge" data-direction="none">
                      no shift
                    </span>
                  )}
                </div>

                {/* Fill carries direction; the track is a lighter step of the
                    same ramp so the state reads across the whole bar. */}
                <div
                  className="meter"
                  role="progressbar"
                  aria-valuenow={Math.abs(net)}
                  aria-valuemin={0}
                  aria-valuemax={MIN_SIGNALS_FOR_SHIFT}
                  aria-label={`${humanise(actionType)} calibration progress`}
                >
                  <div
                    className="meter__fill"
                    data-direction={direction}
                    style={{ width: `${progress * 100}%` }}
                  />
                </div>

                <div className="calibration-row__meta">
                  <span className="calibration-row__stat" data-tone="good">
                    {confirms} confirmed
                  </span>
                  <span className="calibration-row__stat" data-tone="bad">
                    {rejects} rejected
                  </span>
                  <span className="calibration-row__stat">
                    {shifted
                      ? "threshold met"
                      : `${MIN_SIGNALS_FOR_SHIFT - Math.abs(net)} more to shift`}
                  </span>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
