/**
 * Player state machine
 *
 * States: IDLE | GENERATING | PLAYING | PLAYING_FALLBACK
 *
 * Sliding window: [N-1 kept] [N playing] [N+1 prefetch] [N+2 prefetch]
 */

import { useRef, useState, useCallback, useEffect } from 'react'
import { requestTTS, evictTTSCache, putBookmark } from '../api'

export function usePlayer(bookId, paragraphs) {
  const [state, setState] = useState('IDLE')       // IDLE | GENERATING | PLAYING | PLAYING_FALLBACK
  const [currentIndex, setCurrentIndex] = useState(-1)

  const audioRef = useRef(new Audio())
  const prefetchAbortRef = useRef(false)
  const ttsUrlCacheRef = useRef(new Map())   // paragraphId → url (client-side memo)
  const cachedWindowRef = useRef(new Set())  // paragraphIds currently on server cache
  const transitioningRef = useRef(false)     // true while _playUrl is swapping src
  const mediaActionRef = useRef({})          // updated each render for MediaSession handlers

  // Save bookmark periodically while playing
  useEffect(() => {
    if (state !== 'PLAYING' || currentIndex < 0) return
    const pid = paragraphs[currentIndex]?.paragraph_id
    if (pid) putBookmark(bookId, pid).catch(() => {})
  }, [currentIndex, state])

  // Sync external pause (AirPods, system, etc.) back to React state
  useEffect(() => {
    const audio = audioRef.current
    const onExternalPause = () => {
      if (transitioningRef.current) return
      setState(s => (s === 'PLAYING' || s === 'PLAYING_FALLBACK') ? 'IDLE' : s)
    }
    audio.addEventListener('pause', onExternalPause)
    return () => audio.removeEventListener('pause', onExternalPause)
  }, [])

  // MediaSession: register action handlers once
  useEffect(() => {
    if (!('mediaSession' in navigator)) return
    const h = mediaActionRef.current
    navigator.mediaSession.setActionHandler('play', () => h.resume?.())
    navigator.mediaSession.setActionHandler('pause', () => h.pause?.())
    navigator.mediaSession.setActionHandler('nexttrack', () => h.next?.())
    navigator.mediaSession.setActionHandler('previoustrack', () => h.prev?.())
    return () => {
      for (const action of ['play', 'pause', 'nexttrack', 'previoustrack'])
        navigator.mediaSession.setActionHandler(action, null)
    }
  }, [])

  // MediaSession: sync metadata on paragraph change
  useEffect(() => {
    if (!('mediaSession' in navigator)) return
    const para = paragraphs[currentIndex]
    navigator.mediaSession.metadata = new MediaMetadata({
      title: para?.text?.slice(0, 80) ?? 'TwelveReader',
      artist: 'TwelveReader',
    })
  }, [currentIndex, paragraphs])

  // MediaSession: sync playback state
  useEffect(() => {
    if (!('mediaSession' in navigator)) return
    navigator.mediaSession.playbackState =
      state === 'PLAYING' || state === 'PLAYING_FALLBACK' || state === 'GENERATING'
        ? 'playing' : 'paused'
  }, [state])

  const _advanceRef = useRef(null)
  const _startAtRef = useRef(null)

  const _playUrl = useCallback((url, index) => {
    const audio = audioRef.current
    transitioningRef.current = true
    audio.pause()
    audio.src = url
    audio.onended = () => _advanceRef.current?.(index)
    audio.onerror = () => _advanceRef.current?.(index)
    audio.play()
      .then(() => { transitioningRef.current = false })
      .catch(() => { transitioningRef.current = false })
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
      if (ttsUrlCacheRef.current.has(p.paragraph_id)) continue
      try {
        const { url } = await requestTTS(bookId, p.paragraph_id)
        ttsUrlCacheRef.current.set(p.paragraph_id, url)
        cachedWindowRef.current.add(p.paragraph_id)
      } catch (_) {}
    }
  }, [bookId, paragraphs])

  const _startAt = useCallback(async (index) => {
    if (index < 0 || index >= paragraphs.length) return
    const p = paragraphs[index]

    // evict N-2 from the sliding window
    if (index >= 2) {
      const evict = paragraphs[index - 2]
      if (evict) {
        evictTTSCache(bookId, evict.paragraph_id).catch(() => {})
        ttsUrlCacheRef.current.delete(evict.paragraph_id)
        cachedWindowRef.current.delete(evict.paragraph_id)
      }
    }

    setState('GENERATING')
    setCurrentIndex(index)

    try {
      let url = ttsUrlCacheRef.current.get(p.paragraph_id)
      if (!url) {
        const result = await requestTTS(bookId, p.paragraph_id)
        url = result.url
        ttsUrlCacheRef.current.set(p.paragraph_id, url)
        cachedWindowRef.current.add(p.paragraph_id)
      }
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

  const seekTo = useCallback((index) => {
    prefetchAbortRef.current = true
    audioRef.current.pause()
    setState('IDLE')
    // Evict only the current window — not the entire book cache
    const toEvict = [...cachedWindowRef.current]
    cachedWindowRef.current.clear()
    ttsUrlCacheRef.current.clear()
    for (const pid of toEvict) evictTTSCache(bookId, pid).catch(() => {})
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

  // Keep MediaSession action handlers current each render
  mediaActionRef.current.pause = pause
  mediaActionRef.current.resume = resume
  mediaActionRef.current.next = () => {
    const idx = currentIndex
    if (idx >= 0 && idx + 1 < paragraphs.length) _startAtRef.current?.(idx + 1)
  }
  mediaActionRef.current.prev = () => {
    const idx = currentIndex
    if (idx > 0) _startAtRef.current?.(idx - 1)
  }

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
