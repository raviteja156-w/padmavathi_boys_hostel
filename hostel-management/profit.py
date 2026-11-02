"""
Profit / Loss for the Hostel Management app (read-only: it never changes any existing data or rule).

How the numbers are worked out (the same definitions are used by the page AND by the AI agent):

  HOSTEL income   = rent collected (every CONFIRMED student payment dated in the month, Old + New Hostel)
                    + guest money (what guests paid, counted in the month they checked in)
  HOSTEL expense  = Expenditure entries with category "Hostel"
  HOSTEL profit   = HOSTEL income - HOSTEL expense

  CANTEEN profit  = canteen income (entered on the Expenditure page) - expenses with category "Canteen"
                    (exactly what the Expenditure page already shows)

  OVERALL profit  = HOSTEL profit + CANTEEN profit
  After Home      = OVERALL profit - expenses with category "Home"   (home / personal spending, shown separately)

Where the rent figures come from: CONFIRMED rows of payment_submissions. "Delete All Data" removes those rows
from the live table but keeps a backup copy, so the backups that are still available (not permanently deleted)
are read too; every payment is counted once (by its id).
"""
import json
import time
from datetime import datetime, timedelta

from flask import render_template, request

HOSTELS = ('Old Hostel', 'New Hostel')
_CACHE = {'t': 0.0, 'pays': None}
CACHE_SECONDS = 20


def _month_key(d):
    return d.strftime('%Y-%m')


def collect_payments(db, ctx, fresh=False):
    """Every confirmed student payment: live rows + rows kept in Delete-All backups (deduplicated by id)."""
    now = time.time()
    if not fresh and _CACHE['pays'] is not None and now - _CACHE['t'] < CACHE_SECONDS:
        return _CACHE['pays']
    seen = {}
    for r in db.execute("SELECT * FROM payment_submissions WHERE status='CONFIRMED'").fetchall():
        p = dict(r)
        seen[p['id']] = p
    try:
        for c in db.execute("SELECT snapshot FROM data_cleanups WHERE status='DELETED'").fetchall():
            snap = json.loads(c['snapshot'] or '{}')
            for p in snap.get('payments', []):
                pid = p.get('id')
                if pid is not None and pid not in seen:
                    seen[pid] = p
    except Exception:
        try:
            db.conn.rollback()
        except Exception:
            pass
    pays = list(seen.values())
    _CACHE['t'], _CACHE['pays'] = now, pays
    return pays


def _rent_by_month(db, ctx):
    pay_date = ctx['pay_date']
    out = {}
    for p in collect_payments(db, ctx):
        try:
            key = _month_key(pay_date(p))
        except Exception:
            continue
        amt = float(p.get('amount') or 0)
        mode = p.get('payment_mode') or 'Online'
        if mode == 'Cash':
            cash = amt
        elif mode == 'Online + Cash':
            cash = float(p.get('cash_amount') or 0)
        else:
            cash = 0.0
        m = out.setdefault(key, {'Old Hostel': 0.0, 'New Hostel': 0.0, 'cash': 0.0, 'count': 0})
        h = p.get('hostel') if p.get('hostel') in HOSTELS else 'Old Hostel'
        m[h] += amt
        m['cash'] += cash
        m['count'] += 1
    return out


def _guest_by_month(db):
    out = {}
    for r in db.execute('SELECT check_in, amount_paid FROM guests').fetchall():
        ci = (r['check_in'] or '')[:7]
        if len(ci) == 7:
            out[ci] = out.get(ci, 0.0) + float(r['amount_paid'] or 0)
    return out


def _expense_by_month(db):
    out = {}
    for r in db.execute("SELECT substr(expense_date,1,7) AS m, category, COALESCE(SUM(total),0) AS t "
                        "FROM expenses GROUP BY 1, 2").fetchall():
        out.setdefault(r['m'], {})[r['category']] = float(r['t'] or 0)
    return out


def _canteen_income_by_month(db):
    out = {}
    for r in db.execute("SELECT substr(income_date,1,7) AS m, COALESCE(SUM(amount),0) AS t "
                        "FROM canteen_income GROUP BY 1").fetchall():
        out[r['m']] = float(r['t'] or 0)
    return out


def _build(key, label, rent, guest, exp, canteen_in):
    old, new = rent.get('Old Hostel', 0.0), rent.get('New Hostel', 0.0)
    rent_total = old + new
    hostel_income = rent_total + guest
    hostel_exp = exp.get('Hostel', 0.0)
    canteen_exp = exp.get('Canteen', 0.0)
    home_exp = exp.get('Home', 0.0)
    hostel_net = hostel_income - hostel_exp
    canteen_net = canteen_in - canteen_exp
    overall = hostel_net + canteen_net
    r2 = lambda v: round(v, 2)
    return {
        'key': key, 'label': label,
        'rent_old': r2(old), 'rent_new': r2(new), 'rent_total': r2(rent_total),
        'rent_cash': r2(rent.get('cash', 0.0)), 'rent_online': r2(rent_total - rent.get('cash', 0.0)),
        'payments_count': rent.get('count', 0),
        'guest_income': r2(guest),
        'hostel_income': r2(hostel_income), 'hostel_expense': r2(hostel_exp), 'hostel_net': r2(hostel_net),
        'canteen_income': r2(canteen_in), 'canteen_expense': r2(canteen_exp), 'canteen_net': r2(canteen_net),
        'home_expense': r2(home_exp),
        'overall_net': r2(overall), 'overall_after_home': r2(overall - home_exp),
        'total_income': r2(hostel_income + canteen_in),
        'total_expense': r2(hostel_exp + canteen_exp + home_exp),
    }


def _all_data(db, ctx):
    return _rent_by_month(db, ctx), _guest_by_month(db), _expense_by_month(db), _canteen_income_by_month(db)


def month_report(db, ctx, key, data=None):
    rent, guest, exp, cin = data or _all_data(db, ctx)
    return _build(key, ctx['cycle_label'](key),
                  rent.get(key, {}), guest.get(key, 0.0), exp.get(key, {}), cin.get(key, 0.0))


def all_time_report(db, ctx, data=None):
    rent, guest, exp, cin = data or _all_data(db, ctx)
    keys = sorted(set(rent) | set(guest) | set(exp) | set(cin))
    tot_rent = {'Old Hostel': 0.0, 'New Hostel': 0.0, 'cash': 0.0, 'count': 0}
    for k in keys:
        for f in tot_rent:
            tot_rent[f] += rent.get(k, {}).get(f, 0)
    tot_exp = {}
    for k in keys:
        for c, v in exp.get(k, {}).items():
            tot_exp[c] = tot_exp.get(c, 0.0) + v
    rep = _build('all', 'All time' + (f' ({ctx["cycle_label"](keys[0])} - {ctx["cycle_label"](keys[-1])})' if keys else ''),
                 tot_rent, sum(guest.values()), tot_exp, sum(cin.values()))
    rep['months'] = len(keys)
    return rep


def trend(db, ctx, months=6, data=None):
    """The last `months` calendar months, oldest first (ending with the current month)."""
    data = data or _all_data(db, ctx)
    today = ctx['today_ist']()
    y, m = today.year, today.month
    keys = []
    for _ in range(months):
        keys.append(f'{y:04d}-{m:02d}')
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    keys.reverse()
    return [month_report(db, ctx, k, data) for k in keys]


def resolve_month(raw, ctx):
    """'this' / 'last' / 'YYYY-MM' -> 'YYYY-MM' (None if it cannot be understood)."""
    today = ctx['today_ist']()
    raw = (raw or 'this').strip().lower()
    if raw in ('this', 'current', 'now', ''):
        return _month_key(today)
    if raw in ('last', 'previous', 'prev'):
        first = today.replace(day=1)
        return _month_key(first.replace(day=1) - timedelta(days=1))
    try:
        return datetime.strptime(raw, '%Y-%m').strftime('%Y-%m')
    except ValueError:
        return None


def report_for(db, ctx, which):
    """Used by the AI agent: which = 'this' | 'last' | 'YYYY-MM' | 'all' | 'trend'."""
    which = (which or 'this').strip().lower()
    data = _all_data(db, ctx)
    if which == 'all':
        return all_time_report(db, ctx, data)
    if which == 'trend':
        return {'months': trend(db, ctx, 6, data)}
    key = resolve_month(which, ctx)
    if not key:
        return {'error': 'Month must be this, last, all, trend or YYYY-MM'}
    return month_report(db, ctx, key, data)


def register(app, ctx):
    login_required = ctx['login_required']
    get_db = ctx['get_db']

    @app.route('/admin/profit-loss', endpoint='profit_loss')
    @login_required
    def profit_loss_page():
        db = get_db()
        today = ctx['today_ist']()
        this_key = _month_key(today)
        last_key = resolve_month('last', ctx)
        key = resolve_month(request.args.get('month') or ('last' if request.args.get('m') == 'last' else 'this'), ctx) or this_key
        data = _all_data(db, ctx)
        purged = 0
        try:
            purged = db.execute("SELECT COUNT(*) AS n FROM data_cleanups WHERE status='PURGED'").fetchone()['n'] or 0
        except Exception:
            try:
                db.conn.rollback()
            except Exception:
                pass
        return render_template('profit_loss.html',
                               r=month_report(db, ctx, key, data), allt=all_time_report(db, ctx, data),
                               rows=trend(db, ctx, 6, data), key=key, this_key=this_key, last_key=last_key,
                               purged=purged)
