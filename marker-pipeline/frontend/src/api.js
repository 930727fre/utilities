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

export function zipUrl(bookId) {
  return `${BASE}/api/books/${bookId}/zip`
}
