import { useEffect, useMemo, useState } from 'react';
import { createApi, getFixtureMode } from './api';
import February from './February';
import Live from './Live';
import Quality from './Quality';

const TABS = [
  { key: 'february', label: 'Февраль 2026', note: 'тест' },
  { key: 'live', label: 'Live', note: 'сейчас' },
  { key: 'quality', label: 'Качество', note: 'январь' },
];

const CHECKS = [
  { need: 'Прогноз на 48 часов по каждому часу, для ВЭС и двух турбин', where: 'График и переключатель объекта', tab: 'february', target: 'forecast-chart' },
  { need: 'Интервал неопределённости P10–P90', where: 'Светлый коридор вокруг линии P50', tab: 'february', target: 'forecast-chart' },
  { need: 'Повторить прогноз за весь тестовый период', where: '29 выпусков 31.01–28.02 и «Воспроизвести февраль»', tab: 'february', target: 'calendar' },
  { need: 'Без данных из будущего', where: '«Без будущего» — какие прогоны погоды взяты и когда они вышли', tab: 'february', target: 'no-future' },
  { need: 'Агент сам проходит этапы: погода → подготовка → модель → прогноз → анализ → пересчёт', where: 'Панель агента справа', tab: 'february', target: 'agent-stages' },
  { need: 'Пересчёт при обновлении погоды', where: '«Перевыпустить агентом» и «Новый прогон погоды»', tab: 'february', target: 'issue-actions' },
  { need: 'Анализ результата и сводка для диспетчера', where: 'Риски в окне и сводка под лентой агента', tab: 'february', target: 'agent-panel' },
  { need: 'Результат в CSV', where: 'Кнопка «CSV выпуска» и все 29 выпусков одним файлом', tab: 'february', target: 'issue-actions' },
  { need: 'Работа на текущих данных', where: 'Вкладка Live и журнал агента', tab: 'live', target: 'live-status' },
  { need: 'Точность против базовых линий', where: 'Вкладка «Качество», январь', tab: 'quality', target: 'quality-kpis' },
];

function tabFromHash() {
  const key = typeof window === 'undefined' ? '' : window.location.hash.replace('#', '');
  return TABS.some((tab) => tab.key === key) ? key : 'february';
}

function CheckDrawer({ open, onClose, onShow }) {
  useEffect(() => {
    if (!open) return undefined;
    const onKey = (event) => event.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);
  return (
    <>
      <div className={`scrim${open ? ' on' : ''}`} onClick={onClose} aria-hidden="true" />
      <aside className={`drawer${open ? ' on' : ''}`} aria-hidden={!open} aria-label="Как проверить за 3 минуты">
        <header>
          <h2>Как проверить за 3 минуты</h2>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Закрыть">×</button>
        </header>
        <p className="drawer-intro">Требование задачи — и где его увидеть. «Показать» переключит вкладку и подсветит нужное место.</p>
        <ol className="checks">
          {CHECKS.map((check) => (
            <li key={check.need}>
              <div>
                <p className="check-need">{check.need}</p>
                <p className="check-where">{check.where}</p>
              </div>
              <button type="button" className="btn small" onClick={() => onShow(check)}>Показать</button>
            </li>
          ))}
        </ol>
      </aside>
    </>
  );
}

export default function App() {
  const fixtureMode = getFixtureMode();
  const api = useMemo(() => createApi({ fixtureMode }), [fixtureMode]);
  const [tab, setTab] = useState(tabFromHash);
  const [health, setHealth] = useState(null);
  const [drawer, setDrawer] = useState(false);

  useEffect(() => {
    api.getHealth().then(setHealth).catch(() => setHealth({ ok: false }));
  }, [api]);

  useEffect(() => {
    const onHash = () => setTab(tabFromHash());
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  const go = (key) => {
    setTab(key);
    if (window.location.hash !== `#${key}`) window.history.replaceState(null, '', `#${key}`);
  };

  const show = (check) => {
    setDrawer(false);
    go(check.tab);
    setTimeout(() => {
      const element = document.getElementById(check.target);
      if (!element) return;
      element.scrollIntoView({ behavior: 'smooth', block: 'center' });
      element.classList.remove('spotlight');
      void element.offsetWidth;
      element.classList.add('spotlight');
      setTimeout(() => element.classList.remove('spotlight'), 2400);
    }, 120);
  };

  const mode = health?.mode === 'agent' ? 'агент · LLM' : health?.mode === 'deterministic' ? 'агент · без LLM' : health?.ok === false ? 'backend недоступен' : '…';

  return (
    <div className="app">
      <header className="top">
        <div className="brand">
          <span className="logo" aria-hidden="true">
            <svg viewBox="0 0 24 24" width="22" height="22"><path d="M12 12 L12 3 M12 12 L20 16.5 M12 12 L4 16.5" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" /><circle cx="12" cy="12" r="2.2" fill="currentColor" /></svg>
          </span>
          <div>
            <strong>Windcast</strong>
            <span className="brand-sub">прогноз выработки ВЭС · 2 турбины</span>
          </div>
        </div>
        <nav className="tabs" aria-label="Разделы">
          {TABS.map((item) => (
            <button key={item.key} type="button" className={tab === item.key ? 'on' : ''} aria-current={tab === item.key ? 'page' : undefined} onClick={() => go(item.key)}>
              {item.label} <span>{item.note}</span>
            </button>
          ))}
        </nav>
        <div className="top-right">
          <span className={`mode mode-${health?.mode || "unknown"}${health?.ok === false ? " mode-down" : ""}`} title={health?.model_version ? `модель ${health.model_version}` : ''}>{mode}</span>
          {fixtureMode && <span className="mode mode-fixture">фикстуры</span>}
          <button type="button" className="btn primary" onClick={() => setDrawer(true)}>Как проверить<span className="check-long"> за 3 минуты</span></button>
        </div>
      </header>

      <main>
        <February api={api} active={tab === 'february'} />
        <Live api={api} active={tab === 'live'} />
        <Quality api={api} active={tab === 'quality'} />
      </main>

      <CheckDrawer open={drawer} onClose={() => setDrawer(false)} onShow={show} />
    </div>
  );
}
