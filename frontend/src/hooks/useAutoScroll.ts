import { useEffect, useRef, useState } from 'react';
import type { RefObject } from 'react';

type AutoScrollState = {
  anchorRef: RefObject<HTMLDivElement>;
  isAtBottom: boolean;
  scrollToBottom: () => void;
};

const AUTO_SCROLL_BOTTOM_THRESHOLD = 96;

export function useAutoScroll(signal: string, scrollRootRef?: RefObject<HTMLElement | null>): AutoScrollState {
  const anchorRef = useRef<HTMLDivElement | null>(null);
  const shouldFollowRef = useRef(true);
  const [isAtBottom, setIsAtBottom] = useState(true);
  const getScrollRoot = () => scrollRootRef?.current || anchorRef.current;

  const scrollToBottom = () => {
    const root = getScrollRoot();
    if (!root) return;
    shouldFollowRef.current = true;
    root.scrollTo({ top: root.scrollHeight, behavior: 'smooth' });
    setIsAtBottom(true);
  };

  useEffect(() => {
    const root = getScrollRoot();
    if (!root) return;
    const handleScroll = () => {
      const atBottom = root.scrollHeight - root.scrollTop - root.clientHeight <= AUTO_SCROLL_BOTTOM_THRESHOLD;
      shouldFollowRef.current = atBottom;
      setIsAtBottom(atBottom);
    };
    handleScroll();
    root.addEventListener('scroll', handleScroll, { passive: true });
    return () => root.removeEventListener('scroll', handleScroll);
  }, [scrollRootRef]);

  useEffect(() => {
    const root = getScrollRoot();
    if (!root || !shouldFollowRef.current) return;
    // 流式 token 会高频更新，使用 rAF 合并到浏览器布局周期，减少滚动抖动。
    window.requestAnimationFrame(() => {
      root.scrollTo({ top: root.scrollHeight, behavior: 'auto' });
      setIsAtBottom(true);
    });
  }, [signal]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (!event.altKey || event.key !== 'End') return;
      const target = event.target as HTMLElement | null;
      if (target && ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)) return;
      event.preventDefault();
      scrollToBottom();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, []);

  return { anchorRef, isAtBottom, scrollToBottom };
}
