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

export async function listBt() {
  const r = await fetch(`${BASE}/api/bt`)
  if (!r.ok) throw new Error('Failed to list bt')
  return r.json()
}

export async function submitMagnet(magnet) {
  const r = await fetch(`${BASE}/api/bt/magnet`, {
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

export async function listTorrents() {
  const r = await fetch(`${BASE}/api/bt/torrents`)
  if (!r.ok) throw new Error('Failed to list torrents')
  return r.json()
}

export async function deleteTorrent(wrapper) {
  const r = await fetch(`${BASE}/api/bt/torrents/${encodeURIComponent(wrapper)}`, { method: 'DELETE' })
  if (!r.ok) {
    const detail = await r.json().catch(() => null)
    throw new Error(detail?.detail || 'Delete failed')
  }
  return r.json()
}

export async function previewDeleteTorrent(wrapper) {
  const r = await fetch(`${BASE}/api/bt/torrents/${encodeURIComponent(wrapper)}/preview-delete`)
  if (!r.ok) {
    const detail = await r.json().catch(() => null)
    throw new Error(detail?.detail || 'Preview failed')
  }
  return r.json()
}

export async function retryBtFile(path) {
  const r = await fetch(`${BASE}/api/bt/retry`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  })
  if (!r.ok) {
    const detail = await r.json().catch(() => null)
    throw new Error(detail?.detail || 'Retry failed')
  }
  return r.json()
}

