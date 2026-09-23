import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, replayRunEvents } from './api';

function message(error) {
  if (error instanceof ApiError) return error.message;
  return 'Связь с агентом прервалась. Попробуйте ещё раз.';
}

// One place that owns the agent feed: a live run (POST + SSE), or a replay of a saved trace.
export default function useAgentRun(api) {
  const [events, setEvents] = useState([]);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState(null);
  const [caption, setCaption] = useState(null);
  const controllerRef = useRef(null);

  const stop = useCallback(() => {
    controllerRef.current?.abort();
    controllerRef.current = null;
    setRunning(false);
  }, []);

  useEffect(() => () => controllerRef.current?.abort(), []);

  const begin = useCallback((nextCaption) => {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setEvents([]);
    setError(null);
    setCaption(nextCaption);
    setRunning(true);
    return controller;
  }, []);

  const finish = useCallback((controller) => {
    if (controllerRef.current === controller) {
      controllerRef.current = null;
      setRunning(false);
    }
  }, []);

  const push = (event) => setEvents((current) => [...current, event]);

  const start = useCallback(async (issueDate, trigger = 'issue', { scenario = null, caption: runCaption = null } = {}) => {
    const controller = begin(runCaption || 'Запуск в реальном времени');
    try {
      const run = await api.startRun(issueDate, trigger, { signal: controller.signal, scenario });
      if (api.fixtureMode) {
        const result = await replayRunEvents(run.events, push, { delayMs: 450, signal: controller.signal });
        return result.verdict;
      }
      const result = await api.streamRun(run.id, push, { signal: controller.signal });
      return result.verdict;
    } catch (err) {
      if (err?.name !== 'AbortError') setError(message(err));
      return null;
    } finally {
      finish(controller);
    }
  }, [api, begin, finish]);

  const replay = useCallback(async (trace, { delayMs = 120, caption: replayCaption } = {}) => {
    const controller = begin(replayCaption || null);
    try {
      await replayRunEvents(trace.events, push, { delayMs, signal: controller.signal });
      return true;
    } catch (err) {
      if (err?.name !== 'AbortError') setError(message(err));
      return false;
    } finally {
      finish(controller);
    }
  }, [begin, finish]);

  const show = useCallback((trace, nextCaption) => {
    controllerRef.current?.abort();
    controllerRef.current = null;
    setRunning(false);
    setError(null);
    setCaption(nextCaption || null);
    setEvents(trace?.events || []);
  }, []);

  // A single frame of a step-by-step playback driven by the caller.
  const frame = useCallback((nextEvents, nextCaption, isRunning) => {
    controllerRef.current?.abort();
    controllerRef.current = null;
    setRunning(Boolean(isRunning));
    setError(null);
    setCaption(nextCaption || null);
    setEvents(nextEvents || []);
  }, []);

  return { events, running, error, caption, start, replay, show, frame, stop, setError };
}
