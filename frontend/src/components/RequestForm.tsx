import { type FormEvent, type KeyboardEvent, useEffect, useRef, useState } from "react";

interface RequestFormProps {
  onSubmit: (userRequest: string) => void;
  disabled: boolean;
}

/** One example per routing outcome, so the three paths are all one click away. */
const EXAMPLES = [
  { label: "Revenue by mall", band: "low", text: "How much total revenue did Clothing transactions generate, grouped by mall?" },
  { label: "List transactions", band: "low", text: "Show me the transactions paid with Cash at Kanyon mall." },
  { label: "Update a record", band: "medium", text: "Update the payment method on invoice I138884 to Credit Card." },
  { label: "Bulk delete", band: "high", text: "Delete all Clothing transactions from Kanyon mall." },
];

export function RequestForm({ onSubmit, disabled }: RequestFormProps) {
  const [value, setValue] = useState("");
  const textarea = useRef<HTMLTextAreaElement>(null);

  // Focus on mount and again whenever the agent finishes, so a follow-up
  // question can be typed without reaching for the mouse.
  useEffect(() => {
    if (!disabled) textarea.current?.focus();
  }, [disabled]);

  function submit() {
    const trimmed = value.trim();
    if (!trimmed || disabled) return;
    onSubmit(trimmed);
    setValue("");
  }

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    submit();
  }

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      submit();
    }
  }

  return (
    <form className="request-form" onSubmit={handleSubmit}>
      <label htmlFor="user-request">Ask the agent</label>
      <textarea
        id="user-request"
        ref={textarea}
        rows={3}
        placeholder="e.g. How much revenue did Clothing generate at Kanyon?"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={handleKeyDown}
        disabled={disabled}
      />

      <div className="request-form__examples">
        {EXAMPLES.map((example) => (
          <button
            key={example.label}
            type="button"
            className="chip"
            data-band={example.band}
            disabled={disabled}
            title={example.text}
            onClick={() => setValue(example.text)}
          >
            {example.label}
          </button>
        ))}
      </div>

      <div className="request-form__footer">
        <span className="request-form__hint">
          <kbd>Ctrl</kbd>+<kbd>Enter</kbd> to send
        </span>
        <button
          type="submit"
          className="button button--primary"
          disabled={disabled || !value.trim()}
        >
          {disabled ? "Thinking…" : "Send to agent"}
        </button>
      </div>
    </form>
  );
}
