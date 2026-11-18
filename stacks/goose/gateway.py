"""Goose read-only gateway (Phase 5b, 2026-11-15; rules: bot-policy.md section 5).

Runs on the server. Holds the Matrix tokens; the Mac only gets a gateway key.
- Read-only access to allowlisted chats (spaces Family & Home, Friends, Work, Unsorted; never the
  Private space; never rooms named Example-name; plus ~/goose/deny.txt room IDs).
- Goose can only *propose* replies: POST /draft puts a draft in the Drafts room (as @goose).
- A draft is sent, as Owner, only when @owner reacts with a check mark on it in the Drafts room;
  a cross mark discards it. The model has no endpoint that sends to a chat.
- The Goose room (phone chat): /goose/next hands Owner's messages to the Mac, /goose/say posts replies.
Stdlib only. Never logs message text or tokens.
"""
import json, os, re, threading, time, urllib.parse, urllib.request, urllib.error, secrets
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

D = os.environ.get('GOOSE_DIR', '/data')
HS = os.environ.get('HS', 'http://127.0.0.1:6167')
DOM = 'home.example.ts.net'
ME = '@owner:' + DOM
BOT = '@goose:' + DOM
BIND = os.environ.get('BIND', '127.0.0.1')
PORT = int(os.environ.get('PORT', '8787'))
ALLOW_SPACES = {'family & home', 'friends', 'unsorted'}
DENY_SPACES = {'private', 'work'}  # work blocked 2026-11-15 (Owner): strict work/personal divide
DENY_NAME = re.compile(r'\bexample-name\b', re.I)
CHECK = {'✅', '✔', '☑'}  # compared after removing U+FE0F
CROSS = {'❌', '✖', '🗑'}
MAX_DRAFTS_PER_HOUR = 20

os.umask(0o077)  # state.json etc. readable by the owner only
rd = lambda n: open(os.path.join(D, n)).read().strip()
MT, GT = rd('owner-gateway.token'), rd('goose-bot.token')
ROOMS = json.loads(rd('rooms.json'))
KEYF = os.path.join(D, 'gateway.key')
if not os.path.exists(KEYF):
    fd = os.open(KEYF, os.O_WRONLY | os.O_CREAT, 0o600); os.write(fd, secrets.token_urlsafe(32).encode()); os.close(fd)
KEY = rd('gateway.key')
STATE = os.path.join(D, 'state.json')
Q = lambda s: urllib.parse.quote(s, safe='')
lock = threading.Lock()


def log(*a):
    print(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), *a, flush=True)


def mx(method, path, body=None, tok=MT, timeout=40):
    r = urllib.request.Request(HS + path, method=method, data=None if body is None else json.dumps(body).encode(),
                               headers={'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json'})
    try:
        return json.load(urllib.request.urlopen(r, timeout=timeout))
    except urllib.error.HTTPError as e:
        return {'_err': e.code}
    except Exception as e:
        return {'_err': str(e)[:80]}


def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {'handled': [], 'inbox': [], 'drafts_sent_ts': []}


_save_lock = threading.RLock()


def save_state(s):
    with _save_lock:  # one writer at a time (sync thread and HTTP threads both save)
        s['handled'] = s['handled'][-2000:]
        tmp = f'{STATE}.{threading.get_ident()}.tmp'
        with open(tmp, 'w') as f:
            json.dump(s, f)
        os.replace(tmp, STATE)


state = load_state()

# ---------- allowlist ----------
_allow = {'t': 0, 'rooms': {}}


def deny_ids():
    try:
        return {l.split()[0] for l in open(os.path.join(D, 'deny.txt')) if l.strip() and not l.startswith('#')}
    except FileNotFoundError:
        return set()


def room_name(rid):
    n = mx('GET', f'/_matrix/client/v3/rooms/{Q(rid)}/state/m.room.name').get('name')
    if n:
        return n
    mem = mx('GET', f'/_matrix/client/v3/rooms/{Q(rid)}/joined_members').get('joined', {})
    others = [v.get('display_name') or k for k, v in mem.items() if k != ME and not re.match(r'@\w*bot:', k)]
    if len(others) > 1:
        return 'Group: ' + ', '.join(o for o in others if o)[:80]
    return ', '.join(o for o in others if o)[:80] or rid


def network(rid, members):
    for m in members:
        for p in ('telegram', 'signal', 'imessage'):
            if m.startswith('@' + p + '_') or m.startswith('@' + p + 'bot'):
                return {'imessage': 'iMessage', 'telegram': 'Telegram', 'signal': 'Signal'}[p]
    return 'Matrix'


def saved_contact(name):
    """True if every participant shows a real name (a saved contact), not a bare number, short code or email."""
    n = re.sub(r'^Group: ', '', name)
    parts = [re.sub(r' \((iMessage|Signal|Telegram)\)$', '', p.strip()) for p in n.split(',')]
    return all(p and re.search(r'[A-Za-z]', p) and '@' not in p for p in parts)


def allowlist():
    with lock:
        if time.time() - _allow['t'] < 300:
            return _allow['rooms']
    joined = set(mx('GET', '/_matrix/client/v3/joined_rooms').get('joined_rooms', []))
    allow, deny = {}, set(deny_ids())
    spaces = []
    for rid in joined:
        c = mx('GET', f'/_matrix/client/v3/rooms/{Q(rid)}/state/m.room.create')
        if c.get('type') == 'm.space':
            spaces.append((rid, (mx('GET', f'/_matrix/client/v3/rooms/{Q(rid)}/state/m.room.name').get('name') or '').strip()))
    def children(sid):
        st = mx('GET', f'/_matrix/client/v3/rooms/{Q(sid)}/state')
        return [e['state_key'] for e in st if isinstance(e, dict) and e.get('type') == 'm.space.child' and e.get('content', {}).get('via')] if isinstance(st, list) else []
    for sid, sname in spaces:
        if sname.lower() in DENY_SPACES:
            deny |= set(children(sid))
    for sid, sname in spaces:
        if sname.lower() not in ALLOW_SPACES:
            continue
        for rid in children(sid):
            if rid in joined and rid not in deny and rid not in ROOMS.values():
                allow[rid] = {'space': sname}
    out = {}
    for rid, v in allow.items():
        name = room_name(rid)
        if DENY_NAME.search(name):
            continue
        if v['space'].lower() == 'unsorted' and not saved_contact(name):
            continue  # 2026-11-15 (Owner): in Unsorted only saved, named contacts; unknown numbers/emails blocked
        mem = list(mx('GET', f'/_matrix/client/v3/rooms/{Q(rid)}/joined_members').get('joined', {}).keys())
        if any(m in mem for m in ('@ha:' + DOM, BOT)):
            continue  # bot rooms (Home, Goose, Drafts) are not inbox chats
        out[rid] = {'id': rid, 'name': name, 'space': v['space'], 'network': network(rid, mem)}
    with lock:
        _allow.update(t=time.time(), rooms=out)
    log('allowlist rebuilt:', len(out), 'rooms')
    return out


def ok_room(rid):
    return rid in allowlist()


# ---------- reading ----------
_names = {}


def who(uid):
    if uid == ME:
        return 'Owner (me)'
    if uid not in _names:
        _names[uid] = mx('GET', f'/_matrix/client/v3/profile/{Q(uid)}/displayname').get('displayname') or uid.split(':')[0][1:]
    return _names[uid]


def text_of(e):
    c = e.get('content', {})
    t = c.get('msgtype')
    if t in ('m.image', 'm.video', 'm.audio', 'm.file'):
        return f'[{t[2:]}: {c.get("body", "")}]'
    return c.get('body', '')


def messages(rid, limit=30, before=None):
    p = f'/_matrix/client/v3/rooms/{Q(rid)}/messages?dir=b&limit={min(int(limit), 100)}'
    if before:
        p += '&from=' + Q(before)
    r = mx('GET', p)
    out = [{'time': time.strftime('%Y-%m-%d %H:%M', time.localtime(e['origin_server_ts'] / 1000)),
            'from': who(e['sender']), 'text': text_of(e)[:2000]}
           for e in r.get('chunk', []) if e.get('type') == 'm.room.message' and 'm.new_content' not in e.get('content', {})]
    return {'messages': list(reversed(out)), 'older': r.get('end')}


def search(q, limit=20):
    rooms = list(allowlist().keys())
    r = mx('POST', '/_matrix/client/v3/search', {'search_categories': {'room_events': {
        'search_term': q, 'filter': {'rooms': rooms, 'limit': min(int(limit), 50)}, 'order_by': 'recent'}}})
    al = allowlist(); out = []
    for h in r.get('search_categories', {}).get('room_events', {}).get('results', []):
        e = h.get('result', {})
        if e.get('room_id') in al:
            out.append({'chat': al[e['room_id']]['name'], 'chat_id': e['room_id'], 'from': who(e['sender']),
                        'time': time.strftime('%Y-%m-%d %H:%M', time.localtime(e['origin_server_ts'] / 1000)), 'text': text_of(e)[:1000]})
    return {'results': out, 'error': r.get('_err')}


def waiting(days=3):
    cut = time.time() * 1000 - float(days) * 86400000; out = []
    for rid, info in allowlist().items():
        r = mx('GET', f'/_matrix/client/v3/rooms/{Q(rid)}/messages?dir=b&limit=8')
        ev = [e for e in r.get('chunk', []) if e.get('type') == 'm.room.message']
        if ev and ev[0]['sender'] != ME and ev[0]['origin_server_ts'] > cut:
            out.append({'chat': info['name'], 'chat_id': rid, 'network': info['network'], 'from': who(ev[0]['sender']),
                        'time': time.strftime('%Y-%m-%d %H:%M', time.localtime(ev[0]['origin_server_ts'] / 1000)),
                        'last_message': text_of(ev[0])[:400], 'ts': ev[0]['origin_server_ts']})
    out.sort(key=lambda x: -x.pop('ts'))
    return {'waiting': out}


# ---------- drafts ----------
def recipients(rid):
    """Real addresses of everyone else in the chat (from bridge ghost IDs), so a check mark is an informed choice."""
    mem = mx('GET', f'/_matrix/client/v3/rooms/{Q(rid)}/joined_members').get('joined', {})
    out = []
    for uid, v in mem.items():
        if uid == ME or re.match(r'@\w*bot:', uid):
            continue
        local = uid.split(':')[0][1:]
        m = re.match(r'(imessage|signal|telegram)_(.+)', local)
        if m and m.group(1) == 'imessage':
            h = urllib.parse.unquote(m.group(2).replace('=', '%'))
            if re.fullmatch(r'\+1\d{10}', h):
                h = f'+1 {h[2:5]}-{h[5:8]}-{h[8:]}'
            out.append(h)
        else:
            out.append((v.get('display_name') or local) + (f' ({m.group(1).title()})' if m else ''))
    return out or ['(nobody else in this chat)']


def make_draft(rid, text):
    if not ok_room(rid):
        return {'error': 'that chat is not on the allowlist'}
    text = (text or '').strip()
    if not text or len(text) > 4000:
        return {'error': 'draft text empty or too long'}
    now = time.time()
    with lock:
        state['drafts_sent_ts'] = [t for t in state.get('drafts_sent_ts', []) if now - t < 3600]
        if len(state['drafts_sent_ts']) >= MAX_DRAFTS_PER_HOUR:
            return {'error': 'draft limit reached (20 per hour)'}
        state['drafts_sent_ts'].append(now); save_state(state)
    info = allowlist()[rid]
    to = ', '.join(recipients(rid))
    body = f'Draft for {info["name"]} ({info["network"]})\nGOES TO: {to}\n\n{text}\n\nCheck the GOES TO line. React ✅ to send as you, ❌ to discard.'
    r = mx('PUT', f'/_matrix/client/v3/rooms/{Q(ROOMS["drafts"])}/send/m.room.message/d{int(now*1000)}',
           {'msgtype': 'm.text', 'body': body, 'home.goose.draft': {'room_id': rid, 'text': text}}, tok=GT)
    log('draft created for', rid[:10])
    return {'ok': '_err' not in r, 'goes_to': to, 'note': 'Draft is waiting in the Drafts room. It is NOT sent until Owner reacts with a check mark. Tell him who it goes to.'}


def handle_reaction(ev):
    rel = ev.get('content', {}).get('m.relates_to', {})
    key, target = rel.get('key', '').replace('\ufe0f', '').strip(), rel.get('event_id')  # phones add U+FE0F
    if ev.get('sender') != ME or not target or ev['event_id'] in state['handled']:
        return
    state['handled'].append(ev['event_id']); save_state(state)
    d = mx('GET', f'/_matrix/client/v3/rooms/{Q(ROOMS["drafts"])}/event/{Q(target)}')
    draft = d.get('content', {}).get('home.goose.draft')
    if d.get('sender') != BOT or not draft or ('sent:' + target) in state['handled']:
        return
    if key in CHECK:
        rid = draft['room_id']
        if not ok_room(rid):
            mx('PUT', f'/_matrix/client/v3/rooms/{Q(ROOMS["drafts"])}/send/m.room.message/n{int(time.time()*1000)}',
               {'msgtype': 'm.notice', 'body': 'Not sent: that chat is no longer on the allowlist.'}, tok=GT); return
        r = mx('PUT', f'/_matrix/client/v3/rooms/{Q(rid)}/send/m.room.message/g{int(time.time()*1000)}',
               {'msgtype': 'm.text', 'body': draft['text']})
        state['handled'].append('sent:' + target); save_state(state)
        msg = 'Sent ✓' if '_err' not in r else f'Sending failed ({r["_err"]}). Nothing was sent.'
        mx('PUT', f'/_matrix/client/v3/rooms/{Q(ROOMS["drafts"])}/send/m.room.message/n{int(time.time()*1000)}',
           {'msgtype': 'm.notice', 'body': msg, 'm.relates_to': {'m.in_reply_to': {'event_id': target}}}, tok=GT)
        log('draft approved; send', 'ok' if '_err' not in r else 'failed', 'to', rid[:10])
    elif key in CROSS:
        state['handled'].append('sent:' + target); save_state(state)
        mx('PUT', f'/_matrix/client/v3/rooms/{Q(ROOMS["drafts"])}/redact/{Q(target)}/x{int(time.time()*1000)}', {'reason': 'discarded'}, tok=GT)
        log('draft discarded')


# ---------- sync loop (Drafts reactions + Goose room inbox) ----------
inbox_cv = threading.Condition()


def sync_loop():
    flt = json.dumps({'room': {'rooms': [ROOMS['goose'], ROOMS['drafts']], 'timeline': {'limit': 20},
                               'state': {'types': []}, 'ephemeral': {'types': []}, 'account_data': {'types': []}},
                      'presence': {'types': []}, 'account_data': {'types': []}})
    since = state.get('since')
    while True:
        p = '/_matrix/client/v3/sync?timeout=30000&filter=' + Q(flt) + ('&since=' + Q(since) if since else '')
        r = mx('GET', p, timeout=60)
        if '_err' in r:
            time.sleep(10); continue
        first = since is None
        since = r.get('next_batch')
        for rid, room in r.get('rooms', {}).get('join', {}).items():
            for ev in room.get('timeline', {}).get('events', []):
                if first:
                    state['handled'].append(ev.get('event_id'))
                    continue
                if rid == ROOMS['drafts'] and ev.get('type') == 'm.reaction':
                    try:
                        handle_reaction(ev)
                    except Exception as e:
                        log('reaction error', type(e).__name__)
                elif rid == ROOMS['drafts'] and ev.get('type') == 'm.room.message' and ev.get('sender') == ME \
                        and ev['event_id'] not in state['handled']:
                    state['handled'].append(ev['event_id']); save_state(state)
                    if time.time() - state.get('drafts_hint_ts', 0) > 600:
                        state['drafts_hint_ts'] = time.time()
                        mx('PUT', f'/_matrix/client/v3/rooms/{Q(ROOMS["drafts"])}/send/m.room.message/h{int(time.time()*1000)}',
                           {'msgtype': 'm.notice', 'body': 'This room only holds drafts. To ask me something, write in the "Goose" room.'}, tok=GT)
                elif ev.get('content', {}).get('m.relates_to', {}).get('rel_type') == 'm.replace':
                    continue  # edits are not new questions
                elif rid == ROOMS['goose'] and ev.get('type') == 'm.room.message' and ev.get('sender') == ME \
                        and ev['event_id'] not in state['handled']:
                    state['handled'].append(ev['event_id'])
                    with inbox_cv:
                        state['inbox'].append({'id': ev['event_id'], 'text': text_of(ev)[:4000]}); save_state(state)
                        inbox_cv.notify_all()
        state['since'] = since; save_state(state)


def goose_next(wait=25):
    with inbox_cv:
        if not state['inbox']:
            inbox_cv.wait(timeout=min(float(wait), 50))
        items, state['inbox'] = state['inbox'], []
        try:
            save_state(state)
        except Exception as e:
            log('save failed', type(e).__name__)  # still hand the messages over
    return {'messages': items}


def goose_say(text, typing=False):
    if typing:
        mx('PUT', f'/_matrix/client/v3/rooms/{Q(ROOMS["goose"])}/typing/{Q(BOT)}', {'typing': True, 'timeout': 120000}, tok=GT)
        return {'ok': True}
    text = (text or '').strip()[:8000] or '(no answer)'
    r = mx('PUT', f'/_matrix/client/v3/rooms/{Q(ROOMS["goose"])}/send/m.room.message/s{int(time.time()*1000)}',
           {'msgtype': 'm.text', 'body': text}, tok=GT)
    mx('PUT', f'/_matrix/client/v3/rooms/{Q(ROOMS["goose"])}/typing/{Q(BOT)}', {'typing': False}, tok=GT)
    return {'ok': '_err' not in r}


# ---------- HTTP ----------
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def auth(self):
        if not secrets.compare_digest(self.headers.get('X-Gateway-Key', ''), KEY):
            self.reply(401, {'error': 'bad key'}); return False
        return True

    def do_GET(self):
        if not self.auth():
            return
        u = urllib.parse.urlparse(self.path); q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path == '/chats':
                rooms = sorted(allowlist().values(), key=lambda x: (x['space'], x['name'].lower()))
                return self.reply(200, {'chats': rooms})
            if u.path == '/messages':
                if not ok_room(q.get('chat', '')):
                    return self.reply(403, {'error': 'that chat is not on the allowlist'})
                return self.reply(200, messages(q['chat'], q.get('limit', 30), q.get('older')))
            if u.path == '/search':
                return self.reply(200, search(q.get('q', ''), q.get('limit', 20)))
            if u.path == '/waiting':
                return self.reply(200, waiting(q.get('days', 3)))
            if u.path == '/goose/next':
                return self.reply(200, goose_next(q.get('wait', 25)))
            if u.path == '/health':
                return self.reply(200, {'ok': True, 'chats': len(allowlist())})
            self.reply(404, {'error': 'unknown path'})
        except Exception as e:
            log('GET error', u.path, type(e).__name__); self.reply(500, {'error': type(e).__name__})

    def do_POST(self):
        if not self.auth():
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0)) or b'{}'))
            if self.path == '/draft':
                return self.reply(200, make_draft(body.get('chat', ''), body.get('text', '')))
            if self.path == '/goose/say':
                return self.reply(200, goose_say(body.get('text', ''), bool(body.get('typing'))))
            self.reply(404, {'error': 'unknown path'})
        except Exception as e:
            log('POST error', self.path, type(e).__name__); self.reply(500, {'error': type(e).__name__})


if __name__ == '__main__':
    threading.Thread(target=sync_loop, daemon=True).start()
    log('gateway listening on', BIND, PORT)
    ThreadingHTTPServer((BIND, PORT), H).serve_forever()
