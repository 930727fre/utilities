import { useState, useEffect, useRef } from 'react'
import { listBooks, uploadBook, deleteBook, getBook, zipUrl } from '../api'

const STATUS_TITLE = { PARSING: 'Converting', READY: 'Ready', FAILED: 'Failed' }

export default function BookList() {
  const [books, setBooks] = useState([])
  const [uploading, setUploading] = useState(false)
  const [expandedIds, setExpandedIds] = useState(() => new Set())
  const fileRef = useRef()

  function toggleExpand(id) {
    setExpandedIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  useEffect(() => { refresh() }, [])

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
    if (!confirm('Delete this file?')) return
    await deleteBook(bookId)
    setBooks(prev => prev.filter(b => b.id !== bookId))
    setExpandedIds(prev => {
      const next = new Set(prev)
      next.delete(bookId)
      return next
    })
  }

  return (
    <div style={styles.page}>
      <style>{`
        @keyframes statusPulse { 0%,100% { opacity: 0.35 } 50% { opacity: 1 } }
        .status-pulse { animation: statusPulse 1.4s ease-in-out infinite; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
        .fade-in { animation: fadeIn 0.3s ease; }
        button:focus, a:focus { outline: none; }
      `}</style>
      <header style={styles.header}>
        <h1 style={styles.title}>marker-pipeline</h1>
        <button style={styles.uploadBtn} onClick={() => fileRef.current.click()} disabled={uploading} title="Add file">
          {uploading ? '…' : '+'}
        </button>
        <input ref={fileRef} type="file" accept=".epub,.pdf" style={{ display: 'none' }} onChange={handleUpload} />
      </header>

      <div style={styles.grid}>
        {books.length === 0 && (
          <p style={styles.empty}>No files yet. Upload an EPUB or PDF to convert.</p>
        )}
        {books.map(book => {
          const isExpanded = expandedIds.has(book.id)
          return (
            <div key={`${book.id}-${isExpanded}`} className="fade-in" style={styles.card} onClick={() => toggleExpand(book.id)}>
              <div style={styles.topRow}>
                <div style={styles.cardInfo}>
                  <div style={styles.bookTitle}>{book.title}</div>
                  {book.author && <div style={styles.bookAuthor}>{book.author}</div>}
                </div>
                <div style={styles.statusSlot}>
                  {book.status === 'PARSING' && (
                    <span className="status-pulse" style={styles.statusGlyph} title={STATUS_TITLE.PARSING}>○</span>
                  )}
                  {book.status === 'FAILED' && (
                    <span style={styles.statusGlyph} title={STATUS_TITLE.FAILED}>!</span>
                  )}
                </div>
              </div>
              {isExpanded && (
                <div style={styles.actionRow}>
                  <div style={styles.actionSlot}>
                    {book.status === 'READY' && (
                      <a href={zipUrl(book.id)} style={styles.downloadBtn} download
                        title={STATUS_TITLE.READY} onClick={e => e.stopPropagation()}>↓</a>
                    )}
                  </div>
                  <div style={{ ...styles.actionSlot, textAlign: 'right' }}>
                    <button style={styles.deleteBtn} title="Delete"
                      onClick={e => { e.stopPropagation(); handleDelete(book.id) }}>✕</button>
                  </div>
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

const styles = {
  page: { maxWidth: 720, margin: '0 auto', padding: '24px 16px' },
  header: { display: 'flex', alignItems: 'center', marginBottom: 28 },
  title: { flex: 1, fontSize: 24, fontWeight: 700, letterSpacing: -0.5, color: '#e8e3d9', fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' },
  uploadBtn: {
    background: '#c79968', color: '#1c1c1e', border: 'none',
    borderRadius: 8, padding: '6px 14px', cursor: 'pointer', fontSize: 22, fontWeight: 700,
    lineHeight: 1, minWidth: 40,
  },
  grid: { display: 'flex', flexDirection: 'column', gap: 12 },
  empty: { color: '#636366', textAlign: 'center', marginTop: 60 },
  card: {
    background: '#2c2c2e', borderRadius: 12,
    border: '1px solid #3a3a3c',
    boxShadow: '0 1px 4px rgba(0,0,0,0.3)',
    overflow: 'hidden',
    cursor: 'pointer',
    padding: '16px 20px',
  },
  topRow: { display: 'flex', alignItems: 'center', gap: 12 },
  cardInfo: { flex: 1, minWidth: 0 },
  bookTitle: { fontSize: 16, fontWeight: 600, marginBottom: 2, color: '#e8e3d9', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
  bookAuthor: { fontSize: 12, color: '#aeaeb2' },
  statusSlot: { display: 'flex', alignItems: 'center', flexShrink: 0 },
  actionRow: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 12 },
  actionSlot: { minWidth: 24 },
  statusGlyph: {
    color: '#aeaeb2', fontSize: 18, fontWeight: 700, lineHeight: 1,
    cursor: 'default', fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
  },
  downloadBtn: {
    color: '#e8e3d9', fontSize: 24, fontWeight: 700, lineHeight: 1,
    textDecoration: 'none', cursor: 'pointer',
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
  },
  deleteBtn: {
    background: 'none', border: 'none', color: '#636366',
    fontSize: 18, padding: 0, cursor: 'pointer', lineHeight: 1,
  },
}
