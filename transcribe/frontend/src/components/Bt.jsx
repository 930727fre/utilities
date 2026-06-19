import { useState, useEffect, useRef } from 'react'
import { listBt, listTorrents, submitMagnet, deleteTorrent, retryBtFile } from '../api'

// bt tab has two regions: the magnet form + active-torrent list at the
// top (each row = one wrapper folder, phase derived from filesystem + an
// in-memory subprocess registry on the backend), and the library list
// below (filesystem scan, annotation state). No bt-side persistence in
// jobs.json — the wrapper folder name IS the identifier.

const PHASE_LABEL = {
  downloading: '○',
  seeding:     '↑',
  done:        '✓',
  orphaned:    '–',
}
const PHASE_TITLE = {
  downloading: 'Downloading',
  seeding:     'Seeding',
  done:        'Seed limit reached',
  orphaned:    'Subprocess gone (container was restarted); files on disk are still usable',
}

export default function Bt() {
  const [items, setItems] = useState([])
  const [torrents, setTorrents] = useState([])
  const [magnet, setMagnet] = useState('')
  const [expanded, setExpanded] = useState(() => new Set())
  const submittingRef = useRef(false)

  async function refresh() {
    try {
      const [lib, t] = await Promise.all([listBt(), listTorrents()])
      setItems(lib)
      setTorrents(t)
    } catch (e) {
      console.error(e)
    }
  }

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, 2000)
    return () => clearInterval(id)
  }, [])

  function toggleExpand(key) {
    setExpanded(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  async function handleSubmit() {
    const trimmed = magnet.trim()
    if (!trimmed || submittingRef.current) return
    submittingRef.current = true
    setMagnet('')
    try {
      await submitMagnet(trimmed)
      await refresh()
    } catch (err) {
      alert('Submit failed: ' + err.message)
    } finally {
      submittingRef.current = false
    }
  }

  async function handleDelete(wrapper) {
    if (!confirm('Delete this torrent and its files?')) return
    await deleteTorrent(wrapper)
    setExpanded(prev => {
      const next = new Set(prev)
      next.delete(wrapper)
      return next
    })
    await refresh()
  }

  async function handleRetry(path) {
    try {
      await retryBtFile(path)
      await refresh()
    } catch (err) {
      alert('Retry failed: ' + err.message)
    }
  }

  const sortedTorrents = [...torrents].sort((a, b) => a.name.localeCompare(b.name))

  // Each torrent lives in its own per-torrent wrapper folder — group by it.
  // Loose files at /bt root land under an empty group with no header.
  const groups = new Map()
  for (const item of items) {
    const key = item.parent || ''
    if (!groups.has(key)) groups.set(key, [])
    groups.get(key).push(item)
  }

  return (
    <div style={styles.page}>
      <style>{`
        @keyframes statusPulse { 0%,100% { opacity: 0.35 } 50% { opacity: 1 } }
        .status-pulse { animation: statusPulse 1.4s ease-in-out infinite; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
        .fade-in { animation: fadeIn 0.3s ease; }
        .magnet-input { outline: none; }
        .magnet-input:focus { box-shadow: 0 0 0 1px #c79968; }
        .magnet-input::placeholder { color: #636366; }
        button:focus, a:focus { outline: none; }
      `}</style>

      <div style={styles.submitRow}>
        <input
          className="magnet-input"
          type="text"
          placeholder="Paste a magnet: link…"
          value={magnet}
          onChange={e => setMagnet(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') handleSubmit() }}
          style={styles.magnetInput}
        />
        <button onClick={handleSubmit} title="Submit" aria-label="Submit" style={styles.submitBtn}>
          →
        </button>
      </div>

      {sortedTorrents.length > 0 && (
        <div style={styles.torrentsGrid}>
          {sortedTorrents.map(t => {
            const isExpanded = expanded.has(t.name)
            const label = PHASE_LABEL[t.phase] ?? ''
            return (
              <div key={`${t.name}-${isExpanded}`} className="fade-in" style={styles.card} onClick={() => toggleExpand(t.name)}>
                <div style={styles.topRow}>
                  <div style={styles.info}>
                    <div style={styles.cardTitle}>{t.name}</div>
                  </div>
                  <div style={styles.statusSlot}>
                    {label && (
                      <span className={t.phase === 'downloading' ? 'status-pulse' : ''} style={styles.statusGlyph} title={PHASE_TITLE[t.phase] || t.phase}>{label}</span>
                    )}
                  </div>
                </div>
                {isExpanded && (
                  <div style={styles.actionRow}>
                    <div style={{ flex: 1 }} />
                    <button style={styles.deleteBtn} title="Delete torrent + files"
                      onClick={e => { e.stopPropagation(); handleDelete(t.name) }}>✕</button>
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}

      {items.length === 0 && sortedTorrents.length === 0 && (
        <p style={styles.empty}>No torrents yet. Paste a magnet link to start.</p>
      )}

      {[...groups.entries()].map(([groupKey, rows]) => (
        <div key={groupKey || '__root__'} style={styles.group}>
          {groupKey && <div style={styles.groupHeader}>{groupKey}</div>}
          {rows.map(item => <RowItem key={item.path} item={item} onRetry={handleRetry} />)}
        </div>
      ))}
    </div>
  )
}

function RowItem({ item, onRetry }) {
  const state = deriveState(item)
  const errMsg = item.whisper_error || item.annotate_error || ''
  return (
    <div className="fade-in" style={styles.row}>
      <div style={styles.name}>{item.name}</div>
      <div style={styles.slot}>
        {state === 'working' && (
          <span className="status-pulse" style={styles.glyph} title="Working / queued">○</span>
        )}
        {state === 'done' && (
          <span style={{ ...styles.glyph, color: '#636366' }} title="Annotated">✓</span>
        )}
        {state === 'failed' && (
          <>
            <span style={styles.glyph} title={errMsg}>!</span>
            <button style={styles.retryBtn} title="Retry (deletes the SRT and re-runs the pipeline)"
              onClick={() => onRetry(item.path)}>↻</button>
          </>
        )}
      </div>
    </div>
  )
}

function deriveState(item) {
  if (item.in_flight_job_id) return 'working'
  if (item.has_annotation) return 'done'
  if (item.whisper_error || item.annotate_error) return 'failed'
  return 'working'
}

const MONO = 'ui-monospace, SFMono-Regular, Menlo, monospace'

const styles = {
  page: { maxWidth: 720, margin: '0 auto', padding: '0 16px 24px' },

  submitRow: { display: 'flex', gap: 12, marginBottom: 32 },
  magnetInput: {
    flex: 1, background: '#2c2c2e', border: '1px solid #3a3a3c',
    borderRadius: 8, padding: '10px 16px', fontSize: 14, color: '#e8e3d9',
    fontFamily: 'inherit',
  },
  submitBtn: {
    background: '#c79968', color: '#1c1c1e', border: 'none',
    borderRadius: 8, padding: '6px 20px', cursor: 'pointer', fontSize: 22, fontWeight: 700,
    lineHeight: 1,
  },

  torrentsGrid: { display: 'flex', flexDirection: 'column', gap: 12, marginBottom: 24 },
  card: {
    background: '#2c2c2e', borderRadius: 12,
    border: '1px solid #3a3a3c',
    boxShadow: '0 1px 4px rgba(0,0,0,0.3)',
    overflow: 'hidden',
    cursor: 'pointer',
    padding: '16px 20px',
  },
  topRow: { display: 'flex', alignItems: 'center', gap: 12 },
  info: { flex: 1, minWidth: 0 },
  cardTitle: { fontSize: 14, color: '#e8e3d9', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
  statusSlot: { display: 'flex', alignItems: 'center', flexShrink: 0 },
  statusGlyph: {
    color: '#aeaeb2', fontSize: 18, fontWeight: 700, lineHeight: 1,
    cursor: 'default', fontFamily: MONO,
  },
  actionRow: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 12 },
  deleteBtn: {
    background: 'none', border: 'none', color: '#636366',
    fontSize: 18, padding: 0, cursor: 'pointer', lineHeight: 1,
  },

  empty: { color: '#636366', textAlign: 'center', marginTop: 60, fontSize: 14 },

  group: { marginBottom: 24 },
  groupHeader: {
    fontFamily: MONO, fontSize: 12, color: '#aeaeb2',
    padding: '8px 4px', textTransform: 'lowercase', letterSpacing: 0.3,
  },
  row: {
    display: 'flex', alignItems: 'center', gap: 12,
    background: '#2c2c2e', borderRadius: 12, border: '1px solid #3a3a3c',
    boxShadow: '0 1px 4px rgba(0,0,0,0.3)',
    padding: '12px 20px', marginBottom: 8,
  },
  name: {
    flex: 1, minWidth: 0, fontSize: 14, color: '#e8e3d9',
    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
  },
  slot: { display: 'flex', alignItems: 'center', gap: 12, minWidth: 28, justifyContent: 'flex-end' },
  glyph: {
    color: '#aeaeb2', fontSize: 18, fontWeight: 700, lineHeight: 1,
    cursor: 'default', fontFamily: MONO,
  },
  retryBtn: {
    background: 'none', border: 'none', color: '#c79968',
    fontSize: 18, fontWeight: 700, lineHeight: 1, padding: 0, cursor: 'pointer',
    fontFamily: MONO,
  },
}
