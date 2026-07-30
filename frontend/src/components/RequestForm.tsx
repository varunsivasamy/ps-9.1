import { type FormEvent, useState } from "react";

interface RequestFormProps {
  onSubmit: (userRequest: string) => void;
  disabled: boolean;
}

const EXAMPLES = [
  "How much total revenue did Clothing transactions generate?",
  "Update the payment method on invoice I138884 to Credit Card.",
  "Delete all Clothing transactions from Kanyon mall.",
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
