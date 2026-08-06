import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ThemeToggle } from '../components/ThemeToggle';
import { THEME_STORAGE_KEY, useTheme } from './useTheme';

function Probe() {
  const { theme, toggleTheme } = useTheme();
  return <ThemeToggle theme={theme} onToggle={toggleTheme} />;
}

function mockSystemTheme(matches: boolean) {
  const listeners = new Set<(event: MediaQueryListEvent) => void>();
  vi.stubGlobal('matchMedia', vi.fn(() => ({
    matches,
    addEventListener: (_: string, listener: (event: MediaQueryListEvent) => void) => listeners.add(listener),
    removeEventListener: (_: string, listener: (event: MediaQueryListEvent) => void) => listeners.delete(listener),
  })));
  return listeners;
}

beforeEach(() => {
  const values = new Map<string, string>();
  Object.defineProperty(window, 'localStorage', {
    configurable: true,
    value: {
      getItem: (key: string) => values.get(key) || null,
      setItem: (key: string, value: string) => values.set(key, value),
      removeItem: (key: string) => values.delete(key),
    },
  });
  document.documentElement.removeAttribute('data-theme');
  document.documentElement.style.colorScheme = '';
  vi.unstubAllGlobals();
});

afterEach(() => cleanup());

describe('useTheme', () => {
  it('没有已保存偏好时跟随系统主题', () => {
    mockSystemTheme(true);
    render(<Probe />);
    expect(document.documentElement.dataset.theme).toBe('dark');
    expect(screen.getByRole('button', { name: '切换为亮色模式' })).toBeInTheDocument();
  });

  it('已保存偏好优先于系统主题', () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, 'light');
    mockSystemTheme(true);
    render(<Probe />);
    expect(document.documentElement.dataset.theme).toBe('light');
  });

  it('切换后同步根节点和本地存储', () => {
    mockSystemTheme(true);
    render(<Probe />);
    act(() => screen.getByRole('button', { name: '切换为亮色模式' }).click());
    expect(document.documentElement.dataset.theme).toBe('light');
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe('light');
    expect(screen.getByRole('button', { name: '切换为暗色模式' })).toBeInTheDocument();
  });
});
