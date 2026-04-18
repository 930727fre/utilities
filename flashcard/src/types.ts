import type { State } from 'ts-fsrs';

export interface Card {
  id: string;
  word: string;
  sentence: string;
  note: string;
  due: string;           // ISO 8601
  stability: number;
  difficulty: number;
  elapsed_days: number;
  scheduled_days: number;
  lapses: number;
  state: State;          // 0=New, 1=Learning, 2=Review, 3=Relearning
  last_review: string;   // ISO 8601
  lang: string;
  created_at: string;    // ISO 8601
}

export interface Settings {
  fsrs_params: string;
  streak_count: string;
  streak_last_date: string;
  daily_new_count: number | string;
  last_modified: string;
}

export interface Stats {
  streak_count: string;
  due_count: number;
  new_available: number;
}

export interface Queue {
  cards: Card[];
  daily_new_count: string;
  fsrs_params: string;
}
