import { useState, useEffect, useRef } from 'react'
import { listQb, listJobs, submitMagnet, retryJob, deleteJob } from '../api'

// qb tab now has two regions: the magnet submission form + active downloads
// at top (symmetric to the yt tab), and the library listing below. Once a
// magnet's first file lands the download job flips to SUCCESS — whisper +
// annotation are tracked per-file in the library list below, not on the job.

const STATUS_LABEL = {
  PENDING:     '○',
  DOWNLOADING: '○',
  SUCCESS:     '',
  FAILED:      '!',
}
const STATUS_TITLE = {
  PENDING:     'Pending',
  DOWNLOADING: 'Downloading',
  SUCCESS:     'Landed',
  FAILED:      'Failed',
}
const isWorking = (s) => s === 'PENDING' || s === 'DOWNLOADING'

export default function Qb() {
  const [items, setItems] = useState([])
  const [jobs, setJobs] = useState([])
  const [magnet, setMagnet] = useState('')
  const [expandedIds, setExpandedIds] = useState(() => new Set())
  const submittingRef = useRef(false)

  async function refresh() {
    try {
      const [i, j] = await Promise.all([listQb(), listJobs('qb')])
      setItems(i)
      // Only show magnet-initiated jobs; the legacy `/api/qb/transcribe`
      // entry doesn't carry a magnet field and is for manual re-runs only.
      setJobs(j.filter(x => x.magnet))
    } catch (e) {
      console.error(e)
    }
  }

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, 2000)
    return () => clearInterval(id)
  }, [])

  function toggleExpand(id) {
    setExpandedIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
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

  async function handleRetry(id) {
    await retryJob(id)
    await refresh()
  }

  async function handleDelete(id) {
    if (!confirm('Delete this job? (downloaded files stay on disk)')) return
    await deleteJob(id)
    setExpandedIds(prev => {
      const next = new Set(prev)
      next.delete(id)
      return next
    })
    await refresh()
  }

  const sortedJobs = [...jobs].sort((a, b) => b.created_at.localeCompare(a.created_at))

  // qBittorrent typically makes one folder per torrent / season — group by it.
  // Loose files at /qb root land under an empty group with no header.
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

      {sortedJobs.length > 0 && (
        <div style={styles.jobsGrid}>
          {sortedJobs.map(job => {
            const isExpanded = expandedIds.has(job.job_id)
            const label = STATUS_LABEL[job.status] ?? ''
            return (
              <div key={`${job.job_id}-${isExpanded}`} className="fade-in" style={styles.card} onClick={() => toggleExpand(job.job_id)}>
                <div style={styles.topRow}>
                  <div style={styles.info}>
                    <div style={styles.jobTitle}>{job.title}</div>
                  </div>
                  <div style={styles.statusSlot}>
                    {label && (
                      <span className={isWorking(job.status) ? 'status-pulse' : ''} style={styles.statusGlyph} title={STATUS_TITLE[job.status] || job.status}>{label}</span>
                    )}
                  </div>
                </div>
                {isExpanded && (
                  <div style={styles.actionRow}>
                    <div style={{ ...styles.actionSlot, display: 'flex', gap: 16, alignItems: 'center' }}>
                      {job.status === 'FAILED' && (
                        <button style={styles.iconBtn} title="Retry"
                          onClick={e => { e.stopPropagation(); handleRetry(job.job_id) }}>↻</button>
                      )}
                    </div>
                    <div style={{ ...styles.actionSlot, textAlign: 'right' }}>
                      <button style={styles.deleteBtn} title="Delete job entry (downloaded files stay)"
                        onClick={e => { e.stopPropagation(); handleDelete(job.job_id) }}>✕</button>
                    </div>
                  </div>
                )}
                {isExpanded && job.status === 'FAILED' && job.error && (
                  <p style={styles.errorText}>{job.error}</p>
                )}
              </div>
            )
          })}
        </div>
      )}

      {items.length === 0 && sortedJobs.length === 0 && (
        <p style={styles.empty}>No torrents yet. Paste a magnet link to start.</p>
      )}

      {[...groups.entries()].map(([groupKey, rows]) => (
        <div key={groupKey || '__root__'} style={styles.group}>
          {groupKey && <div style={styles.groupHeader}>{groupKey}</div>}
          {rows.map(item => <RowItem key={item.path} item={item} />)}
        </div>
      ))}
    </div>
  )
}

function RowItem({ item }) {
  const state = deriveState(item)
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
        {state === 'whisper_blocked' && (
          <span style={styles.glyph} title="Whisper failed 3 times — restart container to retry">!</span>
        )}
        {state === 'annotation_blocked' && (
          <span style={styles.glyph} title="Annotation failed 3 times — restart container to retry">!</span>
        )}
      </div>
    </div>
  )
}

function deriveState(item) {
  if (item.in_flight_job_id) return 'working'
  if (item.has_annotation) return 'done'
  if (!item.has_srt && item.whisper_blocked) return 'whisper_blocked'
  if (item.has_srt && item.annotation_blocked) return 'annotation_blocked'
  // Pending whisper or annotation — background loop will pick it up within 30s.
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

  jobsGrid: { display: 'flex', flexDirection: 'column', gap: 12, marginBottom: 24 },
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
  jobTitle: { fontSize: 14, color: '#e8e3d9', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
  statusSlot: { display: 'flex', alignItems: 'center', flexShrink: 0 },
  statusGlyph: {
    color: '#aeaeb2', fontSize: 18, fontWeight: 700, lineHeight: 1,
    cursor: 'default', fontFamily: MONO,
  },
  actionRow: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 12 },
  actionSlot: { minWidth: 24 },
  iconBtn: {
    background: 'none', border: 'none', color: '#e8e3d9',
    fontSize: 24, fontWeight: 700, lineHeight: 1, cursor: 'pointer', padding: 0,
    fontFamily: MONO,
  },
  deleteBtn: {
    background: 'none', border: 'none', color: '#636366',
    fontSize: 18, padding: 0, cursor: 'pointer', lineHeight: 1,
  },
  errorText: { fontSize: 12, color: '#aeaeb2', marginTop: 8, wordBreak: 'break-all' },

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
  slot: { display: 'flex', alignItems: 'center', minWidth: 28, justifyContent: 'flex-end' },
  glyph: {
    color: '#aeaeb2', fontSize: 18, fontWeight: 700, lineHeight: 1,
    cursor: 'default', fontFamily: MONO,
  },
}
