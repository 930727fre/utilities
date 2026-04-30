const STATE_LABEL = {
  IDLE: 'Paused',
  GENERATING: 'Loading…',
  PLAYING: 'Playing',
  PLAYING_FALLBACK: 'Error',
}

export default function Player({ state, currentIndex, paragraphs, onPlay, onPause, onResume }) {
  const current = paragraphs[currentIndex]
  const isPlaying = state === 'PLAYING' || state === 'PLAYING_FALLBACK'
  const isGenerating = state === 'GENERATING'

  const handleToggle = () => {
    if (isPlaying) {
      onPause()
    } else if (state === 'IDLE' && currentIndex >= 0) {
      onResume()
    } else if (state === 'IDLE') {
      onPlay(0)
    }
  }

  return (
    <div style={styles.bar}>
      <button style={styles.btn} onClick={handleToggle} disabled={isGenerating || paragraphs.length === 0}>
        {isGenerating ? '⏳' : isPlaying ? '⏸' : '▶'}
      </button>
      <div style={styles.info}>
        <div style={styles.stateLabel}>{STATE_LABEL[state] || state}</div>
        {current && (
          <div style={styles.preview}>
            {current.slice(0, 80)}{current.length > 80 ? '…' : ''}
          </div>
        )}
      </div>
      {paragraphs.length > 0 && (
        <div style={styles.progress}>
          {currentIndex >= 0 ? currentIndex + 1 : 0} / {paragraphs.length}
        </div>
      )}
    </div>
  )
}

const styles = {
  bar: {
    position: 'fixed', bottom: 0, left: 0, right: 0,
    background: '#2c2c2e', borderTop: '1px solid #3a3a3c',
    display: 'flex', alignItems: 'center', gap: 14,
    padding: '12px 20px', paddingBottom: 'max(12px, env(safe-area-inset-bottom))',
    zIndex: 100, boxShadow: '0 -2px 12px rgba(0,0,0,0.4)',
  },
  btn: {
    fontSize: 22, background: 'none', border: 'none',
    cursor: 'pointer', width: 44, height: 44,
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    borderRadius: '50%', flexShrink: 0, color: '#e8e3d9',
  },
  info: { flex: 1, minWidth: 0 },
  stateLabel: { fontSize: 11, color: '#636366', fontWeight: 600, textTransform: 'uppercase', marginBottom: 2 },
  preview: { fontSize: 13, color: '#aeaeb2', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' },
  progress: { fontSize: 12, color: '#636366', flexShrink: 0 },
}
