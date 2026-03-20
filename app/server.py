#!/usr/bin/env python3
"""N40 LinkedIn Content Engine — Cloud-ready Flask backend with Claude API."""

import os
import re
import json
import datetime
from flask import Flask, request, jsonify, send_from_directory

import anthropic

app = Flask(__name__, static_folder='.', static_url_path='')

# ─── AVATAR CONTEXT (baked in) ──────────────────────────────────
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
## Lon Stroschein's Voice — "Direct, intimate, and unavoidable"

CORE IDENTITY:
- Researcher first. Truth-teller. Change agent who has done it and led thousands.
- Reader must think: "How did he get inside my head?"
- Lon has had THOUSANDS of conversations (rambles) with this avatar over 3+ years
- He takes full credit: "In thousands of conversations over three years, I've found..."
- NOT "clients say" — instead "the research shows" / "what I've found" / "from thousands of conversations"
- NEVER include a URL or link in any post
- Give EVERYTHING away. No gates. The avatar should be able to use every word TODAY.

HOW LON WRITES (you MUST replicate this):
- Write like you're talking to ONE person, not an audience
- Second-person ("you") + short lines
- Verbal finger-point without being cruel: "Dude." / "Look." / "Yes…I'm talking to you."
- It reads like private coaching someone found in public
- One idea per line. Repetition for momentum.
- "This isn't X. It's Y." — tight contrast is his signature move
- He earns authority by NAMING WHAT PEOPLE HIDE — what people can't say at work, what success costs, what high performers privately fear
- He never leads with credentials. He leads with recognition.

HOOK PATTERN (first 1-2 lines must create a psychological snap):
- Hard stat + contradiction
- A definitive claim ("The first sign…")
- A harsh but clean reframe ("It's not burnout. It's readiness.")
- The hook should feel like a DIAGNOSIS, not a headline

STRUCTURE PATTERN (compressed paragraphs that feel like steps):
- Establish the stat or claim
- Dismantle the obvious interpretation
- Name the real mechanism ("surrounded…by expectation")
- List what isn't safe (spouse, colleagues, friends, therapist)
- Land the invitation
- Even non-list posts should feel like STEPS — scannable, with "I'll come back to this" shape

THE SAVE-POST RECIPE (follow this structure):
1. Open with a fact or definitive line
2. Name the hidden mechanism (the thing people won't say)
3. Contrast — "This isn't X. It's Y."
4. Give language people can STEAL (1-3 quotable lines they'll screenshot)
5. Give a micro-framework (bullets, steps, "here's what to do") — something USABLE TODAY
6. Close with an invitation (not a pitchy CTA — a doorway, a question that requires a story)

WHAT MAKES LON DIFFERENT FROM GENERIC LINKEDIN:
- No motivational fluff. No "here's why you should believe in yourself."
- No coaching-speak. No "lean into" or "show up as your authentic self."
- Instead: specific, clinical, intimate. Like a doctor who also happens to be your friend.
- The post should feel like a diagnosis + a prescription, not a pep talk.

SAVE TRIGGERS (design every post to be saved):
- Include a repeatable framework (even 3 steps)
- Include one "copy/paste line" — a question to ask themselves, a sentence to write down
- Occasionally use "Save this." — but only when it's earned
- Quotable lines > inspirational fluff
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


def get_client():
    """Get Anthropic client from environment variable."""
    api_key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        raise ValueError('No ANTHROPIC_API_KEY found. Set it as an environment variable.')
    return anthropic.Anthropic(api_key=api_key)


# ─── ROUTES ─────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')


@app.route('/api/generate-questions', methods=['POST'])
def generate_questions():
    """Given a topic, generate 5 interview questions."""
    data = request.json
    topic = data.get('topic', '')
    if not topic:
        return jsonify({'error': 'No topic provided'}), 400

    client = get_client()

    msg = client.messages.create(
        model='claude-sonnet-4-20250514',
        max_tokens=2000,
        system=f"""You are the content interview engine for Lon Stroschein and The Normal 40.

{AVATAR_CONTEXT}

{VOICE_CONTEXT}

{ALGORITHM_CONTEXT}

YOUR JOB: Analyze what Lon gave you — it could be a 3-word topic OR a 400-character thought with a story, framework, or raw insight already embedded. Then generate ONLY the questions needed to fill the gaps.

FIRST, assess what's already in the seed:
- Story / specific moment? (a real conversation, a client situation, a personal experience)
- Emotional truth? (the 3 AM thought, what the avatar won't say out loud)
- Teachable framework? (actionable steps someone could use TODAY)
- Contrarian angle? (what mainstream advice gets wrong)
- Hook material? (a punchy line that would stop the scroll)

THEN, generate 2-5 questions that target ONLY what's missing. Do NOT ask for what Lon already gave you. If he handed you a rich thought with a story and an insight, you might only need 2 questions. If he gave you a bare topic, you might need 5.

Every question should be laser-focused on what will make this post:
1. TEACH something usable TODAY (the avatar walks away with a framework, not just inspiration)
2. STOP THE SCROLL (hook that lands in under 140 characters)
3. GET SAVED (the infographic must be so useful they screenshot it)
4. DRIVE REAL COMMENTS (end with a question that requires a story, not a yes/no)

For each question, provide:
- "label": short label that describes what you're asking for (e.g., "The Moment", "The Framework", "The Line They Won't Say")
- "question": the actual question — specific, not generic. Reference details from what Lon already shared.
- "hint": coaching text that helps Lon give a GREAT answer. Include specific suggestions, story angles, or example formats. The hint should make it easy for Lon to know what a good answer looks like.

CRITICAL: Be specific. Do NOT ask generic questions like "tell me about a time..." — reference the seed material and build on it. If Lon mentioned a surgeon, ask about that surgeon. If he mentioned a feeling, dig into that feeling.

Return ONLY valid JSON array of 2-5 objects. No markdown, no explanation.""",
        messages=[{'role': 'user', 'content': f'Lon\'s seed:\n\n{topic}'}]
    )

    try:
        text = msg.content[0].text.strip()
        if text.startswith('```'):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        questions = json.loads(text)
        return jsonify({'questions': questions})
    except (json.JSONDecodeError, IndexError) as e:
        return jsonify({'error': f'Failed to parse questions: {str(e)}', 'raw': msg.content[0].text}), 500


@app.route('/api/generate-content', methods=['POST'])
def generate_content():
    """Given topic + answers, generate optimized post text + infographic data."""
    data = request.json
    topic = data.get('topic', '')
    answers = data.get('answers', [])
    template = data.get('template', 'list')
    color_mode = data.get('colorMode', 'dark')

    if not topic:
        return jsonify({'error': 'No topic provided'}), 400

    client = get_client()

    answer_labels = data.get('labels', [])
    answers_text = ''
    for i, ans in enumerate(answers):
        if ans and ans != '[skipped]':
            label = answer_labels[i] if i < len(answer_labels) else f'Q{i+1}'
            answers_text += f'\n## {label}\n{ans}\n'

    msg = client.messages.create(
        model='claude-sonnet-4-20250514',
        max_tokens=4000,
        system=f"""You are the content creation engine for Lon Stroschein and The Normal 40.

{AVATAR_CONTEXT}

{VOICE_CONTEXT}

{ALGORITHM_CONTEXT}

Your job: Take Lon's raw interview answers and transform them into TWO things:

1. LINKEDIN POST TEXT
- Hook: MUST be under 140 characters. Land before "see more."
- Use Lon's actual words and stories but sharpen them for maximum impact
- Write in his voice: researcher, truth-teller, not a coach selling something
- 1,100-1,500 characters total
- End with a specific answerable question that drives 15+ word comments (NOT a yes/no question — ask something that requires a story or confession)
- NO URLs. NO links. NO "DM me." NO "link in comments." Give everything away freely.
- Exactly 3 hashtags at the end
- Optimize for SAVES — the post should teach them something they can use TODAY
- The reader should think: "This person just gave me for free what others charge for"

2. INFOGRAPHIC CONTENT (for a "{template}" template)
Structure the content to fit the template format:
- "cheatsheet": title, subtitle, 4-6 section headings with 2-3 bullets each
- "funnel": title, 3-5 stages with names and descriptions
- "system": title, 3-6 categorized rows with labels and content
- "acronym": title, 3-6 letters, each with a word and short description
- "comparison": title, left column header + 5-7 items, right column header + 5-7 items
- "list": title, 5-9 numbered items (short, punchy statements)

The infographic MUST:
- Provide a working framework they can use TODAY
- Be readable at 50% zoom (how LinkedIn displays it)
- Make them screenshot it or send it to a friend
- Include "Lon Stroschein | The Normal 40" as attribution (NO URL — just the name)

Return ONLY valid JSON with this structure:
{{
  "postText": "the full LinkedIn post text",
  "infographic": {{
    "title": "main title",
    "subtitle": "optional subtitle",
    "sections": [...template-specific content...],
    "leftHeader": "for comparison only",
    "rightHeader": "for comparison only",
    "leftItems": ["for comparison only"],
    "rightItems": ["for comparison only"],
    "items": ["for list/funnel/acronym"],
    "descriptions": ["optional descriptions for each item"],
    "categories": ["for system template - row labels"],
    "steps": ["for system template - row content"]
  }}
}}

No markdown fences. No explanation. Just the JSON.""",
        messages=[{
            'role': 'user',
            'content': f'Topic: {topic}\nTemplate: {template}\nColor mode: {color_mode}\n\nLon\'s raw answers:\n{answers_text}'
        }]
    )

    try:
        text = msg.content[0].text.strip()
        if text.startswith('```'):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        content = json.loads(text)
        return jsonify(content)
    except (json.JSONDecodeError, IndexError) as e:
        return jsonify({'error': f'Failed to parse content: {str(e)}', 'raw': msg.content[0].text}), 500


@app.route('/api/refine', methods=['POST'])
def refine():
    """Iterate on existing content with feedback. Supports post text, infographic, or both."""
    data = request.json
    current_post = data.get('postText', '')
    feedback = data.get('feedback', '')
    topic = data.get('topic', '')
    target = data.get('target', 'post')
    template = data.get('template', 'list')
    infographic_data = data.get('infographicData', {})

    client = get_client()

    if target == 'infographic' or target == 'both':
        infographic_json = json.dumps(infographic_data, indent=2) if infographic_data else '{}'

        system_prompt = f"""You are the content refinement engine for Lon Stroschein / The Normal 40.

{VOICE_CONTEXT}
{ALGORITHM_CONTEXT}

Lon has given you feedback on his current content. Apply his feedback precisely while maintaining:
- His voice (researcher, truth-teller)
- Algorithm optimization (saves, hook under 140 chars, 1100-1500 chars, 3 hashtags)
- The avatar connection (they must see themselves in this)
- The infographic must provide a WORKING FRAMEWORK they can use TODAY
- The infographic must be saveable — something they'd screenshot or send to a friend
- NEVER include URLs, links, or website references — not in the post, not on the infographic
- Give ALL the information away. Teach the avatar. No gates, no funnels, no "DM me."

The current template is "{template}". When refining infographic content, return data that matches the template structure:
- "cheatsheet": sections array with heading + items array each
- "funnel": items array (stage names)
- "system": categories array (labels) + steps array (content)
- "acronym": items array (words) + descriptions array
- "comparison": leftHeader, rightHeader, leftItems array, rightItems array
- "list": items array (numbered statements)

Return ONLY valid JSON with this structure:
{{
  "postText": "the refined post text (or unchanged if target is infographic only)",
  "infographic": {{
    "title": "...",
    "subtitle": "...",
    ...template-specific fields...
  }}
}}"""

        user_content = f'Topic: {topic}\nTemplate: {template}\nRefine target: {target}\n\n'
        if current_post:
            user_content += f'Current post text:\n{current_post}\n\n'
        user_content += f'Current infographic data:\n{infographic_json}\n\n'
        user_content += f'Lon\'s feedback:\n{feedback}'

        msg = client.messages.create(
            model='claude-sonnet-4-20250514',
            max_tokens=4000,
            system=system_prompt,
            messages=[{'role': 'user', 'content': user_content}]
        )
    else:
        msg = client.messages.create(
            model='claude-sonnet-4-20250514',
            max_tokens=2000,
            system=f"""You are the content refinement engine for Lon Stroschein / The Normal 40.

{VOICE_CONTEXT}
{ALGORITHM_CONTEXT}

Lon has given you feedback on a draft. Apply it while maintaining:
- His voice (researcher, truth-teller)
- Algorithm optimization (saves, hook under 140 chars, 1100-1500 chars, 3 hashtags)
- The avatar connection (they must see themselves in this)

Return ONLY valid JSON: {{"postText": "refined post text"}}""",
            messages=[{
                'role': 'user',
                'content': f'Topic: {topic}\n\nCurrent draft:\n{current_post}\n\nFeedback:\n{feedback}'
            }]
        )

    try:
        text = msg.content[0].text.strip()
        if text.startswith('```'):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        result = json.loads(text)
        return jsonify(result)
    except (json.JSONDecodeError, IndexError) as e:
        return jsonify({'error': str(e), 'raw': msg.content[0].text}), 500


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

    client = get_client()

    if fmt == 'image':
        visual_instructions = """
For the "visual" field, return:
{
  "lines": ["line 1 of text", "line 2 of text", "line 3 of text"]
}
These lines will be overlaid on a branded B&W photo.
Rules for image text:
- 3-5 short lines MAXIMUM
- Each line should be under 8 words
- The last line should land the punch — the insight that makes them stop
- Think billboard: if you can't read it in 3 seconds, it's too long
- NO URLs, NO hashtags, NO attribution on the image (branding is already on the photo)
- The lines should be a distilled version of the post's core insight — not the hook
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
- Give away the FULL framework. Every slide should teach something usable TODAY.
"""

    msg = client.messages.create(
        model='claude-sonnet-4-20250514',
        max_tokens=4000,
        system=f"""You are the content recycling engine for Lon Stroschein and The Normal 40.

{AVATAR_CONTEXT}

{VOICE_CONTEXT}

{ALGORITHM_CONTEXT}

Your job: Take an old LinkedIn post and COMPLETELY REWRITE it. Not a light edit — a full transformation.

REWRITE RULES:
- New hook (first 140 characters) — must be completely different from the original
- New structure — reorganize the ideas, find a different angle
- Fresh language — no recycled phrases from the original
- Same core insight but delivered in a way that feels new
- Target length: {length_range}
- End with a specific answerable question (NOT yes/no — ask something that requires a story)
- Exactly 3 hashtags at the end
- NO URLs, NO links, NO "DM me", NO "link in comments"
- Give away ALL the value. Teach the avatar. Optimize for SAVES.
- The reader should think: "This person just gave me for free what others charge for"

{visual_instructions}

Return ONLY valid JSON:
{{
  "postText": "the full rewritten LinkedIn post text",
  "visual": {{ ... visual data as described above ... }}
}}

No markdown fences. No explanation. Just the JSON.""",
        messages=[{'role': 'user', 'content': f'Original post to recycle:\n\n{original}'}]
    )

    try:
        text = msg.content[0].text.strip()
        if text.startswith('```'):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        result = json.loads(text)
        return jsonify(result)
    except (json.JSONDecodeError, IndexError) as e:
        return jsonify({'error': f'Failed to parse: {str(e)}', 'raw': msg.content[0].text}), 500


@app.route('/api/recycle-refine', methods=['POST'])
def recycle_refine():
    """Refine recycled content with feedback."""
    data = request.json
    target = data.get('target', 'post')
    fmt = data.get('format', 'image')
    post_text = data.get('postText', '')
    visual_data = data.get('visualData', {})
    feedback = data.get('feedback', '')

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

        msg = client.messages.create(
            model='claude-sonnet-4-20250514',
            max_tokens=4000,
            system=f"""{VOICE_CONTEXT}
{ALGORITHM_CONTEXT}

Refine the visual content based on feedback. {visual_desc}
NEVER include URLs, links, or website references.
Return ONLY valid JSON.""",
            messages=[{'role': 'user', 'content': f'Feedback: {feedback}'}]
        )
    else:
        msg = client.messages.create(
            model='claude-sonnet-4-20250514',
            max_tokens=2000,
            system=f"""{VOICE_CONTEXT}
{ALGORITHM_CONTEXT}

Refine the post text based on feedback. Maintain voice and algorithm optimization.
NEVER include URLs, links, or website references. Give everything away freely.
Return ONLY valid JSON: {{"postText": "refined text"}}""",
            messages=[{'role': 'user', 'content': f'Current post:\n{post_text}\n\nFeedback: {feedback}'}]
        )

    try:
        text = msg.content[0].text.strip()
        if text.startswith('```'):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        return jsonify(json.loads(text))
    except (json.JSONDecodeError, IndexError) as e:
        return jsonify({'error': str(e), 'raw': msg.content[0].text}), 500


@app.route('/api/stats', methods=['GET', 'POST'])
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
    print('\n  N40 LinkedIn Content Engine')
    print('  Open: http://localhost:5555')
    print('  Ctrl+C to stop\n')
    app.run(host='127.0.0.1', port=5555, debug=True)
