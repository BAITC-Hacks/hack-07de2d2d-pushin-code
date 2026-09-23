import { describe, expect, it } from 'vitest';
import { bandPath, formatLocalTime, flagWindow, linePath, windState, WIND_ALERT_MS, WIND_NOMINAL_MS, WIND_START_MS } from './chart';

const x = (index) => index * 10;
const y = (value) => value * 10;

describe('forecast chart parsing helpers', () => {
  it('draws a band from P10 to P90 and skips missing actual segments', () => {
    expect(bandPath([0.1, 0.2], [0.4, 0.5], x, y)).toContain('Z');
    expect(linePath([0.2, null, 0.4], x, y)).toBe('M0.00,2.00M20.00,4.00');
  });

  it('keeps contract thresholds distinct', () => {
    expect(windState(WIND_START_MS - 0.01)).toBe('below-start');
    expect(windState(WIND_NOMINAL_MS)).toBe('nominal');
    expect(windState(WIND_ALERT_MS + 0.01)).toBe('alert');
  });

  it('clamps flag windows to available hourly rows and formats +05 local time', () => {
    expect(flagWindow({ from_h: -4, to_h: 99 }, 48)).toEqual({ from: 1, to: 48 });
    expect(formatLocalTime('2026-02-14T00:00:00+05:00')).toBe('14.02 00:00');
  });
});

