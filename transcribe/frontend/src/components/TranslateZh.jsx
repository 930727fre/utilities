import { useState, useEffect } from 'react'
import { listTranslateZh, retryTranslateZh } from '../api'

// translate_zh tab: filesystem scan only. User mv's annotated folders into
// /translate_zh on the host; the backend's scan loop fetches Chinese subs
// from OpenSubtitles and writes `<stem>.zh-tw.srt` next to each video.
// No magnet form, no jobs.json — every row's state derives from sidecar
// files in its folder.

export default function TranslateZh() {
  const [items, setItems] = useState([])

  async function refresh() {
    try {
      setItems(await listTranslateZh())
    } catch (e) {
      console.error(e)
    }
  }

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, 2000)
    return () => clearInterval(id)
  }, [])

  async function handleRetry(path) {
    try {
      await retryTranslateZh(path)
      await refresh()
    } catch (err) {
      alert('Retry failed: ' + err.message)
    }
  }

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
        button:focus, a:focus { outline: none; }
      `}</style>

      {items.length === 0 && (
        <p style={styles.empty}>Empty. Move an annotated folder into <code>data/translate_zh/</code> to start.</p>
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
  return (
    <div className="fade-in" style={styles.row}>
      <div style={styles.name}>{item.name}</div>
      <div style={styles.slot}>
        {state === 'working' && (
          <span className="status-pulse" style={styles.glyph} title="Looking for Chinese subs…">○</span>
        )}
        {state === 'done' && (
          <span style={{ ...styles.glyph, color: '#636366' }} title="Chinese sub found">✓</span>
        )}
        {state === 'failed' && (
          <>
            <span style={styles.glyph} title={item.error || 'Failed'}>!</span>
            <button style={styles.retryBtn} title="Retry (clears the cached miss and re-queries OpenSubtitles)"
              onClick={() => onRetry(item.path)}>↻</button>
          </>
        )}
      </div>
    </div>
  )
}

function deriveState(item) {
  if (item.has_zh_srt) return 'done'
  if (item.error) return 'failed'
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
