'use strict';
// سرور آنلاین سامورایی جنگل — بدون هیچ پکیج اضافه (فقط Node.js)
const http = require('http'), fs = require('fs'), path = require('path'), crypto = require('crypto'), zlib = require('zlib');
const PORT = process.env.PORT || 3000;
const PUB = path.join(__dirname, 'public');

let ADMINS = [];
try { ADMINS = JSON.parse(fs.readFileSync(path.join(__dirname, 'admins.json'), 'utf8')); }
catch (e) { console.error('مشکل توی admins.json:', e.message); }

// ---------- فایل‌های بازی ----------
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.json': 'application/json', '.png': 'image/png', '.ico': 'image/x-icon' };
const gzCache = {};
const server = http.createServer((req, res) => {
  let u;
  try { u = decodeURIComponent((req.url || '/').split('?')[0]); } catch (e) { res.writeHead(400); res.end(); return; }
  if (u === '/health') { res.end('ok'); return; }
  if (u === '/') u = '/index.html';
  if (u !== '/index.html') { res.writeHead(404); res.end('not found'); return; }
  // index.html یا کنار server.js هست یا داخل پوشه‌ی public
  let f = path.join(__dirname, 'index.html');
  if (!fs.existsSync(f)) f = path.join(PUB, 'index.html');
  fs.readFile(f, (e, d) => {
    if (e) { res.writeHead(404); res.end('not found'); return; }
    const h = { 'Content-Type': MIME[path.extname(f)] || 'application/octet-stream' };
    if (/gzip/.test(req.headers['accept-encoding'] || '') && /\.(html|js|json)$/.test(f)) {
      const k = f + d.length;
      if (!gzCache[k]) gzCache[k] = zlib.gzipSync(d, { level: 6 });
      h['Content-Encoding'] = 'gzip';
      res.writeHead(200, h); res.end(gzCache[k]);
    } else { res.writeHead(200, h); res.end(d); }
  });
});

// ---------- وب‌سوکت ----------
function frame(op, data) {
  const n = data.length; let h;
  if (n < 126) h = Buffer.from([128 | op, n]);
  else if (n < 65536) { h = Buffer.alloc(4); h[0] = 128 | op; h[1] = 126; h.writeUInt16BE(n, 2); }
  else { h = Buffer.alloc(10); h[0] = 128 | op; h[1] = 127; h.writeBigUInt64BE(BigInt(n), 2); }
  return Buffer.concat([h, data]);
}
function send(c, o) {
  if (!c.sock || c.sock.destroyed || !c.sock.writable) return;
  try { c.sock.write(frame(1, Buffer.from(JSON.stringify(o)))); } catch (e) {}
}
function parse(c) {
  for (;;) {
    const b = c.buf; if (b.length < 2) return;
    const fin = !!(b[0] & 128), op = b[0] & 15, masked = !!(b[1] & 128);
    let len = b[1] & 127, off = 2;
    if (len === 126) { if (b.length < 4) return; len = b.readUInt16BE(2); off = 4; }
    else if (len === 127) { if (b.length < 10) return; len = Number(b.readBigUInt64BE(2)); off = 10; }
    if (len > 65536) { c.sock.destroy(); return; }
    const ml = masked ? 4 : 0;
    if (b.length < off + ml + len) return;
    let payload = b.subarray(off + ml, off + ml + len);
    if (masked) {
      const m = b.subarray(off, off + 4), o = Buffer.alloc(len);
      for (let i = 0; i < len; i++) o[i] = payload[i] ^ m[i & 3];
      payload = o;
    }
    c.buf = b.subarray(off + ml + len);
    c.last = Date.now();
    if (op === 8) { try { c.sock.end(frame(8, Buffer.alloc(0))); } catch (e) {} return; }
    if (op === 9) { try { c.sock.write(frame(10, payload)); } catch (e) {} continue; }
    if (op === 10) continue;
    if (op === 0 || op === 1) {
      c.frag = op === 1 ? [payload] : (c.frag || []).concat([payload]);
      if (fin) { const txt = Buffer.concat(c.frag).toString('utf8'); c.frag = null; onMsg(c, txt); }
    }
  }
}

// ---------- منطق بازی ----------
const clients = new Map();   // id -> client
let nextId = 1;
const fails = new Map();     // ip -> [زمان‌های رمز اشتباه]

const hash = s => crypto.createHash('sha256').update(String(s)).digest();
const sameStr = (a, b) => crypto.timingSafeEqual(hash(a), hash(b));
const num = (v, lo, hi) => { v = +v; if (!Number.isFinite(v)) return 0; v = Math.max(lo, Math.min(hi, v)); return Math.round(v * 100) / 100; };
const pub = c => ({ id: c.id, n: c.name, c: c.color, a: c.adm ? 1 : 0, look: c.look || null, pet: c.pet || null });
const bcast = (o, except) => { for (const c of clients.values()) if (c !== except) send(c, o); };

function blocked(ip) {
  const a = (fails.get(ip) || []).filter(t => Date.now() - t < 60000);
  fails.set(ip, a);
  return a.length >= 5;
}

function onMsg(c, txt) {
  let m; try { m = JSON.parse(txt); } catch (e) { return; }
  if (!m || typeof m !== 'object') return;

  if (m.t === 'join' && !c.id) {
    let name = String(m.name || '').replace(/[\u0000-\u001f<>&"]/g, '').trim().slice(0, 16);
    const pass = String(m.pass || '').slice(0, 100);
    let adm = null;
    if (pass) {
      if (blocked(c.ip)) { send(c, { t: 'err', msg: 'رمز رو چند بار اشتباه زدی؛ یه دقیقه صبر کن' }); return; }
      adm = ADMINS.find(a => sameStr(a.pass, pass));
      if (!adm) {
        fails.get(c.ip).push(Date.now());
        send(c, { t: 'err', msg: 'رمز ادمین اشتباهه' }); return;
      }
      name = adm.name;
    } else {
      if ([...name].length < 2) { send(c, { t: 'err', msg: 'اسم باید حداقل ۲ حرف باشه' }); return; }
      if (ADMINS.some(a => a.name.toLowerCase() === name.toLowerCase())) { send(c, { t: 'err', msg: 'این اسم مخصوص ادمینه، یه اسم دیگه بزن' }); return; }
    }
    const dup = [...clients.values()].find(o => o.name.toLowerCase() === name.toLowerCase());
    if (dup) {
      if (adm || dup.ip === c.ip) { try { dup.sock.destroy(); } catch (e) {} drop(dup); }   // همون آدم دوباره وصل شد، اتصال قبلی بسته می‌شه
      else { send(c, { t: 'err', msg: 'این اسم الان توی بازیه، یه اسم دیگه بزن' }); return; }
    }
    if (clients.size >= 60) { send(c, { t: 'err', msg: 'سرور پره' }); return; }
    c.id = nextId++; c.name = name; c.adm = !!adm;
    c.color = adm ? adm.color : '#ffffff';
    c.look = adm ? adm.look : null; c.pet = adm ? adm.pet : null;
    c.s = null;
    clients.set(c.id, c);
    send(c, { t: 'hi', id: c.id, me: Object.assign(pub(c), adm ? { house: adm.house } : {}), list: [...clients.values()].filter(o => o !== c).map(pub) });
    bcast({ t: '+', p: pub(c) }, c);
    if (adm) bcast({ t: 'msg', text: '👑 ادمین ' + name + ' وارد بازی شد' }, c);
    console.log('ورود:', name, adm ? '(ادمین)' : '', '— آنلاین:', clients.size);
    return;
  }

  if (m.t === 's' && c.id && Array.isArray(m.s) && m.s.length >= 11) {
    const s = m.s;
    c.s = [num(s[0], 0, 1500), num(s[1], 0, 1500), num(s[2], -1, 1), num(s[3], -1, 1), num(s[4], 0, 1e6),
           s[5] ? 1 : 0, num(s[6], 0, 1500), num(s[7], 0, 1500), num(s[8], -1, 1), s[9] ? 1 : 0, s[10] ? 1 : 0];
  }
}

function drop(c) {
  if (!c.id || clients.get(c.id) !== c) return;
  clients.delete(c.id);
  bcast({ t: '-', id: c.id });
  console.log('خروج:', c.name, '— آنلاین:', clients.size);
  c.id = null;
}

server.on('upgrade', (req, sock) => {
  if ((req.url || '').split('?')[0] !== '/ws') { sock.destroy(); return; }
  const key = req.headers['sec-websocket-key'];
  if (!key) { sock.destroy(); return; }
  const acc = crypto.createHash('sha1').update(key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest('base64');
  sock.write('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ' + acc + '\r\n\r\n');
  sock.setNoDelay(true);
  const ip = String(req.headers['x-forwarded-for'] || sock.remoteAddress || '').split(',')[0].trim();
  const c = { sock, buf: Buffer.alloc(0), id: null, ip, last: Date.now() };
  sock.on('data', d => { c.buf = Buffer.concat([c.buf, d]); if (c.buf.length > (1 << 20)) { sock.destroy(); return; } parse(c); });
  sock.on('close', () => drop(c));
  sock.on('error', () => {});
});

// ارسال وضعیت همه به همه، ۱۰ بار در ثانیه
setInterval(() => {
  if (clients.size < 2) return;
  const all = [];
  for (const c of clients.values()) if (c.s) all.push([c.id].concat(c.s));
  for (const c of clients.values()) send(c, { t: 'w', n: clients.size, p: all.filter(a => a[0] !== c.id) });
}, 100);

// نگه داشتن اتصال و پاک کردن اتصال‌های مرده
setInterval(() => {
  const now = Date.now();
  for (const c of [...clients.values()]) {
    if (now - c.last > 70000) { try { c.sock.destroy(); } catch (e) {} drop(c); continue; }
    try { c.sock.write(frame(9, Buffer.alloc(0))); } catch (e) {}
  }
}, 25000);

server.listen(PORT, () => console.log('سرور روی پورت', PORT, 'بالا اومد؛ ادمین‌ها:', ADMINS.map(a => a.name).join('، ')));
