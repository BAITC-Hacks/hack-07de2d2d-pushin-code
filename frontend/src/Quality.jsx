import { useEffect, useMemo, useState } from 'react';
import ForecastChart from './ForecastChart';
import { ApiError } from './api';
import { linePath } from './chart';
import { pct } from './format';

const WEEK = { from: '2026-01-15', to: '2026-01-21' };

function errorText(error) {
  return error instanceof ApiError ? error.message : 'Нет связи с backend.';
}

function HorizonChart({ points }) {
  const width = 560;
  const height = 180;
  const pad = { l: 40, r: 12, t: 12, b: 24 };
  const max = Math.max(0.05, ...points.flatMap((p) => [p.model, p.power_curve].filter(Number.isFinite)));
  const top = Math.ceil(max * 10) / 10;
  const x = (i) => pad.l + (i / Math.max(1, points.length - 1)) * (width - pad.l - pad.r);
  const y = (v) => pad.t + (1 - v / top) * (height - pad.t - pad.b);
  const ticks = Array.from({ length: Math.round(top * 20) + 1 }, (_, i) => i * 0.05);
  return (
    <svg className="mini-chart" viewBox={`0 0 ${width} ${height}`} width="100%" role="img" aria-label="Средняя ошибка по горизонту прогноза">
      {ticks.map((t) => (
        <g key={t}>
          <line className="grid" x1={pad.l} x2={width - pad.r} y1={y(t)} y2={y(t)} />
          <text className="axis" x={pad.l - 6} y={y(t) + 4} textAnchor="end">{Math.round(t * 100)}%</text>
        </g>
      ))}
      {points.map((p, i) => (p.h % 12 === 0 || p.h === 1) && (
        <text key={p.h} className="axis" x={x(i)} y={height - 6} textAnchor="middle">{p.h} ч</text>
      ))}
      <path className="line-baseline" d={linePath(points.map((p) => p.power_curve), x, y)} />
      <path className="line-p50" d={linePath(points.map((p) => p.model), x, y)} />
    </svg>
  );
}

export default function Quality({ api, active }) {
  const [metrics, setMetrics] = useState(null);
  const [series, setSeries] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!active || metrics) return;
    api.getMetrics().then(setMetrics).catch((err) => setError(errorText(err)));
    api.getMetricsSeries({ ...WEEK, turbine: 'plant' })
      .then((rows) => setSeries(rows.filter((row) => !row.turbine || row.turbine === 'plant')))
      .catch((err) => setError(errorText(err)));
  }, [active, api, metrics]);

  const derived = useMemo(() => {
    if (!metrics) return null;
    const model = metrics.methods.find((m) => m.key === 'model');
    const baselines = metrics.methods.filter((m) => m.key !== 'model' && Number.isFinite(m.nmae));
    const best = baselines.reduce((a, b) => (a && a.nmae <= b.nmae ? a : b), null);
    const gain = model && best ? (best.nmae - model.nmae) / best.nmae : null;
    const worst = Math.max(...metrics.methods.map((m) => m.nmae).filter(Number.isFinite));
    return { model, best, gain, worst };
  }, [metrics]);

  const rows = useMemo(() => (series || []).map((row, index) => ({ ...row, h: index + 1 })), [series]);

  return (
    <div className={`view view-quality${active ? '' : ' hidden'}`}>
      <div className="quality">
        <header className="q-head">
          <h1>Как модель ошибалась в январе</h1>
          <p>
            {metrics?.issues_count ?? 30} январских выпусков посчитаны так же, как февральские, — только на погоде, доступной в момент выпуска, — и сравнены с фактом.
          </p>
        </header>

        {error && <p className="state-error">{error}</p>}

        {derived && (
          <div className="kpis" id="quality-kpis">
            <div className="kpi" title="nMAE"><span className="kpi-v">{pct(derived.model?.nmae, 1)}</span><span className="kpi-l">средняя ошибка модели в долях номинала</span></div>
            <div className="kpi"><span className="kpi-v">{derived.gain !== null ? `−${Math.round(derived.gain * 100)} %` : '—'}</span><span className="kpi-l">ошибки против лучшей базовой линии ({derived.best?.label?.toLowerCase()})</span></div>
            <div className="kpi" title={`Прогноз честно оценивает свою неуверенность: факт попал в вероятный диапазон в ${pct(metrics.coverage_p10_p90)} часов (цель — 80 %). Технически — покрытие P10–P90.`}><span className="kpi-v">{pct(metrics.coverage_p10_p90)}</span><span className="kpi-l">часов факт попал в вероятный диапазон (цель — 80 %): прогноз честно оценивает свою неуверенность</span></div>
            <div className="kpi"><span className="kpi-v">{metrics.issues_count}</span><span className="kpi-l">выпусков в проверке</span></div>
          </div>
        )}

        {metrics && (
          <div className="q-grid">
            <section className="q-card">
              <h2>Модель против базовых линий</h2>
              <table className="methods">
                <thead><tr><th>Метод</th><th className="num" title="nMAE">средняя ошибка</th><th /></tr></thead>
                <tbody>
                  {metrics.methods.map((m) => (
                    <tr key={m.key} className={m.key === 'model' ? 'is-model' : ''}>
                      <td>{m.label}</td>
                      <td className="num">{pct(m.nmae, 1)}</td>
                      <td className="bar-cell"><span className="bar" style={{ width: `${(m.nmae / derived.worst) * 100}%` }} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="note">Меньше — лучше; базовые линии: «завтра как вчера», медиана по часу и месяцу, кривая мощности по истории SCADA.</p>
            </section>

            <section className="q-card">
              <h2>Ошибка по горизонту</h2>
              <HorizonChart points={metrics.by_horizon} />
              <div className="legend">
                <span><i className="lg-p50" />модель</span>
                <span><i className="lg-base" />кривая мощности</span>
              </div>
              <p className="note">Чем дальше час от выпуска, тем больше ошибка у всех методов.</p>
            </section>
          </div>
        )}

        {rows.length > 0 && (
          <section className="q-card wide" id="quality-week">
            <h2>Неделя 15–21 января: прогноз против факта, ВЭС</h2>
            <ForecastChart rows={rows} showActual showWind={false} height={220} />
            <div className="legend">
              <span title="P50"><i className="lg-p50" />прогноз</span>
              <span title="С вероятностью 80 % выработка будет в этом диапазоне (P10–P90)"><i className="lg-band" />вероятный диапазон (80 %)</span>
              <span><i className="lg-actual" />факт</span>
            </div>
          </section>
        )}
      </div>
    </div>
  );
}
