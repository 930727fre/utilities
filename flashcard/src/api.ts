import type { Card, Settings, Stats, Queue } from './types';

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
  getStats:     () => apiFetch<Stats>('/stats'),
  getQueue:     () => apiFetch<Queue>('/cards/queue'),
  searchCards:  (q: string) => apiFetch<Card[]>(`/cards/search?q=${encodeURIComponent(q)}`),

  batchAddCards: (cards: Card[]) =>
    apiFetch<{ inserted: number }>('/cards/batch', {
      method: 'POST',
      body: JSON.stringify(cards),
    }),

  updateCard: (id: string, fields: Partial<Card>) =>
    apiFetch<Card>(`/cards/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(fields),
    }),

  updateSettings: (fields: Partial<Settings>) =>
    apiFetch<Settings>('/settings', {
      method: 'PATCH',
      body: JSON.stringify(fields),
    }),
};
