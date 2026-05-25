import type {
  Roleplay,
  DrillCard,
  TodayStats,
  ReviewBundle,
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

  uploadAudio: async (file: File) => {
    const form = new FormData();
    form.append('file', file);
    const res = await fetch(`${API_BASE}/upload`, { method: 'POST', body: form });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error((err as { detail?: string }).detail ?? `HTTP ${res.status}`);
    }
    return res.json() as Promise<{ session_id: string; date: string; topic: string }>;
  },

  getReview: () => apiFetch<ReviewBundle>('/today/review'),

  applyAdditions: (candidateIds: string[]) =>
    apiFetch<{ added: number }>('/errors/additions', {
      method: 'POST',
      body: JSON.stringify({ candidate_ids: candidateIds }),
    }),

  applyGraduations: (errorIds: string[]) =>
    apiFetch<{ graduated: number }>('/errors/graduations', {
      method: 'POST',
      body: JSON.stringify({ error_ids: errorIds }),
    }),

  getTodayDrill: () => apiFetch<DrillCard[]>('/today/drill'),
};
