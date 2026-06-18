const BASE = import.meta.env.VITE_API_URL || ''

export async function listJobs(source = 'yt') {
  const r = await fetch(`${BASE}/api/jobs?source=${source}`)
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

export async function listQb() {
  const r = await fetch(`${BASE}/api/qb`)
  if (!r.ok) throw new Error('Failed to list qb')
  return r.json()
}

export async function submitMagnet(magnet) {
  const r = await fetch(`${BASE}/api/qb/magnet`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ magnet }),
  })
  if (!r.ok) {
    const detail = await r.json().catch(() => null)
    throw new Error(detail?.detail || 'Submit failed')
  }
  return r.json()
}
