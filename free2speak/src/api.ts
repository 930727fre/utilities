import type {
  Roleplay,
  DrillCard,
  TodayStats,
  ReviewBundle,
  PracticeState,
  UploadMode,
  DecisionAction,
} from './types';

const API_BASE = '/api';

async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error((err as { detail?: string }).detail ?? `HTTP ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  getTodayStats: () => apiFetch<TodayStats>('/today/stats'),

  getTodayRoleplay: () => apiFetch<Roleplay>('/today/roleplay'),

  uploadAudio: async (file: File, mode: UploadMode) => {
    const form = new FormData();
    form.append('file', file);
    form.append('mode', mode);
    const res = await fetch(`${API_BASE}/upload`, { method: 'POST', body: form });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error((err as { detail?: string }).detail ?? `HTTP ${res.status}`);
    }
    return res.json() as Promise<{ session_id: string; mode: UploadMode }>;
  },

  getReview: () => apiFetch<ReviewBundle>('/today/review'),

  getPracticeState: () => apiFetch<PracticeState>('/today/practice/state'),

  decide: (sessionId: string, candidateId: string, action: DecisionAction) =>
    apiFetch<{ recorded: boolean }>(`/sessions/${sessionId}/decide`, {
      method: 'POST',
      body: JSON.stringify({ candidate_id: candidateId, action }),
    }),

  getTodayDrill: () => apiFetch<DrillCard[]>('/today/drill'),
};
