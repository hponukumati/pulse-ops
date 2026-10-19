import { Bar, BarChart, CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

type Point = { label: string; value: number | null; start: string; end: string };

/**
 * One measure per chart (no dual axes). Single series: the title names it, so no legend.
 * Thin 2px line / rounded bars in the first categorical slot; recessive grid; hover tooltip;
 * every chart has a data-table view.
 */
export function SeriesChart({ title, points, format, kind }: { title: string; points: Point[]; format: (v: number | null) => string; kind: "bar" | "line" }) {
  const axis = { stroke: "var(--border)", tick: { fill: "var(--text-muted)", fontSize: 12 } };
  const sparse = points.filter((p) => p.value !== null).length <= 12;
  const tooltip = (
    <Tooltip
      cursor={kind === "bar" ? { fill: "var(--surface-2)" } : { stroke: "var(--text-muted)", strokeWidth: 1 }}
      content={({ active, payload }) => {
        const p = active ? (payload?.[0]?.payload as Point | undefined) : undefined;
        return p ? (
          <div className="tooltip">
            <div className="secondary">{new Date(p.start).toLocaleString()} – {new Date(p.end).toLocaleTimeString()}</div>
            <strong>{format(p.value)}</strong>
          </div>
        ) : null;
      }}
    />
  );
  return (
    <section className="card chart-card" aria-label={title}>
      <h3>{title}</h3>
      <div className="chart" role="img" aria-label={`${title} over the selected range. A data table follows.`}>
        <ResponsiveContainer width="100%" height="100%">
          {kind === "bar" ? (
            <BarChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
              <CartesianGrid vertical={false} stroke="var(--grid)" />
              <XAxis dataKey="label" {...axis} minTickGap={28} />
              <YAxis {...axis} width={56} tickFormatter={(v: number) => format(v)} />
              {tooltip}
              <Bar dataKey="value" fill="var(--series-1)" radius={[4, 4, 0, 0]} isAnimationActive={false} />
            </BarChart>
          ) : (
            <LineChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
              <CartesianGrid vertical={false} stroke="var(--grid)" />
              <XAxis dataKey="label" {...axis} minTickGap={28} />
              <YAxis {...axis} width={56} tickFormatter={(v: number) => format(v)} />
              {tooltip}
              <Line
                type="monotone"
                dataKey="value"
                stroke="var(--series-1)"
                strokeWidth={2}
                // Sparse data: isolated buckets would draw no visible line, so show 8px markers.
                dot={sparse ? { r: 4, fill: "var(--series-1)", stroke: "var(--surface-1)", strokeWidth: 2 } : false}
                activeDot={{ r: 4 }}
                connectNulls={false}
                isAnimationActive={false}
              />
            </LineChart>
          )}
        </ResponsiveContainer>
      </div>
      <details>
        <summary>Show data table</summary>
        <div className="table-wrap">
          <table>
            <thead><tr><th scope="col">Bucket start (local)</th><th scope="col" className="num">{title}</th></tr></thead>
            <tbody>{points.map((p) => <tr key={p.start}><td>{new Date(p.start).toLocaleString()}</td><td className="num">{format(p.value)}</td></tr>)}</tbody>
          </table>
        </div>
      </details>
    </section>
  );
}
