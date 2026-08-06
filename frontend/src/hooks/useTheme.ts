import { useEffect, useState } from 'react';

export type Theme = 'light' | 'dark';

export const THEME_STORAGE_KEY = 'codepilot.theme';

function systemTheme(): Theme {
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function savedTheme(): Theme | null {
  try {
    const value = window.localStorage.getItem(THEME_STORAGE_KEY);
    return value === 'light' || value === 'dark' ? value : null;
  } catch {
    return null;
  }
}

export function useTheme() {
  const [preference, setPreference] = useState<Theme | null>(savedTheme);
  const [system, setSystem] = useState<Theme>(systemTheme);
  const theme = preference || system;

  useEffect(() => {
    const root = document.documentElement;
    root.dataset.theme = theme;
    root.style.colorScheme = theme;
  }, [theme]);

  useEffect(() => {
    if (preference) return undefined;
    const media = window.matchMedia?.('(prefers-color-scheme: dark)');
    if (!media) return undefined;
    const update = (event: MediaQueryListEvent) => setSystem(event.matches ? 'dark' : 'light');
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, [preference]);

  const toggleTheme = () => {
    const next = theme === 'dark' ? 'light' : 'dark';
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, next);
    } catch {
      // 本地存储不可用时仍允许本次会话切换主题。
    }
    setPreference(next);
  };

  return { theme, toggleTheme };
}
