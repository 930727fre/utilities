import { useEffect, useRef, useState, useCallback } from 'react'
import { getParagraphs, getSpine, getBookmark } from '../api'
import { usePlayer } from '../hooks/usePlayer'
import Player from './Player'

function rewriteHtml(html, bookId, chapterHref) {
  const base = chapterHref.includes('/')
    ? chapterHref.slice(0, chapterHref.lastIndexOf('/') + 1)
    : ''
  // Strip EPUB stylesheets so our dark theme has full control
  const stripped = html
    .replace(/<style[\s\S]*?<\/style>/gi, '')
    .replace(/<link[^>]+rel=["']stylesheet["'][^>]*\/?>/gi, '')
  return stripped.replace(
    /(href|src|url\()=?"([^"#>)][^"#>)]*?)("|\)|>|\s)/g,
    (match, attr, url, end) => {
      if (/^https?:\/\/|^data:|^#/.test(url)) return match
      const resolved = base ? base + url : url
      const rewritten = `/api/books/${bookId}/item/${resolved}`
      return `${attr}="${rewritten}"${end === ')' ? ')' : end}`
    }
  )
}

function injectParagraphIds(container, spineHref, paragraphs) {
  const spineParagraphs = paragraphs.filter(p => p.spine_href === spineHref)
  const elems = [...container.querySelectorAll('p,h1,h2,h3,li')]
    .filter(el => el.textContent.replace(/\s+/g, ' ').trim())
  spineParagraphs.forEach((para, i) => {
    if (elems[i]) elems[i].setAttribute('data-paragraph-id', para.paragraph_id)
  })
}

export default function Reader({ book, onClose, backendDown }) {
  const [paragraphs, setParagraphs] = useState([])
  const [spine, setSpine] = useState([])
  const [spineIndex, setSpineIndex] = useState(0)
  const [chapterHtml, setChapterHtml] = useState('')
  const [loadError, setLoadError] = useState(null)
  const contentRef = useRef(null)
  const bookmarkRestoredRef = useRef(false)

  const player = usePlayer(book.id, paragraphs)
  const playerRef = useRef(player)

  useEffect(() => { if (backendDown) player.pause() }, [backendDown])
  const paragraphsRef = useRef(paragraphs)
  useEffect(() => { playerRef.current = player }, [player])
  useEffect(() => { paragraphsRef.current = paragraphs }, [paragraphs])

  useEffect(() => {
    Promise.all([getParagraphs(book.id), getSpine(book.id)])
      .then(([paras, sp]) => { setParagraphs(paras); setSpine(sp) })
      .catch(e => setLoadError(e.message))
  }, [book.id])

  useEffect(() => {
    if (spine.length === 0) return
    const item = spine[spineIndex]
    if (!item) return
    fetch(`/api/books/${book.id}/item/${item.href}`)
      .then(r => {
        if (!r.ok) throw new Error(`Failed to load chapter: ${item.href}`)
        return r.text()
      })
      .then(html => setChapterHtml(rewriteHtml(html, book.id, item.href)))
      .catch(e => setLoadError(e.message))
  }, [spineIndex, spine, book.id])

  useEffect(() => {
    if (!contentRef.current || spine.length === 0 || paragraphs.length === 0) return
    const item = spine[spineIndex]
    if (!item) return
    injectParagraphIds(contentRef.current, item.href, paragraphs)

    const currentPara = paragraphsRef.current[playerRef.current.currentIndex]
    if (currentPara) {
      highlightParagraph(currentPara.paragraph_id)
      scrollToParagraph(currentPara.paragraph_id)
    }

    if (!bookmarkRestoredRef.current) {
      bookmarkRestoredRef.current = true
      getBookmark(book.id).then(bookmark => {
        if (!bookmark) return
        playerRef.current.resumeFromBookmark(bookmark.paragraph_id)
        scrollToParagraph(bookmark.paragraph_id)
      })
    }
  }, [chapterHtml, paragraphs, spine, spineIndex])

  useEffect(() => {
    if (player.currentIndex < 0) return
    const para = paragraphs[player.currentIndex]
    if (!para) return
    const spineItem = spine.findIndex(s => s.href === para.spine_href)
    if (spineItem >= 0 && spineItem !== spineIndex) {
      setSpineIndex(spineItem)
      return
    }
    highlightParagraph(para.paragraph_id)
    scrollToParagraph(para.paragraph_id)
  }, [player.currentIndex])

  function highlightParagraph(pid) {
    if (!contentRef.current) return
    contentRef.current.querySelectorAll('.tr-highlight')
      .forEach(el => el.classList.remove('tr-highlight'))
    const el = contentRef.current.querySelector(`[data-paragraph-id="${pid}"]`)
    if (el) el.classList.add('tr-highlight')
  }

  function scrollToParagraph(pid) {
    if (!contentRef.current) return
    const el = contentRef.current.querySelector(`[data-paragraph-id="${pid}"]`)
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }

  const handleClick = useCallback((e) => {
    const el = e.target.closest('[data-paragraph-id]')
    if (!el) return
    const pid = el.getAttribute('data-paragraph-id')
    const idx = paragraphsRef.current.findIndex(p => p.paragraph_id === pid)
    if (idx >= 0) playerRef.current.seekTo(idx)
  }, [])

  return (
    <div style={styles.root}>
      <div style={styles.topBar}>
        <button style={styles.backBtn} onClick={() => { player.pause(); onClose() }}>← Library</button>
        <span style={styles.bookTitle}>{book.title}</span>
        <button style={styles.navBtn} onClick={() => setSpineIndex(i => Math.max(0, i - 1))} disabled={spineIndex === 0}>‹</button>
        <button style={styles.navBtn} onClick={() => setSpineIndex(i => Math.min(spine.length - 1, i + 1))} disabled={spineIndex >= spine.length - 1}>›</button>
      </div>

      <div style={styles.viewContainer}>
        {loadError && <div style={styles.msg}>{loadError}</div>}
        {!chapterHtml && !loadError && <div style={styles.msg}>Loading…</div>}
        <div
          ref={contentRef}
          style={styles.content}
          onClick={handleClick}
          dangerouslySetInnerHTML={{ __html: chapterHtml }}
        />
      </div>

      <Player
        state={player.state}
        currentIndex={player.currentIndex}
        paragraphs={paragraphs}
        onPlay={player.play}
        onPause={player.pause}
        onResume={player.resume}
      />

      <style>{`
        .tr-highlight { background: rgba(255,200,50,0.3) !important; border-radius: 3px; }
        [data-paragraph-id] { color: #e0dbd0 !important; }
        [data-paragraph-id] a[href] { color: #7eb8f7 !important; }
        h1, h2, h3, h4, h5, h6 { color: #e8e3d9 !important; }
        h1 a, h2 a, h3 a, h4 a, h5 a, h6 a { color: inherit !important; }
        img { max-width: 100%; height: auto; }
      `}</style>
    </div>
  )
}

const styles = {
  root: { display: 'flex', flexDirection: 'column', height: '100vh', background: '#1c1c1e' },
  topBar: {
    display: 'flex', alignItems: 'center', gap: 16,
    padding: '12px 20px', background: '#2c2c2e',
    borderBottom: '1px solid #3a3a3c', flexShrink: 0,
  },
  backBtn: { background: 'none', border: 'none', cursor: 'pointer', fontSize: 14, color: '#aeaeb2' },
  bookTitle: { fontSize: 15, fontWeight: 600, flex: 1, textAlign: 'center', color: '#e8e3d9' },
  navBtn: { background: 'none', border: 'none', cursor: 'pointer', fontSize: 22, color: '#aeaeb2', padding: '0 8px' },
  viewContainer: { flex: 1, overflowY: 'auto' },
  content: { maxWidth: 720, margin: '0 auto', padding: '40px 48px 120px', lineHeight: 1.8, color: '#e0dbd0' },
  msg: { textAlign: 'center', marginTop: 80, color: '#636366' },
}
