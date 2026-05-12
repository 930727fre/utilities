const recordBtn = document.getElementById('record-btn');
const resultFinalEl = document.getElementById('result-final');
const resultRawEl = document.getElementById('result-raw');
const copyFinalBtn = document.getElementById('copy-final-btn');
const copyRawBtn = document.getElementById('copy-raw-btn');
const metaEl = document.getElementById('meta');
const settingsBtn = document.getElementById('settings-btn');
const settingsPanel = document.getElementById('settings-panel');
const settingsClose = document.getElementById('settings-close');
const correctionsList = document.getElementById('corrections-list');
const addCorrectionBtn = document.getElementById('add-correction');
const saveCorrectionsBtn = document.getElementById('save-corrections');
const canvas = document.getElementById('waveform');
const visualizer = canvas.parentElement;
const timerEl = document.getElementById('timer');
const hintEl = document.getElementById('hint');

const ctx = canvas.getContext('2d');

let mediaRecorder = null;
let audioChunks = [];
let stream = null;
let audioCtx = null;
let analyser = null;
let isRecording = false;

let rafId = null;
const HISTORY_LEN = 64;
let amplitudeHistory = new Array(HISTORY_LEN).fill(0);

let timerHandle = null;
let recStart = 0;

function setHint(s) { hintEl.textContent = s; }

function resizeCanvas() {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.max(1, Math.floor(rect.width * dpr));
  canvas.height = Math.max(1, Math.floor(rect.height * dpr));
}
window.addEventListener('resize', resizeCanvas);
window.addEventListener('orientationchange', resizeCanvas);
resizeCanvas();

async function acquireStream() {
  const s = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      sampleRate: 48000,
      echoCancellation: true,
      noiseSuppression: true,
    },
  });
  if (!audioCtx) {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  }
  if (audioCtx.state === 'suspended') await audioCtx.resume();
  const source = audioCtx.createMediaStreamSource(s);
  analyser = audioCtx.createAnalyser();
  analyser.fftSize = 1024;
  source.connect(analyser);
  return s;
}

function releaseStream() {
  if (stream) {
    stream.getTracks().forEach((t) => t.stop());
    stream = null;
  }
  analyser = null;
}

function startVisualizer() {
  if (rafId) cancelAnimationFrame(rafId);
  amplitudeHistory = new Array(HISTORY_LEN).fill(0);
  visualizer.classList.add('active');
  resizeCanvas();
  const buf = new Uint8Array(analyser.fftSize);
  const tick = () => {
    analyser.getByteTimeDomainData(buf);
    let sum = 0;
    for (let i = 0; i < buf.length; i++) {
      const v = (buf[i] - 128) / 128;
      sum += v * v;
    }
    const rms = Math.sqrt(sum / buf.length);
    amplitudeHistory.push(rms);
    if (amplitudeHistory.length > HISTORY_LEN) amplitudeHistory.shift();
    drawWaveform();
    rafId = requestAnimationFrame(tick);
  };
  tick();
}

function stopVisualizer() {
  if (rafId) cancelAnimationFrame(rafId);
  rafId = null;
  visualizer.classList.remove('active');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
}

function drawWaveform() {
  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  const cx = h / 2;
  const barW = w / HISTORY_LEN;
  const gap = Math.max(2, barW * 0.25);
  const fillW = barW - gap;
  ctx.fillStyle = '#ff453a';
  for (let i = 0; i < amplitudeHistory.length; i++) {
    const amp = amplitudeHistory[i];
    const minBar = 3 * (window.devicePixelRatio || 1);
    const barH = Math.max(minBar, Math.min(h, amp * h * 4));
    const x = i * barW + gap / 2;
    const y = cx - barH / 2;
    const r = Math.min(fillW, barH) / 2;
    roundRect(ctx, x, y, fillW, barH, r);
    ctx.fill();
  }
}

function roundRect(c, x, y, w, h, r) {
  if (w < 1 || h < 1) return;
  c.beginPath();
  c.moveTo(x + r, y);
  c.arcTo(x + w, y, x + w, y + h, r);
  c.arcTo(x + w, y + h, x, y + h, r);
  c.arcTo(x, y + h, x, y, r);
  c.arcTo(x, y, x + w, y, r);
  c.closePath();
}

function startTimer() {
  recStart = performance.now();
  updateTimer();
  timerHandle = setInterval(updateTimer, 100);
}
function stopTimer() {
  if (timerHandle) clearInterval(timerHandle);
  timerHandle = null;
}
function updateTimer() {
  const t = (performance.now() - recStart) / 1000;
  const m = Math.floor(t / 60);
  const s = Math.floor(t % 60);
  const tenths = Math.floor((t * 10) % 10);
  timerEl.textContent = `${m}:${String(s).padStart(2, '0')}.${tenths}`;
}

async function startRecording(e) {
  e.preventDefault();
  if (isRecording) return;
  try {
    stream = await acquireStream();
    mediaRecorder = new MediaRecorder(stream, { mimeType: 'audio/webm;codecs=opus' });
    audioChunks = [];
    mediaRecorder.ondataavailable = (ev) => {
      if (ev.data && ev.data.size > 0) audioChunks.push(ev.data);
    };
    mediaRecorder.start(250);
    isRecording = true;
    recordBtn.classList.add('active');
    copyFinalBtn.classList.add('hidden');
    copyRawBtn.classList.add('hidden');
    setHint('鬆開結束');
    startVisualizer();
    startTimer();
  } catch (err) {
    console.error(err);
    isRecording = false;
    recordBtn.classList.remove('active');
    stopVisualizer();
    stopTimer();
    setHint('麥克風錯誤');
    showToast('麥克風錯誤：' + (err && err.name || ''));
  }
}

async function stopRecording(e) {
  if (e) e.preventDefault();
  if (!isRecording || !mediaRecorder) return;
  isRecording = false;
  recordBtn.classList.remove('active');
  stopVisualizer();
  stopTimer();
  setHint('辨識中…');

  const stopped = new Promise((resolve) => { mediaRecorder.onstop = resolve; });
  try { mediaRecorder.requestData(); } catch (_) {}
  mediaRecorder.stop();
  await stopped;
  releaseStream();

  const blob = new Blob(audioChunks, { type: 'audio/webm' });
  if (blob.size === 0) {
    setHint('沒有錄到聲音');
    return;
  }

  const formData = new FormData();
  formData.append('audio', blob, 'audio.webm');

  const t0 = performance.now();
  try {
    const res = await fetch('/api/transcribe', { method: 'POST', body: formData });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    showResult(data, performance.now() - t0);
    setHint('按住說話');
  } catch (err) {
    console.error(err);
    setHint('辨識失敗');
    showToast('辨識失敗');
  }
}

function showResult(data, totalMs) {
  resultFinalEl.textContent = data.final || '';
  resultRawEl.textContent = data.raw || '';
  copyFinalBtn.classList.toggle('hidden', !data.final);
  copyRawBtn.classList.toggle('hidden', !data.raw);
  const t = data.timing || {};
  metaEl.textContent =
    `${Math.round(totalMs)}ms · whisper ${t.whisper_ms ?? 0} · llm ${t.llm_ms ?? 0}`;
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    showToast('已複製');
  } catch (err) {
    console.error(err);
    showToast('複製失敗');
  }
}
copyFinalBtn.addEventListener('click', () => copyText(resultFinalEl.textContent));
copyRawBtn.addEventListener('click', () => copyText(resultRawEl.textContent));

function showToast(msg) {
  const el = document.createElement('div');
  el.className = 'toast';
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 1500);
}

recordBtn.addEventListener('pointerdown', (e) => {
  try { recordBtn.setPointerCapture(e.pointerId); } catch (_) {}
  startRecording(e);
});
recordBtn.addEventListener('pointerup', (e) => {
  try { recordBtn.releasePointerCapture(e.pointerId); } catch (_) {}
  stopRecording(e);
});
recordBtn.addEventListener('pointercancel', (e) => {
  try { recordBtn.releasePointerCapture(e.pointerId); } catch (_) {}
  stopRecording(e);
});
recordBtn.addEventListener('contextmenu', (e) => e.preventDefault());

settingsBtn.addEventListener('click', async () => {
  settingsPanel.classList.remove('hidden');
  await loadCorrections();
});
settingsClose.addEventListener('click', () => {
  settingsPanel.classList.add('hidden');
});
addCorrectionBtn.addEventListener('click', () => addCorrectionRow('', ''));
saveCorrectionsBtn.addEventListener('click', async () => {
  const data = collectCorrections();
  try {
    const res = await fetch('/api/corrections', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    showToast('已儲存');
  } catch (err) {
    console.error(err);
    showToast('儲存失敗');
  }
});

async function loadCorrections() {
  try {
    const res = await fetch('/api/corrections');
    const data = await res.json();
    correctionsList.innerHTML = '';
    Object.entries(data).forEach(([k, v]) => addCorrectionRow(k, (v || []).join('\n')));
    if (Object.keys(data).length === 0) addCorrectionRow('', '');
  } catch (err) {
    console.error(err);
    showToast('載入失敗');
  }
}

function addCorrectionRow(key, valuesJoined) {
  const row = document.createElement('div');
  row.className = 'correction-row';
  row.innerHTML = `
    <span class="label">正確詞</span>
    <input type="text" class="key" placeholder="正確的詞">
    <span class="label">常被誤聽（每行一個）</span>
    <textarea class="values" placeholder="誤聽1&#10;誤聽2"></textarea>
    <button class="delete" type="button">刪除</button>
  `;
  row.querySelector('.key').value = key;
  row.querySelector('.values').value = valuesJoined;
  row.querySelector('.delete').addEventListener('click', () => row.remove());
  correctionsList.appendChild(row);
}

function collectCorrections() {
  const out = {};
  correctionsList.querySelectorAll('.correction-row').forEach((row) => {
    const key = row.querySelector('.key').value.trim();
    const values = row.querySelector('.values').value
      .split('\n').map((s) => s.trim()).filter(Boolean);
    if (key && values.length) out[key] = values;
  });
  return out;
}

setHint('按住說話');
