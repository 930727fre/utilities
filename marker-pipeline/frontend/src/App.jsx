import { useState, useEffect } from 'react'
import BookList from './components/BookList'

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

  return (
    <>
      <BookList />
      {backendDown && (
        <div style={styles.snackbar}>
          ⚠ Backend is not accessible — retrying…
        </div>
      )}
    </>
  )
}

const styles = {
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
