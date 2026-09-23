export const WIND_START_MS = 3;
export const WIND_NOMINAL_MS = 11.5;
export const WIND_ALERT_MS = 20;

export function clamp(value, min = 0, max = 1) {
  return Math.max(min, Math.min(max, value));
}

export function finiteValues(values) {
  return values.map((value) => (Number.isFinite(Number(value)) ? Number(value) : null));
}

export function linePath(values, x, y) {
  let path = '';
  values.forEach((value, index) => {
    if (!Number.isFinite(value)) return;
    const command = index > 0 && Number.isFinite(values[index - 1]) ? 'L' : 'M';
    path += `${command}${x(index).toFixed(2)},${y(value).toFixed(2)}`;
  });
  return path;
}

export function bandPath(p10, p90, x, y) {
  const upper = p90
    .map((value, index) => (Number.isFinite(value) ? `${index ? 'L' : 'M'}${x(index).toFixed(2)},${y(value).toFixed(2)}` : ''))
    .filter(Boolean);
  const lower = p10
    .map((value, index) => (Number.isFinite(value) ? [index, value] : null))
    .filter(Boolean)
    .reverse()
    .map(([index, value]) => `L${x(index).toFixed(2)},${y(value).toFixed(2)}`);
  return upper.length && lower.length ? `${upper.join('')}${lower.join('')}Z` : '';
}

export function windState(wind) {
  if (!Number.isFinite(wind)) return 'unknown';
  if (wind > WIND_ALERT_MS) return 'alert';
  if (wind < WIND_START_MS) return 'below-start';
  if (wind >= WIND_NOMINAL_MS) return 'nominal';
  return 'operating';
}

export function windStateLabel(wind) {
  const state = windState(wind);
  if (state === 'alert') return '⚠ ветер >20 м/с';
  if (state === 'below-start') return 'ниже пуска 3 м/с';
  if (state === 'nominal') return 'номинал ≥11,5 м/с';
  if (state === 'operating') return 'рабочий диапазон';
  return 'ветер не указан';
}

export function flagWindow(flag, count) {
  const from = Math.max(1, Math.round(Number(flag?.from_h) || 1));
  const to = Math.min(count, Math.max(from, Math.round(Number(flag?.to_h) || from)));
  return { from, to };
}

export function formatPercent(value) {
  if (!Number.isFinite(value)) return '—';
  return `${Math.round(value * 100)} %`;
}

export function formatNumber(value, digits = 1) {
  if (!Number.isFinite(value)) return '—';
  return Number(value).toFixed(digits).replace('.', ',');
}

export function formatLocalTime(value) {
  if (!value) return 'время не указано';
  const localParts = String(value).match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
  if (localParts) return `${localParts[3]}.${localParts[2]} ${localParts[4]}:${localParts[5]}`;
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return String(value);
  const day = String(date.getUTCDate()).padStart(2, '0');
  const month = String(date.getUTCMonth() + 1).padStart(2, '0');
  const hour = String(date.getUTCHours()).padStart(2, '0');
  return `${day}.${month} ${hour}:00`;
}
