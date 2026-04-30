const BASE = import.meta.env.VITE_API_URL || ''

export async function listBooks() {
  const r = await fetch(`${BASE}/api/books`)
  if (!r.ok) throw new Error('Failed to list books')
  return r.json()
}

export async function getBook(bookId) {
  const r = await fetch(`${BASE}/api/books/${bookId}`)
  if (!r.ok) throw new Error('Book not found')
  return r.json()
}

export async function uploadBook(file) {
  const fd = new FormData()
  fd.append('file', file)
  const r = await fetch(`${BASE}/api/books`, { method: 'POST', body: fd })
  if (!r.ok) throw new Error('Upload failed')
  return r.json()
}

export async function deleteBook(bookId) {
  const r = await fetch(`${BASE}/api/books/${bookId}`, { method: 'DELETE' })
  if (!r.ok) throw new Error('Delete failed')
  return r.json()
}

export async function getMd(bookId) {
  const r = await fetch(`${BASE}/api/books/${bookId}/md`)
  if (!r.ok) throw new Error('Failed to load book content')
  return r.text()
}

export async function getBookmark(bookId) {
  const r = await fetch(`${BASE}/api/books/${bookId}/bookmark`)
  if (!r.ok) return null
  return r.json()  // { paragraph_index } or null
}

export async function putBookmark(bookId, paragraphIndex) {
  await fetch(`${BASE}/api/books/${bookId}/bookmark`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ paragraph_index: paragraphIndex }),
  })
}

export async function requestTTS(bookId, index) {
  const r = await fetch(`${BASE}/api/tts/${bookId}/${index}`, { method: 'POST' })
  if (!r.ok) throw new Error('TTS failed')
  return r.json()  // { url, cached }
}

export async function clearTTSCache(bookId) {
  await fetch(`${BASE}/api/tts/${bookId}/cache`, { method: 'DELETE' })
}

export async function evictTTSCache(bookId, index) {
  await fetch(`${BASE}/api/tts/${bookId}/${index}`, { method: 'DELETE' })
}
