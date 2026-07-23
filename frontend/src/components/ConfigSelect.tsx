import { useEffect, useRef, useState } from 'react';
import { Check, ChevronDown } from 'lucide-react';

import type { SelectOption } from '../types';

type ConfigSelectProps = {
  value: string;
  options: SelectOption[];
  onChange: (nextValue: string) => void;
  disabled?: boolean;
};

// 原生 select 的弹层在部分浏览器中无法稳定套用暗色主题，这里只为配置区保留轻量自绘下拉。
export function ConfigSelect({ value, options, onChange, disabled = false }: ConfigSelectProps) {
  const [isOpen, setIsOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const selected = options.find((item) => item.value === value) || options[0];

  useEffect(() => {
    if (!isOpen) return;
    const handlePointerDown = (event: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) setIsOpen(false);
    };
    const handleKeyDown = (event: KeyboardEvent) => { if (event.key === 'Escape') setIsOpen(false); };
    document.addEventListener('mousedown', handlePointerDown);
    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('mousedown', handlePointerDown);
      document.removeEventListener('keydown', handleKeyDown);
    };
  }, [isOpen]);

  return (
    <div className={`select-shell ${isOpen ? 'is-open' : ''} ${disabled ? 'is-disabled' : ''}`} ref={rootRef}>
      <button type="button" className="select-trigger" disabled={disabled} aria-haspopup="listbox" aria-expanded={isOpen} onClick={() => setIsOpen((prev) => !prev)}>
        <span>{selected?.label || '-'}</span>
        <ChevronDown size={15} aria-hidden="true" />
      </button>
      {isOpen ? (
        <div className="select-menu" role="listbox">
          {options.map((item) => {
            const isSelected = item.value === value;
            return (
              <button key={item.value || '__empty__'} type="button" className={`select-option ${isSelected ? 'is-selected' : ''}`} role="option" aria-selected={isSelected} onClick={() => { onChange(item.value); setIsOpen(false); }}>
                <span>{item.label}</span>
                {isSelected ? <Check size={13} aria-hidden="true" /> : null}
              </button>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}
