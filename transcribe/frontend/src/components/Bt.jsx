import { useState, useEffect, useRef } from 'react'
import Hls from 'hls.js'
import { listBt, listTorrents, submitMagnet, deleteTorrent, retryBtFile, translateTorrentZh, translateFileZh, upgradeEnglishTorrent, resolvePlay, reportProgress } from '../api'

const PROGRESS_REPORT_INTERVAL_SEC = 1

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
  const [expandedRows, setExpandedRows] = useState(() => new Set())
  const [playing, setPlaying] = useState(null)  // { path, name } when modal is open
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

  async function handleTranslateFile(path) {
    try {
      await translateFileZh(path)
      await refresh()
    } catch (err) {
      alert('Translate failed: ' + err.message)
    }
  }

  async function handleUpgradeEnglish(wrapper, count) {
    const ok = confirm(
      `Retry OpenSubtitles for ${count} video${count === 1 ? '' : 's'} in this torrent?\n\n` +
      `These SRTs were produced by whisper after OS missed (likely quota-exhausted). ` +
      `Retrying deletes each affected SRT and re-runs the full cascade — including a ` +
      `fresh Claude annotation pass (~$0.05 each).`
    )
    if (!ok) return
    try {
      const res = await upgradeEnglishTorrent(wrapper)
      if (res.deleted === 0) {
        alert('Nothing was upgraded — videos may have moved out of the os-failed state already.')
      }
      await refresh()
    } catch (err) {
      alert('Upgrade failed: ' + err.message)
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

  function upgradeEnglishStatus(torrent) {
    if (torrent.phase === 'downloading') {
      return { ready: false, count: 0, reason: 'Torrent still downloading' }
    }
    const myItems = itemsByTorrent.get(torrent.name) || []
    const candidates = myItems.filter(it => it.os_failed && !it.in_flight_job_id)
    if (candidates.length === 0) {
      return { ready: false, count: 0, reason: 'No whisper-fallback videos to upgrade' }
    }
    return {
      ready: true,
      count: candidates.length,
      reason: `Retry OpenSubtitles for ${candidates.length} video${candidates.length === 1 ? '' : 's'} ` +
              `that fell back to whisper (will re-annotate, ~$0.05 each)`,
    }
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
                  const ues = upgradeEnglishStatus(t)
                  return (
                    <div style={styles.actionRow}>
                      <div style={{ flex: 1 }} />
                      <button style={{ ...styles.translateBtn, ...(ues.ready ? {} : styles.disabledBtn) }}
                        title={ues.reason}
                        disabled={!ues.ready}
                        onClick={e => { e.stopPropagation(); handleUpgradeEnglish(t.name, ues.count) }}>→ E</button>
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
          {rows.map(item => {
            const rowExpanded = expandedRows.has(item.path)
            return (
              <RowItem key={`${item.path}-${rowExpanded}`} item={item}
                isExpanded={rowExpanded}
                onToggle={() => toggleRowExpand(item.path)}
                onRetry={handleRetry}
                onTranslate={handleTranslateFile}
                onPlay={() => setPlaying({ path: item.path, name: item.name })} />
            )
          })}
        </div>
      ))}

      {playing && <PlayerModal path={playing.path} name={playing.name} onClose={() => setPlaying(null)} />}
    </div>
  )
}

function PlayerModal({ path, name, onClose }) {
  const videoRef = useRef(null)
  const [resolved, setResolved] = useState(null)
  const [error, setError] = useState(null)
  // One play session id per modal mount. Jellyfin uses this to dedupe
  // "started" against later "progress" / "stopped" events on the same
  // playback so the watch history doesn't list one episode twice.
  const playSessionId = useRef(crypto.randomUUID())
  // Track last-reported position so we throttle progress beats to ~10s
  // even though the video element fires timeupdate every ~250ms.
  const lastReportRef = useRef(0)

  // Fetch the master.m3u8 URL + subtitle list from transcribe's resolver.
  useEffect(() => {
    let cancelled = false
    resolvePlay(path)
      .then(r => { if (!cancelled) setResolved(r) })
      .catch(e => { if (!cancelled) setError(e.message) })
    return () => { cancelled = true }
  }, [path])

  // Attach HLS to <video>. Safari plays HLS natively; everything else needs hls.js.
  useEffect(() => {
    if (!resolved || !videoRef.current) return
    const video = videoRef.current
    const url = resolved.master_url
    let hls = null
    if (video.canPlayType('application/vnd.apple.mpegurl')) {
      video.src = url
    } else if (Hls.isSupported()) {
      hls = new Hls()
      hls.loadSource(url)
      hls.attachMedia(video)
    } else {
      setError('HLS not supported by this browser')
    }

    // Resume to the position Jellyfin stored from prior playback (any
    // device). Set on loadedmetadata so currentTime is honored — setting
    // it before metadata loads is a no-op in some browsers.
    function onLoaded() {
      if (resolved.resume_at_seconds > 0 && video.duration > resolved.resume_at_seconds + 2) {
        video.currentTime = resolved.resume_at_seconds
      }
    }
    video.addEventListener('loadedmetadata', onLoaded)

    return () => {
      video.removeEventListener('loadedmetadata', onLoaded)
      if (hls) hls.destroy()
    }
  }, [resolved])

  // Playback event reporting. "started" once on first play, "progress"
  // every 10s while playing, "stopped" on close / ended. unmount sends
  // a final stopped beat with the last currentTime — keepalive=true
  // makes it survive page nav too.
  useEffect(() => {
    if (!resolved || !videoRef.current) return
    const video = videoRef.current
    const itemId = resolved.item_id
    const sessionId = playSessionId.current
    let started = false

    function fire(event, isPaused = false) {
      reportProgress({
        itemId,
        positionSeconds: video.currentTime || 0,
        event,
        playSessionId: sessionId,
        isPaused,
      })
    }

    function onPlay() {
      if (!started) { fire('started'); started = true }
      else { fire('progress', false) }
    }
    function onPause() { if (started) fire('progress', true) }
    function onTimeUpdate() {
      if (!started) return
      const now = video.currentTime
      if (Math.abs(now - lastReportRef.current) >= PROGRESS_REPORT_INTERVAL_SEC) {
        lastReportRef.current = now
        fire('progress', video.paused)
      }
    }
    function onEnded() { if (started) fire('stopped') }

    video.addEventListener('play', onPlay)
    video.addEventListener('pause', onPause)
    video.addEventListener('timeupdate', onTimeUpdate)
    video.addEventListener('ended', onEnded)

    return () => {
      video.removeEventListener('play', onPlay)
      video.removeEventListener('pause', onPause)
      video.removeEventListener('timeupdate', onTimeUpdate)
      video.removeEventListener('ended', onEnded)
      // Final stopped beat — modal close, route change, or page unload
      // all land here. keepalive in reportProgress() ensures it ships.
      if (started) fire('stopped')
    }
  }, [resolved])

  // Esc to close.
  useEffect(() => {
    function onKey(e) { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div style={styles.modalBackdrop} onClick={onClose}>
      <div style={styles.modalBody} onClick={e => e.stopPropagation()}>
        <div style={styles.modalHeader}>
          <div style={styles.modalTitle}>{name}</div>
          <button onClick={onClose} style={styles.modalClose} title="Close (Esc)">✕</button>
        </div>
        {error && <div style={styles.modalError}>{error}</div>}
        {!resolved && !error && <div style={styles.modalLoading}>Resolving…</div>}
        {resolved && (
          <video ref={videoRef} controls autoPlay crossOrigin="anonymous" style={styles.video}>
            {resolved.subtitles.map((s, i) => (
              <track key={s.src} kind="subtitles" label={s.label} srcLang={s.srclang}
                src={s.src} default={i === 0} />
            ))}
          </video>
        )}
      </div>
    </div>
  )
}

function RowItem({ item, isExpanded, onToggle, onRetry, onTranslate, onPlay }) {
  const engState = deriveEngState(item)
  const zhState = deriveZhState(item)
  const engErrMsg = item.whisper_error || item.annotate_error || ''
  return (
    <div className="fade-in" style={styles.row} onClick={onToggle}>
      <div style={styles.topRow}>
        <div style={styles.name}>{item.name}</div>
        <div style={styles.slot}>
          {/* Compact status: ○ working / ✓ done / ! failed. Actions
              live in the expanded action row below. */}
          {engState === 'working' && (
            <span className="status-pulse" style={styles.glyph} title="Working / queued">○</span>
          )}
          {engState === 'done' && (
            <span style={{ ...styles.glyph, color: '#636366' }} title="Annotated">✓</span>
          )}
          {engState === 'failed' && (
            <span style={styles.glyph} title={engErrMsg}>!</span>
          )}
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
      {isExpanded && (
        <div style={styles.actionRow}>
          <div style={{ flex: 1 }} />
          {engState === 'done' && (
            <button style={styles.translateBtn}
              title="Play in browser"
              onClick={e => { e.stopPropagation(); onPlay() }}>▸</button>
          )}
          {engState === 'done' && zhState === 'absent' && (
            <button style={styles.translateBtn}
              title="Translate this episode to 繁體中文"
              onClick={e => { e.stopPropagation(); onTranslate(item.path) }}>中</button>
          )}
          {engState === 'failed' && (
            <button style={styles.translateBtn}
              title="Retry (deletes the SRT and re-runs the pipeline)"
              onClick={e => { e.stopPropagation(); onRetry(item.path) }}>↻</button>
          )}
        </div>
      )}
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
  playBtn: {
    background: 'none', border: 'none', color: '#c79968',
    fontSize: 18, fontWeight: 700, lineHeight: 1, padding: 0, cursor: 'pointer',
    fontFamily: MONO,
  },
  zhBtn: {
    background: 'none', border: 'none', color: '#c79968',
    fontSize: 14, fontWeight: 700, lineHeight: 1, padding: 0, cursor: 'pointer',
    fontFamily: MONO,
  },

  modalBackdrop: {
    position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.85)',
    zIndex: 1000, display: 'flex', alignItems: 'center', justifyContent: 'center',
    padding: 16,
  },
  modalBody: {
    background: '#1c1c1e', borderRadius: 12, border: '1px solid #3a3a3c',
    width: '100%', maxWidth: 1200, maxHeight: '92vh',
    display: 'flex', flexDirection: 'column', overflow: 'hidden',
  },
  modalHeader: {
    display: 'flex', alignItems: 'center', gap: 12,
    padding: '12px 16px', borderBottom: '1px solid #3a3a3c',
  },
  modalTitle: {
    flex: 1, fontFamily: MONO, fontSize: 13, color: '#e8e3d9',
    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
  },
  modalClose: {
    background: 'none', border: 'none', color: '#636366',
    fontSize: 18, padding: 0, cursor: 'pointer', lineHeight: 1,
  },
  modalLoading: { color: '#aeaeb2', padding: 24, textAlign: 'center', fontFamily: MONO, fontSize: 13 },
  modalError: { color: '#c79968', padding: 24, textAlign: 'center', fontFamily: MONO, fontSize: 13 },
  video: { width: '100%', height: 'auto', maxHeight: '85vh', background: '#000', display: 'block' },
}
