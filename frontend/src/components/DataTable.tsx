import { useMemo, useState } from "react";

interface DataTableProps {
  rows: Record<string, string>[];
  /** Shown in the header and used for the exported filename. */
  caption: string;
  truncated?: boolean;
}

type SortDir = "asc" | "desc";

/** Numeric-looking columns sort and align as numbers, not as strings. */
function isNumeric(value: string): boolean {
  return value !== "" && !Number.isNaN(Number(value));
}

function toCsv(rows: Record<string, string>[], columns: string[]): string {
  const escape = (v: string) => (/[",\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v);
  return [
    columns.join(","),
    ...rows.map((r) => columns.map((c) => escape(r[c] ?? "")).join(",")),
  ].join("\n");
}

export function DataTable({ rows, caption, truncated }: DataTableProps) {
  const [query, setQuery] = useState("");
  const [sortKey, setSortKey] = useState<string | null>(null);
  const [sortDir, setSortDir] = useState<SortDir>("asc");

  const columns = useMemo(() => Object.keys(rows[0] ?? {}), [rows]);

  const numericColumns = useMemo(() => {
    const set = new Set<string>();
    for (const col of columns) {
      if (rows.every((r) => isNumeric(r[col] ?? ""))) set.add(col);
    }
    return set;
  }, [columns, rows]);

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    let out = needle
      ? rows.filter((r) => columns.some((c) => (r[c] ?? "").toLowerCase().includes(needle)))
      : rows;

    if (sortKey) {
      const numeric = numericColumns.has(sortKey);
      out = [...out].sort((a, b) => {
        const av = a[sortKey] ?? "";
        const bv = b[sortKey] ?? "";
        const cmp = numeric ? Number(av) - Number(bv) : av.localeCompare(bv);
        return sortDir === "asc" ? cmp : -cmp;
      });
    }
    return out;
  }, [rows, columns, query, sortKey, sortDir, numericColumns]);

  function toggleSort(col: string) {
    if (sortKey === col) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(col);
      setSortDir("asc");
    }
  }

  function download() {
    const blob = new Blob([toCsv(visible, columns)], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${caption.toLowerCase().replace(/\s+/g, "-")}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  if (columns.length === 0) return null;

  return (
    <div className="data-block">
      <div className="data-block__bar">
        <span className="data-block__title">
          {caption}
          <span className="data-block__count">
            {visible.length.toLocaleString()}
            {visible.length !== rows.length && ` of ${rows.length.toLocaleString()}`}
            {truncated ? " shown" : ""}
          </span>
        </span>
        <div className="data-block__tools">
          <input
            type="search"
            className="data-block__filter"
            placeholder="Filter rows…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            aria-label={`Filter ${caption}`}
          />
          <button type="button" className="button button--ghost button--sm" onClick={download}>
            Export CSV
          </button>
        </div>
      </div>

      <div className="data-block__scroll">
        <table className="data-table">
          <thead>
            <tr>
              {columns.map((col) => (
                <th
                  key={col}
                  onClick={() => toggleSort(col)}
                  className={numericColumns.has(col) ? "is-numeric" : undefined}
                  aria-sort={
                    sortKey === col ? (sortDir === "asc" ? "ascending" : "descending") : "none"
                  }
                >
                  {col.replace(/_/g, " ")}
                  <span className="data-table__sort">
                    {sortKey === col ? (sortDir === "asc" ? "▲" : "▼") : "↕"}
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {visible.map((row, i) => (
              <tr key={i}>
                {columns.map((col) => (
                  <td key={col} className={numericColumns.has(col) ? "is-numeric" : undefined}>
                    {row[col]}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>

        {visible.length === 0 && (
          <p className="data-block__none">No rows match “{query}”.</p>
        )}
      </div>
    </div>
  );
}
