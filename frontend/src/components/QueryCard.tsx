import { motion } from "framer-motion";
import { AlertTriangle, BarChart2, Brain, RotateCcw, Send, Sparkles, Zap } from "lucide-react";
import { type FormEvent, type KeyboardEvent, useEffect, useRef, useState } from "react";

const EXAMPLES = [
  { label: "Revenue by category",  band: "low",    text: "How much total revenue did Clothing transactions generate, grouped by mall?" },
  { label: "Read transactions",    band: "low",    text: "Show me the transactions paid with Cash at Kanyon mall." },
  { label: "Update a record",      band: "medium", text: "Update the payment method on invoice I138884 to Credit Card." },
  { label: "Bulk delete",          band: "high",   text: "Delete all Clothing transactions from Kanyon mall." },
];

const BAND_COLORS: Record<string, string> = {
  low:    "bg-risk-low-bg   text-risk-low   border-risk-low/30",
  medium: "bg-risk-med-bg  text-risk-med   border-risk-med/30",
  high:   "bg-risk-high-bg  text-risk-high  border-risk-high/30",
};

const BAND_DOT: Record<string, string> = {
  low:    "bg-risk-low",
  medium: "bg-risk-med",
  high:   "bg-risk-high",
};

interface QueryCardProps {
  onSubmit: (text: string) => void;
  disabled: boolean;
}

export function QueryCard({ onSubmit, disabled }: QueryCardProps) {
  const [value, setValue] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (!disabled) ref.current?.focus();
  }, [disabled]);

  function submit() {
    const t = value.trim();
    if (!t || disabled) return;
    onSubmit(t);
    setValue("");
  }

  function handleSubmit(e: FormEvent) { e.preventDefault(); submit(); }

  function handleKey(e: KeyboardEvent<HTMLTextAreaElement>) {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); submit(); }
  }

  const charCount = value.length;

  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3 }}
      className="bg-white rounded-2xl border border-gray-200 shadow-card w-full"
    >
      {/* Card header */}
      <div className="flex items-center gap-3 px-5 py-4 border-b border-gray-100">
        <div className="w-8 h-8 rounded-lg bg-brand-soft flex items-center justify-center">
          <Brain size={16} className="text-brand" />
        </div>
        <div>
          <h2 className="text-sm font-bold text-ink">Natural Language Query</h2>
          <p className="text-xs text-ink-faint">
            Ask the agent anything about the transaction data
          </p>
        </div>
        <div className="ml-auto flex items-center gap-1.5 px-2.5 py-1 rounded-full
                        bg-surface-muted text-xs text-ink-muted font-medium border border-gray-200">
          <Sparkles size={11} className="text-brand" />
          AI Powered
        </div>
      </div>

      <form onSubmit={handleSubmit} className="p-5 flex flex-col gap-4">
        {/* Textarea */}
        <div className="relative">
          <textarea
            ref={ref}
            rows={5}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={handleKey}
            disabled={disabled}
            placeholder="e.g. How much revenue did Clothing generate at Kanyon mall last year?"
            className="w-full resize-none rounded-xl border border-gray-200 bg-surface-subtle
                       px-4 py-3 text-sm text-ink placeholder:text-ink-faint
                       focus:outline-none focus:ring-2 focus:ring-brand/25 focus:border-brand
                       disabled:opacity-50 leading-relaxed"
          />
          <span className="absolute bottom-3 right-3 text-[11px] text-ink-faint font-mono">
            {charCount}/2000
          </span>
        </div>

        {/* Quick examples */}
        <div>
          <p className="text-[11px] font-bold uppercase tracking-widest text-ink-faint mb-2">
            Quick examples
          </p>
          <div className="flex flex-wrap gap-2">
            {EXAMPLES.map((ex) => (
              <button
                key={ex.label}
                type="button"
                disabled={disabled}
                onClick={() => setValue(ex.text)}
                className={`flex items-center gap-1.5 px-3 py-1 rounded-full border text-xs
                            font-medium transition-all hover:shadow-sm disabled:opacity-40
                            ${BAND_COLORS[ex.band]}`}
              >
                <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${BAND_DOT[ex.band]}`} />
                {ex.label}
              </button>
            ))}
          </div>
        </div>

        {/* Risk legend */}
        <div className="flex items-center gap-4 p-3 rounded-xl bg-surface-subtle border border-gray-200">
          <div className="flex items-center gap-1.5 text-xs text-ink-muted">
            <Zap size={12} className="text-risk-low" />
            <span className="font-semibold text-risk-low">Low</span>
            <span className="text-ink-faint">— auto-executed</span>
          </div>
          <div className="flex items-center gap-1.5 text-xs text-ink-muted">
            <AlertTriangle size={12} className="text-risk-med" />
            <span className="font-semibold text-risk-med">Medium</span>
            <span className="text-ink-faint">— needs confirm</span>
          </div>
          <div className="flex items-center gap-1.5 text-xs text-ink-muted">
            <BarChart2 size={12} className="text-risk-high" />
            <span className="font-semibold text-risk-high">High</span>
            <span className="text-ink-faint">— full review</span>
          </div>
        </div>

        {/* Actions */}
        <div className="flex items-center justify-end gap-3 pt-1 flex-wrap">
          <button
            type="button"
            onClick={() => setValue("")}
            disabled={!value || disabled}
            className="flex items-center gap-1.5 px-4 py-2 rounded-lg border border-gray-200
                       text-sm font-medium text-ink-muted hover:bg-surface-muted
                       hover:text-ink disabled:opacity-40 transition-colors"
          >
            <RotateCcw size={13} />
            Clear
          </button>

          <button
            type="submit"
            disabled={!value.trim() || disabled}
            className="flex items-center gap-2 px-6 py-2 rounded-lg bg-brand text-white
                       text-sm font-semibold hover:bg-brand-hover transition-colors
                       disabled:opacity-40 shadow-sm hover:shadow whitespace-nowrap"
          >
            <Send size={14} />
            {disabled ? "Analyzing…" : "Analyze Request"}
          </button>
        </div>
      </form>
    </motion.div>
  );
}
