/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        sidebar: {
          bg:     "#0f1117",
          hover:  "#1a1d26",
          active: "#1e2235",
          border: "#1e2235",
        },
        surface: {
          DEFAULT: "#ffffff",
          subtle:  "#f8f9fc",
          muted:   "#f1f3f8",
        },
        brand: {
          DEFAULT: "#4f6ef7",
          hover:   "#3b5bdb",
          soft:    "#eef1fc",
        },
        ink: {
          DEFAULT: "#1a1f2e",
          muted:   "#4f5b72",
          faint:   "#8b96ab",
        },
        risk: {
          low:        "#16a34a",
          "low-bg":   "#f0fdf4",
          med:        "#d97706",
          "med-bg":   "#fffbeb",
          high:       "#dc2626",
          "high-bg":  "#fef2f2",
        },
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
        mono: ["ui-monospace", "SF Mono", "Menlo", "Consolas", "monospace"],
      },
      boxShadow: {
        card:  "0 1px 3px rgba(20,30,60,.07), 0 4px 16px rgba(20,30,60,.05)",
        panel: "0 2px 8px rgba(20,30,60,.08)",
        lift:  "0 8px 28px rgba(20,30,60,.13)",
      },
    },
  },
  plugins: [],
}
