import { ChevronRight } from 'lucide-react';
import type { ReactNode } from 'react';
import type { StreamEvent } from '../types';

type EventView = { title: string; summary: string; icon: ReactNode; chips: string[]; time: string; tone: string };

export function EventItem({ event, buildView }: { event: StreamEvent; buildView: (event: StreamEvent) => EventView }) {
  const view = buildView(event);
  return <details className={`event-item tone-${view.tone}`}><summary>
    <span className="event-rail"><ChevronRight size={13} /><span className="event-icon">{view.icon}</span></span>
    <span className="event-main"><span className="event-name">{view.title}</span><span className="event-summary" title={view.summary}>{view.summary}</span></span>
    <span className="event-meta">{view.chips.map((chip) => <span key={chip} title={chip}>{chip}</span>)}<time>{view.time}</time></span>
  </summary><pre>{JSON.stringify(event.data, null, 2)}</pre></details>;
}
