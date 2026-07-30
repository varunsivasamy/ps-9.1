import { type FormEvent, useState } from "react";

interface RequestFormProps {
  onSubmit: (userRequest: string) => void;
  disabled: boolean;
}

const EXAMPLES = [
  "Look up the order history for customer C-4471.",
  "Update the email address on file for customer C-1029 to a.new@example.com.",
  "Delete all customer records that have had no activity since 2019.",
];

export function RequestForm({ onSubmit, disabled }: RequestFormProps) {
  const [value, setValue] = useState("");

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const trimmed = value.trim();
    if (!trimmed) return;
    onSubmit(trimmed);
  }

  return (
    <form className="request-form" onSubmit={handleSubmit}>
      <label htmlFor="user-request">What should the agent do?</label>
      <textarea
        id="user-request"
        rows={3}
        placeholder="Describe the request in plain language…"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        disabled={disabled}
      />
      <div className="request-form__footer">
        <div className="request-form__examples">
          {EXAMPLES.map((example) => (
            <button
              key={example}
              type="button"
              className="chip"
              disabled={disabled}
              onClick={() => setValue(example)}
            >
              {example}
            </button>
          ))}
        </div>
        <button type="submit" className="button button--primary" disabled={disabled || !value.trim()}>
          {disabled ? "Sending…" : "Send to agent"}
        </button>
      </div>
    </form>
  );
}
