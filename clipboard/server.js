const express = require('express');
const http = require('http');
const { WebSocketServer } = require('ws');
const path = require('path');

const app = express();
const server = http.createServer(app);
const wss = new WebSocketServer({ server, path: '/ws' });

const MAX_BYTES = 5 * 1024 * 1024; // 5 MB base64 payload limit per slot

const slots = {
  1: { type: 'text', content: '' },
  2: { type: 'text', content: '' },
  3: { type: 'text', content: '' },
};

app.get('/', (_req, res) => res.sendFile(path.join(__dirname, 'index.html')));

wss.on('connection', ws => {
  ws.send(JSON.stringify({ type: 'init', slots }));

  ws.on('message', raw => {
    try {
      const msg = JSON.parse(raw);
      if (msg.type !== 'update' || ![1, 2, 3].includes(msg.slot)) return;
      const data = msg.data;
      if (!data || typeof data.type !== 'string') return;
      if (data.content && data.content.length > MAX_BYTES) return;
      slots[msg.slot] = data;
      const out = JSON.stringify({ type: 'update', slot: msg.slot, data });
      for (const client of wss.clients) {
        if (client !== ws && client.readyState === 1) client.send(out);
      }
    } catch {}
  });

  ws.on('error', () => ws.terminate());
});

const PORT = 3000;
server.listen(PORT, () => console.log(`clipboard listening on :${PORT}`));
