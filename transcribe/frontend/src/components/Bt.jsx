import { useState, useEffect, useRef } from 'react'
import { listBt, listTorrents, submitMagnet, deleteTorrent, previewDeleteTorrent, retryBtFile } from '../api'

function formatBytes(n) {
  if (n >= 1e12) return (n / 1e12).toFixed(2) + ' TB'
  if (n >= 1e9)  return (n / 1e9).toFixed(2) + ' GB'
  if (n >= 1e6)  return (n / 1e6).toFixed(0) + ' MB'
  if (n >= 1e3)  return (n / 1e3).toFixed(0) + ' KB'
  return n + ' B'
}

function buildDeleteConfirmText(plan) {
  const lines = [`Will free ~${formatBytes(plan.bt_size_bytes)} of disk. Files to remove:`, '']

  if (plan.bt_wrapper) {
    lines.push(plan.bt_wrapper + '/')
    const prefix = plan.bt_wrapper + '/'
    for (const f of plan.bt_files) {
      lines.push('  ' + (f.startsWith(prefix) ? f.slice(prefix.length) : f))
    }
    lines.push('')
  }

  if (plan.canonical_files.length) {
    lines.push('Library entries (Movies / TV):')
    for (const f of plan.canonical_files) lines.push('  ' + f)
    lines.push('')
  }

  if (plan.sources_files.length) {
    lines.push('Cached _sources/ files:')
    for (const f of plan.sources_files) lines.push('  ' + f)
    lines.push('')
  }

  if (plan.sentinel) {
    lines.push('Pipeline sentinel:')
    lines.push('  ' + plan.sentinel)
    lines.push('')
  }

  lines.push('No undo. Continue?')
  return lines.join('\n')
}

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
  done:        'Removed from aria2 or errored — no longer seeding',
  orphaned:    'Not tracked by aria2 (dormant / old completed torrent); files on disk are still usable',
}

export default function Bt() {
  const [items, setItems] = useState([])
  const [torrents, setTorrents] = useState([])
  // Optimistic default — until the first poll tells us otherwise we
  // assume aria2 is up, so the initial paint doesn't flash "disabled".
  const [aria2Up, setAria2Up] = useState(true)
  const [stats, setStats] = useState(null)
  const [magnet, setMagnet] = useState('')
  const [expanded, setExpanded] = useState(() => new Set())
  const [expandedRows, setExpandedRows] = useState(() => new Set())
  const submittingRef = useRef(false)

  async function refresh() {
    // Decoupled — listBt (transcribe local filesystem) and listTorrents
    // (aria2 sidecar via transcribe proxy) succeed or fail
    // independently. Items list still updates when aria2 is down.
    const [libRes, tRes] = await Promise.allSettled([listBt(), listTorrents()])
    if (libRes.status === 'fulfilled') setItems(libRes.value)
    else console.error('listBt failed:', libRes.reason)
    if (tRes.status === 'fulfilled') {
      // Envelope: {aria2_up, torrents, stats}
      setTorrents(tRes.value.torrents)
      setAria2Up(tRes.value.aria2_up)
      setStats(tRes.value.stats)
    } else {
      console.error('listTorrents failed:', tRes.reason)
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

  function toggleRowExpand(key) {
    setExpandedRows(prev => {
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
    let plan
    try {
      plan = await previewDeleteTorrent(wrapper)
    } catch (err) {
      alert('Preview failed: ' + err.message)
      return
    }
    if (!confirm(buildDeleteConfirmText(plan))) return
    try {
      await deleteTorrent(wrapper)
    } catch (err) {
      alert('Delete failed: ' + err.message)
      return
    }
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
          placeholder={aria2Up ? "Paste a magnet: link…" : "aria2 sidecar unreachable — new torrents disabled"}
          value={magnet}
          onChange={e => setMagnet(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && aria2Up) handleSubmit() }}
          disabled={!aria2Up}
          style={{ ...styles.magnetInput, ...(aria2Up ? {} : styles.disabledInput) }}
          title={aria2Up ? '' : 'aria2 sidecar is down — start it (utilities/aria2/) to submit new magnets'}
        />
        <button onClick={handleSubmit}
          disabled={!aria2Up}
          title={aria2Up ? 'Submit' : 'aria2 sidecar is down'}
          aria-label="Submit"
          style={{ ...styles.submitBtn, ...(aria2Up ? {} : styles.disabledBtn) }}>
          →
        </button>
      </div>

      {stats && (
        <div style={styles.statsBar}>
          <span>ratio {stats.ratio.toFixed(2)}</span>
          <span title={`total ↓ ${formatBytes(stats.total_downloaded)}   total ↑ ${formatBytes(stats.total_uploaded)}`}>
            {stats.active_count} active
          </span>
          <span>↓ {formatBytes(stats.download_speed)}/s</span>
          <span>↑ {formatBytes(stats.upload_speed)}/s</span>
        </div>
      )}

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
                    {t.progress && t.phase === 'downloading' && (
                      <span style={styles.progressText}
                            title={`${formatBytes(t.progress.completed)} / ${formatBytes(t.progress.total)}`}>
                        {(100 * t.progress.completed / t.progress.total).toFixed(1)}%
                      </span>
                    )}
                    {label && t.phase !== 'downloading' && (
                      <span style={styles.statusGlyph} title={PHASE_TITLE[t.phase] || t.phase}>{label}</span>
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
          {rows.map(item => {
            const rowExpanded = expandedRows.has(item.path)
            return (
              <RowItem key={`${item.path}-${rowExpanded}`} item={item}
                isExpanded={rowExpanded}
                onToggle={() => toggleRowExpand(item.path)}
                onRetry={handleRetry} />
            )
          })}
        </div>
      ))}
    </div>
  )
}

function RowItem({ item, isExpanded, onToggle, onRetry }) {
  const state = deriveState(item)
  const errMsg = item.pipeline_error || ''
  return (
    <div className="fade-in" style={styles.row} onClick={onToggle}>
      <div style={styles.topRow}>
        <div style={styles.name}>{item.name}</div>
        <div style={styles.slot}>
          {/* Compact status: ○ working / ✓ done (English + Chinese
              both landed) / ! failed. Actions live in the expanded
              action row below. */}
          {state === 'working' && (
            <span className="status-pulse" style={styles.glyph} title="Working / queued">○</span>
          )}
          {state === 'done' && (
            <span style={{ ...styles.glyph, color: '#636366' }} title="Annotated + translated">✓</span>
          )}
          {state === 'failed' && (
            <span style={styles.glyph} title={errMsg}>!</span>
          )}
        </div>
      </div>
      {isExpanded && state === 'failed' && (
        <div style={styles.actionRow}>
          <div style={{ flex: 1 }} />
          <button style={styles.actionBtn}
            title="Deep retry — nukes canonical + zh + failure sidecar + all _sources/ cache + archive mirror. Re-runs pipeline from scratch. ~30 min GPU per video."
            onClick={e => { e.stopPropagation(); onRetry(item.path) }}>↻</button>
        </div>
      )}
    </div>
  )
}

// Pipeline is a single track now (translate is the final stage), so
// state collapses to three: working, done, failed. `done` requires
// BOTH English `.srt` and `.zh-tw.srt`.
function deriveState(item) {
  if (item.pipeline_error) return 'failed'
  if (item.has_srt && item.has_zh_srt) return 'done'
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

  statsBar: {
    display: 'flex', gap: 20, alignItems: 'center',
    fontSize: 13, color: '#8e8e93', padding: '10px 4px', marginBottom: 6,
    fontVariantNumeric: 'tabular-nums',
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
  statusSlot: { display: 'flex', alignItems: 'center', flexShrink: 0, gap: 8 },
  progressText: {
    color: '#aeaeb2', fontSize: 12, fontFamily: MONO,
    fontVariantNumeric: 'tabular-nums', cursor: 'default',
  },
  statusGlyph: {
    color: '#aeaeb2', fontSize: 18, fontWeight: 700, lineHeight: 1,
    cursor: 'default', fontFamily: MONO,
  },
  actionRow: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 12, gap: 16 },
  deleteBtn: {
    background: 'none', border: 'none', color: '#636366',
    fontSize: 18, padding: 0, cursor: 'pointer', lineHeight: 1,
  },
  actionBtn: {
    background: 'none', border: '1px solid #c79968', color: '#c79968',
    fontSize: 12, fontWeight: 600, padding: '4px 10px', borderRadius: 6,
    cursor: 'pointer', lineHeight: 1, fontFamily: MONO,
  },
  disabledBtn: {
    opacity: 0.3, cursor: 'not-allowed',
  },
  disabledInput: {
    opacity: 0.4, cursor: 'not-allowed',
  },

  empty: { color: '#636366', textAlign: 'center', marginTop: 60, fontSize: 14 },

  group: { marginBottom: 24 },
  groupHeader: {
    fontFamily: MONO, fontSize: 12, color: '#aeaeb2',
    padding: '8px 4px', textTransform: 'lowercase', letterSpacing: 0.3,
  },
  row: {
    background: '#2c2c2e', borderRadius: 12, border: '1px solid #3a3a3c',
    boxShadow: '0 1px 4px rgba(0,0,0,0.3)',
    overflow: 'hidden',
    cursor: 'pointer',
    padding: '16px 20px', marginBottom: 12,
  },
  name: {
    flex: 1, minWidth: 0, fontSize: 14, color: '#e8e3d9',
    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
  },
  slot: { display: 'flex', alignItems: 'center', gap: 10, minWidth: 28, justifyContent: 'flex-end' },
  glyph: {
    color: '#aeaeb2', fontSize: 18, fontWeight: 700, lineHeight: 1,
    cursor: 'default', fontFamily: MONO,
  },
}
