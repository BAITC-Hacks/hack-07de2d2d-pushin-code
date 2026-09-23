const MONTHS_GEN = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'];
const WEEKDAYS = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'];

export const FLAG_KINDS = {
  ramp: { label: 'Резкое изменение', short: 'рампа' },
  ice: { label: 'Риск обледенения', short: 'лёд' },
  wind_gt20: { label: 'Ветер выше 20 м/с', short: '>20 м/с' },
  models_diverge: { label: 'Погодные модели расходятся', short: 'расхождение' },
};

function parts(value) {
  const match = String(value || '').match(/^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?/);
  if (!match) return null;
  return { y: +match[1], m: +match[2], d: +match[3], hh: match[4] || '00', mm: match[5] || '00' };
}

const pad = (n) => String(n).padStart(2, '0');

// Local wall-clock strings carry +05:00 already; format them without re-zoning.
export function hourLabel(value) {
  const p = parts(value);
  return p ? `${p.hh}:${p.mm}` : '—';
}

export function dayLabel(value) {
  const p = parts(value);
  return p ? `${p.d} ${MONTHS_GEN[p.m - 1]}` : '—';
}

export function shortDate(value) {
  const p = parts(value);
  return p ? `${pad(p.d)}.${pad(p.m)}` : '—';
}

export function weekday(value) {
  const p = parts(value);
  return p ? WEEKDAYS[new Date(Date.UTC(p.y, p.m - 1, p.d)).getUTCDay()] : '';
}

export function nextDay(value) {
  const p = parts(value);
  if (!p) return null;
  const date = new Date(Date.UTC(p.y, p.m - 1, p.d + 1));
  return `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}`;
}

// Weather run times are UTC ("2026-02-13T06:00Z"); show both UTC and Astana (+5).
export function utcLabel(value) {
  const t = Date.parse(value);
  if (!Number.isFinite(t)) return '—';
  const d = new Date(t);
  return `${pad(d.getUTCDate())}.${pad(d.getUTCMonth() + 1)} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`;
}

export function localFromUtc(value, offsetHours = 5) {
  const t = Date.parse(value);
  if (!Number.isFinite(t)) return '—';
  const d = new Date(t + offsetHours * 3600 * 1000);
  return `${pad(d.getUTCDate())}.${pad(d.getUTCMonth() + 1)} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}

export function localStamp(value) {
  const p = parts(value);
  return p ? `${pad(p.d)}.${pad(p.m)} ${p.hh}:${p.mm}` : '—';
}

export function pct(value, digits = 0) {
  if (!Number.isFinite(value)) return '—';
  return `${(value * 100).toFixed(digits).replace('.', ',')} %`;
}

export function plural(n, one, few, many) {
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}

export function rangeLabel(first, second) {
  const a = parts(first);
  const b = parts(second);
  if (!a || !b) return '—';
  return a.m === b.m ? `${a.d}–${b.d} ${MONTHS_GEN[a.m - 1]}` : `${dayLabel(first)} — ${dayLabel(second)}`;
}
