import { useEffect, useMemo, useRef, useState } from 'react';
import { bandPath, flagWindow, formatNumber, formatPercent, linePath, WIND_NOMINAL_MS, WIND_START_MS } from './chart';
import { FLAG_KINDS, hourLabel, dayLabel } from './format';

const PAD = { left: 44, right: 16, top: 30, bottom: 26 };
const GAP = 14;

function useWidth(ref, fallback = 760) {
  const [width, setWidth] = useState(fallback);
  useEffect(() => {
    if (!ref.current || typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.max(300, Math.round(entry.contentRect.width))));
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, [ref]);
  return width;
}

export default function ForecastChart({ rows = [], previousRows = null, flags = [], showActual = false, showWind = true, height = 250 }) {
  const wrapRef = useRef(null);
  const width = useWidth(wrapRef);
  const [hover, setHover] = useState(null);

  const count = rows.length;
  const innerW = width - PAD.left - PAD.right;
  const step = count > 1 ? innerW / (count - 1) : innerW;
  const x = (index) => PAD.left + (count > 1 ? index * step : innerW / 2);
  const y = (value) => PAD.top + (1 - value) * height;
  const kinds = [...new Set(flags.map((flag) => flag.kind))];
  const flagSpace = kinds.length ? kinds.length * 7 + 6 : 0;
  const windH = showWind ? 86 : 0;
  const windTop = PAD.top + height + flagSpace + (showWind ? GAP + 18 : 0);
  const windMax = useMemo(() => {
    const max = Math.max(WIND_NOMINAL_MS + 2, ...rows.map((row) => Number(row.wind_fc_ms) || 0));
    return Math.ceil(max / 5) * 5;
  }, [rows]);
  const yWind = (value) => windTop + (1 - value / windMax) * windH;
  const plotBottom = showWind ? windTop + windH : PAD.top + height + flagSpace;
  const totalH = plotBottom + PAD.bottom;

  const series = useMemo(() => {
    const prevByH = previousRows ? new Map(previousRows.map((row) => [row.h, row.p50])) : null;
    return {
      p10: rows.map((row) => row.p10),
      p50: rows.map((row) => row.p50),
      p90: rows.map((row) => row.p90),
      actual: rows.map((row) => (Number.isFinite(row.actual) ? row.actual : null)),
      wind: rows.map((row) => (Number.isFinite(row.wind_fc_ms) ? row.wind_fc_ms : null)),
      prev: prevByH ? rows.map((row) => prevByH.get(row.h) ?? null) : null,
    };
  }, [rows, previousRows]);

  if (!count) {
    return <div className="chart-empty">Нет почасовых строк для графика.</div>;
  }

  const indexed = rows.map((row, index) => ({ index, time: String(row.target_time_local || '') }));
  const dayStarts = indexed.filter(({ time }) => time.includes('T00:00'));
  const tickEvery = count > 72 ? 24 : 6;
  const hourTicks = indexed.filter(({ time }) => {
    const hh = Number(time.slice(11, 13));
    return Number.isFinite(hh) && hh % tickEvery === 0;
  });

  const hovered = hover !== null ? rows[hover] : null;
  const prevAtHover = hover !== null && series.prev ? series.prev[hover] : null;

  const onMove = (event) => {
    const rect = event.currentTarget.getBoundingClientRect();
    const px = ((event.clientX - rect.left) / rect.width) * width;
    const index = Math.round((px - PAD.left) / step);
    setHover(index >= 0 && index < count ? index : null);
  };

  const tipLeft = hover !== null ? Math.min(Math.max(x(hover) / width, 0.14), 0.8) : 0;

  return (
    <div className="chart" ref={wrapRef}>
      <svg
        viewBox={`0 0 ${width} ${totalH}`}
        width="100%"
        height={totalH}
        role="img"
        aria-label="Прогноз мощности: медиана P50 и коридор P10–P90"
        onMouseMove={onMove}
        onMouseLeave={() => setHover(null)}
      >
        {[0, 0.25, 0.5, 0.75, 1].map((tick) => (
          <g key={tick}>
            <line className="grid" x1={PAD.left} x2={width - PAD.right} y1={y(tick)} y2={y(tick)} />
            <text className="axis" x={PAD.left - 8} y={y(tick) + 4} textAnchor="end">{Math.round(tick * 100)}%</text>
          </g>
        ))}

        {flags.map((flag, index) => {
          const { from, to } = flagWindow(flag, count);
          const x0 = Math.max(PAD.left, x(from - 1) - step / 2);
          const x1 = Math.min(width - PAD.right, x(to - 1) + step / 2);
          const lane = kinds.indexOf(flag.kind);
          return (
            <g key={`${flag.kind}-${index}`} className={`flag flag-${flag.kind}`}>
              <rect className="flag-tick" x={x0 + 1} width={Math.max(3, x1 - x0 - 2)} y={PAD.top + height + 5 + lane * 7} height={4} rx={2} />
              <title>{flag.text || FLAG_KINDS[flag.kind]?.label || flag.kind}</title>
            </g>
          );
        })}

        {dayStarts.map(({ index, time }) => (
          <g key={time}>
            {index > 0 && <line className="day-rule" x1={x(index)} x2={x(index)} y1={PAD.top - 18} y2={plotBottom} />}
            {count <= 72 && <text className="day-label" x={x(index) + 6} y={PAD.top - 10}>{dayLabel(time)}</text>}
          </g>
        ))}

        <path className="band" d={bandPath(series.p10, series.p90, x, y)} />
        {series.prev && <path className="line-prev" d={linePath(series.prev, x, y)} />}
        <path className="line-p50" d={linePath(series.p50, x, y)} />
        {showActual && <path className="line-actual" d={linePath(series.actual, x, y)} />}

        {showWind && (
          <g>
            <text className="panel-label" x={PAD.left} y={windTop - 6}>Ветер на 100 м, м/с</text>
            <line className="grid" x1={PAD.left} x2={width - PAD.right} y1={yWind(0)} y2={yWind(0)} />
            {[{ v: WIND_START_MS, label: 'пуск · 3 м/с' }, { v: WIND_NOMINAL_MS, label: 'номинал · 11,5 м/с' }].map(({ v, label }) => (
              <g key={v}>
                <line className="wind-ref" x1={PAD.left} x2={width - PAD.right} y1={yWind(v)} y2={yWind(v)} />
                <text className="wind-ref-label" x={width - PAD.right - 4} y={yWind(v) - 4} textAnchor="end">{label}</text>
              </g>
            ))}
            <text className="axis" x={PAD.left - 8} y={yWind(windMax) + 8} textAnchor="end">{windMax}</text>
            <path className="line-wind" d={linePath(series.wind, x, yWind)} />
          </g>
        )}

        {hourTicks.map(({ index, time }) => (
          <text key={`t-${time}`} className="axis" x={x(index)} y={totalH - 8} textAnchor="middle">
            {count > 72 ? dayLabel(time) : hourLabel(time)}
          </text>
        ))}

        {hovered && (
          <g className="hover">
            <line x1={x(hover)} x2={x(hover)} y1={PAD.top} y2={plotBottom} />
            <circle cx={x(hover)} cy={y(hovered.p50)} r="4" />
          </g>
        )}
      </svg>

      {hovered && (
        <div className="chart-tip" style={{ left: `${tipLeft * 100}%` }}>
          <div className="tip-time">
            {dayLabel(hovered.target_time_local)}, {hourLabel(hovered.target_time_local)}
            {Number.isFinite(hovered.h) && <span>час {hovered.h}</span>}
          </div>
          <dl>
            <dt>P50</dt><dd className="strong">{formatPercent(hovered.p50)}</dd>
            <dt>P10–P90</dt><dd>{formatPercent(hovered.p10)} – {formatPercent(hovered.p90)}</dd>
            {Number.isFinite(prevAtHover) && (<><dt>прошлая версия</dt><dd>{formatPercent(prevAtHover)}</dd></>)}
            {showActual && Number.isFinite(hovered.actual) && (<><dt>факт</dt><dd>{formatPercent(hovered.actual)}</dd></>)}
            {Number.isFinite(hovered.wind_fc_ms) && (<><dt>ветер</dt><dd>{formatNumber(hovered.wind_fc_ms)} м/с</dd></>)}
            {Number.isFinite(hovered.temp_fc_c) && (<><dt>температура</dt><dd>{formatNumber(hovered.temp_fc_c)} °C</dd></>)}
          </dl>
        </div>
      )}
    </div>
  );
}
