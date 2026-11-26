"""
Expenditure extras for the Hostel Management app (they only ADD to the app; nothing existing is changed):

  1. SEARCH      /admin/expenditure/search      search every expense / income entry of ALL months
  2. PHOTO SCAN  /admin/expenditure/scan        AI reads a bill / handwritten list photo -> fills the rows
  3. AI ANALYSER /admin/ai-analyser             where the money goes (items, categories), price rises,
                                                and how the income should be managed

Numbers are always worked out by plain code from the database (so they are exact). The AI only (a) reads
photos and (b) writes the advice / explanation around those numbers.

Switches (environment variables, all optional)
  GROQ_API_KEY         text advice + photo reading through Groq (same key the admin bot already uses)
  ANTHROPIC_API_KEY    text advice + photo reading through Claude (better at handwriting; used FIRST for photos)
  ANTHROPIC_MODEL      default claude-haiku-4-5-20251001
  GROQ_VISION_MODEL    default meta-llama/llama-4-scout-17b-16e-instruct
No key = the search and the analyser's numbers + rule-based advice still work; only photo reading and the
written AI advice are switched off (the page tells the admin).
"""
import base64
import io
import json
import logging
import os
import re
import time
from collections import Counter, defaultdict
from datetime import datetime

import requests
from flask import jsonify, render_template, request, session, url_for

import profit

log = logging.getLogger('hostel.expense_ai')

GROQ_URL = 'https://api.groq.com/openai/v1/chat/completions'
ANTHROPIC_URL = 'https://api.anthropic.com/v1/messages'
LLM_TIMEOUT = 40
MAX_ROWS = 100

PERIODS = {'this': 'This month', 'last': 'Last month', '3m': 'Last 3 months', '6m': 'Last 6 months', 'all': 'All time'}
CATEGORIES = ('Hostel', 'Canteen', 'Home')


# ==========================================================================
# LLM plumbing (Groq + Anthropic, text and vision)
# ==========================================================================

class AIUnavailable(Exception):
    pass


def _groq_key():
    return (os.environ.get('GROQ_API_KEY') or '').strip()


def _anthropic_key():
    return (os.environ.get('ANTHROPIC_API_KEY') or '').strip()


def ai_available():
    return bool(_groq_key() or _anthropic_key())


def _anthropic_call(system, user_text, image_b64=None, max_tokens=1500):
    content = []
    if image_b64:
        content.append({'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': image_b64}})
    content.append({'type': 'text', 'text': user_text})
    body = {'model': os.environ.get('ANTHROPIC_MODEL', 'claude-haiku-4-5-20251001'), 'max_tokens': max_tokens,
            'system': system, 'messages': [{'role': 'user', 'content': content}]}
    try:
        r = requests.post(ANTHROPIC_URL, json=body, timeout=LLM_TIMEOUT,
                          headers={'x-api-key': _anthropic_key(), 'anthropic-version': '2023-06-01',
                                   'content-type': 'application/json'})
    except requests.RequestException as e:
        raise AIUnavailable('network: ' + e.__class__.__name__)
    if r.status_code == 200:
        parts = (r.json() or {}).get('content') or []
        return ''.join(p.get('text', '') for p in parts if p.get('type') == 'text')
    if r.status_code in (401, 403):
        raise AIUnavailable('The Anthropic key was rejected. Check ANTHROPIC_API_KEY.')
    if r.status_code == 429:
        raise AIUnavailable('The AI is busy right now. Wait a minute and try again.')
    raise AIUnavailable(f'Anthropic error {r.status_code}')


def _groq_models(vision):
    if vision:
        first = (os.environ.get('GROQ_VISION_MODEL') or '').strip()
        chain = [first, 'meta-llama/llama-4-scout-17b-16e-instruct', 'meta-llama/llama-4-maverick-17b-128e-instruct']
    else:
        try:
            import agent                      # re-use the model list the admin bot already keeps up to date
            chain = list(agent._model_order())
        except Exception:
            chain = ['llama-3.3-70b-versatile', 'openai/gpt-oss-120b']
    out = []
    for m in chain:
        if m and m not in out:
            out.append(m)
    return out


def _groq_call(system, user_text, image_b64=None, max_tokens=1500):
    if image_b64:
        user = [{'type': 'text', 'text': user_text},
                {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + image_b64}}]
    else:
        user = user_text
    last = 'no model available'
    for model in _groq_models(bool(image_b64)):
        body = {'model': model, 'temperature': 0.1, 'max_tokens': max_tokens,
                'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]}
        try:
            r = requests.post(GROQ_URL, json=body, timeout=LLM_TIMEOUT,
                              headers={'Authorization': 'Bearer ' + _groq_key(), 'Content-Type': 'application/json'})
        except requests.RequestException as e:
            raise AIUnavailable('network: ' + e.__class__.__name__)
        if r.status_code == 200:
            return (((r.json() or {}).get('choices') or [{}])[0].get('message') or {}).get('content') or ''
        try:
            err = (r.json() or {}).get('error') or {}
        except ValueError:
            err = {}
        low = (str(err.get('code') or '') + ' ' + str(err.get('message') or '')).lower()
        last = f'Groq error {r.status_code}'
        if r.status_code in (400, 404) and ('decommission' in low or 'model_not_found' in low
                                            or 'does not exist' in low or 'terminated' in low):
            continue                          # that model was retired: try the next one
        if r.status_code in (401, 403):
            raise AIUnavailable('The Groq key was rejected. Check GROQ_API_KEY.')
        if r.status_code == 429:
            raise AIUnavailable('The AI is busy right now. Wait a minute and try again.')
        raise AIUnavailable(last)
    raise AIUnavailable(last)


def call_llm(system, user_text, image_b64=None, max_tokens=1500):
    """Photos go to Claude first (best at handwriting), text goes to Groq first. The other one is the back-up."""
    order = []
    if image_b64:
        order = [('anthropic', _anthropic_call)] if _anthropic_key() else []
        order += [('groq', _groq_call)] if _groq_key() else []
    else:
        order = [('groq', _groq_call)] if _groq_key() else []
        order += [('anthropic', _anthropic_call)] if _anthropic_key() else []
    if not order:
        raise AIUnavailable('no_key')
    last = None
    for name, fn in order:
        try:
            return fn(system, user_text, image_b64, max_tokens)
        except AIUnavailable as e:
            log.warning('AI provider %s failed: %s', name, e)
            last = e
    raise last


def _json_from(text):
    m = re.search(r'\{.*\}', text or '', flags=re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


_HITS = {}


def _rate_ok(kind, limit, per=3600):
    key = (session.get('_csrf_token') or request.remote_addr or 'x', kind)
    now = time.time()
    hits = [t for t in _HITS.get(key, []) if now - t < per]
    if len(hits) >= limit:
        _HITS[key] = hits
        return False
    hits.append(now)
    _HITS[key] = hits
    if len(_HITS) > 500:
        for k in [k for k, v in _HITS.items() if not v or now - v[-1] > per]:
            _HITS.pop(k, None)
    return True


def _num(v):
    try:
        if v is None or v == '':
            return None
        if isinstance(v, str):
            v = v.replace(',', '').replace('₹', '').strip()
        f = float(v)
        return f if f == f and abs(f) < 1e9 else None
    except (TypeError, ValueError):
        return None


def _s(v, n=200):
    return str(v or '').strip()[:n]


# ==========================================================================
# 1) PHOTO SCAN
# ==========================================================================

def _prepare_image(file_storage):
    """Photo -> upright JPEG, at most 1600 px, small enough for the AI. Returns base64 text (or raises ValueError)."""
    from PIL import Image, ImageOps
    try:
        img = Image.open(file_storage.stream)
        img.load()
    except Exception:
        raise ValueError('That file is not a picture I can open. Please use a JPG or PNG photo.')
    img = ImageOps.exif_transpose(img)
    if img.mode not in ('RGB', 'L'):
        bg = Image.new('RGB', img.size, (255, 255, 255))
        try:
            bg.paste(img.convert('RGBA'), mask=img.convert('RGBA').split()[-1])
            img = bg
        except Exception:
            img = img.convert('RGB')
    else:
        img = img.convert('RGB')
    img.thumbnail((1600, 1600))
    for q in (85, 75, 65, 55):
        buf = io.BytesIO()
        img.save(buf, 'JPEG', quality=q, optimize=True)
        if buf.tell() < 3_000_000:
            break
    return base64.b64encode(buf.getvalue()).decode('ascii')


SCAN_EXPENSE_SYSTEM = (
    "You read photos of shop bills, receipts, handwritten expense lists and ledger pages for a small hostel and "
    "canteen in India (text may be English, Telugu or Hindi; money is in rupees). Extract EVERY purchased line item.\n"
    "Return ONLY one JSON object, no other text:\n"
    '{"doc_type":"bill|handwritten list|other","date":"YYYY-MM-DD or null","bill_total":number or null,'
    '"items":[{"item":"short name","qty":number,"unit_price":number or null,"line_total":number or null,'
    '"category":"Canteen|Hostel|Home"}],"notes":"one short line about anything unclear"}\n'
    "Rules: qty is 1 when not written; if the unit matters put it in the name (e.g. \"Rice (kg)\"); never invent "
    "items; skip subtotal / GST / tax / discount / grand-total lines (but put the grand total in bill_total); "
    "if a number cannot be read set it to null instead of guessing; dates are written day/month/year; "
    "category: kitchen food, vegetables, groceries, gas, milk -> Canteen; electricity, water, wifi, repairs, "
    "cleaning, furniture, rent, salaries, bed/room things -> Hostel; personal or family things -> Home."
)

SCAN_INCOME_SYSTEM = (
    "You read photos of handwritten sales sheets, collection lists and ledger pages for a small hostel and canteen "
    "in India (English, Telugu or Hindi; rupees). Extract EVERY money-received line.\n"
    "Return ONLY one JSON object, no other text:\n"
    '{"doc_type":"sheet|other","date":"YYYY-MM-DD or null","total":number or null,'
    '"items":[{"note":"short description","amount":number,"section":"Hostel|Canteen|Home"}],"notes":"one short line"}\n'
    "Rules: never invent lines; skip total lines (put the total in total); unreadable numbers -> skip the line; "
    "canteen sales -> Canteen; hostel fees, hall rent, other hostel receipts -> Hostel; personal/family -> Home."
)


def _clean_date(raw, ctx):
    d = ctx['parse_iso_date'](raw) if raw else None
    if d and d <= ctx['today_ist']() and d.year >= ctx['today_ist']().year - 2:
        return d.isoformat()
    return None


def _clean_expense_rows(items, default_cat, use_ai_cat):
    rows = []
    for it in (items or [])[:MAX_ROWS]:
        if not isinstance(it, dict):
            continue
        name = _s(it.get('item'), 120)
        qty = _num(it.get('qty'))
        qty = qty if qty and qty > 0 else 1.0
        price, line = _num(it.get('unit_price')), _num(it.get('line_total'))
        if line is not None and line <= 0:
            line = None
        if price is not None and price <= 0:
            price = None
        if price is None and line is not None:
            price = line / qty
        elif price is not None and line is not None and abs(price * qty - line) > max(2.0, 0.03 * line):
            price = line / qty                # the printed line total is more reliable than a misread unit price
        if not name or price is None:
            continue
        cat = it.get('category') if (use_ai_cat and it.get('category') in CATEGORIES) else default_cat
        rows.append({'item': name, 'qty': round(qty, 2), 'price': round(price, 2), 'category': cat})
    return rows


def _clean_income_rows(items, default_sec, use_ai_cat):
    rows = []
    for it in (items or [])[:MAX_ROWS]:
        if not isinstance(it, dict):
            continue
        amt = _num(it.get('amount'))
        if amt is None or amt <= 0:
            continue
        sec = it.get('section') if (use_ai_cat and it.get('section') in CATEGORIES) else default_sec
        rows.append({'note': _s(it.get('note'), 120), 'amount': round(amt, 2), 'section': sec})
    return rows


# ==========================================================================
# 2) SEARCH
# ==========================================================================

def _all_entries(db, ctx):
    """Every expense and income entry (all months) in one list, newest first."""
    pretty = lambda d: (ctx['parse_iso_date'](d).strftime('%d %b %Y %B') if ctx['parse_iso_date'](d) else '')
    out = []
    for r in db.execute('SELECT * FROM expenses ORDER BY expense_date DESC, id DESC LIMIT 5000').fetchall():
        e = dict(r)
        q = e.get('qty') or 0
        out.append({'type': 'expense', 'kind': 'expense', 'id': e['id'], 'date': e['expense_date'], 'section': e['category'],
                    'label': e['item'], 'detail': f"{q:g} \u00d7 \u20b9{(e.get('price') or 0):,.0f}", 'amount': e.get('total') or 0})
    for r in db.execute('SELECT * FROM canteen_income ORDER BY income_date DESC, id DESC LIMIT 5000').fetchall():
        e = dict(r)
        out.append({'type': 'income', 'kind': 'canteen', 'id': e['id'], 'date': e['income_date'], 'section': 'Canteen',
                    'label': e.get('note') or 'Canteen income', 'detail': '', 'amount': e.get('amount') or 0})
    try:
        for r in db.execute('SELECT * FROM other_income ORDER BY income_date DESC, id DESC LIMIT 5000').fetchall():
            e = dict(r)
            out.append({'type': 'income', 'kind': 'other', 'id': e['id'], 'date': e['income_date'], 'section': e['category'],
                        'label': e.get('note') or f"{e['category']} income", 'detail': '', 'amount': e.get('amount') or 0})
    except Exception:
        try:
            db.conn.rollback()
        except Exception:
            pass
    for e in out:
        e['_hay'] = ' '.join([str(e['label']), e['section'], e['date'] or '', pretty(e['date']), e['type'],
                              'income in' if e['type'] == 'income' else 'expense out spent',
                              str(int(round(e['amount']))), str(e['detail'])]).lower()
    out.sort(key=lambda x: (x['date'] or '', x['id']), reverse=True)
    return out


_OP = re.compile(r'^\s*(>=|<=|>|<)\s*([\d,]+(?:\.\d+)?)\s*$')


def _matches(e, q):
    m = _OP.match(q)
    if m:
        v = float(m.group(2).replace(',', ''))
        a = e['amount'] or 0
        return {'>': a > v, '<': a < v, '>=': a >= v, '<=': a <= v}[m.group(1)]
    return all(tok in e['_hay'] for tok in q.lower().split())


# ==========================================================================
# 3) AI ANALYSER - exact numbers
# ==========================================================================

def _norm_item(name):
    s = re.sub(r'[^\w\s]', ' ', (name or '').lower())
    s = re.sub(r'\b\d+(?:\.\d+)?\s*(?:kgs?|gms?|g|ltrs?|litres?|liters?|l|pcs?|nos)\b', ' ', s)
    words = [w[:-1] if len(w) > 3 and w.endswith('s') and not w.endswith('ss') else w for w in s.split()]
    return ' '.join(words) or (name or '').strip().lower()


def _month_range(first, last):
    y, m = int(first[:4]), int(first[5:7])
    ly, lm = int(last[:4]), int(last[5:7])
    out = []
    while (y, m) <= (ly, lm) and len(out) < 60:
        out.append(f'{y:04d}-{m:02d}')
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def _keys_for(period, data, today):
    this_key = today.strftime('%Y-%m')
    y, m = today.year, today.month

    def back(n):
        ks, yy, mm = [], y, m
        for _ in range(n):
            ks.append(f'{yy:04d}-{mm:02d}')
            mm -= 1
            if mm == 0:
                yy, mm = yy - 1, 12
        return ks[::-1]

    if period == 'this':
        return [this_key]
    if period == 'last':
        return [back(2)[0]]
    if period == '3m':
        return back(3)
    if period == '6m':
        return back(6)
    seen = sorted(set().union(*[set(d) for d in data]))
    return _month_range(seen[0] if seen else this_key, this_key)[-36:]


def _pct(part, whole):
    return round(part * 100.0 / whole, 1) if whole else 0.0


def build_analysis(db, ctx, period='this'):
    period = period if period in PERIODS else 'this'
    today = ctx['today_ist']()
    data = profit._all_data(db, ctx)
    keys = _keys_for(period, data, today)
    first, last = keys[0], keys[-1]
    reps = [profit.month_report(db, ctx, k, data) for k in keys]
    sumf = lambda f: round(sum(r[f] for r in reps), 2)

    # ---- exact spending rows of the period ----
    rows = [dict(r) for r in db.execute(
        "SELECT category, item, qty, price, total, expense_date FROM expenses "
        "WHERE substr(expense_date,1,7) >= %s AND substr(expense_date,1,7) <= %s ORDER BY expense_date, id",
        (first, last)).fetchall()]
    total_exp = round(sum(r['total'] or 0 for r in rows), 2)

    cat_tot = {c: 0.0 for c in CATEGORIES}
    items = {}
    for r in rows:
        tot = r['total'] or 0
        cat_tot[r['category']] = cat_tot.get(r['category'], 0.0) + tot
        k = _norm_item(r['item'])
        it = items.setdefault(k, {'names': Counter(), 'total': 0.0, 'qty': 0.0, 'times': 0, 'cats': Counter(), 'buys': []})
        it['names'][(r['item'] or '').strip()] += 1
        it['total'] += tot
        it['qty'] += r['qty'] or 0
        it['times'] += 1
        it['cats'][r['category']] += tot
        it['buys'].append((r['expense_date'], r['price'] or 0))

    item_list, price_rises = [], []
    for it in items.values():
        name = it['names'].most_common(1)[0][0]
        avg = it['total'] / it['qty'] if it['qty'] else 0
        rec = {'name': name, 'total': round(it['total'], 2), 'percent': _pct(it['total'], total_exp), 'times': it['times'],
               'qty': round(it['qty'], 2), 'avg_price': round(avg, 2), 'category': it['cats'].most_common(1)[0][0],
               'save5': round(it['total'] * 0.05, 2)}
        item_list.append(rec)
        buys = sorted(it['buys'])
        if len(buys) >= 2 and buys[0][1] > 0:
            ch = (buys[-1][1] - buys[0][1]) / buys[0][1]
            if ch >= 0.10:
                price_rises.append({'name': name, 'from': round(buys[0][1], 2), 'to': round(buys[-1][1], 2),
                                    'percent': round(ch * 100, 1)})
    item_list.sort(key=lambda x: x['total'], reverse=True)
    price_rises.sort(key=lambda x: x['percent'], reverse=True)

    categories = [{'name': c, 'total': round(cat_tot.get(c, 0.0), 2), 'percent': _pct(cat_tot.get(c, 0.0), total_exp)}
                  for c in CATEGORIES]
    categories.sort(key=lambda x: x['total'], reverse=True)

    # ---- usual spending (average of up to 3 months before the period) vs now ----
    exp_by_month = data[2]
    earlier = sorted(k for k in exp_by_month if k < first)[-3:]
    usual = {}
    for c in CATEGORIES:
        if earlier:
            usual[c] = round(sum(exp_by_month[k].get(c, 0.0) for k in earlier) / len(earlier), 2)
    n_months = len(keys)
    compare = []
    for c in CATEGORIES:
        now_avg = round(cat_tot.get(c, 0.0) / n_months, 2)
        u = usual.get(c)
        compare.append({'name': c, 'now': now_avg, 'usual': u,
                        'change': _pct(now_avg - u, u) if u else None})

    # ---- income ----
    sources = [('Rent from students', sumf('rent_total')), ('Guest payments', sumf('guest_income')),
               ('Other hostel income', sumf('hostel_other_income')), ('Canteen income', sumf('canteen_income'))]
    op_income = round(sumf('hostel_income') + sumf('canteen_income'), 2)
    sources = [{'name': n, 'amount': a, 'percent': _pct(a, op_income)} for n, a in sources]
    home = {'income': sumf('home_income'), 'expense': sumf('home_expense'), 'net': sumf('home_net')}
    op_exp = round(sumf('hostel_expense') + sumf('canteen_expense'), 2)
    net = round(op_income - op_exp, 2)
    cost_pct = _pct(op_exp, op_income)
    hostel_margin = _pct(sumf('hostel_net'), sumf('hostel_income'))
    canteen_margin = _pct(sumf('canteen_net'), sumf('canteen_income'))

    # ---- how the income should be managed ----
    plan = None
    if op_income > 0:
        reserve_pct = 10.0
        if cost_pct + reserve_pct <= 100:
            save_pct = round(100 - cost_pct - reserve_pct, 1)
            buckets = [('Running costs (food, bills, repairs)', cost_pct), ('Reserve for repairs & emergencies', reserve_pct),
                       ('Savings / growth (new beds, upgrades)', save_pct)]
            cut = 0.0
        else:
            buckets = [('Running costs (food, bills, repairs)', min(cost_pct, 100.0)),
                       ('Reserve for repairs & emergencies', max(0.0, round(100 - cost_pct, 1))),
                       ('Savings / growth (new beds, upgrades)', 0.0)]
            cut = 0.0
        target_cost = 80.0
        cut = round(max(0.0, op_exp - op_income * target_cost / 100.0), 2)
        plan = {'buckets': [{'name': n, 'percent': round(p, 1), 'amount': round(op_income * p / 100.0, 2)} for n, p in buckets],
                'cost_pct': cost_pct, 'target_cost_pct': target_cost, 'cut_needed': cut,
                'reserve_target': round(op_exp / n_months, 2)}

    # ---- last 6 months (always shown, whatever the period) ----
    trend = [{'label': r['label'], 'key': r['key'], 'income': round(r['hostel_income'] + r['canteen_income'], 2),
              'expense': round(r['hostel_expense'] + r['canteen_expense'], 2), 'net': r['overall_net']}
             for r in profit.trend(db, ctx, 6, data)]

    a = {'period': period, 'period_label': PERIODS[period], 'first': first, 'last': last, 'months': n_months,
         'label': ctx['cycle_label'](first) if first == last else f"{ctx['cycle_label'](first)} - {ctx['cycle_label'](last)}",
         'partial': last == today.strftime('%Y-%m'),
         'income': op_income, 'expense': op_exp, 'net': net, 'cost_pct': cost_pct,
         'total_expense_all': total_exp,       # includes Home spending
         'categories': categories, 'items': item_list, 'price_rises': price_rises[:6], 'compare': compare,
         'sources': sources, 'home': home, 'plan': plan, 'trend': trend,
         'hostel_margin': hostel_margin, 'canteen_margin': canteen_margin,
         'hostel_net': sumf('hostel_net'), 'canteen_net': sumf('canteen_net'),
         'canteen_income': sumf('canteen_income'), 'canteen_expense': sumf('canteen_expense')}
    a['insights'] = rule_insights(a)
    return a


def rule_insights(a):
    """Plain-code advice (always available, no AI needed). level: good / warn / bad / info."""
    out = []
    rs = lambda v: f"\u20b9{abs(v):,.0f}"
    if not a['expense'] and not a['income']:
        return [{'level': 'info', 'text': 'No income or spending recorded for this period yet. Add entries on the Expenditure page.'}]
    verdict = 'profit' if a['net'] >= 0 else 'loss'
    out.append({'level': 'good' if a['net'] >= 0 else 'bad',
                'text': f"{a['label']}: income {rs(a['income'])}, spending {rs(a['expense'])} \u2192 {verdict} of {rs(a['net'])}."})
    if a['income'] > 0:
        if a['cost_pct'] > 100:
            out.append({'level': 'bad', 'text': f"Spending is {a['cost_pct']:.0f}% of income - you are spending more than you earn. Bring costs under 80% (cut about {rs(a['plan']['cut_needed'])})."})
        elif a['cost_pct'] > 80:
            out.append({'level': 'warn', 'text': f"Costs use {a['cost_pct']:.0f}% of income, leaving little to save. Target: below 80% (cut about {rs(a['plan']['cut_needed'])})."})
        else:
            out.append({'level': 'good', 'text': f"Costs use only {a['cost_pct']:.0f}% of income - healthy. You can set aside the rest as reserve and savings."})
    top_cat = a['categories'][0] if a['categories'] else None
    if top_cat and top_cat['total'] > 0:
        out.append({'level': 'info', 'text': f"{top_cat['name']} takes the most: {rs(top_cat['total'])} ({top_cat['percent']:.0f}% of all spending)."})
    if a['items']:
        t = a['items'][0]
        out.append({'level': 'info', 'text': f"Biggest single item: {t['name']} - {rs(t['total'])} ({t['percent']:.0f}% of spending), bought {t['times']} time(s)."})
        top3 = sum(i['percent'] for i in a['items'][:3])
        if len(a['items']) > 3 and top3 >= 40:
            names = ', '.join(i['name'] for i in a['items'][:3])
            out.append({'level': 'info', 'text': f"Just 3 items ({names}) make {top3:.0f}% of spending - bargain or buy in bulk there first."})
    for p in a['price_rises'][:3]:
        out.append({'level': 'warn', 'text': f"Price rise: {p['name']} went from \u20b9{p['from']:,.0f} to \u20b9{p['to']:,.0f} (+{p['percent']:.0f}%). Ask another shop."})
    for c in a['compare']:
        if c['usual'] and c['now'] > c['usual'] * 1.2 and c['now'] - c['usual'] > 500:
            out.append({'level': 'warn', 'text': f"{c['name']} spending is {c['change']:.0f}% above your usual {rs(c['usual'])} per month."})
    if a['canteen_expense'] > 0 and a['canteen_income'] <= 0:
        out.append({'level': 'warn', 'text': 'Canteen spending is recorded but no canteen income - add the canteen sales so the profit is correct.'})
    if a['canteen_income'] > 0 and a['canteen_net'] < 0:
        out.append({'level': 'warn', 'text': f"Canteen is running at a loss of {rs(a['canteen_net'])}. Check portion cost vs selling price."})
    if a['hostel_net'] < 0:
        out.append({'level': 'bad', 'text': f"Hostel part is at a loss of {rs(a['hostel_net'])} - check rent collection and Hostel bills."})
    if a['partial']:
        out.append({'level': 'info', 'text': 'This month is still running, so the numbers will change as more entries come in.'})
    return out


def _payload(a):
    """Small, student-free summary that is sent to the AI."""
    return {'period': a['label'], 'months_in_period': a['months'], 'month_still_running': a['partial'],
            'income_total': a['income'], 'spending_total': a['expense'], 'profit_or_loss': a['net'],
            'spending_percent_of_income': a['cost_pct'],
            'spending_by_category': [{k: c[k] for k in ('name', 'total', 'percent')} for c in a['categories']],
            'top_items': [{k: i[k] for k in ('name', 'category', 'total', 'percent', 'times', 'qty', 'avg_price')} for i in a['items'][:15]],
            'price_rises': a['price_rises'], 'income_sources': a['sources'],
            'hostel_profit': a['hostel_net'], 'canteen_profit': a['canteen_net'],
            'hostel_margin_percent': a['hostel_margin'], 'canteen_margin_percent': a['canteen_margin'],
            'home_money': a['home'], 'monthly_trend_last_6': a['trend'],
            'spending_vs_usual_per_month': a['compare'], 'rule_based_plan': a['plan']}


ADVISOR_SYSTEM = (
    "You are a calm, practical money advisor for the owner of a small hostel (with a canteen) in India. "
    "You get exact figures worked out by the app. Use ONLY those figures - never invent numbers, items or prices. "
    "Amounts are in rupees (write \u20b9). Keep sentences short and plain, like talking to a shop owner.\n"
    "Return ONLY one JSON object, no other text:\n"
    '{"summary":"2-3 sentences: how the period went",'
    '"biggest_spend":[{"what":"item or category","amount":number,"percent":number,"comment":"one short line"}],'
    '"income_plan":[{"bucket":"name","percent":number,"amount":number,"why":"one short line"}],'
    '"actions":["concrete step the owner can do this week"],'
    '"warnings":["only real risks seen in the data"]}\n'
    "biggest_spend: the 3-5 places where most money goes (items and/or categories). income_plan: 3-5 buckets that "
    "add up to 100 percent of the income (running costs, reserve, savings/growth, and so on) - start from "
    "rule_based_plan and adjust sensibly. actions: 3-6 items, each with a rupee estimate when possible. "
    "If the data is thin, say so in the summary and keep the lists short."
)


def _lang_line(lang):
    return ("Write every text value in simple Telugu (తెలుగు script). Keep numbers and the \u20b9 sign as digits."
            if lang == 'te' else "Write in simple English.")


def _clean_ai(d):
    if not isinstance(d, dict):
        return None
    out = {'summary': _s(d.get('summary'), 700), 'biggest_spend': [], 'income_plan': [], 'actions': [], 'warnings': []}
    for r in (d.get('biggest_spend') or [])[:5]:
        if isinstance(r, dict) and r.get('what'):
            out['biggest_spend'].append({'what': _s(r.get('what'), 80), 'amount': _num(r.get('amount')),
                                         'percent': _num(r.get('percent')), 'comment': _s(r.get('comment'), 200)})
    for r in (d.get('income_plan') or [])[:6]:
        if isinstance(r, dict) and r.get('bucket'):
            out['income_plan'].append({'bucket': _s(r.get('bucket'), 80), 'percent': _num(r.get('percent')),
                                       'amount': _num(r.get('amount')), 'why': _s(r.get('why'), 200)})
    out['actions'] = [_s(x, 260) for x in (d.get('actions') or [])[:6] if x]
    out['warnings'] = [_s(x, 260) for x in (d.get('warnings') or [])[:4] if x]
    return out if (out['summary'] or out['actions']) else None


# ==========================================================================
# ROUTES
# ==========================================================================

def register(app, ctx):
    login_required = ctx['login_required']
    api_required = ctx['admin_api_required']
    get_db = ctx['get_db']
    csrf_ok = lambda: ctx['validate_csrf'](request.headers.get('X-CSRF-Token', ''))

    @app.route('/admin/expenditure/search', endpoint='expense_search')
    @api_required
    def expense_search():
        q = (request.args.get('q') or '').strip()[:60]
        typ = request.args.get('type') if request.args.get('type') in ('expense', 'income') else ''
        if not q:
            return jsonify({'ok': True, 'count': 0, 'results': [], 'income': 0, 'expense': 0})
        db = get_db()
        found = [e for e in _all_entries(db, ctx) if (not typ or e['type'] == typ) and _matches(e, q)]
        res = []
        for e in found[:150]:
            month = (e['date'] or '')[:7]
            res.append({'type': e['type'], 'section': e['section'], 'label': e['label'], 'detail': e['detail'],
                        'amount': e['amount'], 'date': ctx['parse_iso_date'](e['date']).strftime('%d %b %Y') if ctx['parse_iso_date'](e['date']) else '',
                        'month': month, 'month_label': ctx['cycle_label'](month), 'url': url_for('expenditure', month=month)})
        return jsonify({'ok': True, 'count': len(found), 'shown': len(res), 'results': res,
                        'income': round(sum(e['amount'] for e in found if e['type'] == 'income'), 2),
                        'expense': round(sum(e['amount'] for e in found if e['type'] == 'expense'), 2)})

    @app.route('/admin/expenditure/scan', methods=['POST'], endpoint='expense_scan')
    @api_required
    def expense_scan():
        if not csrf_ok():
            return jsonify({'ok': False, 'error': 'Security check failed. Refresh the page and try again.'}), 403
        if not ai_available():
            return jsonify({'ok': False, 'error': 'Photo reading needs an AI key. Add GROQ_API_KEY or ANTHROPIC_API_KEY on the server.'}), 503
        if not _rate_ok('scan', 30):
            return jsonify({'ok': False, 'error': 'Too many photos in the last hour. Please wait a little.'}), 429
        f = request.files.get('photo')
        if not f or not f.filename:
            return jsonify({'ok': False, 'error': 'Choose a photo first.'}), 400
        mode = 'income' if request.form.get('mode') == 'income' else 'expense'
        default = request.form.get('category') if request.form.get('category') in CATEGORIES else ('Canteen' if mode == 'income' else 'Hostel')
        use_ai_cat = request.form.get('ai_category') == '1'
        try:
            img = _prepare_image(f)
        except ValueError as e:
            return jsonify({'ok': False, 'error': str(e)}), 400
        today = ctx['today_ist']()
        system = SCAN_INCOME_SYSTEM if mode == 'income' else SCAN_EXPENSE_SYSTEM
        try:
            text = call_llm(system, f"Today is {today.isoformat()}. Read this photo and return the JSON.", img, 2500)
        except AIUnavailable as e:
            return jsonify({'ok': False, 'error': str(e) if str(e) != 'no_key' else 'No AI key is set.'}), 502
        d = _json_from(text)
        if not d:
            return jsonify({'ok': False, 'error': 'I could not read this photo. Try a clearer, straighter, well-lit picture.'}), 422
        if mode == 'income':
            rows = _clean_income_rows(d.get('items'), default, use_ai_cat)
            total = round(sum(r['amount'] for r in rows), 2)
            stated = _num(d.get('total'))
        else:
            rows = _clean_expense_rows(d.get('items'), default, use_ai_cat)
            total = round(sum(r['qty'] * r['price'] for r in rows), 2)
            stated = _num(d.get('bill_total'))
        if not rows:
            return jsonify({'ok': False, 'error': 'No items could be read from this photo. Try a clearer picture.'}), 422
        return jsonify({'ok': True, 'mode': mode, 'rows': rows, 'sum': total, 'stated_total': stated,
                        'match': (stated is not None and abs(stated - total) <= max(2.0, 0.01 * stated)),
                        'date': _clean_date(d.get('date'), ctx), 'doc_type': _s(d.get('doc_type'), 40),
                        'notes': _s(d.get('notes'), 200)})

    @app.route('/admin/ai-analyser', endpoint='ai_analyser')
    @login_required
    def ai_analyser():
        db = get_db()
        period = request.args.get('period') if request.args.get('period') in PERIODS else 'this'
        try:
            a = build_analysis(db, ctx, period)
        except Exception:
            log.exception('analyser failed')
            try:
                db.conn.rollback()
            except Exception:
                pass
            return render_template('error.html', code=500, message='The analyser could not read the data. Please try again.'), 500
        return render_template('ai_analyser.html', a=a, periods=PERIODS, period=period, ai_on=ai_available())

    def _analysis_from_body():
        body = request.get_json(silent=True) or {}
        period = body.get('period') if body.get('period') in PERIODS else 'this'
        lang = 'te' if body.get('lang') == 'te' else 'en'
        return body, build_analysis(get_db(), ctx, period), lang

    @app.route('/admin/ai-analyser/insights', methods=['POST'], endpoint='ai_analyser_insights')
    @api_required
    def ai_analyser_insights():
        if not csrf_ok():
            return jsonify({'ok': False, 'error': 'Security check failed. Refresh the page.'}), 403
        if not ai_available():
            return jsonify({'ok': False, 'error': 'no_key'}), 503
        if not _rate_ok('insights', 40):
            return jsonify({'ok': False, 'error': 'Too many requests. Please wait a little.'}), 429
        _, a, lang = _analysis_from_body()
        try:
            text = call_llm(ADVISOR_SYSTEM + '\n' + _lang_line(lang),
                            'Here are the exact figures:\n' + json.dumps(_payload(a), ensure_ascii=False, default=str), None, 1800)
        except AIUnavailable as e:
            return jsonify({'ok': False, 'error': str(e)}), 502
        clean = _clean_ai(_json_from(text))
        if not clean:
            return jsonify({'ok': False, 'error': 'The AI answer was not readable. Please press the button again.'}), 502
        return jsonify({'ok': True, 'ai': clean})

    @app.route('/admin/ai-analyser/ask', methods=['POST'], endpoint='ai_analyser_ask')
    @api_required
    def ai_analyser_ask():
        if not csrf_ok():
            return jsonify({'ok': False, 'error': 'Security check failed. Refresh the page.'}), 403
        if not ai_available():
            return jsonify({'ok': False, 'error': 'no_key'}), 503
        if not _rate_ok('ask', 60):
            return jsonify({'ok': False, 'error': 'Too many questions. Please wait a little.'}), 429
        body, a, lang = _analysis_from_body()
        question = _s(body.get('question'), 300)
        if not question:
            return jsonify({'ok': False, 'error': 'Type a question first.'}), 400
        system = ("You are a calm, practical money advisor for the owner of a small hostel with a canteen in India. "
                  "Answer the owner's question using ONLY the exact figures given; if the figures do not contain the "
                  "answer, say so plainly. Never invent numbers. Rupees (\u20b9). Short, plain sentences, at most 8 lines, "
                  "no markdown tables. " + _lang_line(lang))
        try:
            text = call_llm(system, 'Figures:\n' + json.dumps(_payload(a), ensure_ascii=False, default=str) +
                            '\n\nQuestion: ' + question, None, 900)
        except AIUnavailable as e:
            return jsonify({'ok': False, 'error': str(e)}), 502
        return jsonify({'ok': True, 'answer': _s(text, 1800)})
