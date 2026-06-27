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
  if (!r.ok) throw new Error('Delete failed')
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

export async function translateTorrentZh(wrapper) {
  const r = await fetch(`${BASE}/api/bt/translate-zh`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ wrapper }),
  })
  if (!r.ok) {
    const detail = await r.json().catch(() => null)
    throw new Error(detail?.detail || 'Translate failed')
  }
  return r.json()
}

export async function upgradeEnglishTorrent(wrapper) {
  const r = await fetch(`${BASE}/api/bt/upgrade-english`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ wrapper }),
  })
  if (!r.ok) {
    const detail = await r.json().catch(() => null)
    throw new Error(detail?.detail || 'Upgrade failed')
  }
  return r.json()
}

export async function translateFileZh(path) {
  const r = await fetch(`${BASE}/api/bt/translate-zh-file`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  })
  if (!r.ok) {
    const detail = await r.json().catch(() => null)
    throw new Error(detail?.detail || 'Translate failed')
  }
  return r.json()
}

export async function resolvePlay(path) {
  const r = await fetch(`${BASE}/api/play/resolve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  })
  if (!r.ok) {
    const detail = await r.json().catch(() => null)
    throw new Error(detail?.detail || 'Resolve failed')
  }
  return r.json()
}

// Fire-and-forget; a dropped progress beat just means the resume point
// is a few seconds stale.
export function reportProgress({ path, positionSeconds }) {
  return fetch(`${BASE}/api/play/progress`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      path,
      position_seconds: positionSeconds,
    }),
    keepalive: true,  // lets the final beat fire during page unload
  }).catch(() => {})
}

// Tell live-hls to tear down a session (kill ffmpeg, rm work dir). Called
// when the modal closes. live-hls's idle GC handles the case where this
// never reaches it.
export function endLiveHlsSession(baseUrl, sid) {
  if (!baseUrl || !sid) return Promise.resolve()
  return fetch(`${baseUrl}/api/${sid}`, { method: 'DELETE', keepalive: true })
    .catch(() => {})
}
