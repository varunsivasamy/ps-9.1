import { useState } from "react";

export interface GroupDatum {
  name: string;
  value: number;
}

interface GroupChartProps {
  data: GroupDatum[];
  /** Formats the value for labels and tooltips. */
  format: (value: number) => string;
  /** Names the measure being plotted, e.g. "Revenue". Stands in for a legend. */
  measure: string;
  /** Bars beyond this fold into an "Other" row rather than scrolling forever. */
  maxBars?: number;
}

const BAR_HEIGHT = 18; // ≤24px: the band keeps its leftover as air
const BAR_GAP = 10; // ≥2px surface gap between adjacent bars
const LABEL_WIDTH = 118;
const VALUE_WIDTH = 96;

/**
 * Horizontal bars for "which group is biggest" — magnitude across categories.
 *
 * One measure, so one hue and no legend box: the heading already names what is
 * plotted, and a single swatch would only restate it. Length carries the value;
 * the labels stay in text tokens so nothing is legible by color alone.
 */
export function GroupChart({ data, format, measure, maxBars = 12 }: GroupChartProps) {
  const [hovered, setHovered] = useState<number | null>(null);

  if (data.length === 0) return null;

  const sorted = [...data].sort((a, b) => b.value - a.value);

  // A 40-row chart is a table with extra steps. Keep the ranking readable and
  // let the folded remainder stay honest about how much it represents.
  const shown = sorted.slice(0, maxBars);
  const rest = sorted.slice(maxBars);
  const rows =
    rest.length > 0
      ? [
          ...shown,
          {
            name: `Other (${rest.length})`,
            value: rest.reduce((sum, d) => sum + d.value, 0),
          },
        ]
      : shown;

  const max = Math.max(...rows.map((r) => r.value), 0);
  const plotWidth = 260;
  const height = rows.length * (BAR_HEIGHT + BAR_GAP);

  return (
    <div className="group-chart">
      <div className="group-chart__head">
        <span className="group-chart__measure">{measure} by group</span>
        <span className="group-chart__hint">largest first</span>
      </div>

      <svg
        className="group-chart__svg"
        viewBox={`0 0 ${LABEL_WIDTH + plotWidth + VALUE_WIDTH} ${height}`}
        role="img"
        aria-label={`${measure} by group, ${rows.length} groups, largest first`}
        preserveAspectRatio="xMinYMin meet"
      >
        {rows.map((row, i) => {
          const y = i * (BAR_HEIGHT + BAR_GAP);
          const width = max > 0 ? Math.max((row.value / max) * plotWidth, 2) : 2;
          const isHovered = hovered === i;

          return (
            <g
              key={row.name}
              onMouseEnter={() => setHovered(i)}
              onMouseLeave={() => setHovered(null)}
              className="group-chart__row"
            >
              {/* Hit target spans the full row, not just the bar — a 2px bar is
                  otherwise almost impossible to hover. */}
              <rect
                x="0"
                y={y - BAR_GAP / 2}
                width={LABEL_WIDTH + plotWidth + VALUE_WIDTH}
                height={BAR_HEIGHT + BAR_GAP}
                fill={isHovered ? "var(--panel-sunken)" : "transparent"}
              />

              <text
                x={LABEL_WIDTH - 8}
                y={y + BAR_HEIGHT / 2}
                textAnchor="end"
                dominantBaseline="central"
                className="group-chart__label"
              >
                {row.name.length > 16 ? `${row.name.slice(0, 15)}…` : row.name}
              </text>

              {/* Square at the baseline, 4px rounded at the data end. */}
              <path
                d={roundedRightBar(LABEL_WIDTH, y, width, BAR_HEIGHT, 4)}
                className="group-chart__bar"
                opacity={hovered === null || isHovered ? 1 : 0.45}
              />

              <text
                x={LABEL_WIDTH + width + 8}
                y={y + BAR_HEIGHT / 2}
                dominantBaseline="central"
                className="group-chart__value"
              >
                {format(row.value)}
              </text>

              <title>{`${row.name}: ${format(row.value)}`}</title>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

/** A bar squared off at the baseline and rounded only at the value end. */
function roundedRightBar(x: number, y: number, w: number, h: number, r: number): string {
  const radius = Math.min(r, w);
  return [
    `M ${x} ${y}`,
    `H ${x + w - radius}`,
    `Q ${x + w} ${y} ${x + w} ${y + radius}`,
    `V ${y + h - radius}`,
    `Q ${x + w} ${y + h} ${x + w - radius} ${y + h}`,
    `H ${x}`,
    "Z",
  ].join(" ");
}
