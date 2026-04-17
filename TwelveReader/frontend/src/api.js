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

export async function getSpine(bookId) {
  const r = await fetch(`${BASE}/api/books/${bookId}/spine`)
  if (!r.ok) throw new Error('Failed to load spine')
  return r.json()  // [{ index, href }, ...]
}

export async function getParagraphs(bookId) {
  const r = await fetch(`${BASE}/api/books/${bookId}/paragraphs`)
  if (!r.ok) throw new Error('Failed to load paragraphs')
  return r.json()
}

export async function getBookmark(bookId) {
  const r = await fetch(`${BASE}/api/books/${bookId}/bookmark`)
  if (!r.ok) return null
  return r.json()
}

export async function putBookmark(bookId, paragraphId) {
  await fetch(`${BASE}/api/books/${bookId}/bookmark`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ paragraph_id: paragraphId }),
  })
}

export async function requestTTS(bookId, paragraphId) {
  const r = await fetch(`${BASE}/api/tts/${bookId}/${paragraphId}`, { method: 'POST' })
  if (!r.ok) throw new Error('TTS failed')
  return r.json()  // { url, cached }
}

export async function clearTTSCache(bookId) {
  await fetch(`${BASE}/api/tts/${bookId}/cache`, { method: 'DELETE' })
}

export async function evictTTSCache(bookId, paragraphId) {
  await fetch(`${BASE}/api/tts/${bookId}/${paragraphId}`, { method: 'DELETE' })
}
