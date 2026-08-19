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
from flask import Flask, request, jsonify, send_from_directory, Response

import anthropic

app = Flask(__name__, static_folder=None)

ADMIN_KEY = os.environ.get('ADMIN_KEY', '')
PRO_KEYS = [k.strip() for k in os.environ.get('PRO_KEYS', '').split(',') if k.strip()]
FREE_CHAR_LIMIT = 5000
PRO_CHAR_LIMIT = 50000


def extract_text(msg):
    """Pull the text content from a Claude response, skipping thinking blocks."""
    for block in msg.content:
        if hasattr(block, 'text'):
            return block.text.strip()
    return ''


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
    return avatar, voice, cal, algo, name


def get_client():
    """Get Anthropic client from environment variable."""
    api_key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        raise ValueError('No ANTHROPIC_API_KEY found. Set it as an environment variable.')
    return anthropic.Anthropic(api_key=api_key, timeout=90.0)


@app.errorhandler(Exception)
def handle_error(e):
    return jsonify({'error': f'Something went wrong — try again. ({type(e).__name__})'}), 503


def stream_claude_call(fn):
    """Run fn() in a thread, sending keepalive spaces every 2s.
    fn must return a JSON-serializable dict. Prevents Render 30s idle timeout."""
    result = {'data': None, 'error': None}
    def wrapper():
        try:
            result['data'] = fn()
        except Exception as e:
            result['error'] = f'{type(e).__name__}: {str(e)}'
    def generate():
        t = threading.Thread(target=wrapper)
        t.start()
        while t.is_alive():
            yield ' '
            t.join(timeout=2)
        if result['error']:
            yield json.dumps({'error': result['error']})
        else:
            yield json.dumps(result['data'])
    return Response(generate(), mimetype='application/json')


# ─── ONBOARDING (voice interview for clients) ────────

@app.route('/api/onboard/question', methods=['POST'])
def onboard_question():
    """Dynamic voice interview — builds a client's writing profile."""
    data = request.json
    history = data.get('history', [])
    question_number = len(history) + 1

    client = get_client()

    history_text = ''
    for entry in history:
        history_text += f"\n### {entry.get('label', 'Q')}\n"
        history_text += f"Q: {entry.get('question', '')}\n"
        history_text += f"A: {entry.get('answer', '[skipped]')}\n"

    system_prompt = f"""You are helping someone build a content engine. You're interviewing them in three sections, in order:

SECTION 1 — WHO ARE YOU TALKING TO? (questions 1-3)
Who is their reader? What do they struggle with? What are they quietly hoping someone will say to them?

SECTION 2 — WHAT DO YOU KNOW THAT CAN HELP THEM? (questions 3-5)
Not just at work, but in their living (career, craft, expertise) AND their life (family, relationships, natural talents, how they see the world). The best content comes from people who know something real and share it generously.

The person is likely answering via voice-to-text on their phone, possibly on a walk. Keep questions SHORT — one sentence, conversational, easy to answer out loud.

This is question #{question_number}. Adapt based on what they've told you so far.

RULES:
- Questions must be 1 sentence. No preamble, no "Great answer!" filler.
- Hints must be 1 sentence max. Practical, not philosophical.
- Max 5 questions total. Signal ready after question 4-5.
- The person should be able to answer each question in under 30 seconds.
- Move through the two sections in order. Don't linger on one section too long.
- Do NOT ask about their writing style or voice — that comes later in a separate step.

If asking, return JSON: {{"ready": false, "label": "short label", "question": "the question", "hint": "one-line hint"}}
If you have enough, return: {{"ready": true}}"""

    def do_call():
        msg = client.messages.create(
            model='claude-sonnet-4-5-20250514', max_tokens=1500,
            system=system_prompt,
            messages=[{'role': 'user', 'content': f'Interview so far:\n{history_text if history_text else "(First question)"}'}]
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
            return {'error': 'Unexpected format. Try again.'}

    return stream_claude_call(do_call)


@app.route('/api/onboard/complete', methods=['POST'])
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
        msg = client.messages.create(
            model='claude-sonnet-4-5-20250514', max_tokens=6000,
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

    return stream_claude_call(do_call)


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


@app.route('/api/next-question', methods=['POST'])
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
        msg = client.messages.create(
            model='claude-sonnet-4-5-20250514',
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

    return stream_claude_call(do_call)


@app.route('/api/generate-content', methods=['POST'])
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
        msg = client.messages.create(
            model='claude-sonnet-4-5-20250514', max_tokens=6000,
            system=system_prompt, messages=[{'role': 'user', 'content': user_msg}]
        )
        text = extract_text(msg)
        if text.startswith('```'):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        return json.loads(text)

    return stream_claude_call(do_call)


@app.route('/api/refine', methods=['POST'])
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
        msg = client.messages.create(
            model='claude-sonnet-4-5-20250514', max_tokens=6000,
            system=system_prompt, messages=[{'role': 'user', 'content': user_content}]
        )
        text = extract_text(msg)
        if text.startswith('```'):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        return json.loads(text)

    return stream_claude_call(do_call)


@app.route('/api/recycle', methods=['POST'])
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
        msg = client.messages.create(
        model='claude-sonnet-4-5-20250514',
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
        if text.startswith('```'):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        return json.loads(text)

    return stream_claude_call(do_call)


@app.route('/api/recycle-refine', methods=['POST'])
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
        msg = client.messages.create(
            model='claude-sonnet-4-5-20250514', max_tokens=max_tok,
            system=sys_prompt, messages=[{'role': 'user', 'content': usr_msg}]
        )
        text = extract_text(msg)
        if text.startswith('```'):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        return json.loads(text)

    return stream_claude_call(do_call)


@app.route('/api/generate-note', methods=['POST'])
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
        msg = client.messages.create(
            model='claude-sonnet-4-5-20250514', max_tokens=2000,
            system=sys_prompt, messages=[{'role': 'user', 'content': usr_msg}]
        )
        return {'note': extract_text(msg)}

    return stream_claude_call(do_call)


@app.route('/api/refine-note', methods=['POST'])
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
        msg = client.messages.create(
            model='claude-sonnet-4-5-20250514', max_tokens=2000,
            system=sys_prompt, messages=[{'role': 'user', 'content': usr_msg}]
        )
        return {'note': extract_text(msg)}

    return stream_claude_call(do_call)


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
        msg = client.messages.create(
        model='claude-sonnet-4-5-20250514',
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
        if text.startswith('```'):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        return json.loads(text)

    return stream_claude_call(do_call)


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
    per_page = int(request.args.get('per_page', 25))

    if search:
        posts = [p for p in posts if search in p['text'].lower()]
    if min_comments:
        posts = [p for p in posts if p['comments'] >= min_comments]
    if min_chars:
        posts = [p for p in posts if p['char_count'] >= min_chars]

    total = len(posts)
    posts = posts[page * per_page:(page + 1) * per_page]

    return jsonify({'posts': posts, 'total': total, 'page': page, 'per_page': per_page})


@app.route('/api/vault-recycle', methods=['POST'])
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
        msg = client.messages.create(
        model='claude-sonnet-4-5-20250514',
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
        if text.startswith('```'):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        return json.loads(text)

    return stream_claude_call(do_call)


@app.route('/api/stats', methods=['GET', 'POST'])
@require_admin_key
def stats():
    """In-memory analytics stats (no filesystem needed)."""
    if not hasattr(app, '_stats'):
        app._stats = []

    if request.method == 'GET':
        return jsonify(app._stats)

    if request.method == 'POST':
        app._stats.append(request.json)
        return jsonify({'saved': True})


if __name__ == '__main__':
    print('\n  N.40 Content Engine')
    print('  Open: http://localhost:5555')
    print('  Ctrl+C to stop\n')
    app.run(host='127.0.0.1', port=5555, debug=True)
