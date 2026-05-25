import type { Card, Stats, Queue } from './types';

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

  reviewCard: (id: string, rating: number) =>
    apiFetch<Card>(`/cards/${id}/review`, {
      method: 'POST',
      body: JSON.stringify({ rating }),
    }),

  updateCard: (id: string, fields: Partial<Card>) =>
    apiFetch<Card>(`/cards/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(fields),
    }),

  regenerateExamples: (id: string) =>
    apiFetch<Card>(`/cards/${id}/examples`, { method: 'POST' }),
};
