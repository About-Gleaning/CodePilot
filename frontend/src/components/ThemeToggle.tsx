import { Moon, Sun } from 'lucide-react';

import type { Theme } from '../hooks/useTheme';

export function ThemeToggle({ theme, onToggle }: { theme: Theme; onToggle: () => void }) {
  const nextLabel = theme === 'dark' ? '亮色模式' : '暗色模式';
  return (
    <button
      type="button"
      className="theme-toggle"
      onClick={onToggle}
      aria-label={`切换为${nextLabel}`}
      title={`切换为${nextLabel}`}
    >
      {theme === 'dark' ? <Sun size={14} /> : <Moon size={14} />}
      <span>{theme === 'dark' ? 'LIGHT' : 'DARK'}</span>
    </button>
  );
}
