export interface Roleplay {
  id: string;
  date: string;
  topic: string;
  rationale: string;
  script: string;
}

export interface ErrorCandidate {
  id: string;
  title: string;
  you_said: string;
  native: string;
  note?: string;
}

export interface GraduateCandidate {
  id: string;
  title: string;
  evidence: string;
}

export interface DrillCard {
  id: string;
  prompt: string;
  answer: string;
  source_error_id?: string | null;
}

export interface TodayStats {
  streak_count: number;
  practice_done_today: boolean;
  drill_done_today: boolean;
  active_errors_count: number;
}

export interface ReviewBundle {
  additions: ErrorCandidate[];
  graduations: GraduateCandidate[];
}

export type PracticeStep = 'roleplay' | 'additions' | 'graduations';

export interface PracticeState {
  step: PracticeStep;
  session_id?: string;
}

export type UploadMode = 'roleplay' | 'freestyle';

export type DecisionAction = 'added' | 'skipped' | 'graduated' | 'kept';
