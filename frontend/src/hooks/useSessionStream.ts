import { useEffect, useRef } from 'react';
import type { MutableRefObject } from 'react';

import type { StreamEvent } from '../types';

type SessionStreamOptions = {
  eventTypes: string[];
  lastSeqRef: MutableRefObject<number>;
  onEvent: (event: StreamEvent) => void;
};

export function useSessionStream({ eventTypes, lastSeqRef, onEvent }: SessionStreamOptions) {
  const eventSourceRef = useRef<EventSource | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const onEventRef = useRef(onEvent);

  useEffect(() => { onEventRef.current = onEvent; }, [onEvent]);

  const closeStream = () => {
    if (reconnectTimerRef.current !== null) {
      window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    eventSourceRef.current?.close();
    eventSourceRef.current = null;
  };

  const connectStream = (afterSeq: number) => {
    closeStream();
    const source = new EventSource(`/api/session/stream?after_seq=${afterSeq}`);
    const receive = (event: Event) => onEventRef.current(JSON.parse((event as MessageEvent).data) as StreamEvent);
    source.onmessage = receive;
    eventTypes.forEach((eventType) => source.addEventListener(eventType, receive));
    source.onerror = () => {
      if (eventSourceRef.current !== source) return;
      source.close();
      reconnectTimerRef.current = window.setTimeout(() => connectStream(lastSeqRef.current), 1200);
    };
    eventSourceRef.current = source;
  };

  useEffect(() => closeStream, []);

  return { connectStream, closeStream };
}
