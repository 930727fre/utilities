const BASE = import.meta.env.VITE_API_URL || ''

export async function listJobs(source = 'youtube') {
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

export async function listLibrary() {
  const r = await fetch(`${BASE}/api/library`)
  if (!r.ok) throw new Error('Failed to list library')
  return r.json()
}

export async function transcribeFile(path) {
  const r = await fetch(`${BASE}/api/library/transcribe`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  })
  if (!r.ok) throw new Error('Transcribe failed')
  return r.json()
}
