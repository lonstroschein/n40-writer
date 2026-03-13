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
## Lon Stroschein's Voice
- Researcher first. Truth-teller. Change agent who has done it and led thousands.
- Reader must think: "How did he get inside my head?"
- Lon has had THOUSANDS of conversations (rambles) with this avatar over 3+ years
- He takes full credit: "In thousands of conversations over three years, I've found..."
- NOT "clients say" — instead "the research shows" / "what I've found" / "from thousands of conversations"
- CTAs invite a conversation, never pitch a product
- Available, accessible, doing incredible things — make others want to join the movement
- NEVER include a URL or link in any post — LinkedIn suppresses posts with outbound links
- Give EVERYTHING away. No gates. The avatar should be able to use every word TODAY.
- The reach comes from being so useful they save it, not from driving clicks
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

Your job: Generate 5 interview questions for Lon based on his topic. These questions will draw out the raw material needed to create a LinkedIn post + saveable infographic.

QUESTION STRUCTURE (exactly 5):
1. YOUR STORY — Ask for a specific moment (his or a client's) where this topic hit hardest. Suggest 2-3 story angles from his work that might fit.
2. INSIDE THEIR HEAD — Ask what the avatar is thinking about this topic that they'd never say at work. The 3 AM thought.
3. THE FRAMEWORK — Ask for 3-5 actionable steps someone could use TODAY. Not theory — what does he actually walk people through?
4. THE CONTRARIAN TAKE — Ask what mainstream advice gets wrong about this. What would make someone say "I never thought of it that way"? Reference 1-2 viral content angles on this topic that are working right now.
5. THE HOOK — Ask for one sentence under 140 characters that would stop the avatar mid-scroll. Give 2-3 example formats (confession, stat, question).

For each question, provide:
- "label": short label (e.g., "Your Story")
- "question": the actual question
- "hint": coaching text that helps Lon give a great answer (story suggestions, examples, what to aim for)

Return ONLY valid JSON array of 5 objects. No markdown, no explanation.""",
        messages=[{'role': 'user', 'content': f'Topic: {topic}'}]
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

    answers_text = ''
    labels = ['Your Story', 'Inside Their Head', 'The Framework', 'The Contrarian Take', 'The Hook']
    for i, ans in enumerate(answers):
        if ans and ans != '[skipped]':
            label = labels[i] if i < len(labels) else f'Q{i+1}'
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
