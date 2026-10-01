export interface DonutSegment {
  label: string;
  value: number;
  color: string;
}

const SIZE = 120;
const RADIUS = 42;
const STROKE = 16;
const CIRCUMFERENCE = 2 * Math.PI * RADIUS;

/**
 * A small donut for a 2-4 category part-to-whole split (accepted/rejected/
 * open, resolved/rejected/ignored) -- direct-labeled in the legend since a
 * series count this low reads fine as color + label, per the dataviz
 * skill's series-count ladder.
 */
export default function DonutChart({
  title,
  segments,
}: {
  title: string;
  segments: DonutSegment[];
}) {
  const total = segments.reduce((sum, s) => sum + s.value, 0);
  let offset = 0;

  return (
    <div className="donut-chart">
      <div className="donut-chart-head">{title}</div>
      <div className="donut-chart-body">
        <svg width={SIZE} height={SIZE} viewBox={`0 0 ${SIZE} ${SIZE}`} role="img" aria-label={title}>
          <circle
            cx={SIZE / 2}
            cy={SIZE / 2}
            r={RADIUS}
            fill="none"
            stroke="var(--border-default)"
            strokeWidth={STROKE}
          />
          {total > 0 &&
            segments
              .filter((s) => s.value > 0)
              .map((s) => {
                const length = (s.value / total) * CIRCUMFERENCE;
                // 2px surface gap between adjacent segments, per the mark
                // spec for stacked/donut fills.
                const dash = `${Math.max(0, length - 2)} ${CIRCUMFERENCE - length + 2}`;
                const el = (
                  <circle
                    key={s.label}
                    cx={SIZE / 2}
                    cy={SIZE / 2}
                    r={RADIUS}
                    fill="none"
                    stroke={s.color}
                    strokeWidth={STROKE}
                    strokeDasharray={dash}
                    strokeDashoffset={-offset}
                    strokeLinecap="round"
                    transform={`rotate(-90 ${SIZE / 2} ${SIZE / 2})`}
                  />
                );
                offset += length;
                return el;
              })}
          <text
            x={SIZE / 2}
            y={SIZE / 2}
            textAnchor="middle"
            dominantBaseline="central"
            className="donut-chart-total"
          >
            {total}
          </text>
        </svg>
        <ul className="donut-chart-legend">
          {segments.map((s) => (
            <li key={s.label}>
              <span className="donut-chart-swatch" style={{ background: s.color }} />
              <span>{s.label}</span>
              <strong className="mono">
                {s.value}
                {total > 0 && <span className="muted"> ({Math.round((s.value / total) * 100)}%)</span>}
              </strong>
            </li>
          ))}
        </ul>
      </div>
      {total === 0 && <div className="muted dash-empty">No data in this window.</div>}
    </div>
  );
}
