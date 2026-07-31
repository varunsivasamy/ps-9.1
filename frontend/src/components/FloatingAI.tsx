import { AnimatePresence, motion } from "framer-motion";
import { Bot, X } from "lucide-react";
import { useState } from "react";

export function FloatingAI() {
  const [open, setOpen] = useState(false);

  return (
    <div className="fixed bottom-6 right-6 z-50 flex flex-col items-end gap-3">
      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ opacity: 0, scale: 0.9, y: 10 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.9, y: 10 }}
            transition={{ duration: 0.2 }}
            className="w-72 bg-white rounded-2xl border border-gray-200 shadow-lift p-4"
          >
            <div className="flex items-center gap-2 mb-3">
              <div className="w-7 h-7 rounded-full bg-brand flex items-center justify-center">
                <Bot size={14} className="text-white" />
              </div>
              <span className="text-sm font-bold text-ink">AI Assistant</span>
              <button
                type="button"
                onClick={() => setOpen(false)}
                className="ml-auto text-ink-faint hover:text-ink"
              >
                <X size={14} />
              </button>
            </div>
            <p className="text-xs text-ink-muted leading-relaxed">
              I can help you write queries, understand risk scores, or explain
              why an action was routed the way it was. Just type a question in
              the main query box.
            </p>
            <div className="mt-3 flex flex-wrap gap-1.5">
              {["How does risk scoring work?", "What is a bulk delete?", "Explain audit trail"].map((q) => (
                <span
                  key={q}
                  className="px-2 py-0.5 text-[11px] rounded-full bg-brand-soft
                             text-brand font-medium border border-brand/20 cursor-pointer
                             hover:bg-brand hover:text-white transition-colors"
                >
                  {q}
                </span>
              ))}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      <motion.button
        type="button"
        onClick={() => setOpen((o) => !o)}
        whileHover={{ scale: 1.06 }}
        whileTap={{ scale: 0.95 }}
        className="w-13 h-13 rounded-full bg-brand text-white shadow-lift
                   flex items-center justify-center hover:bg-brand-hover transition-colors"
        style={{ width: 52, height: 52 }}
        title="AI Assistant"
      >
        {open ? <X size={20} /> : <Bot size={20} />}
      </motion.button>
    </div>
  );
}
