import { useState, useEffect, useRef } from 'react'
import { listJobs, submitJob, retryJob, deleteJob, downloadUrl, playerUrl } from '../api'

const STATUS_LABEL = {
  PENDING:      '○',
  DOWNLOADING:  '○',
  TRANSCRIBING: '○',
  SUCCESS:      '',
  FAILED:       '!',
}
const STATUS_TITLE = {
  PENDING:      'Pending',
  DOWNLOADING:  'Downloading',
  TRANSCRIBING: 'Transcribing',
  SUCCESS:      'Ready',
  FAILED:       'Failed',
}
const isWorking = (s) => s === 'PENDING' || s === 'DOWNLOADING' || s === 'TRANSCRIBING'

export default function JobList() {
  const [jobs, setJobs] = useState([])
  const [expandedIds, setExpandedIds] = useState(() => new Set())
  const [url, setUrl] = useState('')
  const submittingRef = useRef(false)

  async function refresh() {
    try {
      setJobs(await listJobs())
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
    const trimmed = url.trim()
    if (!trimmed || submittingRef.current) return
    submittingRef.current = true
    setUrl('')
    try {
      await submitJob(trimmed)
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
    if (!confirm('Delete this job and its files?')) return
    await deleteJob(id)
    setExpandedIds(prev => {
      const next = new Set(prev)
      next.delete(id)
      return next
    })
    await refresh()
  }

  const sorted = [...jobs].sort((a, b) => b.created_at.localeCompare(a.created_at))

  return (
    <div style={styles.page}>
      <style>{`
        @keyframes statusPulse { 0%,100% { opacity: 0.35 } 50% { opacity: 1 } }
        .status-pulse { animation: statusPulse 1.4s ease-in-out infinite; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
        .fade-in { animation: fadeIn 0.3s ease; }
        .url-input { outline: none; }
        .url-input:focus { box-shadow: 0 0 0 1px #c79968; }
        .url-input::placeholder { color: #636366; }
        button:focus, a:focus { outline: none; }
      `}</style>

      <h1 style={styles.title}>xyt</h1>

      <div style={styles.submitRow}>
        <input
          className="url-input"
          type="text"
          placeholder="Paste a YouTube URL…"
          value={url}
          onChange={e => setUrl(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') handleSubmit() }}
          style={styles.urlInput}
        />
        <button onClick={handleSubmit} title="Submit" aria-label="Submit" style={styles.submitBtn}>
          →
        </button>
      </div>

      <div style={styles.grid}>
        {sorted.length === 0 && (
          <p style={styles.empty}>No jobs yet. Paste a YouTube URL to start.</p>
        )}
        {sorted.map(job => {
          const isExpanded = expandedIds.has(job.job_id)
          const label = STATUS_LABEL[job.status] ?? ''
          return (
            <div key={`${job.job_id}-${isExpanded}`} className="fade-in" style={styles.card} onClick={() => toggleExpand(job.job_id)}>
              <div style={styles.topRow}>
                <div style={styles.info}>
                  <div style={styles.jobTitle}>{job.title}</div>
                  <div style={styles.jobUrl}>{job.url}</div>
                </div>
                <div style={styles.statusSlot}>
                  {label && (
                    <span className={isWorking(job.status) ? 'status-pulse' : ''} style={styles.statusGlyph} title={STATUS_TITLE[job.status] || job.status}>{label}</span>
                  )}
                </div>
              </div>
              {isExpanded && (
                <div style={styles.actionRow}>
                  <div style={styles.actionSlot}>
                    {job.status === 'SUCCESS' && (
                      <button style={styles.iconBtn} title="Play"
                        onClick={e => { e.stopPropagation(); window.open(playerUrl(job.job_id), '_blank') }}>▸</button>
                    )}
                    {job.status === 'FAILED' && (
                      <button style={styles.iconBtn} title="Retry"
                        onClick={e => { e.stopPropagation(); handleRetry(job.job_id) }}>↻</button>
                    )}
                  </div>
                  <div style={{ ...styles.actionSlot, textAlign: 'center' }}>
                    {job.status === 'SUCCESS' && job.files?.srt && (
                      <a href={downloadUrl(job.job_id, 'srt')} download style={styles.srtBtn} title="Download SRT"
                        onClick={e => e.stopPropagation()}>SRT</a>
                    )}
                  </div>
                  <div style={{ ...styles.actionSlot, textAlign: 'right' }}>
                    <button style={styles.deleteBtn} title="Delete"
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
    </div>
  )
}

const MONO = 'ui-monospace, SFMono-Regular, Menlo, monospace'

const styles = {
  page: { maxWidth: 720, margin: '0 auto', padding: '24px 16px' },
  title: { fontSize: 24, fontWeight: 700, letterSpacing: -0.5, color: '#e8e3d9', fontFamily: MONO, marginBottom: 24 },
  submitRow: { display: 'flex', gap: 12, marginBottom: 32 },
  urlInput: {
    flex: 1, background: '#2c2c2e', border: '1px solid #3a3a3c',
    borderRadius: 8, padding: '10px 16px', fontSize: 14, color: '#e8e3d9',
    fontFamily: 'inherit',
  },
  submitBtn: {
    background: '#c79968', color: '#1c1c1e', border: 'none',
    borderRadius: 8, padding: '6px 20px', cursor: 'pointer', fontSize: 22, fontWeight: 700,
    lineHeight: 1,
  },
  grid: { display: 'flex', flexDirection: 'column', gap: 12 },
  empty: { color: '#636366', textAlign: 'center', marginTop: 60, fontSize: 14 },
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
  jobTitle: { fontSize: 16, fontWeight: 600, marginBottom: 2, color: '#e8e3d9', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
  jobUrl: { fontSize: 12, color: '#aeaeb2', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
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
  srtBtn: {
    color: '#e8e3d9', fontSize: 14, fontWeight: 600, lineHeight: 1,
    textDecoration: 'none', cursor: 'pointer',
  },
  deleteBtn: {
    background: 'none', border: 'none', color: '#636366',
    fontSize: 18, padding: 0, cursor: 'pointer', lineHeight: 1,
  },
  errorText: { fontSize: 12, color: '#aeaeb2', marginTop: 8, wordBreak: 'break-all' },
}
