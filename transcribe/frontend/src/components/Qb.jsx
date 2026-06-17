import { useState, useEffect } from 'react'
import { listQb } from '../api'

export default function Qb() {
  const [items, setItems] = useState([])

  async function refresh() {
    try {
      setItems(await listQb())
    } catch (e) {
      console.error(e)
    }
  }

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, 2000)
    return () => clearInterval(id)
  }, [])

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
      `}</style>

      {items.length === 0 && (
        <p style={styles.empty}>No video files in /qb yet.</p>
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
