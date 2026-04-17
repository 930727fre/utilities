import { useState, useEffect, useRef } from 'react'
import { listBooks, uploadBook, deleteBook, getBook } from '../api'

const STATUS_LABEL = { PARSING: 'Parsing…', READY: 'Ready', FAILED: 'Failed' }
const STATUS_COLOR = { PARSING: '#f0a500', READY: '#30d158', FAILED: '#ff453a' }

export default function BookList({ onOpen }) {
  const [books, setBooks] = useState([])
  const [uploading, setUploading] = useState(false)
  const fileRef = useRef()

  useEffect(() => {
    refresh()
  }, [])

  // Poll parsing books
  useEffect(() => {
    const parsing = books.filter(b => b.status === 'PARSING')
    if (parsing.length === 0) return
    const id = setTimeout(async () => {
      const updated = await Promise.all(
        parsing.map(b => getBook(b.id).catch(() => b))
      )
      setBooks(prev =>
        prev.map(b => {
          const u = updated.find(x => x.id === b.id)
          return u ? u : b
        })
      )
    }, 2000)
    return () => clearTimeout(id)
  }, [books])

  async function refresh() {
    try {
      setBooks(await listBooks())
    } catch (e) {
      console.error(e)
    }
  }

  async function handleUpload(e) {
    const file = e.target.files[0]
    if (!file) return
    setUploading(true)
    try {
      const { book_id } = await uploadBook(file)
      const newBook = { id: book_id, title: file.name, author: '', status: 'PARSING' }
      setBooks(prev => [newBook, ...prev])
    } catch (err) {
      alert('Upload failed: ' + err.message)
    } finally {
      setUploading(false)
      fileRef.current.value = ''
    }
  }

  async function handleDelete(bookId) {
    if (!confirm('Delete this book?')) return
    await deleteBook(bookId)
    setBooks(prev => prev.filter(b => b.id !== bookId))
  }

  return (
    <div style={styles.page}>
      <header style={styles.header}>
        <h1 style={styles.title}>TwelveReader</h1>
        <button style={styles.uploadBtn} onClick={() => fileRef.current.click()} disabled={uploading}>
          {uploading ? 'Uploading…' : '+ Add Book'}
        </button>
        <input ref={fileRef} type="file" accept=".epub" style={{ display: 'none' }} onChange={handleUpload} />
      </header>

      <div style={styles.grid}>
        {books.length === 0 && (
          <p style={styles.empty}>No books yet. Upload an EPUB to get started.</p>
        )}
        {books.map(book => (
          <div key={book.id} style={styles.card}>
            <div style={styles.cardBody} onClick={() => book.status === 'READY' && onOpen(book)}>
              <div style={styles.bookIcon}>📖</div>
              <div style={styles.cardInfo}>
                <div style={styles.bookTitle}>{book.title}</div>
                {book.author && <div style={styles.bookAuthor}>{book.author}</div>}
                <div style={{ ...styles.status, color: STATUS_COLOR[book.status] }}>
                  {STATUS_LABEL[book.status] || book.status}
                </div>
                {book.status === 'FAILED' && (
                  <div style={styles.failMsg}>This book failed to parse. Please re-upload.</div>
                )}
              </div>
            </div>
            <button style={styles.deleteBtn} onClick={() => handleDelete(book.id)}>✕</button>
          </div>
        ))}
      </div>
    </div>
  )
}

const styles = {
  page: { maxWidth: 720, margin: '0 auto', padding: '24px 16px' },
  header: { display: 'flex', alignItems: 'center', marginBottom: 28 },
  title: { flex: 1, fontSize: 28, fontWeight: 700, letterSpacing: -0.5, color: '#e8e3d9' },
  uploadBtn: {
    background: '#3a3a3c', color: '#e8e3d9', border: 'none',
    borderRadius: 8, padding: '8px 18px', cursor: 'pointer', fontSize: 14,
  },
  grid: { display: 'flex', flexDirection: 'column', gap: 12 },
  empty: { color: '#636366', textAlign: 'center', marginTop: 60 },
  card: {
    display: 'flex', alignItems: 'center',
    background: '#2c2c2e', borderRadius: 12,
    boxShadow: '0 1px 4px rgba(0,0,0,0.3)',
    overflow: 'hidden',
  },
  cardBody: { flex: 1, display: 'flex', alignItems: 'center', gap: 16, padding: '16px 20px', cursor: 'pointer' },
  bookIcon: { fontSize: 36 },
  cardInfo: { flex: 1 },
  bookTitle: { fontSize: 16, fontWeight: 600, marginBottom: 2, color: '#e8e3d9' },
  bookAuthor: { fontSize: 13, color: '#aeaeb2', marginBottom: 4 },
  status: { fontSize: 12, fontWeight: 600 },
  failMsg: { fontSize: 11, color: '#ff453a', marginTop: 2 },
  deleteBtn: {
    background: 'none', border: 'none', color: '#636366',
    fontSize: 16, padding: '16px 14px', cursor: 'pointer',
  },
}
