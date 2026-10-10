#!/usr/bin/env python3
"""N40 Content Engine — Two firewalled engines on one server.
Admin (/) = Lon's private engine, requires ADMIN_KEY.
Client (/client) = public client engine, voice profiles via localStorage."""

import os
import re
import json
import time
import threading
import functools
import uuid
import io
import zipfile
import random
from flask import Flask, request, jsonify, send_from_directory, Response

import anthropic
import requests

app = Flask(__name__, static_folder=None)

ADMIN_KEY = os.environ.get('ADMIN_KEY', '')
PRO_KEYS = [k.strip() for k in os.environ.get('PRO_KEYS', '').split(',') if k.strip()]
FREE_CHAR_LIMIT = 5000
PRO_CHAR_LIMIT = 50000

# ---------------------------------------------------------------------------
# Model tiers
#
# Writing routes carry Lon's voice, so they run on the strongest model we have.
# Mechanical routes — asking the next interview question, turning interview
# answers into a profile — get nothing from that and would cost 10x for it.
# Both are env-overridable so the tier can be changed in Render without a
# deploy (e.g. WRITER_MODEL=claude-opus-5 to halve the writing cost).
# ---------------------------------------------------------------------------
WRITER_MODEL = os.environ.get('WRITER_MODEL', 'claude-fable-5')
UTILITY_MODEL = os.environ.get('UTILITY_MODEL', 'claude-haiku-4-5')

# The routes that emit long output — a post plus a full article plus
# infographic JSON — briefly ran a faster model to fit under the ~30s ceiling
# on a single response. Generation now happens on a job the client polls (see
# run_as_job), so that ceiling no longer applies and these are back on the
# writing model. Kept as its own knob purely as an escape hatch: set
# HEAVY_MODEL in Render to pull just the long routes onto a faster model
# without touching the short ones or shipping a deploy.
HEAVY_MODEL = os.environ.get('HEAVY_MODEL', WRITER_MODEL)

# ---------------------------------------------------------------------------
# Durable store
#
# Render's filesystem is ephemeral — anything written next to the code is gone
# on the next deploy, which is why analytics used to live in a module global
# and voice profiles live in the browser. Point DATA_DIR at a mounted disk and
# dismissals, saved styles and stats survive deploys and follow Lon between
# devices. Without one everything still works, it just resets on deploy, and
# /api/storage reports which you are getting so it is never a silent surprise.
# ---------------------------------------------------------------------------
DATA_DIR = os.environ.get('DATA_DIR', os.path.join(os.path.dirname(__file__), '.data'))

_store_lock = threading.RLock()


def _probe_store():
    """Whether a DATA_DIR is configured AND actually writable.

    A set env var on its own proves nothing: if the disk never mounted, the
    path is usually still writable on the container's own ephemeral filesystem,
    and claiming that is durable would be worse than claiming nothing. This at
    least catches the case where the directory cannot be written at all.
    """
    if not os.environ.get('DATA_DIR'):
        return False
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        probe = os.path.join(DATA_DIR, '.write-probe')
        with open(probe, 'w') as f:
            f.write('ok')
        os.remove(probe)
        return True
    except OSError:
        return False


STORE_IS_PERSISTENT = _probe_store()


def _store_path(name):
    return os.path.join(DATA_DIR, name + '.json')


def store_read(name, default):
    with _store_lock:
        try:
            with open(_store_path(name)) as f:
                return json.load(f)
        except (FileNotFoundError, ValueError, OSError):
            return default


def store_write(name, value):
    """Write via a temp file and rename, so a crash mid-write cannot leave a
    truncated file behind that would read back as an empty store."""
    with _store_lock:
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp = _store_path(name) + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(value, f)
            os.replace(tmp, _store_path(name))
        except OSError as e:
            # Surfaced to the caller as a plain message rather than a 500, so a
            # misconfigured disk reads as "could not save" instead of a crash.
            raise RuntimeError(f'Could not write to {DATA_DIR}: {e}')
    return value


# ---------------------------------------------------------------------------
# Spend protection
#
# The client engine is deliberately public, so every model-backed endpoint is
# reachable without credentials — and each call costs real money. These caps
# keep a scraper or a bored visitor from running up the Anthropic bill. Admins
# (valid ADMIN_KEY) and Pro keyholders bypass the per-IP limit.
# ---------------------------------------------------------------------------
RATE_LIMIT_PER_HOUR = int(os.environ.get('RATE_LIMIT_PER_HOUR', '40'))
RATE_LIMIT_PER_DAY = int(os.environ.get('RATE_LIMIT_PER_DAY', '200'))
# Circuit breaker across all callers, so one bad day can't drain the account.
GLOBAL_DAILY_CAP = int(os.environ.get('GLOBAL_DAILY_CAP', '2000'))

_rate_lock = threading.Lock()
_hits_by_ip = {}          # ip -> list[timestamp]
_global_hits = []         # list[timestamp]


def _client_ip():
    fwd = request.headers.get('X-Forwarded-For', '')
    if fwd:
        return fwd.split(',')[0].strip()
    return request.remote_addr or 'unknown'


def _prune(stamps, now, window):
    return [t for t in stamps if now - t < window]


def rate_limited(f):
    """Cap model-backed calls per IP, and globally, per rolling window."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if is_admin() or _has_pro_key():
            return f(*args, **kwargs)

        now = time.time()
        ip = _client_ip()
        with _rate_lock:
            global _global_hits
            _global_hits = _prune(_global_hits, now, 86400)
            if len(_global_hits) >= GLOBAL_DAILY_CAP:
                return jsonify({
                    'error': 'This tool has hit its daily capacity. Try again tomorrow.'
                }), 429

            stamps = _prune(_hits_by_ip.get(ip, []), now, 86400)
            if len([t for t in stamps if now - t < 3600]) >= RATE_LIMIT_PER_HOUR:
                return jsonify({
                    'error': 'Too many requests. Please wait a few minutes and try again.'
                }), 429
            if len(stamps) >= RATE_LIMIT_PER_DAY:
                return jsonify({
                    'error': 'Daily limit reached for this connection. Try again tomorrow.'
                }), 429

            stamps.append(now)
            _hits_by_ip[ip] = stamps
            _global_hits.append(now)

            # Keep the IP table from growing without bound.
            if len(_hits_by_ip) > 5000:
                for k in [k for k, v in _hits_by_ip.items() if not _prune(v, now, 86400)]:
                    _hits_by_ip.pop(k, None)

        return f(*args, **kwargs)
    return decorated


def _has_pro_key():
    """True when the request carries a valid Pro key."""
    try:
        data = request.get_json(silent=True) or {}
    except Exception:
        return False
    key = (data.get('pro_key') or '').strip()
    return bool(key and key in PRO_KEYS)


# ---------------------------------------------------------------------------
# Rambles — transcripts from Fathom (recent) and Google Drive (the archive)
#
# Two sources, one shape. The Drive .docx files turn out to be Fathom exports
# themselves, so a single parser handles both and a ramble looks the same to
# the generator wherever it came from.
#
# Scope matters more than it looks. A bare Drive full-text search for a phrase
# like "lost my dad" returns the Gap manuscript, old LinkedIn archives and the
# content playbook before it returns a single client call — the corpora sit in
# adjacent folders and share vocabulary. Drive's query language cannot scope to
# a folder *tree* (`in parents` only matches a direct parent, and rambles sit
# three levels down), so we keep a metadata-only map of the ramble files —
# ids and paths, no content — and filter every search through it. It is also
# what Random Ramble draws from.
# ---------------------------------------------------------------------------
RAMBLES_ROOT_ID = os.environ.get('RAMBLES_ROOT_ID', '1Xbh6wn6QQtiokd6sDKlt5aYr8W9FAv9E')
COMMUNITY_ROOT_ID = os.environ.get('COMMUNITY_ROOT_ID', '1DXPY5CMtVG0G2ObQAnX3QfkbzZdUwcyD')
FATHOM_API_KEY = os.environ.get('FATHOM_API_KEY', '')
FATHOM_BASE = 'https://api.fathom.ai/external/v1'
RECENT_DAYS = int(os.environ.get('RECENT_DAYS', '60'))
DOCX_MIME = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'

_google_token = {'value': '', 'expires': 0}
_google_lock = threading.Lock()


def _google_access_token():
    """Exchange the stored refresh token for an access token, cached until it
    nearly expires. GOOGLE_DRIVE_TOKEN_JSON holds client_id, client_secret and
    refresh_token — the same shape the dashboard uses for Calendar."""
    with _google_lock:
        if _google_token['value'] and time.time() < _google_token['expires']:
            return _google_token['value']
        raw = os.environ.get('GOOGLE_DRIVE_TOKEN_JSON', '')
        if not raw:
            raise RuntimeError('Google Drive is not connected. Set GOOGLE_DRIVE_TOKEN_JSON.')
        cfg = json.loads(raw)
        r = requests.post(cfg.get('token_uri') or 'https://oauth2.googleapis.com/token', data={
            'client_id': cfg['client_id'],
            'client_secret': cfg['client_secret'],
            'refresh_token': cfg['refresh_token'],
            'grant_type': 'refresh_token',
        }, timeout=30)
        if r.status_code != 200:
            raise RuntimeError('Google refused the refresh token — it may have been revoked.')
        body = r.json()
        _google_token['value'] = body['access_token']
        _google_token['expires'] = time.time() + int(body.get('expires_in', 3600)) - 120
        return _google_token['value']


def _drive(path, **params):
    r = requests.get(f'https://www.googleapis.com/drive/v3/{path}',
                     headers={'Authorization': 'Bearer ' + _google_access_token()},
                     params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def fathom_transcript_text(tr):
    """Flatten a Fathom transcript into speaker-labelled lines.

    Each segment's `speaker` is an object, not a string — {display_name,
    matched_calendar_invitee_email} — so naive formatting renders the whole
    dict into the text. Only the display name is used; the matched email is
    the invitee's real address and has no business in a writing prompt.
    """
    if isinstance(tr, str):
        return tr
    lines = []
    for seg in (tr or []):
        spk = seg.get('speaker')
        name = spk.get('display_name', '') if isinstance(spk, dict) else (spk or '')
        text = (seg.get('text') or '').strip()
        if text:
            lines.append(f'{name}: {text}' if name else text)
    return '\n'.join(lines)


def docx_to_text(blob):
    """Pull readable text out of a .docx without a parsing library — a .docx is
    a zip, and the paragraph text lives in w:t elements."""
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        xml = z.read('word/document.xml').decode('utf8', 'replace')
    lines = []
    for para in re.findall(r'<w:p[ >].*?</w:p>', xml, re.S):
        txt = ''.join(re.findall(r'<w:t[^>]*>([^<]*)</w:t>', para)).strip()
        if txt:
            lines.append(txt)
    return '\n'.join(lines)


def _drive_list_all(q, fields, cap=200):
    """Page a Drive query to exhaustion."""
    items, page = [], None
    for _ in range(cap):
        res = _drive('files', q=q, fields=f'nextPageToken, files({fields})',
                     pageSize=1000, pageToken=page or '',
                     corpora='user', includeItemsFromAllDrives='false',
                     supportsAllDrives='false')
        items += res.get('files', [])
        page = res.get('nextPageToken')
        if not page:
            break
    return items


def build_rambles_index():
    """Map the archive with two sweeps instead of a walk.

    Walking the tree costs one API call per folder, and the archive has well
    over a thousand of them — roughly 2,000 sequential calls, ten to twenty
    minutes, long enough that the browser gives up and the job gets reaped
    before it can hand anything back.

    Listing every folder once and every .docx once is ~40 calls. Parentage
    comes back with the files, so the tree is reassembled in memory rather
    than by asking Drive to walk it. Same result, roughly two orders of
    magnitude less waiting.
    """
    folders = _drive_list_all(
        "mimeType = 'application/vnd.google-apps.folder' and trashed = false",
        'id, name, parents')
    kids = {f['id']: (f.get('name', ''), (f.get('parents') or [None])[0]) for f in folders}

    def ancestry(fid, limit=12):
        """Walk up to a known root; returns (corpus, trail) or None if the file
        sits outside both ramble trees."""
        trail = []
        seen = set()
        while fid and fid not in seen and len(trail) <= limit:
            seen.add(fid)
            if fid == RAMBLES_ROOT_ID:
                return 'client-ramble', '/'.join(reversed(trail))
            if fid == COMMUNITY_ROOT_ID:
                return 'community', '/'.join(reversed(trail))
            node = kids.get(fid)
            if not node:
                return None
            trail.append(node[0])
            fid = node[1]
        return None

    docs = _drive_list_all(
        f"mimeType = '{DOCX_MIME}' and trashed = false",
        'id, name, parents, modifiedTime')

    out = []
    for f in docs:
        parent = (f.get('parents') or [None])[0]
        if not parent:
            continue
        place = ancestry(parent)
        if not place:
            continue                      # outside the ramble corpora
        corpus, trail = place
        m = re.search(r'(\d{4})[.\-_](\d{2})[.\-_](\d{2})', f['name'])
        out.append({
            'id': f['id'], 'name': f['name'], 'path': trail, 'corpus': corpus,
            'person': trail.split('/')[1] if '/' in trail else trail,
            'date': '-'.join(m.groups()) if m else '',
            'modified': f.get('modifiedTime', ''),
        })

    store_write('rambles_index', {'built': time.strftime('%Y-%m-%d %H:%M:%S'),
                                  'folders_seen': len(folders),
                                  'docs_seen': len(docs),
                                  'files': out})
    return out


def extract_text(msg):
    """Pull the text content from a Claude response, skipping thinking blocks."""
    for block in msg.content:
        if hasattr(block, 'text'):
            return block.text.strip()
    return ''


def parse_json_response(text):
    """Parse a JSON object out of a model response.

    The prompts all ask for bare JSON, but a model that reasons first will
    occasionally open with a line of preamble or wrap the object in a fence
    anyway. Falling back to the outermost braces beats failing a whole
    generation over a stray sentence.
    """
    text = (text or '').strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find('{'), text.rfind('}')
        if start == -1 or end <= start:
            raise
        return json.loads(text[start:end + 1])


def is_admin():
    key = request.headers.get('X-Admin-Key', '')
    return bool(ADMIN_KEY and key == ADMIN_KEY)


def require_admin_key(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not is_admin():
            return jsonify({'error': 'Unauthorized'}), 403
        return f(*args, **kwargs)
    return decorated


# ─── VOICE CONTEXTS (Lon's defaults — used when no profile is provided) ───

AVATAR_CONTEXT = """
## The Normal 40 Avatar
Elite performer — physician, executive, founder, attorney — 15-25 years in. Winning on paper. Dying inside.

PAIN POINTS:
- Sunday Night Pit starting at 4 PM
- Vacation ends mentally 36 hours before the flight
- Incapable of being present at home — mind always at work
- Marriage has lost intimacy
- Faking that they even care about metrics they're paid to hit
- Success feels like prison
- Guilty for having so much and feeling so little

WHAT THEY WANT:
- Freedom to chase life without guilt
- Youthful energy and curiosity again
- Move from Architect to Archaeologist
- Stop faking it
- Permission and readiness, not motivation
- A roadmap that doesn't blow up their life

THE MOMENT THEY REACH OUT:
"I reached a place where I would trade what I have... but it's lonely and embarrassing to not know what I'd trade it for."

VERBATIM LINES:
- "They're faking that they even care. No wonder they're burning out."
- "I don't know why I can't be happy... I used to be happier."
- "You didn't fail at success — success finished its job."
- "If death is the undefeated champion against life, then tolerance is the undefeated champion against living."

SURVEY DATA (n=702 professionals, 38-60, 15+ years in career):
- 57% plan to LEAVE their employer in 5 years
- 43% don't know what they want in 5 years
- 80% believe their best work is ahead
- 75% say their spouse knows how they feel (but the research says otherwise)
- Only 41% use their core gifts every day

DEFINING PARADOX: 80% know best work is ahead → 57% are leaving → 43% don't know where they're going.
"""

VOICE_CONTEXT = """
## Lon Stroschein's Voice & Style OS

CORE PRINCIPLE: You write to EXPOSE, not impress. The reader must feel: (1) Seen — "How did he know that?", (2) Called out — "Damn. I've been hiding.", (3) Invited forward — "I need to do something." That order is non-negotiable.

CORE IDENTITY:
- Researcher first. Truth-teller. Change agent who has done it and led thousands.
- Lon has had THOUSANDS of conversations (rambles) with this avatar over 3+ years
- He takes full credit: "In thousands of conversations over three years, I've found..."
- NOT "clients say" — instead "the research shows" / "what I've found"
- NEVER include a URL or link in any post
- Give EVERYTHING away. No gates. The avatar should be able to use every word TODAY.
- He sounds like "a trusted man, telling the truth, to someone successful enough to hide and tired enough to finally hear it."

THE LON PATTERN (structural spine of every strong piece):
1. Name the tension
2. Describe how it feels privately
3. Contrast outer success with inner truth
4. State the cost of staying
5. Offer a reframe or language
6. Challenge toward action
7. End with a line that sticks (3-8 words, inevitable, hard to argue with)

HOW LON WRITES:
- Write like you're talking to ONE person across a table, not an audience
- Second-person ("you") + short lines
- Verbal finger-point without cruelty: "Dude." / "Look." / "Yes…I'm talking to you."
- Private coaching someone found in public
- Short sentences carry weight ("You've outgrown your life."). Medium sentences explain. Lists build pressure.
- Questions are MIRRORS, not decoration ("Can't or won't?")
- "This isn't X. It's Y." — tight contrast is his signature move
- He earns authority by NAMING WHAT PEOPLE HIDE
- He never leads with credentials. He leads with recognition.
- Rhythm: tension/release through contrast (success vs truth, image vs self, safety vs regret)
- Repetition must ESCALATE, not circle
- Best paragraphs RISE: observation → emotional truth → consequence → challenge

HOOK PATTERN (first 1-2 lines create a psychological snap):
- Unwanted Truth: "You've outgrown your life."
- Contradiction: "You can win the game and still not want the prize."
- Hidden Internal: "The hardest part isn't leaving. It's admitting you want to."
- Pattern Statement: "High performers live with two forces..."
- The hook should feel like a DIAGNOSIS, not a headline

THE SAVE-POST RECIPE:
1. Open with a fact or definitive line
2. Name the hidden mechanism (the thing people won't say)
3. Contrast — "This isn't X. It's Y."
4. Give language people can STEAL (1-3 quotable lines they'll screenshot)
5. Give a micro-framework (bullets, steps, "here's what to do") — something USABLE TODAY
6. Close with an invitation (a doorway, a question that requires a story — NOT "DM me")

CORRECTION LIST (apply these to EVERY output):
- Over-explaining after the punch → TRUST THE LINE. Stop sooner.
- Too many one-line paragraphs → let some sentences carry meaning together
- Big concepts without anchors → ground in a real moment or visible cost
- Sermon-like cadence without story → add lived detail before declaration
- Saying "truth" without naming it → write the actual forbidden sentence
- Stacking too many ideas → ONE POST, ONE PUNCH
- Framework before wound → framework EXPLAINS the wound, doesn't replace it
- Same truth restated → advance the idea, don't circle it
- Moving past objections too fast → build the bridge

WORDS TO NEVER USE: leverage, optimize, synergy, actionable insights, transformative, unlock your potential, maximize, strategic alignment, live your best life, step into your power, embrace the journey, be unapologetically you, dream big, thrive, abundance, empowered, curated, hacks, lean into, show up as your authentic self

SIGNATURE LANGUAGE (use freely): outgrown, truth, choice, clarity, courage, freedom, drift, cost, trade, permission, readiness, image, identity, The Quiet Voice, Bet on yourself, Make the trade, Can't or won't?, Staying is not neutral, The box of success, Ripples of impact

CALIBRATION LINES (this is what Lon sounds like at his best):
- "You've outgrown your life."
- "Staying is not free. It just sends the bill later."
- "You are admired for the very things that are exhausting you."
- "Your best decade is not behind you. But it will be if you keep living like this."
- "Autopilot isn't failure. It's just where growth goes to die."
- "Can't or won't?"
- "I'm tired of being impressive."
"""

LON_CALIBRATION = """
## WHAT MAKES LON SOUND LIKE LON (calibrate against these — this is his ACTUAL voice)

READ THESE CAREFULLY. If your output doesn't feel like these, you've failed.

### Pattern 1 — The Story Walk (his most natural mode)
"Go where it takes you… Most mornings I walk. Most mornings, it's the same path. This wasn't most mornings. I started walking towards the sunrise. I had no agenda, no plan, just walking. Not far from me is a new road…that dead ends at a major highway. And for nearly two miles, I was alone. Until, there was Randy. I noticed him early, long before he saw me. My initial instinct was to turn around. But I kept walking. As I got closer, I noticed a uniform, then a badge. I waved and said, 'Good morning.' He said, 'Good morning. I'm waiting for my wife.' Then, all of a sudden, I heard the honking. And Randy? He was waving and loving it. Damn, Randy. Well done."

### Pattern 2 — The Vulnerable Confession
"In a few hours, I start a 24-hour trek to Cusco, Peru. Where, by design, or maybe foolishness, no one is waiting for me. This week marks two years since I made The Trade. My old life, for more than a decade, lived well but did not flow. I would leave home as one person, but in an instant, would transform into who I needed to be at work. Look, feeling unsatisfied about where you're going is the worst feeling in the world. Mostly because you can't tell anyone."

### Pattern 3 — The Short Invitation
"You'll feel it today: an impulse to do something good. To open the door. Say 'Good job.' Write the note. Hug the friend. Speak the truth. And if you're like most people, you'll talk yourself out of it. You will waste your impulse to be … human. Today, don't waste the impulse. Be up to something. Be the ripple. Be human."

## VOICE RULES (derived from Lon's actual writing, not an idealized version)

1. STORIES FIRST — Lon leads with a real moment, a real person, a real place. Names, times, details. "I was alone in my study, drinking." NOT "You've been hiding from the truth."
2. WARM AND CASUAL — "Dude." "Ugh." "Damn, Randy." "Holy crap." He's a friend talking, not a guru preaching.
3. HE INVITES, DOESN'T COMMAND — "You are invited." "Be up to something." "Welcome to the Normal 40." NOT "Stop hiding." "Wake up." "Make the change."
4. HE OWNS HIS MESS — "I spelled college wrong." "I was a long way down their list." "I was numbing." He earns trust through imperfection.
5. RHYTHM IS BREATH — Short lines breathe. "Then we rambled. Then we dreamed. Since then, we've become friends." Repetition BUILDS, it doesn't loop.
6. HIS ENDINGS ARE INVITATIONS — "Be up to something." "Welcome to your Normal 40." "This is a lifetime…up to something." NOT motivational commands.
7. SPECIFIC > CLEVER — "On our family farm at 5:21 AM" beats any polished metaphor. Real details are his signature.
8. HIS SIGNATURE PHRASES — "Be up to something", "Welcome to the Normal 40", "JFDS", "The Trade", "the #normal40 highway", "We have room for more", "This is a lifetime…up to something"
9. HE DOESN'T SOUND LIKE A CONTENT CREATOR — No polished hooks, no "3 things I learned", no motivational speaker cadence. He sounds like a trusted friend who happens to write well.
10. THE QUESTION AT THE END — When he asks, it invites a STORY. "Share your Randy story." "What are the omens telling you?" NOT "Are you ready to change?"
"""

ALGORITHM_CONTEXT = """
## LinkedIn Algorithm Rules
- SAVES are the #1 signal — design every post to be saved/screenshotted
- Comments of 15+ words are weighted heavily — end with a specific answerable question
- Dwell time matters — dense infographics keep people on the post longer
- First 140 characters = the hook (must land before "see more" cutoff on mobile)
- 3 hashtags MAX at the bottom — more triggers algorithmic penalty
- Target post length: 1,100-1,500 characters for infographic companion posts
- Document/PDF posts get 2-3x reach vs image posts
- The infographic must provide a WORKING FRAMEWORK they can use TODAY
- The infographic must be saveable — something they'd screenshot or send to a friend
- NEVER include URLs or links — LinkedIn actively suppresses posts with outbound links
- NEVER gate content or tease "DM me for more" — give ALL of the information away freely
- The post should teach. The avatar should walk away with something they can use immediately.
- No selling. No funnels. No "link in comments." Pure value = maximum reach.
"""


def get_contexts(data=None):
    """Extract voice contexts from request body.
    Admin requests fall back to Lon's defaults. Client requests get empty strings."""
    if data is None:
        data = request.json or {}
    profile = data.get('profile', {})
    if is_admin():
        avatar = profile.get('avatar_context') or AVATAR_CONTEXT
        voice = profile.get('voice_context') or VOICE_CONTEXT
        cal = profile.get('calibration') or LON_CALIBRATION
        algo = profile.get('algorithm_context') or ALGORITHM_CONTEXT
    else:
        avatar = profile.get('avatar_context', '')
        voice = profile.get('voice_context', '')
        cal = profile.get('calibration', '')
        algo = profile.get('algorithm_context', '')
    name = profile.get('name') or 'Writer'

    # A saved style layers ON TOP of the voice OS rather than replacing it.
    # Swapping a whole calibrated voice for a few lines of style note is how you
    # lose the thing that makes the output sound like Lon, so the note is an
    # additional constraint applied last, where it can bend the voice but not
    # erase it.
    style_note = (data.get('style_note') or '').strip()
    if not style_note:
        style_id = data.get('style_id')
        if style_id:
            saved = store_read('styles', {}).get(str(style_id))
            if saved:
                style_note = (saved.get('note') or '').strip()
    if style_note:
        voice = f"""{voice}

## STYLE OVERLAY — applies to this piece only
Everything above still governs. Apply this on top of it, and where the two
genuinely collide, this wins:

{style_note}"""

    return avatar, voice, cal, algo, name


# The strong writing models think before they answer, so a single turn can run
# for minutes. 90s was tuned for Haiku and cuts Fable off mid-sentence.
MODEL_TIMEOUT = float(os.environ.get('MODEL_TIMEOUT', '600'))


def get_client():
    """Get Anthropic client from environment variable."""
    api_key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        raise ValueError('No ANTHROPIC_API_KEY found. Set it as an environment variable.')
    return anthropic.Anthropic(api_key=api_key, timeout=MODEL_TIMEOUT)


def call_model(client, **kwargs):
    """Run one completion over a streaming connection.

    Streaming is what makes a long turn survivable — a blocking create() on a
    multi-minute generation trips the SDK's HTTP timeout, and the reasoning
    models routinely take that long. Callers still get one finished message
    back; this runs on a job thread, so nothing is holding a browser
    connection open while it works.
    """
    with client.messages.stream(**kwargs) as stream:
        return stream.get_final_message()


@app.errorhandler(Exception)
def handle_error(e):
    return jsonify({'error': f'Something went wrong — try again. ({type(e).__name__})'}), 503


# ---------------------------------------------------------------------------
# Background jobs
#
# Generation used to happen inside the request, with keepalive bytes holding
# the connection open. That stopped working once the writing routes moved to a
# model that reasons first: there is a ~30s ceiling on a single response in
# front of this app, and it caps total duration, so no amount of keepalive
# saves a longer generation — the response is simply cut and the caller gets a
# 200 with no content. Handing back a job id and letting the client poll takes
# the ceiling out of play, which is what lets these routes use the model we
# actually want instead of the fastest one that fits under 30s.
#
# The store is in-process, which is fine on a single worker and is also why
# jobs are reaped rather than kept.
# ---------------------------------------------------------------------------
JOB_TTL = 900

_jobs = {}
_jobs_lock = threading.Lock()


def _reap_jobs(now):
    """Drop finished jobs nobody collected. Caller must hold _jobs_lock."""
    for k in [k for k, v in _jobs.items() if now - v['ts'] > JOB_TTL]:
        _jobs.pop(k, None)


def run_as_job(fn):
    """Start fn() in the background and hand back a job id to poll.

    fn must return a JSON-serializable dict. Errors are captured and returned
    from the job endpoint rather than raised here, so the caller sees the same
    {'error': ...} shape it always did.
    """
    job_id = uuid.uuid4().hex
    now = time.time()
    with _jobs_lock:
        _reap_jobs(now)
        _jobs[job_id] = {'status': 'running', 'data': None, 'error': None, 'ts': now}

    def worker():
        try:
            data = fn()
            status, payload = 'done', {'data': data}
        except Exception as e:
            status, payload = 'error', {'error': f'{type(e).__name__}: {str(e)}'}
        with _jobs_lock:
            job = _jobs.get(job_id)
            if job is not None:
                job.update(status=status, ts=time.time(), **payload)

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({'job_id': job_id})


@app.route('/api/job/<job_id>')
def job_status(job_id):
    """Poll a generation started by run_as_job()."""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return jsonify({'error': 'That generation expired. Try again.'}), 404
        if job['status'] == 'running':
            return jsonify({'status': 'running'})
        # Finished either way — hand it over once and free the slot.
        _jobs.pop(job_id, None)
        if job['status'] == 'error':
            return jsonify({'error': job['error']})
        return jsonify(job['data'])


# ─── ONBOARDING (voice interview for clients) ────────

ONBOARD_QUESTIONS = [
    {
        "label": "Your reader",
        "question": "Describe the person you want to reach with your content. Don't overthink this. Just start talking about them.",
        "hint": "Who are they? What do they do? What's going on in their life?"
    },
    {
        "label": "What they feel",
        "question": "Tell me about what your reader is feeling. What are they worried about in the job? What are they afraid to ask for help with?",
        "hint": "The stuff they think about on the drive home."
    },
    {
        "label": "Someone you helped",
        "question": "Tell me about someone specific — someone you've helped before. What were they dealing with and what changed?",
        "hint": "A real person, a real situation."
    },
]


@app.route('/api/onboard/question', methods=['POST'])
@rate_limited
def onboard_question():
    """Fixed voice interview — three questions, always the same."""
    data = request.json
    history = data.get('history', [])
    q_index = len(history)

    if q_index >= len(ONBOARD_QUESTIONS):
        return jsonify({'ready': True})

    q = ONBOARD_QUESTIONS[q_index]
    return jsonify({'ready': False, 'label': q['label'], 'question': q['question'], 'hint': q['hint']})

    return run_as_job(do_call)


@app.route('/api/onboard/complete', methods=['POST'])
@rate_limited
def onboard_complete():
    """Take the voice interview answers and generate a full writing profile.
    Returns the profile to the client (stored in localStorage, not a database)."""
    data = request.json
    history = data.get('history', [])
    writer_name = data.get('name', 'Writer')
    pro_key = data.get('pro_key', '')
    char_limit = PRO_CHAR_LIMIT if (pro_key and pro_key in PRO_KEYS) else FREE_CHAR_LIMIT
    own_writing = data.get('own_writing', '')[:char_limit]
    admired_writing = data.get('admired_writing', '')[:char_limit]
    influences = data.get('influences', '')

    client = get_client()

    interview_text = ''
    for entry in history:
        interview_text += f"\n### {entry.get('label', 'Q')}\n"
        interview_text += f"Q: {entry.get('question', '')}\n"
        interview_text += f"A: {entry.get('answer', '')}\n"

    sound_section = ''
    if own_writing:
        sound_section += f"\n\n## THEIR OWN WRITING SAMPLES\n{own_writing}"
    if admired_writing:
        sound_section += f"\n\n## WRITING THEY ADMIRE\n{admired_writing}"
    if influences:
        sound_section += f"\n\n## WRITERS & CREATORS THEY LOVE\n{influences}"

    def do_call():
        msg = call_model(client,
            model=UTILITY_MODEL, max_tokens=6000,
            system="""You are building a complete writer's voice profile from an interview AND writing samples.

Generate FOUR sections, each clearly labeled and detailed:

1. AVATAR_CONTEXT — Who they write for. Pain points, desires, the moment they reach out, defining paradox. Build this from the interview answers about their audience.

2. VOICE_CONTEXT — How they write. If they provided their own writing samples, analyze those closely — pull exact phrases, sentence rhythms, how they open and close, their cadence. If they shared writing they admire or named influences, blend those patterns in. Be extremely specific.

3. CALIBRATION — If they provided their own writing, use those as the calibration examples verbatim. If they shared admired writing, note what to borrow from it. Derive 8-10 voice rules from the actual samples. If no samples were provided, synthesize what their best writing WOULD sound like based on everything they said.

4. ALGORITHM_CONTEXT — Platform rules for LinkedIn customized for their audience.

Return ONLY valid JSON:
{
  "avatar_context": "full avatar context text",
  "voice_context": "full voice context text",
  "calibration": "full calibration text",
  "algorithm_context": "full algorithm context text",
  "summary": "2-3 sentence summary of their voice for display"
}""",
            messages=[{'role': 'user', 'content': f'Writer: {writer_name}\n\nInterview answers:\n{interview_text}{sound_section}'}]
        )
        raw = extract_text(msg)
        if raw.startswith('```'):
            raw = raw.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        profile = json.loads(raw)

        return {
            'ok': True,
            'profile': {
                'avatar_context': profile.get('avatar_context', ''),
                'voice_context': profile.get('voice_context', ''),
                'calibration': profile.get('calibration', ''),
                'algorithm_context': profile.get('algorithm_context', ''),
            },
            'summary': profile.get('summary', 'Profile created.')
        }

    return run_as_job(do_call)


# ─── CONTENT ROUTES ──────────────────────────────────

@app.route('/robots.txt')
def robots():
    return Response("User-agent: *\nDisallow: /\n", mimetype='text/plain')


@app.route('/api/health')
def health():
    return jsonify({'ok': True})


@app.route('/api/verify-pro', methods=['POST'])
def verify_pro():
    data = request.json or {}
    key = data.get('pro_key', '')
    if key and key in PRO_KEYS:
        return jsonify({'ok': True, 'tier': 'pro'})
    return jsonify({'error': 'Invalid key'}), 403


@app.route('/api/verify-key', methods=['POST'])
def verify_key():
    if is_admin():
        return jsonify({'ok': True})
    return jsonify({'error': 'Invalid key'}), 403


@app.route('/')
def index():
    return send_from_directory(os.path.dirname(__file__), 'index.html')


@app.route('/client')
def client_page():
    return send_from_directory(os.path.dirname(__file__), 'client.html')


@app.route('/n40-brand.css')
def brand_css():
    return send_from_directory(os.path.dirname(__file__), 'n40-brand.css')


@app.route('/images/<path:filename>')
def serve_image(filename):
    return send_from_directory(os.path.join(os.path.dirname(__file__), 'images'), filename)


def _manifest(name, short_name, start_url):
    """Web app manifest — what makes this installable to a home screen.

    Icons are the N.40 mark on brand midnight. iOS paints its own background
    behind a transparent icon, so these are deliberately opaque rather than
    letting the orange square land on whatever iOS picks.
    """
    resp = jsonify({
        'name': name,
        'short_name': short_name,
        'description': 'Write, recycle and publish N.40 content.',
        'start_url': start_url,
        'scope': '/',
        'display': 'standalone',
        'background_color': '#101109',
        'theme_color': '#101109',
        'icons': [
            {'src': '/images/icon-192.png', 'sizes': '192x192',
             'type': 'image/png', 'purpose': 'any'},
            {'src': '/images/icon-512.png', 'sizes': '512x512',
             'type': 'image/png', 'purpose': 'any'},
            {'src': '/images/icon-512-maskable.png', 'sizes': '512x512',
             'type': 'image/png', 'purpose': 'maskable'},
        ],
    })
    resp.headers['Content-Type'] = 'application/manifest+json'
    return resp


@app.route('/manifest.webmanifest')
def manifest_admin():
    return _manifest('N.40 Content Engine', 'N.40 Writer', '/')


@app.route('/client.webmanifest')
def manifest_client():
    return _manifest('N.40 Writer', 'N.40 Writer', '/client')


@app.route('/api/next-question', methods=['POST'])
@rate_limited
def next_question():
    """Generate the NEXT interview question dynamically, streamed to keep connection alive."""
    data = request.json
    topic = data.get('topic', '')
    history = data.get('history', [])
    question_number = len(history) + 1

    if not topic:
        return jsonify({'error': 'No topic provided'}), 400

    avatar, voice, cal, algo, user_name = get_contexts(data)
    client = get_client()

    history_text = ''
    for entry in history:
        history_text += f"\n### {entry.get('label', 'Q')}\n"
        history_text += f"Question: {entry.get('question', '')}\n"
        history_text += f"Answer: {entry.get('answer', '[skipped]')}\n"

    system_prompt = f"""You are the content interview engine for {user_name}.

{avatar}

{voice}

{algo}

YOU ARE BUILDING A POST DYNAMICALLY — one question at a time. This is question #{question_number}.

YOUR JOB: Look at EVERYTHING {user_name} has given you so far (the seed + all answers) and decide:

OPTION A — ASK THE NEXT QUESTION: Generate the ONE question that will most improve this content right now. Your question should adapt to what was just said. If a story was given, dig deeper into the emotion. If a framework, ask for the wound it explains. If a surface answer, push toward the real thing.

OPTION B — SIGNAL READY: If you have enough material for a world-class LinkedIn post AND Substack article (you need: a story/moment, emotional truth, teachable framework, hook material, and a closing question angle), return {{"ready": true}} instead.

VOICE COACHING — Watch for gaps using the correction list:
- Over-explaining after the punch → ask for the SHORT version
- Big concepts without anchors → ask for a real moment, a visible cost
- Sermon-like cadence without story → push for lived detail
- Saying "truth" without naming it → ask to write the actual forbidden sentence
- Framework before wound → ask for the wound first
- Stacking too many ideas → focus on ONE punch

YOUR QUESTION MUST:
- Reference specific details from what was already said (names, phrases, moments)
- Target what's MISSING — do NOT ask for what was already given
- Push toward what will make this post saveable, shareable, and algorithm-optimized
- Be conversational, not clinical — you're a creative partner, not a form

ALGORITHM AWARENESS — You're building toward:
- LinkedIn: Hook under 140 chars, 1100-1500 chars, save-optimized, 3 hashtags, question that drives 15+ word comments
- Substack: Deeper exploration, 800-1200 words, more story, more teaching, newsletter-intimate tone

If asking a question, return ONLY this JSON:
{{
  "ready": false,
  "label": "short label (e.g., The Moment, The Cost, The Line)",
  "question": "the actual question — specific, referencing what was said",
  "hint": "coaching text that helps nail the answer. Be specific. Give examples of what a great answer looks like.",
  "missing": ["list of what's still needed after this question, e.g., 'hook material', 'closing question angle'"]
}}

If ready, return ONLY: {{"ready": true}}

NEVER ask more than 6 questions total. By question 5-6, if you don't have enough, work with what you have and signal ready."""

    user_content = f'{user_name}\'s seed:\n\n{topic}\n\n--- CONVERSATION SO FAR ---\n{history_text if history_text else "(First question — no answers yet)"}'

    def do_call():
        msg = call_model(client,
            model=UTILITY_MODEL,
            max_tokens=1500,
            system=system_prompt,
            messages=[{'role': 'user', 'content': user_content}]
        )
        raw = extract_text(msg)
        if raw.startswith('```'):
            raw = raw.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r'\{[\s\S]*\}', raw)
            if match:
                return json.loads(match.group())
            return {'error': 'Claude returned an unexpected format. Try again.'}

    return run_as_job(do_call)


@app.route('/api/generate-content', methods=['POST'])
@rate_limited
def generate_content():
    """Given topic + full interview history, generate LinkedIn post + Substack post + infographic."""
    data = request.json
    topic = data.get('topic', '')
    history = data.get('history', [])
    template = data.get('template', 'list')
    color_mode = data.get('colorMode', 'dark')

    if not topic:
        return jsonify({'error': 'No topic provided'}), 400

    avatar, voice, cal, algo, user_name = get_contexts(data)
    client = get_client()

    answers_text = ''
    for entry in history:
        if entry.get('answer') and entry.get('answer') != '[skipped]':
            answers_text += f"\n## {entry.get('label', 'Q')}\n{entry.get('answer', '')}\n"

    system_prompt = f"""You are the content creation engine for {user_name}.

{avatar}

{cal}

{algo}

Your job: Take raw interview answers and write them into THREE things — in the writer's voice. Read the calibration examples above. If your output doesn't sound like those, start over.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. LINKEDIN POST TEXT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Hook: Under 140 characters. A real moment or scene, NOT a motivational slogan.
- Use actual words, stories, and names from the interview. Don't paraphrase into generic wisdom.
- 1,100-1,500 characters total
- End with a question that invites a STORY — NOT "Are you ready?"
- NO URLs. NO links. NO "DM me." NO "link in comments."
- Exactly 3 hashtags at the end
- The reader should feel seen, not lectured

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
2. SUBSTACK POST
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Same core story, expanded into a letter to one person.

- Title: Conversational, not clickbait.
- Subtitle: One line that sets up the tension.
- Length: 800-1,200 words
- Open with a moment or scene — NOT a thesis statement.
- More story, more texture than LinkedIn allows. Let it breathe.
- Go deeper into the framework.
- Include section breaks (---) where natural.
- End with something that sits with the reader, not a CTA.
- Format in markdown (## for headings, **bold** for emphasis, --- for section breaks).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
3. INFOGRAPHIC CONTENT (for a "{template}" template)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CRITICAL: LinkedIn displays infographics at ~46% size in the feed.

RULES FOR ALL TEMPLATES:
- Title: 6 words max. Punchy. Not a sentence.
- Items: 3-5 words each. Fragments > full sentences.
- NEVER write full sentences on an infographic.

Structure by template:
- "quote": single "quote" field — one devastating line, max 12 words
- "list": title (4-6 words) + 3-5 items (5 words max each)
- "comparison": title + leftHeader (3 words), rightHeader (3 words), 3-4 items per side
- "funnel": title + 3-4 stages (4 words max each)
- "cheatsheet": title + 4 sections (heading + 2 bullet fragments each)
- "acronym": title + 3-5 letters, each with word + 5-word description
- "system": title + 3-4 categorized rows (label + content, 5 words max)

Include "{user_name} | Content Engine" as attribution (NO URL).

Return ONLY valid JSON:
{{
  "postText": "the full LinkedIn post text",
  "substackTitle": "Substack article title",
  "substackSubtitle": "Substack subtitle",
  "substackBody": "full Substack article in markdown",
  "infographic": {{
    "title": "main title",
    "subtitle": "optional subtitle",
    "sections": [...],
    "leftHeader": "comparison only",
    "rightHeader": "comparison only",
    "leftItems": ["comparison only"],
    "rightItems": ["comparison only"],
    "items": ["for list/funnel/acronym"],
    "descriptions": ["optional descriptions"],
    "categories": ["system template labels"],
    "steps": ["system template content"]
  }}
}}

No markdown fences. No explanation. Just the JSON."""

    user_msg = f'Topic: {topic}\nTemplate: {template}\nColor mode: {color_mode}\n\n{user_name}\'s raw answers:\n{answers_text}'

    def do_call():
        msg = call_model(client,
            model=HEAVY_MODEL, max_tokens=6000,
            system=system_prompt, messages=[{'role': 'user', 'content': user_msg}]
        )
        text = extract_text(msg)
        return parse_json_response(text)

    return run_as_job(do_call)


@app.route('/api/refine', methods=['POST'])
@rate_limited
def refine():
    """Iterate on existing content with feedback."""
    data = request.json
    current_post = data.get('postText', '')
    current_substack = data.get('substackBody', '')
    feedback = data.get('feedback', '')
    topic = data.get('topic', '')
    target = data.get('target', 'post')
    template = data.get('template', 'list')
    infographic_data = data.get('infographicData', {})

    avatar, voice, cal, algo, user_name = get_contexts(data)
    client = get_client()

    infographic_json = json.dumps(infographic_data, indent=2) if infographic_data else '{}'

    return_fields = []
    if target in ('post', 'both', 'all'):
        return_fields.append('"postText": "refined LinkedIn post"')
    if target in ('substack', 'all'):
        return_fields.append('"substackTitle": "refined title"')
        return_fields.append('"substackSubtitle": "refined subtitle"')
        return_fields.append('"substackBody": "refined Substack article in markdown"')
    if target in ('infographic', 'both', 'all'):
        return_fields.append('"infographic": { ...template-specific fields... }')

    system_prompt = f"""You are the content refinement engine for {user_name}.

{voice}
{algo}

Apply the feedback precisely while maintaining:
- The writer's voice
- Algorithm optimization (saves, hook under 140 chars, 1100-1500 chars, 3 hashtags for LinkedIn)
- The avatar connection (they must see themselves in this)
- NEVER include URLs, links, or website references
- Give ALL the information away. No gates, no funnels, no "DM me."

REFINING TARGET: {target}

For LinkedIn: Keep 1100-1500 chars, hook under 140, 3 hashtags, save-optimized.
For Substack: Keep 800-1200 words, newsletter-intimate, deeper teaching, clean markdown.
For infographic (template: "{template}"): Keep readable at 46% zoom, fragments not sentences.

Template structures:
- "cheatsheet": sections array with heading + items
- "funnel": items array
- "system": categories + steps arrays
- "acronym": items + descriptions arrays
- "comparison": leftHeader, rightHeader, leftItems, rightItems
- "list": items array
- "quote": quote field

Return ONLY valid JSON with: {{ {", ".join(return_fields)} }}"""

    user_content = f'Topic: {topic}\nTemplate: {template}\nRefine target: {target}\n\n'
    if current_post:
        user_content += f'Current LinkedIn post:\n{current_post}\n\n'
    if current_substack:
        user_content += f'Current Substack article:\n{current_substack}\n\n'
    if infographic_data:
        user_content += f'Current infographic data:\n{infographic_json}\n\n'
    user_content += f'Feedback:\n{feedback}'

    def do_call():
        msg = call_model(client,
            model=HEAVY_MODEL, max_tokens=6000,
            system=system_prompt, messages=[{'role': 'user', 'content': user_content}]
        )
        text = extract_text(msg)
        return parse_json_response(text)

    return run_as_job(do_call)


@app.route('/api/recycle', methods=['POST'])
@rate_limited
def recycle():
    """Recycle an old post into fresh algorithm-optimized content + visual."""
    data = request.json
    original = data.get('original', '')
    length = data.get('length', 'medium')
    fmt = data.get('format', 'image')
    slides = data.get('slides', 6)

    if not original:
        return jsonify({'error': 'No original post provided'}), 400

    length_range = {
        'short': '600-900 characters',
        'medium': '1,100-1,500 characters',
        'long': '1,800-2,200 characters'
    }.get(length, '1,100-1,500 characters')

    avatar, voice, cal, algo, user_name = get_contexts(data)
    client = get_client()

    if fmt == 'image':
        visual_instructions = """
For the "visual" field, return:
{
  "lines": ["line 1 of text", "line 2 of text", "line 3 of text"]
}
These lines will be overlaid on a branded B&W photo.

WHAT MAKES PEOPLE SAVE AN IMAGE — pick ONE of these three types:
1. A DIRECT REFRAME — a line that changes how they see their situation.
2. A SHORT LISTICLE (up to 4 items) — a list of things they'll screenshot.
3. A QUOTE THAT CHANGES HOW THEY SEE THE WORLD — pull the strongest line from the post itself.

Rules:
- 3-5 short lines MAXIMUM
- Each line under 8 words
- The last line lands the punch
- Billboard test: readable in 3 seconds
- NO URLs, NO hashtags, NO attribution (branding is on the photo)
"""
    else:
        visual_instructions = f"""
For the "visual" field, return:
{{
  "slides": [
    {{"title": "Title slide headline", "subtitle": "optional subtitle"}},
    {{"heading": "Slide 2 heading", "text": "Slide 2 body text — 2-3 sentences max"}},
    ... repeat for {slides} total slides ...,
    {{"text": "Closing thought or question", "cta": true}}
  ]
}}
Rules for carousel:
- Exactly {slides} slides
- Slide 1 = title slide (hook that stops the scroll)
- Last slide = closing thought + conversation starter (NOT a CTA to visit a website)
- Middle slides = the framework, one idea per slide
- Each slide body: 2-3 sentences MAX. Dense but scannable.
- NO URLs, NO links, NO "DM me", NO website references
"""

    def do_call():
        msg = call_model(client,
        model=HEAVY_MODEL,
        max_tokens=4000,
        system=f"""You are refreshing a post for {user_name}.

{avatar}

{cal}

{algo}

Your job: REFRESH this post for today's algorithm — do NOT rewrite it. The voice IS the post. Keep stories, names, warmth, casual tone, invitational endings.

WHAT YOU KEEP (almost everything):
- Exact phrasing, stories, names, places, and specific details
- Casual warmth
- Invitational endings
- Structure and flow — don't reorganize
- Imperfections and vulnerability — that's what makes it real

WHAT YOU MAY TIGHTEN (lightly):
- Cut any line that restates what a stronger line already said
- Sharpen the hook if it's soft (under 140 chars, must land before "see more")
- Strengthen the close — 3-8 words, inevitable, hard to argue with
- Ensure one "copy/paste line" people will screenshot
- Ensure the closing question requires a story (NOT yes/no)
- Target length: {length_range}
- Exactly 3 hashtags at the end
- NO URLs, NO links, NO "DM me", NO "link in comments"

{visual_instructions}

Return ONLY valid JSON:
{{
  "postText": "the improved LinkedIn post text",
  "visual": {{ ... visual data as described above ... }}
}}

No markdown fences. No explanation. Just the JSON.""",
        messages=[{'role': 'user', 'content': f'Post to improve:\n\n{original}'}]
    )
        text = extract_text(msg)
        return parse_json_response(text)

    return run_as_job(do_call)


@app.route('/api/recycle-refine', methods=['POST'])
@rate_limited
def recycle_refine():
    """Refine recycled content with feedback."""
    data = request.json
    target = data.get('target', 'post')
    fmt = data.get('format', 'image')
    post_text = data.get('postText', '')
    visual_data = data.get('visualData', {})
    feedback = data.get('feedback', '')

    avatar, voice, cal, algo, user_name = get_contexts(data)
    client = get_client()

    if target == 'visual':
        if fmt == 'image':
            visual_desc = """The visual is a branded image with text overlay.
Current data: """ + json.dumps(visual_data) + """
Return {"visual": {"lines": ["line 1", "line 2", ...]}}
Keep lines short (under 8 words each), 3-5 lines max."""
        else:
            visual_desc = """The visual is a carousel.
Current data: """ + json.dumps(visual_data) + """
Return {"visual": {"slides": [...]}} maintaining the same structure."""

        sys_prompt = f"""{voice}
{algo}

Refine the visual content based on feedback. {visual_desc}
NEVER include URLs, links, or website references.
Return ONLY valid JSON."""
        usr_msg = f'Feedback: {feedback}'
        max_tok = 4000
    else:
        sys_prompt = f"""{voice}
{algo}

Refine the post text based on feedback. Maintain voice and algorithm optimization.
NEVER include URLs, links, or website references. Give everything away freely.
Return ONLY valid JSON: {{"postText": "refined text"}}"""
        usr_msg = f'Current post:\n{post_text}\n\nFeedback: {feedback}'
        max_tok = 2000

    def do_call():
        msg = call_model(client,
            model=HEAVY_MODEL, max_tokens=max_tok,
            system=sys_prompt, messages=[{'role': 'user', 'content': usr_msg}]
        )
        text = extract_text(msg)
        return parse_json_response(text)

    return run_as_job(do_call)


@app.route('/api/generate-note', methods=['POST'])
@rate_limited
def generate_note():
    """Take a short thought and write it as a LinkedIn Note — 4 length modes."""
    data = request.json
    thought = data.get('thought', '')
    edge = data.get('edge', 'teach')
    length = data.get('length', 'note')

    if not thought:
        return jsonify({'error': 'No thought provided'}), 400

    avatar, voice, cal, algo, user_name = get_contexts(data)

    edge_instructions = {
        'teach': 'The note should TEACH — give the reader something they can use today. Name the mechanism. Give the language.',
        'reframe': 'The note should REFRAME — take what they believe and flip it. Show them the thing they\'ve been looking at wrong.',
        'confrontation': 'The note should CONFRONT — call them out with precision and care. Name what they\'re hiding from. Direct without cruel.',
        'truth': 'The note should deliver a TRUTH — say the thing nobody else will say. The sentence people read twice.'
    }.get(edge, '')

    length_instructions = {
        'sniper': """## SNIPER MODE — Justin Welsh / Alex Hormozi energy

LENGTH: 1-2 lines. Under 150 characters. That's it.

THIS IS NOT the usual voice. This is algorithm-optimized, pattern-interrupt, scroll-stopping copy.

RULES:
- One line or two. MAX.
- Hard truth, stated plainly. No warm-up.
- Contrasts, inversions, and reframes that stop the scroll
- Punchy. Blunt. Zero fat.
- NO stories. NO invitations. NO hashtags. NO warmth. Just the hit.
- The reader should screenshot this and send it to someone.""",

        'punch': """## PUNCH MODE — Sharp and tactical

LENGTH: 3-5 lines. 150-300 characters.

PATTERNS:
- Open with the contrarian claim
- One line of proof or context
- Close with the punchline

Still blunt. Still tactical.

RULES:
- 3-5 lines max
- No hashtags. No links. No emojis.
- Every line earns its place
- The last line should be the one people remember""",

        'note': f"""## NOTE MODE — Room to breathe

LENGTH: 6-10 lines. 300-600 characters.

{cal}

RULES:
- 6-10 lines. Let the thought develop.
- Warm, invitational, real
- Can include a brief moment or image
- NO hashtags. NO links. NO emojis.""",

        'letter': f"""## LETTER MODE — Most personal

LENGTH: 10-20 lines. 600-1200 characters.

Write a short letter to one person. A story, a confession, a memory.

{cal}

RULES:
- 10-20 lines. Let the story breathe.
- Warm, specific, vulnerable, invitational
- Include a real detail (a place, a time of day, a person's name if relevant)
- NO hashtags at end. NO links. NO emojis."""
    }.get(length, '')

    client = get_client()

    sys_prompt = f"""You are shaping {user_name}'s raw thought into a LinkedIn Note.

{avatar}

{edge_instructions}

{length_instructions}

CRITICAL — KEEP THE WRITER'S WORDS:
You are SHAPING, not rewriting. The words ARE the post.

- USE exact phrasing, word choices, rhythm
- DO NOT add stories not mentioned
- DO NOT wrap the thought in a narrative
- DO NOT soften the edge or pad with context
- You may TIGHTEN (cut words that don't earn their place)
- You may SHARPEN (make the punchline land harder)
- You may RESTRUCTURE (reorder for impact — punch first, context second)
- You may ADD one line max

Result: A truth bomb. Direct conversation with one reader. Not a story. Not a sermon.

NEVER USE: leverage, optimize, synergy, actionable, transformative, unlock your potential, thrive, abundance, empowered, curated, hacks

Return ONLY the note text. No JSON. No quotes. No explanation. Just the note, ready to post."""

    usr_msg = f'{user_name}\'s raw thought (keep the words, shape don\'t rewrite):\n\n{thought}'

    def do_call():
        msg = call_model(client,
            model=WRITER_MODEL, max_tokens=2000,
            system=sys_prompt, messages=[{'role': 'user', 'content': usr_msg}]
        )
        return {'note': extract_text(msg)}

    return run_as_job(do_call)


@app.route('/api/refine-note', methods=['POST'])
@rate_limited
def refine_note():
    """Refine a LinkedIn Note with feedback."""
    data = request.json
    thought = data.get('thought', '')
    edge = data.get('edge', 'teach')
    length = data.get('length', 'note')
    current = data.get('current', '')
    feedback = data.get('feedback', '')

    length_desc = {
        'sniper': 'SNIPER: 1-2 lines max, under 150 chars. Blunt. No warmth, no stories, just the hit.',
        'punch': 'PUNCH: 3-5 lines, 150-300 chars. Sharp, tactical, pattern-interrupt.',
        'note': 'NOTE: 6-10 lines, 300-600 chars. Warm, invitational, casual. End with an invitation.',
        'letter': 'LETTER: 10-20 lines, 600-1200 chars. Personal — story, confession, memory. Real details, real warmth.'
    }.get(length, '')

    avatar, voice, cal, algo, user_name = get_contexts(data)
    client = get_client()

    sys_prompt = f"""You are refining a LinkedIn Note.

Mode: {length_desc}
Edge: {edge}.

Apply the feedback precisely. Stay in the mode.

NO hashtags. NO links. NO emojis.

Return ONLY the refined note text. No JSON. No quotes. No explanation."""

    usr_msg = f'Original thought: {thought}\n\nCurrent note:\n{current}\n\nFeedback: {feedback}'

    def do_call():
        msg = call_model(client,
            model=WRITER_MODEL, max_tokens=2000,
            system=sys_prompt, messages=[{'role': 'user', 'content': usr_msg}]
        )
        return {'note': extract_text(msg)}

    return run_as_job(do_call)


TRADE_CHAPTERS = {
    1: {
        "title": "The Awakening — Is This All There Is?",
        "core": """Something is off, you don't know why, and you're finally ready to admit it. This is where restlessness becomes undeniable.
Key themes: The three phrases people say before they're ready: "I have a great life, but..." / "I just feel like something's missing." / "I'm not sure who I am anymore."
The problem isn't burnout — it's misalignment. You're not worn out. You're done pretending.
That first gut punch — the car, the shower, 2am — when you admitted something had to change. That was your first Trade. You just haven't made it yet."""
    },
    2: {
        "title": "I Guess That Makes Two of Us",
        "core": """The first real trade — the one that hurt people you love. Chasing who you're becoming can cost relationships. That's when you learn what a real trade costs — and why it's still worth it.
Key themes: "I didn't leave because I hated it. I left because I knew I was done." / The moment someone read your story and said "That's exactly how I feel" / The trade always leaves a mark. That's what makes it real. / You don't have to blow it all up. But you do have to ask: Will I regret not finding out?"""
    },
    3: {
        "title": "Maybe My Work Here Is Done",
        "core": """The 4 Phases of Massive Action: Explore → Invest → Test → Trade.
Identity is a process, not a prison. Clarity is earned through motion. This is the permission slip chapter.
Key themes: "You don't have to quit in order to start. But you have to start if you ever want to feel ready to quit." / Your pattern IS the plan. You're already doing it — just not consciously. / What you're going through is normal. It's predictable. Once you see the pattern, everything feels less random."""
    },
    4: {
        "title": "The Clock — Yours",
        "core": """The Normal 40 Clock. Life is a four-quarter game. Halftime is now. The second half won't play itself.
Key themes: "You're not tired. You're at halftime." / This isn't burnout — it's a shift in values, from achievement to alignment, from money to meaning. / There's a day when you stop seeing in quarters and start seeing in decades. That's when you realize you don't want to climb anymore. You want to build. / The clock doesn't wait. You're either using it or losing it."""
    },
    5: {
        "title": "The Brutal Reality of You (+ The Other Side of the Marriage)",
        "core": """The cost of silence and the power of honesty. You can be successful and deeply disconnected at the same time. Your spouse already knew.
Key themes: The marriage contract that's 15 years out of date — you gave them security, they gave you loyalty, but it's time to renegotiate. / "You've outgrown your image." / "Your spouse already knows. You're just not talking." / What does your spouse want FOR you? Not FROM you. Have you asked?"""
    },
    6: {
        "title": "The Box + The Awakening + The Choice",
        "core": """The emotional climax. The prison you built. The events that crack it open. The dare to decide.
The Box has four walls: The paycheck / The reputation / The family expectations / The image.
The 5 D's: Downsizing. Divorce. Drinking. Diagnosis. Death. But there's one D that saves everything: Decide.
Key themes: "Your box looks great from the outside. That's what makes it so dangerous." / "You don't need another D. You need a Decision." / Most people don't change until they're forced. But you can choose to change before you have to."""
    },
    7: {
        "title": "The Action + The Trade & The Financials",
        "core": """Movement and money. Stop wondering, start calculating. How to test, how to fund it, how to make a trade you won't regret.
Key themes: "I used my net worth to buy back my life." / "What if a small part of your net worth is your insurance policy against regret?" / The big leap comes after the small step, not before. / "Retirement is a lie if you waste your best years getting there." / The real question isn't "Can I afford it?" — it's "Can I afford not to?" / The cost of regret — not just financial, but emotional, relational, spiritual."""
    },
    8: {
        "title": "The Trade of a Lifetime — Your Final Line",
        "core": """Legacy, mortality, and courage. The mirror, one last time.
Key themes: "Your final line is still unwritten." / "Will your final line be: 'I'm glad I did.' Or: 'I wish I would have tried.'" / You still have time. / What are you willing to trade to become who you're capable of being? / "You're not late. You're just early — if you start now." / This isn't the end. It's the beginning of everything."""
    }
}


@app.route('/api/generate-trade', methods=['POST'])
@rate_limited
@require_admin_key
def generate_trade():
    """Generate content from a chapter of The Trade book."""
    data = request.json
    chapter_num = data.get('chapter', 1)
    lens = data.get('lens', 'framework')
    angle = data.get('angle', '')

    chapter = TRADE_CHAPTERS.get(chapter_num, TRADE_CHAPTERS[1])

    lens_instructions = {
        'framework': 'Pull the core FRAMEWORK from this chapter and teach it. Name the model, the steps, the pattern. Give the reader something they can use today. But wrap it in a story.',
        'story': 'Tell a STORY from this chapter. A real moment — a person, a place, a conversation. Let the teaching come through the story, not after it.',
        'confession': 'Write this as a CONFESSION. Admitting something vulnerable about the journey through this chapter. The kind of thing that makes people DM "I needed to hear this."',
        'reframe': 'Take the biggest idea in this chapter and REFRAME it. Show the reader the thing they\'ve been looking at wrong. Flip the assumption.',
        'challenge': 'CHALLENGE the reader directly from this chapter. Not a sermon — a dare. The kind of thing said across a table.'
    }.get(lens, '')

    avatar, voice, cal, algo, user_name = get_contexts(data)
    client = get_client()

    angle_line = f"\n\n{user_name}'s specific angle for this post:\n{angle}" if angle else ""

    def do_call():
        msg = call_model(client,
        model=HEAVY_MODEL,
        max_tokens=6000,
        system=f"""You are writing content from "The Trade" — an Amazon #1 Bestseller about elite performers who are winning on paper but dying inside.

{avatar}

{cal}

{algo}

## THE CHAPTER

{chapter['title']}

{chapter['core']}

## YOUR JOB

{lens_instructions}

PRODUCE THREE THINGS:

### 1. LINKEDIN POST (1,100-1,500 characters)
- Teach from this chapter
- Open with a moment, a memory, or a line from the book — not a motivational slogan
- The reader should learn something usable. Give everything away.
- End with a question that invites a STORY, not a yes/no
- 3 hashtags at the end. NO URLs, links, or CTAs.

### 2. SUBSTACK ARTICLE (800-1,200 words)
- Same chapter, deeper. A letter to one person.
- Open with a scene or memory. Let it breathe.
- Teach the full framework that LinkedIn doesn't have room for.
- Section breaks (---) where natural.
- End with something that sits with them. NO CTAs.

### 3. IMAGE TEXT
- 3-5 short lines for a branded 1080x1080 image
- Pull from the chapter's strongest line or actual phrasing
- Each line under 8 words. Last line lands.
- NO URLs, hashtags, or attribution.

Return ONLY valid JSON:
{{
  "postText": "the LinkedIn post",
  "substackTitle": "article title",
  "substackSubtitle": "subtitle",
  "substackBody": "full article body in markdown",
  "imageLines": ["line 1", "line 2", "line 3"]
}}

No markdown fences. No explanation. Just the JSON.""",
        messages=[{
            'role': 'user',
            'content': f'Chapter {chapter_num}: {chapter["title"]}\nLens: {lens}{angle_line}'
        }]
    )
        text = extract_text(msg)
        return parse_json_response(text)

    return run_as_job(do_call)


@app.route('/api/vault', methods=['GET'])
@require_admin_key
def vault():
    """Serve the post vault — ranked LinkedIn history."""
    vault_path = os.path.join(os.path.dirname(__file__), 'vault.json')
    if not os.path.exists(vault_path):
        return jsonify([])
    with open(vault_path, 'r') as f:
        posts = json.load(f)

    search = request.args.get('q', '').lower()
    min_comments = int(request.args.get('min_comments', 0))
    min_chars = int(request.args.get('min_chars', 0))
    page = int(request.args.get('page', 0))
    # Capped so a stray per_page cannot try to serialize all 2,350 posts at once.
    per_page = max(1, min(int(request.args.get('per_page', 50)), 200))
    # 'only' lists what has been dismissed, so a mistake can be found and undone.
    dismissed_mode = request.args.get('dismissed', 'hide')

    dismissed = store_read('dismissed', {})

    if dismissed_mode == 'only':
        posts = [p for p in posts if str(p['id']) in dismissed]
    elif dismissed_mode != 'show':
        posts = [p for p in posts if str(p['id']) not in dismissed]

    if search:
        posts = [p for p in posts if search in p['text'].lower()]
    if min_comments:
        posts = [p for p in posts if p['comments'] >= min_comments]
    if min_chars:
        posts = [p for p in posts if p['char_count'] >= min_chars]

    total = len(posts)
    posts = posts[page * per_page:(page + 1) * per_page]

    # Flag each row so the browser can label and un-dismiss without a second call.
    for p in posts:
        entry = dismissed.get(str(p['id']))
        p['dismissed'] = bool(entry)
        p['dismissed_reason'] = (entry or {}).get('reason', '') if isinstance(entry, dict) else ''

    return jsonify({'posts': posts, 'total': total, 'page': page, 'per_page': per_page,
                    'dismissed_total': len(dismissed)})


@app.route('/api/vault/dismiss', methods=['POST'])
@require_admin_key
def vault_dismiss():
    """Mark a vault post as never-recycle, or put it back.

    Dismissal is for posts tied to a moment that cannot be reused — a launch, a
    date, an event. Reversible on purpose: the cost of a wrong dismissal should
    be one click, not a lost post.
    """
    data = request.json or {}
    post_id = data.get('id')
    if post_id is None:
        return jsonify({'error': 'No post id provided'}), 400

    dismissed = store_read('dismissed', {})
    key = str(post_id)

    if data.get('dismissed', True):
        dismissed[key] = {'at': time.strftime('%Y-%m-%d %H:%M:%S'),
                          'reason': (data.get('reason') or '').strip()[:200]}
    else:
        dismissed.pop(key, None)

    store_write('dismissed', dismissed)
    return jsonify({'ok': True, 'dismissed': key in dismissed,
                    'dismissed_total': len(dismissed),
                    'persistent': STORE_IS_PERSISTENT})


@app.route('/api/vault-recycle', methods=['POST'])
@rate_limited
@require_admin_key
def vault_recycle():
    """Recycle a vault post into fresh LinkedIn + Substack content."""
    data = request.json
    original = data.get('original', '')
    original_date = data.get('original_date', '')

    if not original:
        return jsonify({'error': 'No post provided'}), 400

    avatar, voice, cal, algo, user_name = get_contexts(data)
    client = get_client()

    def do_call():
        msg = call_model(client,
        model=HEAVY_MODEL,
        max_tokens=6000,
        system=f"""You are refreshing a LinkedIn post for {user_name}.

{avatar}

{cal}

{algo}

This post is from {original_date or "the archive"}. It already worked — people responded to it. Your job is to REFRESH it for today's algorithm, NOT rewrite it.

CRITICAL RULES:
- Keep exact phrasing wherever it's strong — which is most of it
- Keep stories, names, places, and specific details INTACT
- Keep casual warmth
- Keep invitational endings
- DO NOT add motivational speaker language
- DO NOT replace stories with abstract wisdom
- DO NOT make it sound more "polished" or "professional"
- DO NOT add words from the NEVER USE list

WHAT YOU MAY DO:
- Tighten the hook so it lands in under 140 characters
- Cut lines that say the same thing twice
- Make sure the post ends with a question that invites a story
- Add 3 hashtags at the end
- Target 1,100-1,500 characters
- Remove any URLs or "link in comments" type language

PRODUCE THREE THINGS:

### 1. LINKEDIN POST (1,100-1,500 characters)
Refreshed version. Must still sound like the original.

### 2. SUBSTACK ARTICLE (800-1,200 words)
Same core story and lesson, expanded. A letter to one person. More texture, more story. NO CTAs, NO links.

### 3. IMAGE TEXT
3-5 short lines for a branded 1080x1080 image.
Pull from the post's strongest ACTUAL line.
Each line under 8 words. Last line lands.
NO URLs, hashtags, or attribution.

Return ONLY valid JSON:
{{
  "postText": "the LinkedIn post",
  "substackTitle": "article title",
  "substackSubtitle": "subtitle",
  "substackBody": "full article body in markdown",
  "imageLines": ["line 1", "line 2", "line 3"]
}}

No markdown fences. No explanation. Just the JSON.""",
        messages=[{'role': 'user', 'content': f'Original post ({original_date}):\n\n{original}'}]
    )
        text = extract_text(msg)
        return parse_json_response(text)

    return run_as_job(do_call)


@app.route('/api/rambles/status')
@require_admin_key
def rambles_status():
    """What is wired up, so the tab can say so instead of failing silently."""
    idx = store_read('rambles_index', {})
    return jsonify({
        'fathom': bool(FATHOM_API_KEY),
        'drive': bool(os.environ.get('GOOGLE_DRIVE_TOKEN_JSON')),
        'indexed': len(idx.get('files', [])),
        'built': idx.get('built', ''),
        'recent_days': RECENT_DAYS,
    })


@app.route('/api/rambles/reindex', methods=['POST'])
@require_admin_key
def rambles_reindex():
    """Rebuild the metadata map. Minutes, not seconds — it walks the tree."""
    def do_call():
        files = build_rambles_index()
        return {'indexed': len(files)}
    return run_as_job(do_call)


@app.route('/api/rambles/recent')
@require_admin_key
def rambles_recent():
    """Recent Ramble — anything Fathom recorded in the last RECENT_DAYS."""
    if not FATHOM_API_KEY:
        return jsonify({'error': 'Fathom is not connected. Set FATHOM_API_KEY.'}), 400
    after = time.strftime('%Y-%m-%dT%H:%M:%SZ',
                          time.gmtime(time.time() - RECENT_DAYS * 86400))
    items, cursor = [], None
    for _ in range(12):
        params = {'created_after': after, 'include_summary': 'true'}
        if cursor:
            params['cursor'] = cursor
        r = requests.get(f'{FATHOM_BASE}/meetings',
                         headers={'X-Api-Key': FATHOM_API_KEY},
                         params=params, timeout=45)
        if r.status_code == 429:
            time.sleep(int(r.headers.get('Retry-After') or 5))
            continue
        r.raise_for_status()
        body = r.json()
        items += body.get('items', [])
        cursor = body.get('next_cursor')
        if not cursor:
            break
    # Fathom's identifier is `recording_id`, and it is an int — not `id` or
    # `meeting_id`, which do not exist on the object at all.
    out = [{
        'source': 'fathom',
        'id': str(m.get('recording_id') or ''),
        'title': m.get('title') or m.get('meeting_title') or 'Untitled call',
        'date': (m.get('recording_start_time') or m.get('created_at') or '')[:10],
    } for m in items if m.get('recording_id')]
    out.sort(key=lambda x: x['date'], reverse=True)
    return jsonify({'rambles': out, 'days': RECENT_DAYS})


@app.route('/api/rambles/search')
@require_admin_key
def rambles_search():
    """Search the archive. Drive does the full-text matching; the metadata map
    keeps results inside actual rambles instead of returning the manuscript."""
    q = (request.args.get('q') or '').strip()
    if not q:
        return jsonify({'error': 'Type something to search for.'}), 400
    idx = store_read('rambles_index', {})
    known = {f['id']: f for f in idx.get('files', [])}
    if not known:
        return jsonify({'error': 'No ramble index yet. Run Reindex first.'}), 400

    safe = q.replace("\\", "\\\\").replace("'", "\\'")
    res = _drive('files',
                 q=f"fullText contains '{safe}' and mimeType = '{DOCX_MIME}' and trashed = false",
                 fields='files(id, name, modifiedTime)', pageSize=100)
    hits = []
    for f in res.get('files', []):
        meta = known.get(f['id'])
        if not meta:
            continue           # outside the ramble corpora — drop it
        hits.append({'source': 'drive', 'id': f['id'], 'title': f['name'],
                     'date': meta.get('date', ''), 'person': meta.get('person', ''),
                     'corpus': meta.get('corpus', ''), 'path': meta.get('path', '')})
    hits.sort(key=lambda h: h['date'], reverse=True)
    return jsonify({'rambles': hits, 'query': q, 'searched': len(known)})


@app.route('/api/rambles/random')
@require_admin_key
def rambles_random():
    """Random Ramble — one transcript at random from the archive."""
    idx = store_read('rambles_index', {})
    files = [f for f in idx.get('files', []) if f.get('corpus') == 'client-ramble']
    if not files:
        return jsonify({'error': 'No ramble index yet. Run Reindex first.'}), 400
    pick = random.choice(files)
    return jsonify({'ramble': {'source': 'drive', 'id': pick['id'], 'title': pick['name'],
                               'date': pick.get('date', ''), 'person': pick.get('person', ''),
                               'corpus': pick.get('corpus', ''), 'path': pick.get('path', '')}})


@app.route('/api/rambles/transcript', methods=['POST'])
@require_admin_key
def rambles_transcript():
    """Fetch one transcript. Only now does anything get downloaded."""
    data = request.json or {}
    source, rid = data.get('source'), data.get('id')
    if not rid:
        return jsonify({'error': 'No ramble selected.'}), 400

    def do_call():
        if source == 'fathom':
            # There is no filter for a single recording — passing recording_id
            # is ignored and still returns a full page — so page until it turns
            # up. Transcripts are large, so pages stay small.
            after = time.strftime('%Y-%m-%dT%H:%M:%SZ',
                                  time.gmtime(time.time() - RECENT_DAYS * 86400))
            cursor = None
            for _ in range(40):
                params = {'created_after': after, 'include_transcript': 'true', 'limit': 5}
                if cursor:
                    params['cursor'] = cursor
                r = requests.get(f'{FATHOM_BASE}/meetings',
                                 headers={'X-Api-Key': FATHOM_API_KEY},
                                 params=params, timeout=120)
                if r.status_code == 429:
                    time.sleep(int(r.headers.get('Retry-After') or 5))
                    continue
                r.raise_for_status()
                body = r.json()
                for m in body.get('items', []):
                    if str(m.get('recording_id')) == str(rid):
                        return {'text': fathom_transcript_text(m.get('transcript')),
                                'title': m.get('title') or m.get('meeting_title') or ''}
                cursor = body.get('next_cursor')
                if not cursor:
                    break
            return {'error': 'That call is no longer in the recent window.'}
        blob = requests.get(f'https://www.googleapis.com/drive/v3/files/{rid}',
                            headers={'Authorization': 'Bearer ' + _google_access_token()},
                            params={'alt': 'media'}, timeout=120).content
        return {'text': docx_to_text(blob)}

    return run_as_job(do_call)


@app.route('/api/rambles/insights', methods=['POST'])
@rate_limited
@require_admin_key
def rambles_insights():
    """The three hardest-hitting things the client said, and two themes worth
    writing about.

    Quotes must be verbatim and must come from the client, not from Lon — the
    useful material is what the other person admitted, not what the coach
    said. Themes are judged against the avatar: useful, helpful, fascinating
    to an elite performer who looks fine on paper and is not.
    """
    data = request.json or {}
    transcript = (data.get('transcript') or '').strip()
    if not transcript:
        return jsonify({'error': 'No transcript provided.'}), 400

    avatar, voice, cal, algo, user_name = get_contexts(data)
    client = get_client()

    def do_call():
        msg = call_model(client,
            model=HEAVY_MODEL, max_tokens=2000,
            system=f"""You are reading a transcript of a real conversation between {user_name} and one other person.

{avatar}

QUOTES — pick exactly three.
- Verbatim. Word for word from the transcript. Never tidied, never paraphrased.
- From the OTHER person, not {user_name}. What the client admitted is the
  material; what the coach said is not.
- Pick the three that land hardest: the admission, the thing said reluctantly,
  the line that gives away more than intended. Not the tidiest summary line.
- If a quote needs context to make sense, add one short line of it.

THEMES — pick exactly two.
- A theme is not a topic. "Career change" is a topic. "You can be good at a
  job and still have outgrown it" is a theme.
- Judge each against the avatar above: would this be USEFUL, HELPFUL and
  FASCINATING to someone winning on paper and quietly trapped?
- Say in one line why it would land with that reader.

Return ONLY valid JSON:
{{"quotes": [{{"text": "...", "context": "..."}}],
  "themes": [{{"theme": "...", "why": "..."}}]}}""",
            messages=[{'role': 'user', 'content': transcript}]
        )
        out = parse_json_response(extract_text(msg))

        # Check every quote actually appears in the transcript. Asking for
        # verbatim is not enough on its own — in testing one of three came
        # back smoothed into a line the person never said in one breath. These
        # get attributed to a real client in public, so an unverified quote is
        # marked rather than passed off as real.
        flat = ' '.join(transcript.split()).lower()
        for q in out.get('quotes', []):
            probe = ' '.join((q.get('text') or '').split()).lower()
            q['verbatim'] = bool(probe) and probe in flat
        return out

    return run_as_job(do_call)


@app.route('/api/rambles/generate', methods=['POST'])
@rate_limited
@require_admin_key
def rambles_generate():
    """Write a LinkedIn post from a ramble transcript.

    Anonymisation is in the prompt, not a later cleanup pass: these are real
    client conversations, and the rule is first name and hometown only, with
    written approval before anything identifying goes out. Making that a
    default of the generator means the unsafe version never gets written in
    the first place.
    """
    data = request.json or {}
    transcript = (data.get('transcript') or '').strip()
    if not transcript:
        return jsonify({'error': 'No transcript provided.'}), 400

    avatar, voice, cal, algo, user_name = get_contexts(data)
    client = get_client()
    is_client_call = data.get('corpus') != 'community'

    privacy = ("""
## PRIVACY — non-negotiable
This is a real private conversation with a real person.
- Use a first name only, never a surname. A hometown is allowed; nothing else that identifies them.
- No employer, job title, school, or any detail that would single them out.
- Never imply the person endorsed anything.
- If the story cannot be told without identifying them, tell it as a composite
  and say nothing that pins it to one person.
""" if is_client_call else "")

    def do_call():
        msg = call_model(client,
            model=HEAVY_MODEL, max_tokens=6000,
            system=f"""You are writing a LinkedIn post for {user_name}, drawn from a real conversation.

{avatar}

{voice}

{cal}

{algo}
{privacy}
Find the one moment in this transcript that lands hardest — a line the person
said, a turn they took, a thing they admitted. Build the post around that. Do
not summarise the call and do not list what was discussed.

Target 1,100-1,500 characters. Open with a hook under 140 characters. End with
a question that invites a story. No URLs, no links, no hashtags beyond three.

Return ONLY valid JSON: {{"postText": "the post"}}""",
            messages=[{'role': 'user',
                       'content': f'Conversation: {data.get("title") or "untitled"}\n\n{transcript}'}]
        )
        return parse_json_response(extract_text(msg))

    return run_as_job(do_call)


@app.route('/api/storage')
@require_admin_key
def storage_status():
    """Say plainly whether anything saved here will survive the next deploy."""
    return jsonify({
        'persistent': STORE_IS_PERSISTENT,
        'path': DATA_DIR,
        'dismissed': len(store_read('dismissed', {})),
        'styles': len(store_read('styles', {})),
    })


@app.route('/api/styles', methods=['GET', 'POST'])
@require_admin_key
def styles():
    """Named writing styles that layer on top of the voice OS.

    A style is a short note, not a whole voice — "blunter, shorter sentences,
    no story" or "lean on the research, cite the survey". get_contexts() appends
    it to the voice as a final constraint.
    """
    if request.method == 'GET':
        saved = store_read('styles', {})
        return jsonify({'styles': sorted(saved.values(), key=lambda s: s.get('name', '').lower()),
                        'persistent': STORE_IS_PERSISTENT})

    data = request.json or {}
    name = (data.get('name') or '').strip()
    note = (data.get('note') or '').strip()
    if not name or not note:
        return jsonify({'error': 'A style needs both a name and a note.'}), 400

    saved = store_read('styles', {})
    style_id = str(data.get('id') or uuid.uuid4().hex[:12])
    saved[style_id] = {
        'id': style_id,
        'name': name[:60],
        'note': note[:4000],
        'updated': time.strftime('%Y-%m-%d %H:%M:%S'),
    }
    store_write('styles', saved)
    return jsonify({'ok': True, 'style': saved[style_id]})


@app.route('/api/styles/<style_id>', methods=['DELETE'])
@require_admin_key
def delete_style(style_id):
    saved = store_read('styles', {})
    if saved.pop(str(style_id), None) is None:
        return jsonify({'error': 'No such style.'}), 404
    store_write('styles', saved)
    return jsonify({'ok': True})


@app.route('/api/stats', methods=['GET', 'POST'])
@require_admin_key
def stats():
    """Analytics stats. These used to live in a module global and vanish on
    every restart; they go through the store now so they last as long as
    DATA_DIR does."""
    if request.method == 'GET':
        return jsonify(store_read('stats', []))

    entries = store_read('stats', [])
    entries.append(request.json)
    store_write('stats', entries)
    return jsonify({'saved': True, 'total': len(entries),
                    'persistent': STORE_IS_PERSISTENT})


if __name__ == '__main__':
    print('\n  N.40 Content Engine')
    print('  Open: http://localhost:5555')
    print('  Ctrl+C to stop\n')
    app.run(host='127.0.0.1', port=5555, debug=True)
