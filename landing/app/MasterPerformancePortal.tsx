"use client";

import { PointerEvent, useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";

const API_BASE =
  process.env.NEXT_PUBLIC_TRAXION_API_URL ??
  "https://api-staging-025a.up.railway.app";

const RANGE_OPTIONS = ["1y", "180d", "90d", "all"] as const;
type RangeKey = (typeof RANGE_OPTIONS)[number];

type PerformancePoint = {
  at: string;
  pct: number;
};

type PerformancePayload = {
  range: RangeKey;
  started_at: string;
  range_started_at: string;
  updated_at: string | null;
  current_pct: number;
  points: PerformancePoint[];
  status: "ready" | "collecting";
};

type PlotPoint = PerformancePoint & {
  x: number;
  y: number;
  time: number;
};

const VIEW_W = 1000;
const VIEW_H = 360;
const LEFT = 74;
const RIGHT = 24;
const TOP = 30;
const BOTTOM = 48;
const PLOT_W = VIEW_W - LEFT - RIGHT;
const PLOT_H = VIEW_H - TOP - BOTTOM;
const TOOLTIP_FLIP_RATIO = 0.82;

function formatPct(value: number) {
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(2)}%`;
}

function formatTooltipDate(value: string) {
  return new Intl.DateTimeFormat("it-IT", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function formatAxisDate(timestamp: number) {
  return new Intl.DateTimeFormat("it-IT", {
    day: "2-digit",
    month: "short",
  }).format(new Date(timestamp));
}

function smoothPath(points: PlotPoint[]) {
  if (points.length === 0) return "";
  if (points.length === 1) return `M ${points[0].x} ${points[0].y}`;

  let path = `M ${points[0].x} ${points[0].y}`;
  for (let index = 0; index < points.length - 1; index += 1) {
    const current = points[index];
    const next = points[index + 1];
    const midX = (current.x + next.x) / 2;
    path += ` C ${midX} ${current.y}, ${midX} ${next.y}, ${next.x} ${next.y}`;
  }
  return path;
}

function PerformanceChart() {
  const [range, setRange] = useState<RangeKey>("all");
  const [payload, setPayload] = useState<PerformancePayload | null>(null);
  const [error, setError] = useState(false);
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);

  useEffect(() => {
    const controller = new AbortController();

    const load = async () => {
      try {
        const response = await fetch(
          `${API_BASE}/api/v1/public/master-performance?range=${range}`,
          { signal: controller.signal, cache: "no-store" },
        );
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const next = (await response.json()) as PerformancePayload;
        setPayload(next);
        setError(false);
        setHoverIndex(null);
      } catch (caught) {
        if (controller.signal.aborted) return;
        console.error("TRAXION public performance unavailable", caught);
        setError(true);
      }
    };

    void load();
    const refresh = window.setInterval(load, 60_000);
    return () => {
      controller.abort();
      window.clearInterval(refresh);
    };
  }, [range]);

  const chart = useMemo(() => {
    const source = payload?.points ?? [];
    if (source.length === 0) return null;

    const timestamps = source.map((point) => new Date(point.at).getTime());
    const values = source.map((point) => point.pct);
    const minTime = Math.min(...timestamps);
    const maxTime = Math.max(...timestamps);
    const timeSpan = Math.max(maxTime - minTime, 1);

    let minValue = Math.min(...values, 0);
    let maxValue = Math.max(...values, 0);
    const rawSpan = maxValue - minValue;
    const pad = Math.max(rawSpan * 0.16, 0.08);
    minValue -= pad;
    maxValue += pad;
    const valueSpan = Math.max(maxValue - minValue, 0.01);

    const points: PlotPoint[] = source.map((point, index) => {
      const time = timestamps[index];
      return {
        ...point,
        time,
        x: LEFT + ((time - minTime) / timeSpan) * PLOT_W,
        y: TOP + ((maxValue - point.pct) / valueSpan) * PLOT_H,
      };
    });

    const line = smoothPath(points);
    const area = `${line} L ${points.at(-1)?.x ?? LEFT} ${TOP + PLOT_H} L ${points[0].x} ${TOP + PLOT_H} Z`;
    const yTicks = Array.from({ length: 5 }, (_, index) => {
      const ratio = index / 4;
      return {
        value: maxValue - ratio * valueSpan,
        y: TOP + ratio * PLOT_H,
      };
    });
    const xTicks = Array.from({ length: 5 }, (_, index) => {
      const ratio = index / 4;
      const time = minTime + ratio * timeSpan;
      return { time, x: LEFT + ratio * PLOT_W };
    });

    return { points, line, area, yTicks, xTicks, minTime, timeSpan };
  }, [payload]);

  const handlePointerMove = (event: PointerEvent<SVGSVGElement>) => {
    if (!chart?.points.length) return;
    const rect = event.currentTarget.getBoundingClientRect();
    const localX = ((event.clientX - rect.left) / rect.width) * VIEW_W;
    const ratio = Math.min(1, Math.max(0, (localX - LEFT) / PLOT_W));
    const target = chart.minTime + ratio * chart.timeSpan;
    let nearest = 0;
    let distance = Number.POSITIVE_INFINITY;
    chart.points.forEach((point, index) => {
      const nextDistance = Math.abs(point.time - target);
      if (nextDistance < distance) {
        nearest = index;
        distance = nextDistance;
      }
    });
    setHoverIndex(nearest);
  };

  const selected = chart && hoverIndex !== null ? chart.points[hoverIndex] : null;
  const tooltipOnLeft = selected
    ? (selected.x - LEFT) / PLOT_W >= TOOLTIP_FLIP_RATIO
    : false;

  return (
    <section className="master-performance-section" aria-labelledby="master-performance-title">
      <div className="shell">
        <div className="master-performance-heading">
          <div>
            <p className="section-kicker">TRX / PERFORMANCE</p>
            <h2 id="master-performance-title">Andamento operativo del modello.</h2>
          </div>
          <p>
            Performance percentuale del wallet master MAINNET di riferimento, letta direttamente
            dal portfolio Hyperliquid dal 26 agosto 2026. Nessun saldo o valore assoluto viene esposto.
          </p>
        </div>

        <div className="master-performance-card">
          <div className="master-performance-card-head">
            <div>
              <span className="master-performance-label">Performance master</span>
              <strong className={payload && payload.current_pct < 0 ? "negative" : ""}>
                {payload ? formatPct(payload.current_pct) : "—"}
              </strong>
            </div>
            <div className="master-range-switch" aria-label="Intervallo performance">
              {RANGE_OPTIONS.map((option) => (
                <button
                  type="button"
                  key={option}
                  className={range === option ? "active" : ""}
                  onClick={() => setRange(option)}
                >
                  {option === "all" ? "All" : option.toUpperCase()}
                </button>
              ))}
            </div>
          </div>

          <div className="master-chart-wrap">
            {error && !payload ? (
              <div className="master-chart-state">Dati temporaneamente non disponibili.</div>
            ) : !chart ? (
              <div className="master-chart-state">Caricamento performance…</div>
            ) : (
              <>
                <svg
                  className="master-performance-svg"
                  viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
                  role="img"
                  aria-label="Grafico della performance percentuale del modello master"
                  onPointerMove={handlePointerMove}
                  onPointerLeave={() => setHoverIndex(null)}
                >
                  <defs>
                    <linearGradient id="masterArea" x1="0" x2="0" y1="0" y2="1">
                      <stop offset="0%" stopColor="currentColor" stopOpacity="0.25" />
                      <stop offset="100%" stopColor="currentColor" stopOpacity="0.015" />
                    </linearGradient>
                  </defs>

                  {chart.yTicks.map((tick) => (
                    <g key={tick.y}>
                      <line className="master-grid-line" x1={LEFT} x2={VIEW_W - RIGHT} y1={tick.y} y2={tick.y} />
                      <text className="master-axis-label" x={LEFT - 12} y={tick.y + 4} textAnchor="end">
                        {formatPct(tick.value)}
                      </text>
                    </g>
                  ))}

                  {chart.xTicks.map((tick, index) => (
                    <text
                      className="master-axis-label"
                      key={tick.x}
                      x={tick.x}
                      y={VIEW_H - 16}
                      textAnchor={index === 0 ? "start" : index === 4 ? "end" : "middle"}
                    >
                      {formatAxisDate(tick.time)}
                    </text>
                  ))}

                  <path className="master-area" d={chart.area} fill="url(#masterArea)" />
                  <path className="master-line" d={chart.line} />

                  {selected && (
                    <>
                      <line className="master-cursor-line" x1={selected.x} x2={selected.x} y1={TOP} y2={TOP + PLOT_H} />
                      <circle className="master-cursor-dot" cx={selected.x} cy={selected.y} r="5" />
                    </>
                  )}
                </svg>

                {selected && (
                  <div
                    className={`master-tooltip ${tooltipOnLeft ? "is-left" : "is-right"}`}
                    style={{
                      left: `${(selected.x / VIEW_W) * 100}%`,
                      top: `${(selected.y / VIEW_H) * 100}%`,
                    }}
                  >
                    <strong>{formatPct(selected.pct)}</strong>
                    <span>{formatTooltipDate(selected.at)}</span>
                  </div>
                )}
              </>
            )}
          </div>

          <div className="master-performance-foot">
            <span>
              {payload?.started_at
                ? `Serie TRAXION attiva dal ${formatTooltipDate(payload.started_at)}`
                : "Serie TRAXION dall’avvio operativo"}
            </span>
            <span>Metodo: PnL del portfolio Hyperliquid normalizzato sull’equity di partenza.</span>
          </div>
        </div>
      </div>
    </section>
  );
}

export default function MasterPerformancePortal() {
  const [host, setHost] = useState<HTMLElement | null>(null);

  useEffect(() => {
    const hero = document.querySelector<HTMLElement>(".hero");
    if (!hero) return;

    const existing = document.getElementById("master-performance");
    const node = existing ?? document.createElement("div");
    node.id = "master-performance";
    if (!existing) hero.insertAdjacentElement("afterend", node);
    setHost(node);

    return () => {
      if (!existing) node.remove();
    };
  }, []);

  return host ? createPortal(<PerformanceChart />, host) : null;
}