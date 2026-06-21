import { useState, useEffect, useRef } from 'react'
import { listBt, listTorrents, submitMagnet, deleteTorrent, retryBtFile, translateTorrentZh } from '../api'

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

  async function handleTranslate(wrapper, count) {
    const ok = confirm(`Translate ${count} video${count === 1 ? '' : 's'} in this torrent to 繁體中文?\n\n` +
      `Gemini Flash Lite — ~$0.01 and ~1 minute per video.`)
    if (!ok) return
    try {
      const res = await translateTorrentZh(wrapper)
      if (res.queued === 0) {
        alert(`Already translated — nothing queued.`)
      }
      await refresh()
    } catch (err) {
      alert('Translate failed: ' + err.message)
    }
  }

  // Group videos under their torrent's outer-wrapper name so we can decide
  // whether a torrent is ready to translate (all videos annotated, not
  // downloading, none currently being translated) and surface per-video
  // Chinese-sub state on the row.
  const itemsByTorrent = new Map()
  for (const item of items) {
    const torrentName = (item.parent || '').split('/')[0]
    if (!torrentName) continue
    if (!itemsByTorrent.has(torrentName)) itemsByTorrent.set(torrentName, [])
    itemsByTorrent.get(torrentName).push(item)
  }

  function translateStatus(torrent) {
    if (torrent.phase === 'downloading') {
      return { ready: false, count: 0, reason: 'Torrent still downloading' }
    }
    const myItems = itemsByTorrent.get(torrent.name) || []
    if (myItems.length === 0) {
      return { ready: false, count: 0, reason: 'No videos found in this torrent yet' }
    }
    if (myItems.some(it => it.zh_in_flight)) {
      return { ready: false, count: 0, reason: 'Translation in progress…' }
    }
    const done = myItems.filter(it => it.has_annotation).length
    if (done < myItems.length) {
      return { ready: false, count: 0, reason: `${done} of ${myItems.length} videos annotated — wait for the rest` }
    }
    const untranslated = myItems.filter(it => !it.has_zh_srt).length
    if (untranslated === 0) {
      return { ready: false, count: 0, reason: 'All videos already have Chinese subs' }
    }
    return {
      ready: true,
      count: untranslated,
      reason: `Translate ${untranslated} video${untranslated === 1 ? '' : 's'} to 繁體中文 via Gemini`,
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
                {isExpanded && (() => {
                  const ts = translateStatus(t)
                  return (
                    <div style={styles.actionRow}>
                      <div style={{ flex: 1 }} />
                      <button style={{ ...styles.translateBtn, ...(ts.ready ? {} : styles.disabledBtn) }}
                        title={ts.reason}
                        disabled={!ts.ready}
                        onClick={e => { e.stopPropagation(); handleTranslate(t.name, ts.count) }}>→ 中</button>
                      <button style={styles.deleteBtn} title="Delete torrent + files"
                        onClick={e => { e.stopPropagation(); handleDelete(t.name) }}>✕</button>
                    </div>
                  )
                })()}
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
  const engState = deriveEngState(item)
  const zhState = deriveZhState(item)
  const engErrMsg = item.whisper_error || item.annotate_error || ''
  // `※ os failed:` means OS lookup missed and whisper took over. The
  // English SRT is fine, but the user may want to retry to get a
  // human-translated OS sub now that the daily quota has reset.
  // Only surface when whisper had to step in (the marker only appears
  // on whisper-produced SRTs, never on bundled/OS-success paths).
  const osFailed = item.os_failed
  return (
    <div className="fade-in" style={styles.row}>
      <div style={styles.name}>{item.name}</div>
      <div style={styles.slot}>
        {engState === 'working' && (
          <span className="status-pulse" style={styles.glyph} title="Working / queued">○</span>
        )}
        {engState === 'done' && (
          <span style={{ ...styles.glyph, color: '#636366' }} title="Annotated">✓</span>
        )}
        {engState === 'done' && osFailed && (
          <button style={styles.retryBtn}
            title={`OS missed (${osFailed}) — whisper produced this SRT. Click to retry the OpenSubtitles lookup (deletes the SRT and re-runs the full pipeline, including re-annotation).`}
            onClick={() => onRetry(item.path)}>E</button>
        )}
        {engState === 'failed' && (
          <>
            <span style={styles.glyph} title={engErrMsg}>!</span>
            <button style={styles.retryBtn} title="Retry (deletes the SRT and re-runs the pipeline)"
              onClick={() => onRetry(item.path)}>↻</button>
          </>
        )}
        {/* zh state only meaningful after annotation is done */}
        {engState === 'done' && zhState !== 'absent' && (
          <span style={styles.zhDivider}>·</span>
        )}
        {engState === 'done' && zhState === 'translating' && (
          <span className="status-pulse" style={styles.zhGlyph} title="Translating to 繁體中文">中</span>
        )}
        {engState === 'done' && zhState === 'done' && (
          <span style={styles.zhGlyph} title="Chinese sub ready">中</span>
        )}
        {engState === 'done' && zhState === 'failed' && (
          <span style={{ ...styles.zhGlyph, color: '#c79968' }} title={`中: ${item.zh_error}`}>!</span>
        )}
      </div>
    </div>
  )
}

function deriveEngState(item) {
  if (item.in_flight_job_id) return 'working'
  if (item.has_annotation) return 'done'
  if (item.whisper_error || item.annotate_error) return 'failed'
  return 'working'
}

function deriveZhState(item) {
  if (item.zh_in_flight) return 'translating'
  if (item.has_zh_srt) return 'done'
  if (item.zh_error) return 'failed'
  return 'absent'
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
  actionRow: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 12, gap: 16 },
  deleteBtn: {
    background: 'none', border: 'none', color: '#636366',
    fontSize: 18, padding: 0, cursor: 'pointer', lineHeight: 1,
  },
  translateBtn: {
    background: 'none', border: '1px solid #c79968', color: '#c79968',
    fontSize: 12, fontWeight: 600, padding: '4px 10px', borderRadius: 6,
    cursor: 'pointer', lineHeight: 1, fontFamily: MONO,
  },
  disabledBtn: {
    opacity: 0.3, cursor: 'not-allowed',
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
  slot: { display: 'flex', alignItems: 'center', gap: 10, minWidth: 28, justifyContent: 'flex-end' },
  glyph: {
    color: '#aeaeb2', fontSize: 18, fontWeight: 700, lineHeight: 1,
    cursor: 'default', fontFamily: MONO,
  },
  zhDivider: {
    color: '#3a3a3c', fontSize: 14, lineHeight: 1, cursor: 'default',
  },
  zhGlyph: {
    color: '#aeaeb2', fontSize: 14, fontWeight: 700, lineHeight: 1,
    cursor: 'default', fontFamily: MONO,
  },
  retryBtn: {
    background: 'none', border: 'none', color: '#c79968',
    fontSize: 18, fontWeight: 700, lineHeight: 1, padding: 0, cursor: 'pointer',
    fontFamily: MONO,
  },
}
