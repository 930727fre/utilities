const BASE = import.meta.env.VITE_API_URL || ''

export async function listJobs() {
  const r = await fetch(`${BASE}/api/jobs`)
  if (!r.ok) throw new Error('Failed to list jobs')
  return r.json()
}

export async function submitJob(url) {
  const r = await fetch(`${BASE}/api/jobs`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url }),
  })
  if (!r.ok) throw new Error('Submit failed')
  return r.json()
}

export async function retryJob(id) {
  const r = await fetch(`${BASE}/api/jobs/${id}/retry`, { method: 'POST' })
  if (!r.ok) throw new Error('Retry failed')
  return r.json()
}

export async function deleteJob(id) {
  const r = await fetch(`${BASE}/api/jobs/${id}`, { method: 'DELETE' })
  if (!r.ok) throw new Error('Delete failed')
  return r.json()
}

export async function annotateJob(id) {
  const r = await fetch(`${BASE}/api/jobs/${id}/annotate`, { method: 'POST' })
  if (!r.ok) throw new Error('Annotate failed')
  return r.json()
}

export function downloadUrl(jobId, kind) {
  return `${BASE}/api/download/${jobId}/${kind}`
}

export function playerUrl(jobId) {
  return `${BASE}/player/${jobId}`
}
