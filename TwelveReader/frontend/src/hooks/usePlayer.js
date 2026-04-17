/**
 * Player state machine
 *
 * States: IDLE | GENERATING | PLAYING | PLAYING_FALLBACK
 *
 * Sliding window: [N-1 kept] [N playing] [N+1 prefetch] [N+2 prefetch]
 */

import { useRef, useState, useCallback, useEffect } from 'react'
import { requestTTS, clearTTSCache, evictTTSCache, putBookmark } from '../api'

export function usePlayer(bookId, paragraphs) {
  const [state, setState] = useState('IDLE')       // IDLE | GENERATING | PLAYING | PLAYING_FALLBACK
  const [currentIndex, setCurrentIndex] = useState(-1)

  const audioRef = useRef(new Audio())
  const prefetchAbortRef = useRef(false)

  // Save bookmark periodically while playing
  useEffect(() => {
    if (state !== 'PLAYING' || currentIndex < 0) return
    const pid = paragraphs[currentIndex]?.paragraph_id
    if (pid) putBookmark(bookId, pid).catch(() => {})
  }, [currentIndex, state])

  const _advanceRef = useRef(null)
  const _startAtRef = useRef(null)

  const _playUrl = useCallback((url, index) => {
    const audio = audioRef.current
    audio.pause()
    audio.src = url
    audio.onended = () => _advanceRef.current?.(index)
    audio.onerror = () => _advanceRef.current?.(index)
    audio.play().catch(() => {})
  }, [])

  const _advance = useCallback((fromIndex) => {
    const next = fromIndex + 1
    if (next >= paragraphs.length) {
      setState('IDLE')
      setCurrentIndex(-1)
      return
    }
    _startAtRef.current?.(next)
  }, [paragraphs])

  const _prefetch = useCallback(async (fromIndex) => {
    prefetchAbortRef.current = false
    const targets = [fromIndex + 1, fromIndex + 2].filter(i => i < paragraphs.length)
    for (const i of targets) {
      if (prefetchAbortRef.current) break
      const p = paragraphs[i]
      try {
        await requestTTS(bookId, p.paragraph_id)
      } catch (_) {}
    }
  }, [bookId, paragraphs])

  const _startAt = useCallback(async (index) => {
    if (index < 0 || index >= paragraphs.length) return
    const p = paragraphs[index]

    // evict N-2 from the sliding window
    if (index >= 2) {
      const evict = paragraphs[index - 2]
      if (evict) evictTTSCache(bookId, evict.paragraph_id).catch(() => {})
    }

    setState('GENERATING')
    setCurrentIndex(index)

    try {
      const { url } = await requestTTS(bookId, p.paragraph_id)
      setState('PLAYING')
      _playUrl(url, index)
      _prefetch(index)  // fire-and-forget
    } catch (_) {
      setState('PLAYING_FALLBACK')
      _playUrl('/audio/tts_failed.wav', index)
    }
  }, [bookId, paragraphs, _playUrl, _prefetch])

  _advanceRef.current = _advance
  _startAtRef.current = _startAt

  const play = useCallback((index) => {
    prefetchAbortRef.current = true
    audioRef.current.pause()
    _startAt(index)
  }, [_startAt])

  const seekTo = useCallback(async (index) => {
    prefetchAbortRef.current = true
    audioRef.current.pause()
    setState('IDLE')
    await clearTTSCache(bookId)
    _startAt(index)
  }, [bookId, _startAt])

  const pause = useCallback(() => {
    audioRef.current.pause()
    setState('IDLE')
  }, [])

  const resume = useCallback(() => {
    if (currentIndex < 0) return
    const audio = audioRef.current
    if (!audio.src || audio.readyState === 0) {
      // No audio loaded (e.g. after bookmark restore) — generate and play
      _startAtRef.current?.(currentIndex)
    } else {
      audio.play().catch(() => {})
      setState('PLAYING')
    }
  }, [currentIndex])

  const resumeFromBookmark = useCallback((paragraphId) => {
    const idx = paragraphs.findIndex(p => p.paragraph_id === paragraphId)
    if (idx >= 0) setCurrentIndex(idx)
    // do NOT auto-play; just scroll into view
  }, [paragraphs])

  return {
    state,
    currentIndex,
    play,
    seekTo,
    pause,
    resume,
    resumeFromBookmark,
  }
}
