"""
AI assistants for the Hostel Management app.

  * User bot   (POST /api/assistant/user)        - answers "how do I ...?" questions from a fixed
                                                   knowledge base. It NEVER touches the database and
                                                   never sees admin data, so it cannot leak any.
  * Admin bot  (POST /admin/api/assistant)       - English / Telugu assistant for the admin dashboard.
                                                   Read-only questions are answered from live data.
                                                   The one data-changing action (shifting a resident to
                                                   another room) is validated with the app's own
                                                   capacity rules and always needs an explicit confirmation.

Everything works with no extra configuration. If ANTHROPIC_API_KEY is set in the environment, an LLM is
used ONLY as a fallback to understand wording the built-in rules did not recognise:
  - user bot : it may only pick one entry of the fixed knowledge base (it cannot write answers);
  - admin bot: it may only turn the sentence into the same structured command the rules produce.
Every action is still validated and confirmed by the server code below.
"""
import os
import re
import json
import time
import secrets
import difflib
from html import escape, unescape

from flask import jsonify, request, session

OUT_OF_SCOPE = "I can help with questions related to this application. For other questions, please contact Ravi."
OUT_OF_SCOPE_TE = "ఈ అప్లికేషన్‌కు సంబంధించిన ప్రశ్నలకు నేను సహాయం చేయగలను. ఇతర ప్రశ్నల కోసం దయచేసి Ravi ని సంప్రదించండి."
RESTRICTED_USER = "Sorry, that information is restricted to authorized administrators."
RESTRICTED_USER_TE = "క్షమించండి, ఆ సమాచారం అధీకృత అడ్మిన్‌లకు మాత్రమే పరిమితం."

PENDING_TTL = 120  # seconds an unconfirmed admin action stays valid


# ==========================================================================
# TEXT HELPERS (Telugu + English)
# ==========================================================================

_TELUGU = re.compile('[\u0C00-\u0C7F]')
_TOKEN = re.compile('[A-Za-z0-9\u0C00-\u0C7F]+')
_TE_DIGITS = str.maketrans('౦౧౨౩౪౫౬౭౮౯', '0123456789')


def has_telugu(s):
    return bool(_TELUGU.search(s or ''))


_C = {'క': 'k', 'ఖ': 'kh', 'గ': 'g', 'ఘ': 'gh', 'ఙ': 'ng', 'చ': 'ch', 'ఛ': 'chh', 'జ': 'j', 'ఝ': 'jh', 'ఞ': 'n',
      'ట': 't', 'ఠ': 'th', 'డ': 'd', 'ఢ': 'dh', 'ణ': 'n', 'త': 't', 'థ': 'th', 'ద': 'd', 'ధ': 'dh', 'న': 'n',
      'ప': 'p', 'ఫ': 'ph', 'బ': 'b', 'భ': 'bh', 'మ': 'm', 'య': 'y', 'ర': 'r', 'ల': 'l', 'వ': 'v', 'శ': 'sh',
      'ష': 'sh', 'స': 's', 'హ': 'h', 'ళ': 'l', 'ఱ': 'r'}
_V = {'అ': 'a', 'ఆ': 'a', 'ఇ': 'i', 'ఈ': 'i', 'ఉ': 'u', 'ఊ': 'u', 'ఋ': 'ru', 'ఎ': 'e', 'ఏ': 'e', 'ఐ': 'ai',
      'ఒ': 'o', 'ఓ': 'o', 'ఔ': 'au'}
_S = {'ా': 'a', 'ి': 'i', 'ీ': 'i', 'ు': 'u', 'ూ': 'u', 'ృ': 'ru', 'ె': 'e', 'ే': 'e', 'ై': 'ai', 'ొ': 'o',
      'ో': 'o', 'ౌ': 'au'}
_VIRAMA = '్'


def te_to_latin(s):
    """Rough Telugu -> Latin transliteration, used only to match spoken Telugu names to stored names."""
    out = []
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch in _C:
            out.append(_C[ch])
            nxt = s[i + 1] if i + 1 < n else ''
            if nxt in _S:
                out.append(_S[nxt])
                i += 1
            elif nxt == _VIRAMA:
                i += 1
            else:
                out.append('a')
        elif ch in _V:
            out.append(_V[ch])
        elif ch in ('ం', 'ఁ'):
            out.append('n')
        elif ch == 'ః':
            out.append('h')
        elif ch in _S or ch == _VIRAMA:
            pass
        else:
            out.append(ch)
        i += 1
    return ''.join(out)


def phon_key(word):
    """Rough phonetic key so 'Manohar', 'manohar' and Telugu 'మనోహర్' compare as equal."""
    w = te_to_latin(word).lower()
    for a, b in (('w', 'v'), ('ee', 'i'), ('oo', 'u'), ('aa', 'a'), ('sh', 's'), ('th', 't'), ('dh', 'd'),
                 ('kh', 'k'), ('gh', 'g'), ('bh', 'b'), ('ph', 'p'), ('ch', 'c'), ('z', 'j'), ('y', 'i')):
        w = w.replace(a, b)
    w = re.sub('[^a-z]', '', w).replace('h', '')
    return re.sub(r'(.)\1+', r'\1', w)


_NUMW = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
         'ఒకటి': 1, 'ఒకటో': 1, 'రెండు': 2, 'రెండో': 2, 'మూడు': 3, 'మూడో': 3, 'నాలుగు': 4, 'నాలుగో': 4,
         'ఐదు': 5, 'అయిదు': 5, 'ఐదో': 5, 'ఆరు': 6, 'ఆరో': 6, 'ఏడు': 7, 'ఏడో': 7, 'ఎనిమిది': 8, 'ఎనిమిదో': 8,
         'తొమ్మిది': 9, 'తొమ్మిదో': 9, 'పది': 10, 'పదో': 10}


def tokenize(text):
    """Lower-cased tokens; Telugu digits and number words become ASCII digits."""
    toks = _TOKEN.findall((text or '').translate(_TE_DIGITS).lower())
    out = []
    for t in toks:
        m = re.fullmatch(r'(\d+)(?:వ|వది|th|st|nd|rd)?', t)
        if m:
            out.append(m.group(1))
        else:
            out.append(str(_NUMW.get(t, t)))
    return out


def tr(lang, en, te):
    return te if lang == 'te' else en


def rupee(n):
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        n = 0
    return '₹' + (f'{n:,.0f}' if n == int(n) else f'{n:,.2f}')


# ==========================================================================
# OPTIONAL LLM FALLBACK (off unless ANTHROPIC_API_KEY is set)
# ==========================================================================

def _llm_json(system, user):
    key = (os.environ.get('ANTHROPIC_API_KEY') or '').strip()
    if not key:
        return None
    try:
        import requests
        r = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={'x-api-key': key, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'},
            json={'model': os.environ.get('ANTHROPIC_MODEL', 'claude-haiku-4-5-20251001'), 'max_tokens': 300,
                  'system': system, 'messages': [{'role': 'user', 'content': user}]},
            timeout=12)
        if r.status_code != 200:
            return None
        text = ''.join(b.get('text', '') for b in r.json().get('content', []) if b.get('type') == 'text')
        m = re.search(r'\{.*\}', text, flags=re.S)
        return json.loads(m.group(0)) if m else None
    except Exception:
        return None


# ==========================================================================
# USER BOT  (fixed knowledge base, no database access)
# ==========================================================================

# Each entry: keywords (matched against the lower-cased message; Telugu keywords included) and a
# bilingual answer. Only features that really exist on the user side are described.
USER_KB = [
    {'id': 'greeting', 'only_short': True,
     'kw': ['hi', 'hello', 'hey', 'good morning', 'good evening', 'namaste', 'నమస్తే', 'హలో', 'హాయ్'],
     'title': 'Greeting',
     'en': {'text': "Hello! I'm the hostel assistant. I can explain how to check your rent, make a payment, record a payment and more."},
     'te': {'text': 'నమస్తే! నేను హాస్టల్ అసిస్టెంట్‌ని. మీ అద్దె చూడటం, చెల్లింపు చేయడం, చెల్లింపు నమోదు చేయడం వంటివి వివరిస్తాను.'}},
    {'id': 'what_can_you_do', 'kw': ['help', 'what can you do', 'options', 'features', 'steps', 'step by step', 'how to use', 'how do i use', 'operation', 'how does this work', 'what does this app', 'what is this app', 'సహాయం', 'ఏమి చేయగలవు'],
     'title': 'What can the assistant do',
     'en': {'text': 'You can ask me things like:', 'steps': ['How do I check my rent?', 'How do I make a payment?', 'How do I record a payment?', 'How do I check my room details?', 'How do I see if my payment is confirmed?']},
     'te': {'text': 'మీరు ఇలా అడగవచ్చు:', 'steps': ['నా అద్దె ఎలా చూడాలి?', 'చెల్లింపు ఎలా చేయాలి?', 'చెల్లింపును ఎలా నమోదు చేయాలి?', 'నా గది వివరాలు ఎలా చూడాలి?', 'నా చెల్లింపు నిర్ధారణ అయిందో ఎలా తెలుసుకోవాలి?']}},
    {'id': 'check_rent', 'kw': ['check my rent', 'my rent', 'rent due', 'amount due', 'how much do i owe', 'how much rent', 'balance', 'pending amount', 'dues', 'due amount', 'rent', 'అద్దె', 'బకాయి', 'బాకీ'],
     'title': 'Check my rent',
     'en': {'text': 'Your rent details are on the Payments dashboard:', 'steps': [
         'Open <b>Payments</b> from the top menu.',
         'Enter the mobile number your room was registered with and tap <b>Continue</b>.',
         'The dashboard shows your <b>Amount Due</b>, your total rent and the amount paid.']},
     'te': {'text': 'మీ అద్దె వివరాలు Payments డ్యాష్‌బోర్డ్‌లో ఉంటాయి:', 'steps': [
         'పై మెనూలో <b>Payments</b> తెరవండి.',
         'మీ గది నమోదు చేసిన మొబైల్ నంబర్ ఇచ్చి <b>Continue</b> నొక్కండి.',
         'డ్యాష్‌బోర్డ్‌లో <b>Amount Due</b>, మొత్తం అద్దె, చెల్లించిన మొత్తం కనిపిస్తాయి.']}},
    {'id': 'room_details', 'kw': ['room details', 'my room', 'which room', 'room number', 'my hostel', 'which hostel', 'where is my room', 'check my room', 'నా గది', 'గది వివరాలు', 'రూమ్ నంబర్'],
     'title': 'Check my room details',
     'en': {'text': 'Your hostel and room are shown on your Payments dashboard:', 'steps': [
         'Open <b>Payments</b> and enter your registered mobile number.',
         'Tap <b>Continue</b>. At the top you will see your name with your hostel and room (for example "Old Hostel • Room 2").']},
     'te': {'text': 'మీ హాస్టల్, గది వివరాలు Payments డ్యాష్‌బోర్డ్‌లో కనిపిస్తాయి:', 'steps': [
         '<b>Payments</b> తెరిచి మీ నమోదిత మొబైల్ నంబర్ ఇవ్వండి.',
         '<b>Continue</b> నొక్కండి. పైన మీ పేరుతో పాటు హాస్టల్, గది కనిపిస్తాయి.']}},
    {'id': 'make_payment', 'kw': ['make a payment', 'make payment', 'pay my rent', 'pay rent', 'how do i pay', 'how to pay', 'pay online', 'qr', 'upi', 'phonepe', 'payment number', 'చెల్లింపు ఎలా', 'ఎలా చెల్లించాలి', 'చెల్లించాలి'],
     'title': 'Make a payment',
     'en': {'text': 'To pay your hostel rent:', 'steps': [
         'Open <b>Payments</b> and enter your registered mobile number, then tap <b>Continue</b>.',
         'On the dashboard, use the QR code or the Payment Number / Name shown there to pay online (tap <b>COPY</b> to copy the number).',
         'After paying, tap <b>RECORD PAYMENT</b> so the hostel admin knows about it (see "How do I record a payment?").']},
     'te': {'text': 'హాస్టల్ అద్దె చెల్లించడానికి:', 'steps': [
         '<b>Payments</b> తెరిచి మీ నమోదిత మొబైల్ నంబర్ ఇచ్చి <b>Continue</b> నొక్కండి.',
         'డ్యాష్‌బోర్డ్‌లో కనిపించే QR కోడ్ లేదా Payment Number / Name ఉపయోగించి ఆన్‌లైన్‌లో చెల్లించండి (నంబర్ కాపీ చేయడానికి <b>COPY</b> నొక్కండి).',
         'చెల్లించిన తర్వాత <b>RECORD PAYMENT</b> నొక్కండి, అప్పుడే హాస్టల్ అడ్మిన్‌కు తెలుస్తుంది.']}},
    {'id': 'record_payment', 'kw': ['record payment', 'record a payment', 'submit payment', 'submit my payment', 'add my payment', 'upload receipt', 'receipt', 'screenshot', 'paid in cash', 'cash payment', 'paid cash', 'paid online', 'i have paid', 'i paid', 'చెల్లింపు నమోదు', 'రసీదు', 'క్యాష్', 'నగదు'],
     'title': 'Record a payment',
     'en': {'text': 'After you have paid (online or in cash):', 'steps': [
         'Open <b>Payments</b>, enter your registered mobile number and tap <b>Continue</b>.',
         'Tap <b>RECORD PAYMENT</b>. Your name, room and rent are already filled in and locked.',
         'Enter the <b>Amount Paid</b> (it cannot be more than your current due), the <b>Payment Date</b> and the <b>Payment Mode</b> (Online, Cash or Online + Cash).',
         'For online payments, upload the receipt screenshot (JPG or PNG, up to 8 MB). For cash, fill in the cash amount and who you paid it to.',
         'Tap <b>SUBMIT PAYMENT</b>. It stays <b>PENDING</b> until the hostel admin confirms it.']},
     'te': {'text': 'మీరు చెల్లించిన తర్వాత (ఆన్‌లైన్ లేదా నగదు):', 'steps': [
         '<b>Payments</b> తెరిచి మొబైల్ నంబర్ ఇచ్చి <b>Continue</b> నొక్కండి.',
         '<b>RECORD PAYMENT</b> నొక్కండి. మీ పేరు, గది, అద్దె ఇప్పటికే నింపబడి లాక్ అయి ఉంటాయి.',
         '<b>Amount Paid</b> (ప్రస్తుత బాకీ కంటే ఎక్కువ ఉండకూడదు), <b>Payment Date</b>, <b>Payment Mode</b> (Online, Cash లేదా Online + Cash) ఇవ్వండి.',
         'ఆన్‌లైన్ అయితే రసీదు స్క్రీన్‌షాట్ (JPG లేదా PNG, 8 MB వరకు) అప్‌లోడ్ చేయండి. నగదు అయితే నగదు మొత్తం, ఎవరికి ఇచ్చారో నింపండి.',
         '<b>SUBMIT PAYMENT</b> నొక్కండి. హాస్టల్ అడ్మిన్ నిర్ధారించే వరకు ఇది <b>PENDING</b> గా ఉంటుంది.']}},
    {'id': 'payment_status', 'kw': ['payment status', 'my payment', 'see my payment', 'confirmed', 'pending', 'is my payment', 'payment confirmed', 'payment history', 'recent submissions', 'my submissions', 'నిర్ధారణ', 'పెండింగ్'],
     'title': 'Check payment status',
     'en': {'text': 'To see whether your payment was confirmed:', 'steps': [
         'Open <b>Payments</b>, enter your registered mobile number and tap <b>Continue</b>.',
         'Scroll to <b>Your Recent Submissions</b>. Each payment shows <b>PENDING</b> (waiting for the hostel admin) or <b>CONFIRMED</b>.']},
     'te': {'text': 'మీ చెల్లింపు నిర్ధారణ అయిందో లేదో చూడటానికి:', 'steps': [
         '<b>Payments</b> తెరిచి మొబైల్ నంబర్ ఇచ్చి <b>Continue</b> నొక్కండి.',
         '<b>Your Recent Submissions</b> వరకు స్క్రోల్ చేయండి. ప్రతి చెల్లింపు <b>PENDING</b> (అడ్మిన్ కోసం వేచి ఉంది) లేదా <b>CONFIRMED</b> గా కనిపిస్తుంది.']}},
    {'id': 'vacancy', 'kw': ['vacancy', 'vacant', 'available room', 'empty room', 'free room', 'free bed', 'any room', 'bed available', 'ఖాళీ', 'ఖాళీగా'],
     'title': 'Room vacancy',
     'en': {'text': "Room vacancy is not shown in the student-side app. Please contact Ravi to ask about available rooms."},
     'te': {'text': 'గదుల ఖాళీల సమాచారం విద్యార్థుల విభాగంలో చూపబడదు. అందుబాటులో ఉన్న గదుల కోసం దయచేసి Ravi ని సంప్రదించండి.'}},
    {'id': 'book_room', 'kw': ['book a room', 'book room', 'booking', 'join the hostel', 'join hostel', 'admission', 'new student', 'register', 'get a room', 'allot', 'room allotment', 'గది బుక్', 'బుక్ చేయ', 'చేరాలి'],
     'title': 'Book a room',
     'en': {'text': "Rooms cannot be booked from the student-side app. Rooms are assigned by the hostel admin when a student is registered, so please contact Ravi to join or to ask for a room."},
     'te': {'text': 'విద్యార్థుల విభాగం నుండి గదిని బుక్ చేయలేరు. విద్యార్థిని నమోదు చేసినప్పుడు హాస్టల్ అడ్మిన్ గదిని కేటాయిస్తారు, కాబట్టి చేరడానికి లేదా గది కోసం దయచేసి Ravi ని సంప్రదించండి.'}},
    {'id': 'change_info', 'kw': ['change my', 'change information', 'update my', 'edit my', 'change name', 'change number', 'change phone', 'change mobile', 'wrong name', 'wrong details', 'submit information', 'change room', 'shift room', 'మార్చ', 'సవరించ', 'తప్పు'],
     'title': 'Change my information',
     'en': {'text': "Students cannot edit their own name, mobile number, room or rent in this app. Your details on the Record Payment form are locked. To correct something or change rooms, please contact Ravi."},
     'te': {'text': 'ఈ యాప్‌లో విద్యార్థులు తమ పేరు, మొబైల్ నంబర్, గది లేదా అద్దెను మార్చలేరు. Record Payment ఫారమ్‌లో మీ వివరాలు లాక్ అయి ఉంటాయి. ఏదైనా సరిదిద్దాలన్నా, గది మార్చాలన్నా దయచేసి Ravi ని సంప్రదించండి.'}},
    {'id': 'switch_number', 'kw': ['switch mobile', 'switch number', 'not you', 'wrong number', 'another number', 'different number', 'login', 'log in', 'sign in', 'mobile number'],
     'title': 'Mobile number / switching',
     'en': {'text': 'This app identifies you by the mobile number your room was registered with:', 'steps': [
         'Open <b>Payments</b> and type that 10-digit mobile number, then tap <b>Continue</b>.',
         'If the dashboard shows someone else, tap <b>Not you? Switch mobile number</b> at the bottom and enter your number again.',
         'If you see "No student found", the number is not the one registered. Please check with Ravi.']},
     'te': {'text': 'ఈ యాప్ మీ గది నమోదు చేసిన మొబైల్ నంబర్‌తో మిమ్మల్ని గుర్తిస్తుంది:', 'steps': [
         '<b>Payments</b> తెరిచి ఆ 10 అంకెల మొబైల్ నంబర్ ఇచ్చి <b>Continue</b> నొక్కండి.',
         'డ్యాష్‌బోర్డ్‌లో వేరొకరు కనిపిస్తే, కింద ఉన్న <b>Not you? Switch mobile number</b> నొక్కి మీ నంబర్ మళ్లీ ఇవ్వండి.',
         '"No student found" అని వస్తే ఆ నంబర్ నమోదిత నంబర్ కాదు. దయచేసి Ravi ని అడగండి.']}},
    {'id': 'qr_missing', 'kw': ['qr not', 'no qr', 'qr code not', 'qr missing', 'not set up'],
     'title': 'QR code not available',
     'en': {'text': 'If the dashboard says the QR code is not set up yet, online payment details are not available at the moment. Please contact Ravi.'},
     'te': {'text': 'QR కోడ్ ఇంకా సెట్ చేయలేదని డ్యాష్‌బోర్డ్‌లో కనిపిస్తే, ప్రస్తుతం ఆన్‌లైన్ చెల్లింపు వివరాలు అందుబాటులో లేవు. దయచేసి Ravi ని సంప్రదించండి.'}},
    {'id': 'menu_options', 'kw': ['what does', 'what is payments', 'home option', 'menu', 'what is record payment', 'what is copy', 'this option', 'this button', 'ఈ ఆప్షన్', 'ఈ బటన్'],
     'title': 'What the options do',
     'en': {'text': 'The student-side options are:', 'steps': [
         '<b>Home / Payments</b>: enter your mobile number to open your payments dashboard.',
         '<b>Amount Due</b>: what you still owe for the month.',
         '<b>COPY</b>: copies the payment number so you can paste it into your payment app.',
         '<b>RECORD PAYMENT</b>: tell the hostel admin about a payment you made (online or cash).',
         '<b>PENDING / CONFIRMED</b>: whether the admin has confirmed that payment.']},
     'te': {'text': 'విద్యార్థుల విభాగంలోని ఆప్షన్లు:', 'steps': [
         '<b>Home / Payments</b>: మీ మొబైల్ నంబర్ ఇచ్చి పేమెంట్స్ డ్యాష్‌బోర్డ్ తెరవండి.',
         '<b>Amount Due</b>: ఆ నెలకు మీరు ఇంకా చెల్లించాల్సిన మొత్తం.',
         '<b>COPY</b>: పేమెంట్ నంబర్‌ను కాపీ చేస్తుంది.',
         '<b>RECORD PAYMENT</b>: మీరు చేసిన చెల్లింపు (ఆన్‌లైన్ లేదా నగదు) గురించి అడ్మిన్‌కు తెలియజేయడానికి.',
         '<b>PENDING / CONFIRMED</b>: అడ్మిన్ ఆ చెల్లింపును నిర్ధారించారో లేదో.']}},
]

# Requests the user bot must always refuse (credentials, config, admin area, other people's records).
_USER_RESTRICTED = re.compile(
    r'pass\s?word|passcode|credential|api[\s_-]?key|secret|token|(?<![a-z])\.?env\b|environment variable|database|\bdb\b|'
    r'supabase|connection string|private config|config(uration)?\b|admin\s?login|adminlogin|admin (url|link|page|panel|dashboard|account|username|user name|access)|'
    r'login (as|to) admin|\badmin\b.*\b(pass|login|credential|key)|source code|hack|bypass|'
    r'(other|another|everyone|everybody|someone|somebody|other people|other students|other residents)(\x27s)?\b.{0,25}\b(students?|residents?|members?|rents?|balances?|phones?|numbers?|details|payments?|contacts?)|'
    r'list of (all )?(students|residents|members)|all (the )?(students|residents|members)|'
    r'పాస్\u200c?వర్డ్|పాస్\u200c?వర్డు|రహస్య|డేటాబేస్|అడ్మిన్ లాగిన్|ఇతర విద్యార్థుల|అందరి')


def _render(entry, lang):
    block = entry.get(lang) or entry['en']
    text = block['text']
    steps = block.get('steps') or []
    html = '<div>' + (text if '<b>' in text else escape(text)) + '</div>'
    if steps:
        html += '<ol style="margin:.35rem 0 0 1.1rem;list-style:decimal">' + ''.join(f'<li>{s}</li>' for s in steps) + '</ol>'
    speak = re.sub('<[^>]+>', '', text + ' ' + ' '.join(f'{i + 1}. {s}' for i, s in enumerate(steps)))
    return html, speak.strip()


def _stem(t):
    """Very light plural handling so 'payments' matches 'payment' (English tokens only)."""
    return t[:-1] if len(t) > 3 and t.endswith('s') and not t.endswith('ss') and not has_telugu(t) else t


def _score_kb(entries, message):
    msg = ' ' + ' '.join(_stem(t) for t in tokenize(message)) + ' '
    raw = (message or '').lower()
    best, best_score = None, 0
    for e in entries:
        score = 0
        for k in e['kw']:
            kk = k.lower()
            if has_telugu(kk):
                hit = kk in raw
            else:
                hit = (' ' + ' '.join(_stem(t) for t in tokenize(kk)) + ' ') in msg
            if hit:
                score += len(kk.split()) * 2 + (1 if len(kk) > 6 else 0)
        if e.get('only_short') and len(tokenize(message)) > 4:
            score = 0
        if score > best_score:
            best, best_score = e, score
    return best, best_score


_HOW_WORDS = ('how', 'where', 'steps', 'step', 'procedure', 'process', 'ఎలా', 'ఎక్కడ')


def _personal_answer(message, p, lang):
    """Answers about the signed-in student's OWN balance / payment status / room, using only the
    figures their own dashboard already shows. `p` is built by the route from that one student's record."""
    toks = tokenize(message)
    raw = message.lower()
    joined = ' '.join(toks)
    if 'how much' not in joined and (any(t in _HOW_WORDS for t in toks) or any(w in raw for w in _HOW_WORDS if has_telugu(w))):
        return None  # a "how do I ..." question gets the step-by-step answer instead
    mine = any(t in ('my', 'mine') for t in toks) or 'నా' in raw or 'how much' in joined or 'am i' in joined
    if not mine:
        return None
    due, total, paid = p['due'], p['total_rent'], p['paid']
    if any(w in joined for w in ('balance', 'due', 'dues', 'owe', 'outstanding', 'pending amount')) or any(w in raw for w in ('బాకీ', 'బ్యాలెన్స్')) \
            or ('how much' in joined and any(w in joined for w in ('pay', 'owe', 'rent'))):
        pend = ''
        if p.get('pending_count'):
            pend = tr(lang, f" A payment of {rupee(p['pending_amount'])} is waiting for the admin to confirm.",
                      f" {rupee(p['pending_amount'])} చెల్లింపు అడ్మిన్ నిర్ధారణ కోసం వేచి ఉంది.")
        if due > 0:
            t = tr(lang, f"Your current balance is <b>{rupee(due)}</b> (total rent {rupee(total)}, paid {rupee(paid)}).{pend}",
                   f"మీ ప్రస్తుత బాకీ <b>{rupee(due)}</b> (మొత్తం అద్దె {rupee(total)}, చెల్లించినది {rupee(paid)}).{pend}")
        else:
            t = tr(lang, f"You have no balance due right now (total rent {rupee(total)}, paid {rupee(paid)}).{pend}",
                   f"ప్రస్తుతం మీకు బాకీ ఏమీ లేదు (మొత్తం అద్దె {rupee(total)}, చెల్లించినది {rupee(paid)}).{pend}")
        return _user_ok(t, lang)
    if any(w in joined for w in ('status', 'confirmed', 'confirm', 'approved', 'payment')) or 'నిర్ధారణ' in raw:
        last = (p.get('last') or [None])[0]
        if not last:
            t = tr(lang, "I don't see any payment submitted from your number yet.", 'మీ నంబర్ నుండి ఇంకా ఎలాంటి చెల్లింపు నమోదు కాలేదు.')
        elif last['status'] == 'CONFIRMED':
            t = tr(lang, f"Your latest payment of {rupee(last['amount'])} ({last['date']}) is <b>CONFIRMED</b> by the admin.",
                   f"మీ తాజా చెల్లింపు {rupee(last['amount'])} ({last['date']}) అడ్మిన్ ద్వారా <b>నిర్ధారించబడింది</b>.")
        else:
            t = tr(lang, f"Your latest payment of {rupee(last['amount'])} ({last['date']}) is <b>PENDING</b>. It will show as confirmed once the admin checks it.",
                   f"మీ తాజా చెల్లింపు {rupee(last['amount'])} ({last['date']}) <b>పెండింగ్</b>లో ఉంది. అడ్మిన్ తనిఖీ చేసిన తర్వాత నిర్ధారణ అవుతుంది.")
        return _user_ok(t, lang)
    if any(w in joined for w in ('room', 'hostel')) or 'గది' in raw:
        t = tr(lang, f"You are in <b>{escape(p['hostel'])} • {escape(p['room'])}</b> ({p['sharing']} sharing).",
               f"మీరు <b>{escape(p['hostel'])} • {escape(p['room'])}</b> లో ఉన్నారు ({p['sharing']} షేరింగ్).")
        return _user_ok(t, lang)
    return None


def _user_ok(html_text, lang):
    return {'ok': True, 'html': html_text, 'speak': unescape(re.sub('<[^>]+>', '', html_text)), 'lang': lang}


def user_reply(message, personal=None):
    message = (message or '').strip()[:300]
    lang = 'te' if has_telugu(message) else 'en'
    if not message:
        e = USER_KB[1]
        html, speak = _render(e, lang)
        return {'ok': True, 'html': html, 'speak': speak, 'lang': lang}

    # 1) confidential / admin-side / other people's data: always refused, never looked up.
    if _USER_RESTRICTED.search(message):
        t = RESTRICTED_USER_TE if lang == 'te' else RESTRICTED_USER
        return {'ok': True, 'html': escape(t), 'speak': t, 'lang': lang}

    # 1b) the signed-in student's own balance / status / room (never anyone else's)
    if personal:
        ans = _personal_answer(message, personal, lang)
        if ans:
            return ans

    # 2) knowledge-base match
    entry, score = _score_kb(USER_KB, message)
    if entry is None or score < 2:
        # 3) optional LLM: may only choose an existing entry id, it cannot write the answer.
        listing = '\n'.join(f"{e['id']}: {e['title']}" for e in USER_KB)
        res = _llm_json(
            'You route questions for a hostel app help bot. Reply ONLY with JSON {"id": "<id>"} choosing the one '
            'entry below that answers the question, or {"id": "none"} if the question is unrelated to the entries '
            'or asks for private/admin/credential information.\n' + listing, message)
        picked = (res or {}).get('id') if isinstance(res, dict) else None
        entry = next((e for e in USER_KB if e['id'] == picked), None)
    if entry is None:
        t = OUT_OF_SCOPE + (('\n' + OUT_OF_SCOPE_TE) if lang == 'te' else '')
        return {'ok': True, 'html': escape(t).replace('\n', '<br>'), 'speak': t, 'lang': lang}
    html, speak = _render(entry, lang)
    return {'ok': True, 'html': html, 'speak': speak, 'lang': lang}


# ==========================================================================
# ADMIN ASSISTANT
# ==========================================================================

HOSTEL_WORDS = {'Old Hostel': ('old', 'ఓల్డ్', 'ఓల్డు', 'పాత'), 'New Hostel': ('new', 'న్యూ', 'న్యు', 'కొత్త')}
ROOM_WORDS = ('room', 'rooms', 'రూమ్', 'రూం', 'రూము', 'గది')
FILLERS = ('number', 'no', 'num', 'nr', 'నంబర్', 'నెంబర్', 'నంబరు', 'నెంబరు', 'సంఖ్య')
HALL_WORDS = ('hall', 'హాల్')
SHIFT_WORDS = ('shift', 'move', 'transfer', 'relocate', 'shifting', 'moving')
SHIFT_TE = ('షిఫ్ట్', 'మార్చ', 'తరలి', 'మార్పు')
DELETE_WORDS = ('delete', 'remove', 'erase', 'evict', 'vacate')
DELETE_TE = ('తొలగ', 'డిలీట్', 'ఖాళీ చేయ', 'ఖాళీచేయ')
YES_WORDS = ('yes', 'confirm', 'confirmed', 'ok', 'okay', 'proceed', 'sure', 'do it', 'అవును', 'సరే', 'నిర్ధారించు', 'చేయి', 'చేయండి')
NO_WORDS = ('no', 'cancel', 'stop', 'dont', "don't", 'వద్దు', 'కాదు', 'రద్దు', 'ఆపు')

STOP = set(('shift move transfer relocate shifting moving change from to into in the a an of for please student resident '
            'room rooms hall hostel number no num old new and then at this that him her he she is are was be do make '
            'show tell give me my who what how many much which where when details detail info information rent paid '
            'balance due contact phone vacant empty free available vacancy total all list status check find get '
            'ను ని కి కు నుండి నుంచి లో లోకి గది రూమ్ రూం హాస్టల్ ఓల్డ్ న్యూ కొత్త పాత చేయండి చేయి మార్చండి మార్చు '
            'ఎవరు ఎంత ఎన్ని చూపించు చెప్పండి చెప్పు ఏమిటి ఎవరూ గదులు గదిలో ఖాళీ ఖాళీగా ఉన్నాయి ఉన్నారు మొత్తం బాకీ '
            'బ్యాలెన్స్ విద్యార్థులు వివరాలు అద్దె షిఫ్ట్ షిఫ్టు తరలించు students residents dues').split())


def _hostel_of(tok):
    for h, words in HOSTEL_WORDS.items():
        if any(tok == w or (has_telugu(w) and tok.startswith(w)) for w in words):
            return h
    return None


def _is_room_word(tok):
    return tok in ROOM_WORDS or tok.startswith(('రూమ్', 'రూం', 'గది'))


def _is_marker(tok):
    return tok in ('from', 'to', 'into') or (has_telugu(tok) and tok.endswith(('నుండి', 'నుంచి', 'కి', 'కు', 'లోకి')))


def parse_locations(tokens):
    """Finds every 'room N [hostel]' mention. Returns list of dicts (hostel, room, role)."""
    mentions = []
    n = len(tokens)
    i = 0
    while i < n:
        t = tokens[i]
        room, start, end = None, i, i
        if _is_room_word(t):
            j = i + 1
            while j < n and j <= i + 3 and tokens[j] in FILLERS:
                j += 1
            if j < n and tokens[j].isdigit():
                room, end = int(tokens[j]), j
            elif j < n and (tokens[j] in HALL_WORDS or tokens[j].startswith('హాల్')):
                room, end = 11, j
        elif t.isdigit() and i + 1 < n and _is_room_word(tokens[i + 1]) and not (i > 0 and _is_room_word(tokens[i - 1])):
            room, end = int(t), i + 1
        elif t in HALL_WORDS or t.startswith('హాల్'):
            room = 11
        if room is not None:
            mentions.append({'room': room, 'start': start, 'end': end, 'hostel': None, 'role': None})
            i = end + 1
        else:
            i += 1

    hostel_pos = [(k, _hostel_of(t)) for k, t in enumerate(tokens) if _hostel_of(t)]
    distinct = {h for _, h in hostel_pos}
    for idx, m in enumerate(mentions):
        lo = mentions[idx - 1]['end'] + 1 if idx else 0
        hi = mentions[idx + 1]['start'] if idx + 1 < len(mentions) else n
        near = []
        for k, h in hostel_pos:
            if not (lo <= k < hi):
                continue
            between = tokens[m['end'] + 1:k] if k > m['end'] else tokens[k + 1:m['start']]
            if any(_is_marker(t) for t in between):   # a hostel on the other side of from/to belongs to the other room
                continue
            near.append((min(abs(k - m['end']), abs(k - m['start'])), h))
        if near:
            m['hostel'] = min(near)[1]
        elif m['room'] == 11:
            m['hostel'] = 'New Hostel'          # the Hall exists only in the New Hostel
        elif len(distinct) == 1:
            m['hostel'] = next(iter(distinct))
        # role: English words before the mention, Telugu postpositions after it.
        back = tokens[max(lo, m['start'] - 4):m['start']]
        fwd = tokens[m['end'] + 1:min(hi, m['end'] + 5)]
        if 'from' in back or any(t.endswith(('నుండి', 'నుంచి')) for t in fwd):
            m['role'] = 'from'
        elif 'to' in back or 'into' in back or any(t.endswith(('కి', 'కు', 'లోకి')) and has_telugu(t) for t in fwd):
            m['role'] = 'to'
    return mentions


def split_src_dst(mentions):
    """(src, dst) as (hostel, room) tuples or None, using explicit roles first, then order."""
    src = next((m for m in mentions if m['role'] == 'from'), None)
    dst = next((m for m in mentions if m['role'] == 'to'), None)
    rest = [m for m in mentions if m is not src and m is not dst]
    if src is None and dst is None:
        if len(mentions) >= 2:
            src, dst = mentions[0], mentions[1]
    elif src is None and len(mentions) >= 2 and rest:
        src = rest[0]
    elif dst is None and len(mentions) >= 2 and rest:
        dst = rest[0]
    key = lambda m: (m['hostel'], m['room']) if m else None
    return key(src), key(dst)


def name_tokens(tokens):
    return [t for t in tokens if t not in STOP and not t.isdigit() and len(t) >= 3
            and not _is_room_word(t) and not _hostel_of(t) and not t.startswith(('హాల్', 'హాస్టల్'))]


def _tok_match(a, b):
    ka, kb = phon_key(a), phon_key(b)
    if len(ka) < 3 or len(kb) < 3:
        return False
    return ka == kb or difflib.SequenceMatcher(None, ka, kb).ratio() >= 0.84


def match_students(students, tokens):
    """[(score, student)] for students whose name matches the typed/spoken words (best first)."""
    words = name_tokens(tokens)
    if not words:
        return []
    out = []
    for s in students:
        parts = [p for p in re.split(r'[^A-Za-z\u0C00-\u0C7F]+', s['name'] or '') if len(p) >= 3]
        score = sum(1 for p in parts if any(_tok_match(p, w) for w in words))
        if score:
            out.append((score, s))
    out.sort(key=lambda x: (-x[0], (x[1]['name'] or '').lower()))
    return out


def _loc(hostel, room, lang='en'):
    label = ('Hall' if lang == 'en' else 'హాల్') if room == 11 else (f'Room {room}' if lang == 'en' else f'రూమ్ {room}')
    return f'{hostel} {label}' if lang == 'en' else f"{'ఓల్డ్' if hostel == 'Old Hostel' else 'న్యూ'} హాస్టల్ {label}"


def _short_contact(c):
    c = str(c or '')
    return '…' + c[-4:] if len(c) >= 4 else c


def _reply(lang, html, speak=None, chips=None):
    plain = speak if speak is not None else re.sub('<[^>]+>', ' ', html)
    return {'ok': True, 'html': html, 'speak': re.sub(r'\s+', ' ', unescape(plain)).strip(), 'lang': lang, 'chips': chips or []}


# ---- admin knowledge base (feature explanations; matches the real dashboard labels) ----
ADMIN_KB = [
    {'kw': ['add student', 'add a student', 'new student', 'register student', 'save student', 'విద్యార్థిని చేర్చ', 'కొత్త విద్యార్థి'],
     'en': ['<b>Add Student</b> registers a resident into a room:', 'Tap <b>Add Student</b> on the dashboard.', 'Choose Hostel, Room (or Hall) and Sharing, then enter name, mobile number, total rent and date of join.', 'Tap <b>SAVE STUDENT</b>. A full room (all beds taken) is rejected automatically.'],
     'te': ['<b>Add Student</b> విద్యార్థిని గదిలో నమోదు చేస్తుంది:', 'డ్యాష్‌బోర్డ్‌లో <b>Add Student</b> నొక్కండి.', 'హాస్టల్, రూమ్ (లేదా హాల్), షేరింగ్ ఎంచుకుని పేరు, మొబైల్ నంబర్, మొత్తం అద్దె, చేరిన తేదీ ఇవ్వండి.', '<b>SAVE STUDENT</b> నొక్కండి. గది నిండి ఉంటే అది ఆటోమేటిక్‌గా తిరస్కరించబడుతుంది.']},
    {'kw': ['edit student', 'edit a student', 'change student', 'update student', 'edit details', 'విద్యార్థిని సవరించ', 'ఎడిట్'],
     'en': ['To change a resident\'s details:', 'Find the student on the dashboard (use search or the hostel/room filters).', 'Tap <b>Edit</b>, change the fields and save. Room capacity is re-checked on save.', 'To only move someone to another room, you can also just tell me, e.g. "Shift Manohar from room 2 Old Hostel to room 3 Old Hostel".'],
     'te': ['విద్యార్థి వివరాలు మార్చడానికి:', 'డ్యాష్‌బోర్డ్‌లో సెర్చ్ లేదా హాస్టల్/రూమ్ ఫిల్టర్లతో విద్యార్థిని కనుగొనండి.', '<b>Edit</b> నొక్కి మార్చి సేవ్ చేయండి. సేవ్ చేసేటప్పుడు గది సామర్థ్యం మళ్లీ తనిఖీ అవుతుంది.', 'గది మాత్రమే మార్చాలంటే నన్ను అడగవచ్చు, ఉదా: "మనోహర్ ను ఓల్డ్ హాస్టల్ రూమ్ 2 నుండి రూమ్ 3 కి మార్చండి".']},
    {'kw': ['delete student', 'make empty', 'make it empty', 'free a bed', 'empty a bed', 'విద్యార్థిని తొలగ', 'బెడ్ ఖాళీ'],
     'en': ['Removing a resident is permanent (their record is deleted). Two ways:', 'Menu <b>2) Make Empty</b> in this assistant: pick hostel, room and member, then confirm.', 'Or tap <b>Delete</b> on the student card and confirm in the pop-up.'],
     'te': ['విద్యార్థిని తొలగించడం శాశ్వతం (రికార్డ్ డిలీట్ అవుతుంది). రెండు మార్గాలు:', 'ఈ అసిస్టెంట్‌లో మెనూ <b>2) Make Empty</b>: హాస్టల్, రూమ్, సభ్యుడిని ఎంచుకుని నిర్ధారించండి.', 'లేదా విద్యార్థి కార్డ్‌లో <b>Delete</b> నొక్కి పాప్-అప్‌లో నిర్ధారించండి.']},
    {'kw': ['export excel', 'excel', 'download excel', 'spreadsheet', 'ఎక్సెల్'],
     'en': ['<b>Export Excel</b> downloads a workbook with Old Hostel, New Hostel and Summary sheets.', '<b>Import Excel</b> adds students from a sheet with columns: Hostel, Room Number, Sharing, Name, Contact, Total Rent, Amount Paid. Rows that fail validation are skipped and reported.'],
     'te': ['<b>Export Excel</b> ఓల్డ్ హాస్టల్, న్యూ హాస్టల్, సమ్మరీ షీట్లతో వర్క్‌బుక్ డౌన్‌లోడ్ చేస్తుంది.', '<b>Import Excel</b> ఈ కాలమ్స్ ఉన్న షీట్ నుండి విద్యార్థులను చేర్చుతుంది: Hostel, Room Number, Sharing, Name, Contact, Total Rent, Amount Paid. చెల్లని వరుసలు వదిలేసి నివేదిస్తుంది.']},
    {'kw': ['pdf', 'report', 'print', 'పిడిఎఫ్', 'రిపోర్ట్'],
     'en': ['<b>Generate PDF</b> offers <b>OLD HOSTEL PDF</b>, <b>NEW HOSTEL PDF</b> and <b>COMPLETE HOSTEL PDF</b>. Rooms are listed in order with totals, page numbers and the generation date.'],
     'te': ['<b>Generate PDF</b> లో <b>OLD HOSTEL PDF</b>, <b>NEW HOSTEL PDF</b>, <b>COMPLETE HOSTEL PDF</b> ఉంటాయి. గదులు క్రమంలో మొత్తాలు, పేజీ నంబర్లు, తేదీతో వస్తాయి.']},
    {'kw': ['due list', 'who has to pay', 'who owes', 'overdue', 'defaulter', 'బాకీ జాబితా', 'డ్యూ లిస్ట్'],
     'en': ['<b>Due List</b> shows students whose rent is due: from their due day of the month until the payment is confirmed. From there you can call the student, add a note, or use <b>Edit / Record Payment</b>.'],
     'te': ['<b>Due List</b> అద్దె చెల్లించాల్సిన విద్యార్థులను చూపిస్తుంది: నెలలో వారి గడువు రోజు నుండి చెల్లింపు నిర్ధారణ అయ్యే వరకు. అక్కడి నుండి కాల్ చేయవచ్చు, నోట్ రాయవచ్చు, <b>Edit / Record Payment</b> వాడవచ్చు.']},
    {'kw': ['confirm payment', "today's payments", 'todays payments', 'pending payment', 'payments awaiting', 'confirm a payment', 'చెల్లింపును నిర్ధారించ', 'పెండింగ్ చెల్లింపు'],
     'en': ['Student payments wait for you in <b>Today\'s Payments</b>:', 'Open <b>Today\'s Payments</b> from the dashboard.', 'Use <b>VIEW PROOF</b> to check the receipt, then tap <b>CONFIRM PAYMENT</b>. Only then do the student\'s paid amount and balance change.'],
     'te': ['విద్యార్థుల చెల్లింపులు <b>Today\'s Payments</b> లో మీ కోసం ఉంటాయి:', 'డ్యాష్‌బోర్డ్ నుండి <b>Today\'s Payments</b> తెరవండి.', '<b>VIEW PROOF</b> తో రసీదు చూసి <b>CONFIRM PAYMENT</b> నొక్కండి. అప్పుడే విద్యార్థి చెల్లించిన మొత్తం, బ్యాలెన్స్ మారతాయి.']},
    {'kw': ['unconfirmed', 'paid not confirmed', 'marked paid'],
     'en': ['<b>Paid (Unconfirmed)</b> lists students marked as paid this month without an admin-confirmed payment. You can view the receipt, <b>EDIT</b>, or <b>DELETE</b> from there.'],
     'te': ['<b>Paid (Unconfirmed)</b> ఈ నెలలో అడ్మిన్ నిర్ధారణ లేకుండా చెల్లించినట్లు గుర్తించిన విద్యార్థులను చూపిస్తుంది. అక్కడ రసీదు చూడవచ్చు, <b>EDIT</b> లేదా <b>DELETE</b> చేయవచ్చు.']},
    {'kw': ['payment settings', 'qr code', 'upload qr', 'payment number', 'phonepe', 'పేమెంట్ సెట్టింగ్స్'],
     'en': ['<b>Payment Settings</b> lets you set, for each hostel, the QR code (JPG/PNG), the payment/PhonePe number and the account name. Students see the details of their own hostel on their dashboard.'],
     'te': ['<b>Payment Settings</b> లో ప్రతి హాస్టల్‌కు QR కోడ్ (JPG/PNG), పేమెంట్/ఫోన్‌పే నంబర్, అకౌంట్ పేరు సెట్ చేయవచ్చు. విద్యార్థులు తమ హాస్టల్ వివరాలను డ్యాష్‌బోర్డ్‌లో చూస్తారు.']},
    {'kw': ['per day rent', 'per-day rent', 'daily rent', 'rent per day', 'day rent', 'daily rate', 'per day', 'రోజు అద్దె', 'రోజుకు'],
     'en': ['__DAILY_RATES__', 'To use it: menu <b>3) Calculate Per-Day Rent</b>, pick the hostel, room and student, enter the number of days. You can then save it as that student\'s total rent.'],
     'te': ['__DAILY_RATES__', 'వాడటానికి: మెనూ <b>3) Calculate Per-Day Rent</b>, హాస్టల్, రూమ్, విద్యార్థిని ఎంచుకుని రోజుల సంఖ్య ఇవ్వండి. తర్వాత దాన్ని ఆ విద్యార్థి మొత్తం అద్దెగా సేవ్ చేయవచ్చు.']},
    {'kw': ['pause bed', 'pause a bed', 'paused bed', 'pause student', 'pause a student', 'పాజ్', 'బెడ్ పాజ్'],
     'en': ['<b>Pause Bed</b> temporarily marks beds as paused without changing the student, rent, sharing or payments:', 'Open <b>Pause Bed</b> in the sidebar (or the dashboard card).', 'Tap <b>Pause Bed</b>, choose the Hostel, then the Room (or Hall), tick one or more students, and enter the number of days.', 'Check the summary and tap <b>Confirm Pause</b>. The pause starts today and ends automatically; no manual un-pause is needed.', 'Use <b>Delete</b> on a paused card to remove only that pause record (the student is never deleted).'],
     'te': ['<b>Pause Bed</b> విద్యార్థి, అద్దె, షేరింగ్, చెల్లింపులను మార్చకుండా బెడ్‌లను తాత్కాలికంగా పాజ్ చేస్తుంది:', 'సైడ్‌బార్‌లో (లేదా డ్యాష్‌బోర్డ్ కార్డ్‌లో) <b>Pause Bed</b> తెరవండి.', '<b>Pause Bed</b> నొక్కి హాస్టల్, తర్వాత రూమ్ (లేదా హాల్) ఎంచుకుని ఒకరు లేదా ఎక్కువ మంది విద్యార్థులను టిక్ చేసి రోజుల సంఖ్య ఇవ్వండి.', 'సారాంశం చూసి <b>Confirm Pause</b> నొక్కండి. పాజ్ ఈరోజు మొదలై ఆటోమేటిక్‌గా ముగుస్తుంది; మాన్యువల్‌గా అన్‌పాజ్ చేయాల్సిన అవసరం లేదు.', 'పాజ్ చేసిన కార్డ్‌లో <b>Delete</b> నొక్కితే ఆ పాజ్ రికార్డ్ మాత్రమే తొలగిపోతుంది (విద్యార్థి ఎప్పటికీ తొలగించబడరు).']},
    {'kw': ['voice', 'speak', 'microphone', 'mic', 'read aloud', 'text to speech', 'listen', 'telugu', 'language', 'వాయిస్', 'మాట్లాడ', 'తెలుగు'],
     'en': ['Voice: tap the <b>mic</b> button, pick <b>EN</b> or <b>తె</b> for the language you will speak, and say your command. It is typed into the chat and processed like a normal message. Tap the <b>speaker</b> button to have replies read aloud.', 'Voice needs a browser with speech support (Chrome or Edge work best). Telugu speech needs a Telugu voice installed on the device.'],
     'te': ['వాయిస్: <b>mic</b> బటన్ నొక్కి, మీరు మాట్లాడే భాషగా <b>EN</b> లేదా <b>తె</b> ఎంచుకుని మీ కమాండ్ చెప్పండి. అది చాట్‌లో టైప్ అయి సాధారణ సందేశంలా ప్రాసెస్ అవుతుంది. సమాధానాలు వినడానికి <b>speaker</b> బటన్ నొక్కండి.', 'వాయిస్‌కు స్పీచ్ సపోర్ట్ ఉన్న బ్రౌజర్ కావాలి (Chrome లేదా Edge మంచివి). తెలుగు మాట్లాడటానికి/వినడానికి పరికరంలో తెలుగు వాయిస్ ఉండాలి.']},
    {'kw': ['what can you do', 'help', 'commands', 'how to use', 'examples', 'సహాయం', 'ఏమి చేయగలవు'],
     'en': ['I can answer in English or Telugu. Try:', '"How many vacant rooms are available?"', '"Who is in room 2 Old Hostel?"', '"Show details of Manohar"', '"Total balance due"', '"Shift Manohar from room 2 Old Hostel to room 3 Old Hostel" (I will ask you to confirm before changing anything)'],
     'te': ['నేను ఇంగ్లీష్ లేదా తెలుగులో సమాధానం ఇస్తాను. ప్రయత్నించండి:', '"ఎన్ని గదులు ఖాళీగా ఉన్నాయి?"', '"ఓల్డ్ హాస్టల్ రూమ్ 2 లో ఎవరు ఉన్నారు?"', '"మనోహర్ వివరాలు చూపించు"', '"మొత్తం బాకీ ఎంత?"', '"మనోహర్ ను ఓల్డ్ హాస్టల్ రూమ్ 2 నుండి రూమ్ 3 కి మార్చండి" (ఏదైనా మార్చే ముందు నిర్ధారణ అడుగుతాను)']},
]

_ADMIN_SECRETS = re.compile(
    r'pass\s?word|passcode|credential|api[\s_-]?key|secret|token|(?<![a-z])\.?env\b|environment variable|database (url|password|credential)|'
    r'connection string|service role|పాస్\u200c?వర్డ్|పాస్\u200c?వర్డు|రహస్య')


def _admin_kb_reply(message, lang, ctx):
    entry, score = _score_kb(ADMIN_KB, message)
    if not entry or score < 2:
        return None
    lines = entry.get(lang) or entry['en']
    rates = ctx['daily_rate']
    out = []
    for ln in lines:
        if ln == '__DAILY_RATES__':
            r1, r2 = rates(1), rates(4)
            ln = (f'Per-day rent: <b>1, 2 and 3 sharing = {rupee(r1)} per day</b>; every other sharing type = <b>{rupee(r2)} per day</b>. Total = daily rate x number of days.'
                  if lang == 'en' else
                  f'రోజువారీ అద్దె: <b>1, 2, 3 షేరింగ్ = రోజుకు {rupee(r1)}</b>; మిగిలిన అన్ని షేరింగ్‌లు = <b>రోజుకు {rupee(r2)}</b>. మొత్తం = రోజువారీ రేటు x రోజుల సంఖ్య.')
        out.append(ln)
    html = '<div>' + out[0] + '</div>'
    if len(out) > 1:
        html += '<ol style="margin:.35rem 0 0 1.1rem;list-style:decimal">' + ''.join(f'<li>{s}</li>' for s in out[1:]) + '</ol>'
    return _reply(lang, html)


# ---- data answers ------------------------------------------------------

def _vacancy(db, ctx):
    rows = db.execute('SELECT hostel, room_number, sharing FROM students').fetchall()
    occ = {}
    for r in rows:
        info = occ.setdefault((r['hostel'], r['room_number']), {'sharing': r['sharing'], 'count': 0})
        info['count'] += 1
    result = {}
    for hostel in ctx['HOSTELS']:
        h = {'vacant_beds': 0, 'capacity': 0, 'occupied': 0, 'with_vacancy': [], 'unassigned': []}
        for rn in ctx['rooms_for_hostel'](hostel):
            if (hostel, rn) in occ:
                sh, c = occ[(hostel, rn)]['sharing'], occ[(hostel, rn)]['count']
                v = max(sh - c, 0)
                h['capacity'] += sh
                h['occupied'] += c
                h['vacant_beds'] += v
                if v:
                    h['with_vacancy'].append((rn, v))
            else:
                h['unassigned'].append(rn)
        result[hostel] = h
    return result


def _ans_vacancy(db, ctx, lang, only_hostel=None):
    data = _vacancy(db, ctx)
    hostels = [only_hostel] if only_hostel else list(data)
    total = sum(data[h]['vacant_beds'] for h in hostels)
    lab = lambda rn: ('Hall' if rn == 11 else str(rn)) if lang == 'en' else ('హాల్' if rn == 11 else str(rn))
    parts = [tr(lang, f'<b>{total}</b> vacant bed(s) in total.', f'మొత్తం <b>{total}</b> ఖాళీ బెడ్లు ఉన్నాయి.')]
    for h in hostels:
        d = data[h]
        name = h if lang == 'en' else ('ఓల్డ్ హాస్టల్' if h == 'Old Hostel' else 'న్యూ హాస్టల్')
        wv = ', '.join(f'{lab(rn)} ({v})' for rn, v in d['with_vacancy']) or tr(lang, 'none', 'ఏవీ లేవు')
        ua = ', '.join(lab(rn) for rn in d['unassigned']) or tr(lang, 'none', 'ఏవీ లేవు')
        parts.append(tr(
            lang,
            f"<b>{escape(name)}</b>: {d['vacant_beds']} vacant of {d['capacity']} beds in assigned rooms.<br>"
            f"Rooms with vacant beds: {wv}.<br>Rooms with no students assigned: {ua}.",
            f"<b>{escape(name)}</b>: కేటాయించిన గదుల్లో {d['capacity']} బెడ్లలో {d['vacant_beds']} ఖాళీ.<br>"
            f"ఖాళీ బెడ్లు ఉన్న గదులు: {wv}.<br>విద్యార్థులు కేటాయించని గదులు: {ua}."))
    return _reply(lang, '<br>'.join(parts))


def _ans_room(db, ctx, lang, hostel, room):
    if hostel not in ctx['HOSTELS'] or room not in ctx['rooms_for_hostel'](hostel):
        return _reply(lang, escape(tr(lang, 'That room does not exist.', 'ఆ గది లేదు.')))
    rows = db.execute('SELECT name, contact, sharing, balance FROM students WHERE hostel=%s AND room_number=%s ORDER BY LOWER(name)',
                      (hostel, room)).fetchall()
    where = escape(_loc(hostel, room, lang))
    if not rows:
        return _reply(lang, tr(lang, f'{where} has no students assigned.', f'{where} లో విద్యార్థులు ఎవరూ లేరు.'))
    sharing = rows[0]['sharing']
    lis = ''.join(f"<li>{escape(r['name'])} ({_short_contact(r['contact'])}) - {tr(lang, 'balance', 'బ్యాలెన్స్')} {rupee(r['balance'])}</li>" for r in rows)
    head = tr(lang, f'<b>{where}</b>: {len(rows)}/{sharing} occupied, {max(sharing - len(rows), 0)} vacant.',
              f'<b>{where}</b>: {len(rows)}/{sharing} నిండాయి, {max(sharing - len(rows), 0)} ఖాళీ.')
    return _reply(lang, f'<div>{head}</div><ul style="margin:.35rem 0 0 1.1rem;list-style:disc">{lis}</ul>')


def _ans_students(lang, matches):
    if len(matches) > 5:
        return _reply(lang, escape(tr(lang, 'Several students match. Please give the full name.', 'చాలా మంది విద్యార్థులు సరిపోతున్నారు. పూర్తి పేరు చెప్పండి.')))
    blocks = []
    for _, s in matches:
        blocks.append(tr(
            lang,
            f"<b>{escape(s['name'])}</b> - {escape(_loc(s['hostel'], s['room_number'], lang))} ({s['sharing']} sharing)<br>"
            f"Contact: {escape(str(s['contact']))}<br>Rent {rupee(s['total_rent'])}, paid {rupee(s['amount_paid'])}, balance {rupee(s['balance'])}",
            f"<b>{escape(s['name'])}</b> - {escape(_loc(s['hostel'], s['room_number'], lang))} ({s['sharing']} షేరింగ్)<br>"
            f"ఫోన్: {escape(str(s['contact']))}<br>అద్దె {rupee(s['total_rent'])}, చెల్లించినది {rupee(s['amount_paid'])}, బ్యాలెన్స్ {rupee(s['balance'])}"))
    return _reply(lang, '<hr style="margin:.4rem 0">'.join(blocks))


def _ans_totals(db, ctx, lang, tokens, raw):
    stats = ctx['get_dashboard_stats'](db)
    wants_count = ('students' in tokens or 'residents' in tokens or 'ఎంత మంది' in raw or 'విద్యార్థులు' in raw)
    if wants_count and not any(w in raw for w in ('balance', 'due', 'paid', 'rent', 'బాకీ', 'బ్యాలెన్స్', 'అద్దె')):
        return _reply(lang, tr(lang,
                               f"<b>{stats['total']}</b> students in total: Old Hostel {stats['old_count']}, New Hostel {stats['new_count']}.",
                               f"మొత్తం <b>{stats['total']}</b> మంది విద్యార్థులు: ఓల్డ్ హాస్టల్ {stats['old_count']}, న్యూ హాస్టల్ {stats['new_count']}."))
    return _reply(lang, tr(lang,
                           f"Total rent to receive {rupee(stats['total_rent'])}; paid (confirmed this month) {rupee(stats['total_paid'])}; balance due <b>{rupee(stats['total_balance'])}</b>.",
                           f"స్వీకరించాల్సిన మొత్తం అద్దె {rupee(stats['total_rent'])}; ఈ నెల నిర్ధారణ అయిన చెల్లింపులు {rupee(stats['total_paid'])}; బాకీ <b>{rupee(stats['total_balance'])}</b>."))


# ---- shifting a resident -------------------------------------------------

def _plan_shift(db, ctx, student, dst, lang):
    """Validates the move with the app's own capacity rule. Returns (plan, error_text)."""
    dh, dr = dst
    if dh not in ctx['HOSTELS'] or dr not in ctx['rooms_for_hostel'](dh):
        if dr == 11:
            return None, tr(lang, 'The Hall exists only in the New Hostel.', 'హాల్ న్యూ హాస్టల్‌లో మాత్రమే ఉంది.')
        return None, tr(lang, f'{dh or "That hostel"} has no such room.', 'ఆ గది ఆ హాస్టల్‌లో లేదు.')
    if (student['hostel'], student['room_number']) == (dh, dr):
        return None, tr(lang, f"{student['name']} is already in {_loc(dh, dr)}.", f"{student['name']} ఇప్పటికే ఆ గదిలోనే ఉన్నారు.")
    ok, _msg = ctx['check_capacity'](db, dh, dr, student['sharing'], exclude_id=student['id'])
    if not ok:
        count, existing = ctx['room_occupancy'](db, dh, dr, exclude_id=student['id'])
        return None, tr(lang, f'{_loc(dh, dr)} is already full ({count}/{existing or student["sharing"]} occupied).',
                        f'{_loc(dh, dr, "te")} నిండిపోయింది ({count}/{existing or student["sharing"]}).')
    count, existing = ctx['room_occupancy'](db, dh, dr, exclude_id=student['id'])
    new_sharing = existing if (count and existing) else student['sharing']
    return {'sid': student['id'], 'dst': [dh, dr], 'new_sharing': int(new_sharing)}, None


def _shift_preview(db, ctx, lang, student, dst):
    plan, err = _plan_shift(db, ctx, student, dst, lang)
    if err:
        return _reply(lang, escape(err))
    token = secrets.token_hex(8)
    session['ai_pending'] = {'t': token, 'kind': 'shift', 'sid': plan['sid'], 'dst': plan['dst'], 'exp': time.time() + PENDING_TTL}
    fh, fr_ = student['hostel'], student['room_number']
    note_en = note_te = ''
    if plan['new_sharing'] != student['sharing']:
        note_en = f" The room is {plan['new_sharing']}-sharing, so this student's sharing will change from {student['sharing']} to {plan['new_sharing']}."
        note_te = f" ఆ గది {plan['new_sharing']}-షేరింగ్ కాబట్టి ఈ విద్యార్థి షేరింగ్ {student['sharing']} నుండి {plan['new_sharing']} కి మారుతుంది."
    en = (f"Move <b>{escape(student['name'])}</b> ({_short_contact(student['contact'])}) from <b>{escape(_loc(fh, fr_))}</b> "
          f"to <b>{escape(_loc(*plan['dst']))}</b>? Rent and payments stay unchanged.{note_en}")
    te = (f"<b>{escape(student['name'])}</b> ({_short_contact(student['contact'])}) ను <b>{escape(_loc(fh, fr_, 'te'))}</b> నుండి "
          f"<b>{escape(_loc(*plan['dst'], 'te'))}</b> కి మార్చాలా? అద్దె, చెల్లింపులు మారవు.{note_te}")
    chips = [{'label': tr(lang, 'Yes, shift', 'అవును, మార్చు'), 'cls': 'primary', 'action': {'confirm': token}},
             {'label': tr(lang, 'Cancel', 'రద్దు'), 'action': {'cancel': True}}]
    return _reply(lang, tr(lang, en, te), chips=chips)


def _do_shift(db, ctx, lang, pending):
    row = db.execute('SELECT * FROM students WHERE id=%s', (pending['sid'],)).fetchone()
    if not row:
        return _reply(lang, escape(tr(lang, 'That student was not found any more. Nothing changed.', 'ఆ విద్యార్థి ఇకపై కనిపించడం లేదు. ఏమీ మారలేదు.')))
    student = dict(row)
    plan, err = _plan_shift(db, ctx, student, tuple(pending['dst']), lang)  # re-validate at the moment of the change
    if err:
        return _reply(lang, escape(err + tr(lang, ' Nothing was changed.', ' ఏమీ మారలేదు.')))
    from datetime import datetime
    # Same columns the dashboard "Edit" form writes for a room change; rent / payments / photo are untouched.
    db.execute('UPDATE students SET hostel=%s, room_number=%s, sharing=%s, updated_at=%s WHERE id=%s',
               (plan['dst'][0], plan['dst'][1], plan['new_sharing'], datetime.utcnow().isoformat(), student['id']))
    db.commit()
    where = _loc(*plan['dst'], lang)
    return _reply(lang, tr(lang,
                           f"Done ✅ <b>{escape(student['name'])}</b> is now in <b>{escape(where)}</b>.<br><span style=\"color:#64748B;font-size:.8rem\">Reload the dashboard to refresh the room list.</span>",
                           f"పూర్తయింది ✅ <b>{escape(student['name'])}</b> ఇప్పుడు <b>{escape(where)}</b> లో ఉన్నారు.<br><span style=\"color:#64748B;font-size:.8rem\">గదుల జాబితా కోసం డ్యాష్‌బోర్డ్‌ను రీలోడ్ చేయండి.</span>"))


def _handle_shift(db, ctx, lang, students, tokens, src, dst):
    if not dst:
        return _reply(lang, escape(tr(lang, 'Which room should I shift to? Please say the hostel and room number.',
                                      'ఏ గదికి మార్చాలి? హాస్టల్ మరియు రూమ్ నంబర్ చెప్పండి.')))
    if any(h is None for h in dst):
        return _reply(lang, escape(tr(lang, 'Which hostel is the destination room in: Old Hostel or New Hostel?',
                                      'గమ్యం గది ఏ హాస్టల్‌లో ఉంది: ఓల్డ్ హాస్టల్ లేదా న్యూ హాస్టల్?')))
    matches = match_students(students, tokens)
    if not matches:
        return _reply(lang, escape(tr(lang, 'I could not find a resident with that name. Please check the name.',
                                      'ఆ పేరుతో విద్యార్థి కనిపించలేదు. దయచేసి పేరు సరిచూడండి.')))
    if src:
        if src[0] is None:
            return _reply(lang, escape(tr(lang, 'Which hostel is the current room in?', 'ప్రస్తుత గది ఏ హాస్టల్‌లో ఉంది?')))
        in_src = [m for m in matches if (m[1]['hostel'], m[1]['room_number']) == tuple(src)]
        if not in_src:
            where = '; '.join(f"{m[1]['name']} - {_loc(m[1]['hostel'], m[1]['room_number'], lang)}" for m in matches[:4])
            return _reply(lang, escape(tr(lang, f'No resident with that name is in {_loc(*src)}. Matches found: {where}.',
                                          f'{_loc(*src, "te")} లో ఆ పేరుతో ఎవరూ లేరు. దొరికినవి: {where}.')))
        matches = in_src
    best = matches[0][0]
    top = [m[1] for m in matches if m[0] == best]
    if len(top) > 1:
        token = secrets.token_hex(8)
        session['ai_pending'] = {'t': token, 'kind': 'choose', 'ids': [s['id'] for s in top[:6]], 'dst': list(dst), 'exp': time.time() + PENDING_TTL}
        chips = [{'label': f"{s['name']} - {_loc(s['hostel'], s['room_number'], lang)} ({_short_contact(s['contact'])})",
                  'action': {'choose': s['id'], 'token': token}} for s in top[:6]]
        chips.append({'label': tr(lang, 'Cancel', 'రద్దు'), 'action': {'cancel': True}})
        return _reply(lang, escape(tr(lang, 'More than one resident matches. Which one do you mean?', 'ఒకటి కంటే ఎక్కువ మంది సరిపోతున్నారు. ఎవరిని ఉద్దేశించారు?')), chips=chips)
    return _shift_preview(db, ctx, lang, top[0], tuple(dst))


# ---- main admin entry ----------------------------------------------------

def _llm_admin_intent(message):
    res = _llm_json(
        'Convert a hostel admin message (English or Telugu) into JSON. Keys: "intent" one of '
        '"shift","vacancy","room_members","student_info","totals","other"; "student" (name as spoken, or ""); '
        '"from_hostel","from_room","to_hostel","to_room" (hostel is "Old Hostel" or "New Hostel"; room is an integer, Hall=11; '
        'use null if not said); "hostel","room" for vacancy/room_members. Reply with JSON only.', message)
    return res if isinstance(res, dict) else None


def admin_reply(db, ctx, payload):
    pending = session.get('ai_pending')
    if pending and pending.get('exp', 0) < time.time():
        session.pop('ai_pending', None)
        pending = None

    message = (payload.get('message') or '').strip()[:400]
    if message:
        lang = 'te' if has_telugu(message) else 'en'      # replies follow the language the admin wrote in
    else:
        lang = payload.get('lang') if payload.get('lang') in ('en', 'te') else 'en'

    # --- button actions ---
    if payload.get('cancel'):
        session.pop('ai_pending', None)
        return _reply(lang, escape(tr(lang, 'Okay, nothing was changed.', 'సరే, ఏమీ మార్చలేదు.')))
    if payload.get('confirm'):
        if not pending or pending.get('kind') != 'shift' or not secrets.compare_digest(str(payload['confirm']), pending['t']):
            return _reply(lang, escape(tr(lang, 'That request has expired. Please give the command again.', 'ఆ అభ్యర్థన గడువు ముగిసింది. దయచేసి కమాండ్ మళ్లీ ఇవ్వండి.')))
        session.pop('ai_pending', None)
        return _do_shift(db, ctx, lang, pending)
    if payload.get('choose') is not None:
        try:
            sid = int(payload['choose'])
        except (TypeError, ValueError):
            sid = None
        if (not pending or pending.get('kind') != 'choose' or sid not in pending['ids']
                or not secrets.compare_digest(str(payload.get('token', '')), pending['t'])):
            return _reply(lang, escape(tr(lang, 'That request has expired. Please give the command again.', 'ఆ అభ్యర్థన గడువు ముగిసింది. దయచేసి కమాండ్ మళ్లీ ఇవ్వండి.')))
        session.pop('ai_pending', None)
        row = db.execute('SELECT * FROM students WHERE id=%s', (sid,)).fetchone()
        if not row:
            return _reply(lang, escape(tr(lang, 'That student was not found.', 'ఆ విద్యార్థి కనిపించలేదు.')))
        return _shift_preview(db, ctx, lang, dict(row), tuple(pending['dst']))

    if not message:
        return _reply(lang, escape(tr(lang, 'Please type or say something.', 'దయచేసి ఏదైనా టైప్ చేయండి లేదా చెప్పండి.')))

    tokens = tokenize(message)
    raw = message.lower()
    joined = ' '.join(tokens)

    # --- typed yes / no for a pending confirmation ---
    if pending and pending.get('kind') == 'shift' and len(tokens) <= 3:
        said_no = any(t in NO_WORDS for t in tokens) or any(w in raw for w in NO_WORDS if has_telugu(w))
        said_yes = (any(t in YES_WORDS for t in tokens) or 'do it' in raw or any(w in raw for w in YES_WORDS if has_telugu(w)))
        if said_yes and not said_no:
            session.pop('ai_pending', None)
            return _do_shift(db, ctx, lang, pending)
        if said_no:
            session.pop('ai_pending', None)
            return _reply(lang, escape(tr(lang, 'Okay, nothing was changed.', 'సరే, ఏమీ మార్చలేదు.')))

    # --- never reveal secrets / credentials / configuration ---
    if _ADMIN_SECRETS.search(message):
        return _reply(lang, escape(tr(lang, "I can't show passwords, keys or configuration details. I can help with hostel data and features.",
                                      'పాస్‌వర్డ్‌లు, కీలు లేదా కాన్ఫిగరేషన్ వివరాలను నేను చూపించలేను. హాస్టల్ డేటా, ఫీచర్లలో సహాయం చేయగలను.')))

    students = [dict(r) for r in db.execute('SELECT * FROM students').fetchall()]
    mentions = parse_locations(tokens)
    src, dst = split_src_dst(mentions)

    is_shift = any(t in SHIFT_WORDS for t in tokens) or any(w in raw for w in SHIFT_TE) or (
        'change' in tokens and mentions)
    is_delete = (any(t in DELETE_WORDS for t in tokens) or any(w in raw for w in DELETE_TE)) and not is_shift

    if is_shift:
        return _handle_shift(db, ctx, lang, students, tokens, src, dst)

    if is_delete:
        return _reply(lang, escape(tr(
            lang, "For safety I don't delete residents by chat. Use menu option '2) Make Empty' here, or the Delete button on the student card; both ask you to confirm.",
            "భద్రత కోసం చాట్ ద్వారా విద్యార్థులను తొలగించను. ఇక్కడ మెనూలోని '2) Make Empty' లేదా విద్యార్థి కార్డ్‌లోని Delete బటన్ ఉపయోగించండి; రెండూ నిర్ధారణ అడుగుతాయి.")))

    hostel_words = {h for h in (_hostel_of(t) for t in tokens) if h}
    only_hostel = next(iter(hostel_words)) if len(hostel_words) == 1 else None
    asks_vacancy = any(k in joined for k in ('vacan', 'empty', 'free', 'available')) or 'ఖాళీ' in raw

    # Feature / how-to questions (including per-day rent rates) are answered from the admin knowledge base.
    kb = _admin_kb_reply(message, lang, ctx)
    if kb:
        return kb

    if asks_vacancy:
        if len(mentions) == 1 and mentions[0]['hostel']:
            return _ans_room(db, ctx, lang, mentions[0]['hostel'], mentions[0]['room'])
        return _ans_vacancy(db, ctx, lang, only_hostel)

    if len(mentions) == 1 and mentions[0]['hostel']:
        m = mentions[0]
        return _ans_room(db, ctx, lang, m['hostel'], m['room'])
    if len(mentions) == 1 and not mentions[0]['hostel']:
        return _reply(lang, escape(tr(lang, 'Which hostel: Old Hostel or New Hostel?', 'ఏ హాస్టల్: ఓల్డ్ హాస్టల్ లేదా న్యూ హాస్టల్?')))

    totals_kw = ('total' in tokens or 'dues' in tokens or 'due' in tokens or 'balance' in tokens or 'how many students' in joined
                 or 'students' in tokens or 'మొత్తం' in raw or 'బాకీ' in raw or 'బ్యాలెన్స్' in raw or 'ఎంత మంది' in raw)
    matches = match_students(students, tokens)
    if matches and not (totals_kw and ('total' in tokens or 'మొత్తం' in raw)):
        return _ans_students(lang, matches)
    if totals_kw:
        return _ans_totals(db, ctx, lang, tokens, raw)

    # Optional LLM: only maps the sentence to the same structured intents used above.
    intent = _llm_admin_intent(message)
    if intent:
        kind = intent.get('intent')
        h = lambda v: v if v in ctx['HOSTELS'] else None
        rm = lambda v: v if isinstance(v, int) and not isinstance(v, bool) else None
        if kind == 'shift' and intent.get('student') and rm(intent.get('to_room')):
            toks = tokenize(str(intent['student']))
            s_ = (h(intent.get('from_hostel')), rm(intent.get('from_room')))
            d_ = (h(intent.get('to_hostel')) or h(intent.get('from_hostel')), rm(intent['to_room']))
            return _handle_shift(db, ctx, lang, students, toks, s_ if s_[1] else None, d_)
        if kind == 'vacancy':
            return _ans_vacancy(db, ctx, lang, h(intent.get('hostel')))
        if kind == 'room_members' and h(intent.get('hostel')) and rm(intent.get('room')):
            return _ans_room(db, ctx, lang, intent['hostel'], intent['room'])
        if kind == 'student_info' and intent.get('student'):
            mm = match_students(students, tokenize(str(intent['student'])))
            if mm:
                return _ans_students(lang, mm)
        if kind == 'totals':
            return _ans_totals(db, ctx, lang, tokens, raw)

    return _reply(lang, escape(tr(
        lang, "I didn't understand that. Try: \"How many vacant rooms are available?\", \"Who is in room 2 Old Hostel?\" or \"Shift Manohar from room 2 Old Hostel to room 3 Old Hostel\". Ask \"help\" to see more.",
        "నాకు అర్థం కాలేదు. ఇలా ప్రయత్నించండి: \"ఎన్ని గదులు ఖాళీగా ఉన్నాయి?\" లేదా \"మనోహర్ ను ఓల్డ్ హాస్టల్ రూమ్ 2 నుండి రూమ్ 3 కి మార్చండి\". మరిన్ని చూడటానికి \"సహాయం\" అని అడగండి.")))


# ==========================================================================
# ROUTES
# ==========================================================================

_hits = {}


def _rate_limited(key, limit=30, window=60):
    now = time.time()
    q = [t for t in _hits.get(key, []) if now - t < window]
    if len(q) >= limit:
        _hits[key] = q
        return True
    q.append(now)
    _hits[key] = q
    if len(_hits) > 5000:
        _hits.clear()
    return False


def register(app, ctx):
    """ctx: helpers from app.py (get_db, validate_csrf, admin_api_required, check_capacity, ...)."""
    admin_api_required = ctx['admin_api_required']
    validate_csrf = ctx['validate_csrf']

    @app.route('/api/assistant/user', methods=['POST'], endpoint='assistant_user_api')
    def assistant_user_api():
        if not validate_csrf(request.headers.get('X-CSRF-Token', '')):
            return jsonify({'ok': False, 'error': 'Security check failed. Please refresh the page.'}), 400
        if _rate_limited('u:' + (request.headers.get('X-Forwarded-For', request.remote_addr) or '?').split(',')[0].strip()):
            return jsonify({'ok': False, 'error': 'Too many questions. Please wait a minute and try again.'}), 429
        body = request.get_json(silent=True) or {}
        personal = None
        try:
            # Only when a student has signed in with their mobile number; only their OWN record is read.
            if session.get('payment_student_id'):
                db = ctx['get_db']()
                st = ctx['current_payment_student'](db)
                if st:
                    subs = db.execute('SELECT amount, status, submitted_at FROM payment_submissions WHERE student_id=%s ORDER BY id DESC LIMIT 3',
                                      (st['id'],)).fetchall()
                    pend = [x for x in subs if x['status'] == 'PENDING']
                    personal = {
                        'hostel': st['hostel'], 'room': (ctx.get('room_label') or (lambda n: 'Hall' if n == 11 else f'Room {n}'))(st['room_number']), 'sharing': st['sharing'],
                        'total_rent': st.get('total_rent') or 0, 'paid': st.get('amount_paid') or 0,
                        'due': ctx['student_due_amount'](st),
                        'pending_count': len(pend), 'pending_amount': sum(x['amount'] or 0 for x in pend),
                        'last': [{'amount': x['amount'], 'status': x['status'], 'date': (x['submitted_at'] or '')[:10]} for x in subs],
                    }
        except Exception:
            personal = None
        return jsonify(user_reply(str(body.get('message', '')), personal))

    @app.route('/admin/api/assistant', methods=['POST'], endpoint='assistant_admin_api')
    @admin_api_required
    def assistant_admin_api():
        if not validate_csrf(request.headers.get('X-CSRF-Token', '')):
            return jsonify({'ok': False, 'error': 'Security check failed. Please refresh the page.'}), 400
        if _rate_limited('a:' + str(session.get('_csrf_token', ''))[:8], limit=60):
            return jsonify({'ok': False, 'error': 'Too many requests. Please wait a minute.'}), 429
        body = request.get_json(silent=True) or {}
        try:
            return jsonify(admin_reply(ctx['get_db'](), ctx, body))
        except Exception:
            app.logger.exception('Admin assistant failed')
            return jsonify({'ok': False, 'error': 'Something went wrong. Nothing was changed.'}), 500
