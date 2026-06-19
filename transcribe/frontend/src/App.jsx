import { useState, useEffect } from 'react'
import JobList from './components/JobList'
import Bt from './components/Bt'
import TranslateZh from './components/TranslateZh'

function useBackendHealth() {
  const [down, setDown] = useState(false)

  useEffect(() => {
    let cancelled = false
    async function check() {
      try {
        const r = await fetch('/health', { cache: 'no-store' })
        if (!cancelled) setDown(!r.ok)
      } catch {
        if (!cancelled) setDown(true)
      }
    }
    check()
    const id = setInterval(check, 3000)
    return () => { cancelled = true; clearInterval(id) }
  }, [])

  return down
}

export default function App() {
  const backendDown = useBackendHealth()
  const [tab, setTab] = useState('yt')

  return (
    <>
      <div style={styles.tabBar}>
        <button
          style={tab === 'yt' ? styles.tabActive : styles.tab}
          onClick={() => setTab('yt')}>yt</button>
        <button
          style={tab === 'bt' ? styles.tabActive : styles.tab}
          onClick={() => setTab('bt')}>bt</button>
        <button
          style={tab === 'translate_zh' ? styles.tabActive : styles.tab}
          onClick={() => setTab('translate_zh')}>translate_zh</button>
      </div>
      {tab === 'yt' && <JobList />}
      {tab === 'bt' && <Bt />}
      {tab === 'translate_zh' && <TranslateZh />}
      {backendDown && (
        <div style={styles.snackbar}>
          ⚠ Backend is not accessible — retrying…
        </div>
      )}
    </>
  )
}

const MONO = 'ui-monospace, SFMono-Regular, Menlo, monospace'

const styles = {
  tabBar: {
    maxWidth: 720, margin: '0 auto', padding: '24px 16px 0',
    display: 'flex', gap: 24, borderBottom: '1px solid #3a3a3c', marginBottom: 24,
  },
  tab: {
    background: 'none', border: 'none', cursor: 'pointer',
    fontFamily: MONO, fontSize: 14, color: '#aeaeb2',
    padding: '8px 0', borderBottom: '2px solid transparent', marginBottom: -1,
  },
  tabActive: {
    background: 'none', border: 'none', cursor: 'pointer',
    fontFamily: MONO, fontSize: 14, color: '#e8e3d9',
    padding: '8px 0', borderBottom: '2px solid #c79968', marginBottom: -1,
  },
  snackbar: {
    position: 'fixed', top: 20, left: '50%', transform: 'translateX(-50%)',
    background: '#3a3a3c', color: '#e8e3d9',
    padding: '12px 24px', borderRadius: 10,
    fontSize: 14, fontWeight: 500,
    boxShadow: '0 4px 16px rgba(0,0,0,0.5)',
    zIndex: 999,
    whiteSpace: 'nowrap',
  },
}
