import { Bell, Clock, Search, Wifi } from "lucide-react";
import { useEffect, useState } from "react";
import type { HealthResponse } from "../types";

interface TopHeaderProps {
  health: HealthResponse | null;
  healthError: string | null;
  sessionId: string;
  onSessionIdChange: (v: string) => void;
  onNewSession: () => void;
}

export function TopHeader({
  health,
  healthError,
  sessionId,
  onNewSession,
}: TopHeaderProps) {
  const [time, setTime] = useState(new Date());
  const [query, setQuery] = useState("");

  useEffect(() => {
    const t = setInterval(() => setTime(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

  const apiOk = !healthError && health?.dynamodb === "reachable";

  return (
    <header className="h-14 bg-white border-b border-gray-200 flex items-center
                       gap-4 px-5 shrink-0 z-10">
      {/* Search */}
      <div className="relative flex-1 max-w-md">
        <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-ink-faint" />
        <input
          type="text"
          placeholder="Search queries, sessions, records…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          className="w-full pl-9 pr-4 py-1.5 text-sm bg-surface-subtle border border-gray-200
                     rounded-lg focus:outline-none focus:ring-2 focus:ring-brand/30
                     focus:border-brand placeholder:text-ink-faint"
        />
      </div>

      <div className="flex items-center gap-3 ml-auto">
        {/* API status */}
        <div className={`flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-semibold
          ${apiOk
            ? "bg-risk-low-bg text-risk-low"
            : "bg-risk-high-bg text-risk-high"}`}>
          <Wifi size={12} />
          {apiOk ? "API Online" : healthError ? "API Offline" : "Checking…"}
        </div>

        {/* Session pill */}
        <button
          type="button"
          onClick={onNewSession}
          title="New session"
          className="hidden sm:flex items-center gap-1.5 px-3 py-1 rounded-full text-xs
                     font-mono font-medium bg-surface-muted text-ink-muted
                     hover:bg-brand-soft hover:text-brand transition-colors border border-gray-200"
        >
          {sessionId.slice(0, 16)}…
        </button>

        {/* Clock */}
        <div className="flex items-center gap-1.5 text-xs text-ink-muted">
          <Clock size={13} />
          {time.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
        </div>

        {/* Notifications */}
        <button
          type="button"
          className="relative w-8 h-8 flex items-center justify-center rounded-lg
                     hover:bg-surface-muted transition-colors"
        >
          <Bell size={16} className="text-ink-muted" />
          <span className="absolute top-1 right-1 w-2 h-2 bg-brand rounded-full" />
        </button>
      </div>
    </header>
  );
}
