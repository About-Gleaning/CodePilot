import { useRef, useState } from 'react';
import type { KeyboardEvent, RefObject } from 'react';

import type { QuestionAnswer, QuestionItem, QuestionRequest } from '../types';

type QuestionInteractionOptions = {
  onSubmit: (request: QuestionRequest, answers: Record<string, QuestionAnswer>) => Promise<boolean>;
  onDecline: (request: QuestionRequest) => Promise<boolean>;
};

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function extractQuestionItems(source: unknown): QuestionItem[] | null {
  if (!Array.isArray(source)) {
    return null;
  }
  const questions = source.map((item) => {
    const raw = asRecord(item);
    const options = Array.isArray(raw.options)
      ? raw.options.map((option) => {
        const value = asRecord(option);
        return { value: String(value.value || ''), label: String(value.label || '') };
      }).filter((option) => option.value && option.label)
      : [];
    return {
      id: String(raw.id || ''),
      question: String(raw.question || ''),
      multiple: Boolean(raw.multiple),
      options,
    };
  }).filter((question) => question.id && question.question && question.options.length);
  return questions.length ? questions : null;
}

export function useQuestionInteraction({ onSubmit, onDecline }: QuestionInteractionOptions) {
  const [questionRequest, setQuestionRequest] = useState<QuestionRequest | null>(null);
  const [questionAnswers, setQuestionAnswers] = useState<Record<string, QuestionAnswer>>({});
  const [activeQuestionIndex, setActiveQuestionIndex] = useState(0);
  const [activeQuestionOptionIndex, setActiveQuestionOptionIndex] = useState(0);
  const [questionError, setQuestionError] = useState('');
  const questionOptionsRef = useRef<HTMLDivElement | null>(null);
  const questionNoteRef = useRef<HTMLTextAreaElement | null>(null);

  const clearQuestion = () => {
    setQuestionRequest(null);
    setQuestionAnswers({});
    setActiveQuestionIndex(0);
    setActiveQuestionOptionIndex(0);
    setQuestionError('');
  };

  const restorePendingQuestion = (value: unknown) => {
    const data = asRecord(value);
    const questions = extractQuestionItems(data.questions);
    const questionId = typeof data.question_id === 'string' ? data.question_id : '';
    const request = questionId && questions?.length ? { question_id: questionId, questions } : null;
    setQuestionRequest(request);
    setQuestionAnswers(request ? Object.fromEntries(request.questions.map((question) => [question.id, { values: [], note: '' }])) : {});
    setActiveQuestionIndex(0);
    setActiveQuestionOptionIndex(0);
    setQuestionError('');
  };

  const focusQuestionOptions = () => window.requestAnimationFrame(() => questionOptionsRef.current?.focus());
  const focusQuestionNote = () => window.requestAnimationFrame(() => questionNoteRef.current?.focus());

  const updateQuestionChoice = (question: QuestionItem, value: string, checked: boolean) => {
    setQuestionAnswers((prev) => {
      const current = prev[question.id] || { values: [], note: '' };
      const values = question.multiple
        ? checked ? Array.from(new Set([...current.values, value])) : current.values.filter((item) => item !== value)
        : [value];
      return { ...prev, [question.id]: { ...current, values } };
    });
    setQuestionError('');
  };

  const updateQuestionNote = (questionId: string, note: string) => {
    setQuestionAnswers((prev) => ({ ...prev, [questionId]: { ...(prev[questionId] || { values: [], note: '' }), note } }));
  };

  const moveActiveQuestion = (step: number) => {
    if (!questionRequest) return;
    setActiveQuestionIndex((prev) => Math.min(Math.max(prev + step, 0), questionRequest.questions.length - 1));
    setActiveQuestionOptionIndex(0);
    setQuestionError('');
    focusQuestionOptions();
  };

  const confirmActiveQuestion = async () => {
    if (!questionRequest) return;
    const question = questionRequest.questions[activeQuestionIndex];
    const answer = question && questionAnswers[question.id];
    if (!question || !answer?.values.length) {
      setQuestionError('请先选择一个选项。');
      return;
    }
    setQuestionError('');
    if (activeQuestionIndex < questionRequest.questions.length - 1) {
      setActiveQuestionIndex((prev) => prev + 1);
      setActiveQuestionOptionIndex(0);
      focusQuestionOptions();
      return;
    }
    await onSubmit(questionRequest, questionAnswers);
  };

  const handleQuestionOptionsKeyDown = (event: KeyboardEvent<HTMLDivElement>, question: QuestionItem) => {
    if (!question.options.length) return;
    const lastIndex = question.options.length - 1;
    const chooseIndex = (nextIndex: number, shouldSelect: boolean) => {
      const index = Math.min(Math.max(nextIndex, 0), lastIndex);
      setActiveQuestionOptionIndex(index);
      const option = question.options[index];
      if (option && shouldSelect) updateQuestionChoice(question, option.value, true);
    };
    if (event.key === 'Tab') { event.preventDefault(); focusQuestionNote(); return; }
    if (event.key === 'ArrowDown') { event.preventDefault(); chooseIndex(activeQuestionOptionIndex >= lastIndex ? 0 : activeQuestionOptionIndex + 1, !question.multiple); return; }
    if (event.key === 'ArrowUp') { event.preventDefault(); chooseIndex(activeQuestionOptionIndex <= 0 ? lastIndex : activeQuestionOptionIndex - 1, !question.multiple); return; }
    if (event.key === 'Home') { event.preventDefault(); chooseIndex(0, !question.multiple); return; }
    if (event.key === 'End') { event.preventDefault(); chooseIndex(lastIndex, !question.multiple); return; }
    if (event.key !== ' ' && event.key !== 'Enter') return;
    event.preventDefault();
    const option = question.options[activeQuestionOptionIndex];
    const current = questionAnswers[question.id] || { values: [], note: '' };
    if (!option) return;
    if (event.key === ' ') {
      updateQuestionChoice(question, option.value, question.multiple ? !current.values.includes(option.value) : true);
      return;
    }
    if (!current.values.length) {
      updateQuestionChoice(question, option.value, true);
      return;
    }
    void confirmActiveQuestion();
  };

  const handleQuestionNoteKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Tab') { event.preventDefault(); focusQuestionOptions(); }
  };

  const handleQuestionDecline = async () => {
    if (questionRequest && await onDecline(questionRequest)) clearQuestion();
  };

  return {
    questionRequest, questionAnswers, activeQuestionIndex, activeQuestionOptionIndex, questionError,
    questionOptionsRef: questionOptionsRef as RefObject<HTMLDivElement>, questionNoteRef: questionNoteRef as RefObject<HTMLTextAreaElement>,
    setActiveQuestionIndex, setActiveQuestionOptionIndex, setQuestionError,
    restorePendingQuestion, clearQuestion, updateQuestionChoice, updateQuestionNote, focusQuestionOptions,
    moveActiveQuestion, confirmActiveQuestion, handleQuestionOptionsKeyDown, handleQuestionNoteKeyDown, handleQuestionDecline,
  };
}
