import { useEffect, useRef, useState, useCallback } from 'react'
import { useVirtualizer } from '@tanstack/react-virtual'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { getMd, getBookmark } from '../api'
import { usePlayer } from '../hooks/usePlayer'
import Player from './Player'

function splitParagraphs(md) {
  if (md.startsWith('---')) {
    for (const marker of ['\n---\n', '\n---', '\n...\n', '\n...']) {
      const pos = md.indexOf(marker, 3)
      if (pos !== -1) { md = md.slice(pos + marker.length); break }
    }
  }
  return md.split(/\n{2,}/).map(b => b.trim()).filter(Boolean)
}

export default function Reader({ book, onClose, backendDown }) {
  const [paragraphs, setParagraphs] = useState([])
  const [loadError, setLoadError] = useState(null)
  const parentRef = useRef(null)
  const bookmarkRestoredRef = useRef(false)

  const player = usePlayer(book.id, paragraphs)
  const playerRef = useRef(player)
  useEffect(() => { playerRef.current = player }, [player])
  useEffect(() => { if (backendDown) player.pause() }, [backendDown])

  useEffect(() => {
    getMd(book.id)
      .then(md => setParagraphs(splitParagraphs(md)))
      .catch(e => setLoadError(e.message))
  }, [book.id])

  const virtualizer = useVirtualizer({
    count: paragraphs.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 100,
    overscan: 5,
  })

  // Scroll to current paragraph when it changes
  useEffect(() => {
    if (player.currentIndex >= 0) {
      virtualizer.scrollToIndex(player.currentIndex, { align: 'center', behavior: 'smooth' })
    }
  }, [player.currentIndex])

  // Restore bookmark once paragraphs are loaded
  useEffect(() => {
    if (paragraphs.length === 0 || bookmarkRestoredRef.current) return
    bookmarkRestoredRef.current = true
    getBookmark(book.id).then(bm => {
      if (!bm) return
      const idx = bm.paragraph_index
      playerRef.current.resumeFromBookmark(idx)
      virtualizer.scrollToIndex(idx, { align: 'center' })
    })
  }, [paragraphs])

  const handleClick = useCallback((index) => {
    playerRef.current.seekTo(index)
  }, [])

  return (
    <div style={styles.root}>
      <div style={styles.topBar}>
        <button style={styles.backBtn} onClick={() => { player.pause(); onClose() }}>← Library</button>
        <span style={styles.bookTitle}>{book.title}</span>
      </div>

      <div ref={parentRef} style={styles.scroll}>
        {loadError && <div style={styles.msg}>{loadError}</div>}
        {!paragraphs.length && !loadError && <div style={styles.msg}>Loading…</div>}
        {paragraphs.length > 0 && (
          <div style={{ height: virtualizer.getTotalSize(), position: 'relative' }}>
            {virtualizer.getVirtualItems().map(vItem => (
              <div
                key={vItem.key}
                data-index={vItem.index}
                ref={virtualizer.measureElement}
                style={{
                  position: 'absolute', top: 0, left: 0, width: '100%',
                  transform: `translateY(${vItem.start}px)`,
                }}
              >
                <div
                  className="tr-content"
                  style={{
                    ...styles.para,
                    background: vItem.index === player.currentIndex
                      ? 'rgba(255,200,50,0.15)' : 'transparent',
                  }}
                  onClick={() => handleClick(vItem.index)}
                >
                  <ReactMarkdown
                    remarkPlugins={[remarkGfm]}
                    components={{
                      img: ({src, alt}) => (
                        <img
                          src={src?.startsWith('images/') ? `/api/books/${book.id}/assets/${src}` : src}
                          alt={alt}
                          style={{maxWidth:'100%', height:'auto', borderRadius:4}}
                        />
                      )
                    }}
                  >{paragraphs[vItem.index]}</ReactMarkdown>
                </div>
              </div>
            ))}
          </div>
        )}
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
        .tr-content p, .tr-content li { color: #e0dbd0; line-height: 1.8; margin: 0; }
        .tr-content h1, .tr-content h2, .tr-content h3,
        .tr-content h4, .tr-content h5, .tr-content h6 { color: #e8e3d9; margin: 0.4em 0 0.2em; }
        .tr-content a { color: #7eb8f7; }
        .tr-content strong { color: #f0ebe0; }
        .tr-content em { color: #d4cfbf; }
        .tr-content img { max-width: 100%; height: auto; border-radius: 4px; }
        .tr-content table { border-collapse: collapse; width: 100%; margin: 0.5em 0; }
        .tr-content th, .tr-content td { border: 1px solid #3a3a3c; padding: 6px 12px; text-align: left; color: #e0dbd0; }
        .tr-content th { background: #2c2c2e; font-weight: 600; }
      `}</style>
    </div>
  )
}

const styles = {
  root: { display: 'flex', flexDirection: 'column', height: '100vh', background: '#1c1c1e' },
  topBar: {
    display: 'flex', alignItems: 'center', gap: 16,
    padding: '12px 20px',
    paddingTop: 'max(12px, env(safe-area-inset-top))',
    background: '#2c2c2e', borderBottom: '1px solid #3a3a3c', flexShrink: 0,
  },
  backBtn: { background: 'none', border: 'none', cursor: 'pointer', fontSize: 14, color: '#aeaeb2' },
  bookTitle: { fontSize: 15, fontWeight: 600, flex: 1, textAlign: 'center', color: '#e8e3d9', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
  scroll: { flex: 1, overflowY: 'auto', paddingBottom: 80 },
  para: {
    maxWidth: 720, margin: '0 auto', padding: '12px 48px',
    cursor: 'pointer', borderRadius: 6, transition: 'background 0.15s',
  },
  msg: { textAlign: 'center', marginTop: 80, color: '#636366' },
}
