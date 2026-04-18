// src/lib/fsrs.ts
import {
  fsrs,
  Rating,
  type Grade,
  type Card as FSRSCard,
  type FSRSParameters
} from 'ts-fsrs';
import type { Card } from '../types';


export function createFSRSCard(card: Card): FSRSCard {
  // Coerce to Number in case backend returns numeric fields as strings
  return {
    due: new Date(card.due),
    stability: Number(card.stability) || 0,
    difficulty: Number(card.difficulty) || 0,
    elapsed_days: Number(card.elapsed_days) || 0,
    scheduled_days: Number(card.scheduled_days) || 0,
    lapses: Number(card.lapses) || 0,
    state: Number(card.state) || 0,
    last_review: card.last_review ? new Date(card.last_review) : undefined,
    reps: 0,
    learning_steps: 0,
  } as FSRSCard;
}

// 這裡的參數型別也同步改為 FSRSParameters
export function computeNext(card: Card, rating: Rating, params: FSRSParameters) {
  const f = fsrs(params);
  const fsrsCard = createFSRSCard(card);
  const next = f.next(fsrsCard, new Date(), rating as Grade).card;

  return {
    due: next.due.toISOString(),
    stability: next.stability,
    difficulty: next.difficulty,
    elapsed_days: next.elapsed_days,
    scheduled_days: next.scheduled_days,
    lapses: next.lapses,
    state: next.state,
    last_review: new Date().toISOString(),
  };
}