const express = require('express');
const http = require('http');
const { WebSocketServer } = require('ws');
const path = require('path');
const fs = require('fs');
const heicConvert = require('heic-convert');

const app = express();
const server = http.createServer(app);
const wss = new WebSocketServer({ server, path: '/ws' });

const MAX_BYTES = 5 * 1024 * 1024; // 5 MB base64 payload limit per slot
const DATA_DIR = '/data';

const slots = {
  1: { type: 'text', content: '' },
  2: { type: 'text', content: '' },
  3: { type: 'text', content: '' },
};

// ── Disk mirror ────────────────────────────────────────────────────────────
// Each slot mirrors to /data/slotN.<ext>. The slot stays the source of truth;
// the file is just a read-only view for processes outside this container.

fs.mkdirSync(DATA_DIR, { recursive: true });

// Wipe any stale files from a previous run — in-memory slots start empty,
// so the disk view should match.
for (const f of fs.readdirSync(DATA_DIR)) {
  if (/^slot[1-3](\.|$)/.test(f)) {
    try { fs.unlinkSync(path.join(DATA_DIR, f)); } catch {}
  }
}

function safeExt(name) {
  if (typeof name !== 'string') return '';
  const m = name.match(/\.([A-Za-z0-9]{1,16})$/);
  return m ? '.' + m[1].toLowerCase() : '';
}

function clearSlotFiles(n) {
  for (const f of fs.readdirSync(DATA_DIR)) {
    if (new RegExp(`^slot${n}(\\.|$)`).test(f)) {
      try { fs.unlinkSync(path.join(DATA_DIR, f)); } catch {}
    }
  }
}

function writeSlot(n, data) {
  clearSlotFiles(n);
  try {
    if (data.type === 'text') {
      if (!data.content) return; // empty text = no file
      fs.writeFileSync(path.join(DATA_DIR, `slot${n}.txt`), data.content, 'utf8');
    } else if (data.type === 'image' || data.type === 'file') {
      // content is a data URL: "data:<mime>;base64,<payload>"
      const comma = typeof data.content === 'string' ? data.content.indexOf(',') : -1;
      if (comma < 0) return;
      const buf = Buffer.from(data.content.slice(comma + 1), 'base64');
      const ext = safeExt(data.name) || (data.type === 'image' ? '.bin' : '');
      fs.writeFileSync(path.join(DATA_DIR, `slot${n}${ext}`), buf);
    }
  } catch (err) {
    console.error(`slot ${n} write failed:`, err.message);
  }
}

// Transcode HEIC/HEIF → JPEG. Browsers can't render <img src="data:image/heic;...">,
// and the Anthropic API doesn't accept HEIC either. Doing this server-side once
// fixes both the in-browser preview and the on-disk file for downstream readers.
async function maybeTranscode(data) {
  if (data.type !== 'image' && data.type !== 'file') return data;
  const ext = safeExt(data.name);
  if (ext !== '.heic' && ext !== '.heif') return data;
  const comma = typeof data.content === 'string' ? data.content.indexOf(',') : -1;
  if (comma < 0) return data;
  try {
    const inputBuf = Buffer.from(data.content.slice(comma + 1), 'base64');
    const jpgBuf = await heicConvert({ buffer: inputBuf, format: 'JPEG', quality: 0.5 });
    const jpgB64 = Buffer.from(jpgBuf).toString('base64');
    return {
      type: 'image',
      content: `data:image/jpeg;base64,${jpgB64}`,
      name: data.name.replace(/\.(heic|heif)$/i, '.jpg'),
      size: jpgBuf.length,
    };
  } catch (err) {
    console.error(`HEIC transcode failed:`, err.message);
    return data;
  }
}

const slotSeq = { 1: 0, 2: 0, 3: 0 };

app.get('/', (_req, res) => res.sendFile(path.join(__dirname, 'index.html')));

wss.on('connection', ws => {
  ws.send(JSON.stringify({ type: 'init', slots }));

  ws.on('message', async raw => {
    try {
      const msg = JSON.parse(raw);
      if (msg.type !== 'update' || ![1, 2, 3].includes(msg.slot)) return;
      let data = msg.data;
      if (!data || typeof data.type !== 'string') return;
      if (data.content && data.content.length > MAX_BYTES) return;

      const seq = ++slotSeq[msg.slot];
      const original = data;
      data = await maybeTranscode(data);
      if (slotSeq[msg.slot] !== seq) return; // superseded during transcode

      const modified = data !== original;
      slots[msg.slot] = data;
      writeSlot(msg.slot, data);
      const out = JSON.stringify({ type: 'update', slot: msg.slot, data });
      for (const client of wss.clients) {
        if (client.readyState !== 1) continue;
        if (client === ws && !modified) continue; // skip echo unless we rewrote the data
        client.send(out);
      }
    } catch {}
  });

  ws.on('error', () => ws.terminate());
});

const PORT = 3000;
server.listen(PORT, () => console.log(`clipboard listening on :${PORT}`));
