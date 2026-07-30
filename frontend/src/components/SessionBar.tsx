import type { HealthResponse } from "../types";

interface SessionBarProps {
  sessionId: string;
  onSessionIdChange: (value: string) => void;
  onNewSession: () => void;
  health: HealthResponse | null;
  healthError: string | null;
}

export function SessionBar({
  sessionId,
  onSessionIdChange,
  onNewSession,
  health,
  healthError,
}: SessionBarProps) {
  const healthLabel = healthError
    ? "API unreachable"
    : health
      ? `API ${health.status} · DynamoDB ${health.dynamodb}`
      : "Checking API…";

  const healthClass = healthError
    ? "health-badge health-badge--down"
    : health?.dynamodb === "reachable"
      ? "health-badge health-badge--up"
      : "health-badge health-badge--warn";

  return (
    <div className="session-bar">
      <div className="session-bar__field">
        <label htmlFor="session-id">Session ID</label>
        <div className="session-bar__input-row">
          <input
            id="session-id"
            type="text"
            value={sessionId}
            onChange={(e) => onSessionIdChange(e.target.value)}
            spellCheck={false}
          />
          <button type="button" className="button button--ghost" onClick={onNewSession}>
            New session
          </button>
        </div>
      </div>
      <span className={healthClass} title={healthError ?? undefined}>
        {healthLabel}
      </span>
    </div>
  );
}
