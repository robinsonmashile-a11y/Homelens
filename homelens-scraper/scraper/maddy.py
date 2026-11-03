"""
Maddy fallback scoring.

The statistical engine (scraper/engine.py) cannot score a listing that has no usable
comparison baseline. This pass runs right after the engine and gives each such listing a
score, verdict and badge from Maddy (the AI layer, data/maddy_prompt.txt, used unchanged),
written into listing_signals next to the engine's rows.

    scored_by = 'maddy', comparison_method = 'maddy_fallback'
    every statistical field (gap_pct, gap_rands, z_score, medians ...) stays NULL:
    Maddy gives a judgment, never a measured gap.

Rules
  * The engine is always primary. Maddy only writes where listing_signals has no ENGINE row
    for the listing; the engine (compute_listing_signals) overwrites a Maddy row the moment
    it can score the listing itself and never the reverse. Checked again right before writing.
  * Cached: a listing is sent to Maddy only if it has no Maddy row, or its input_hash (the
    listing's pricing-relevant fields + the prompt text) changed since her last read.
  * Cheap model (Haiku tier); tokens and estimated cost are reported every run.
  * Safety caps, same spirit as the engine's blended-baseline guardrails: with no measured
    comparison Maddy's rows never carry the two loudest claims. 'strong_deal' becomes 'deal'
    and 'excellent_deal' becomes 'good_deal' (score capped at SCORE_CAP_WHEN_CAPPED). Counted
    in the report. Set CAP_LOUD_CLAIMS = False to lift.
  * Not scored (and counted, so nothing is hidden): listings with no price, and listings the
    pipeline flagged is_outlier (price/size judged to be a data error).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger('homelens.maddy')

PROMPT_PATH = Path(__file__).resolve().parent.parent / 'data' / 'maddy_prompt.txt'
MODEL = 'claude-haiku-5-5'
PRICE_PER_MTOK_IN = 0.10          # USD, prompts up to 100K tokens
PRICE_PER_MTOK_OUT = 0.50
MAX_TOKENS = 1500
WORKERS = 10
WRITE_BATCH = 50
PAGE = 1000
IN_CHUNK = 100

SCORED_BY = 'maddy'
METHOD = 'maddy_fallback'

VERDICTS = ('strong_deal', 'deal', 'fair', 'above', 'overpriced')
BADGES = ('excellent_deal', 'good_deal', 'fair', 'above_market', 'overpriced')
CONFIDENCES = ('medium', 'low')

CAP_LOUD_CLAIMS = True
SCORE_CAP_WHEN_CAPPED = 75

CITY_NAMES = {'cape-town': 'Cape Town', 'johannesburg': 'Johannesburg'}

_LISTING_COLS = ('listing_id,city,source,title,description,suburb,suburb_normalised,listing_type,'
                 'property_type,bedrooms,bathrooms,size_m2,erf_size,price,is_furnished,views,parking,'
                 'levy_monthly,rates_monthly,pet_friendly,security,first_seen,is_luxury,is_outlier')

# Fields that change Maddy's read. first_seen / last_seen are deliberately NOT here: listing age
# moves every day and must not trigger a re-score.
_HASH_FIELDS = ('city', 'suburb_normalised', 'listing_type', 'property_type', 'bedrooms', 'bathrooms',
                'size_m2', 'erf_size', 'price', 'is_furnished', 'views', 'parking', 'levy_monthly',
                'rates_monthly', 'title', 'description')


# ── prompt / hash ─────────────────────────────────────────────────────────────

def load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding='utf-8').strip()


def prompt_version(prompt: str | None = None) -> str:
    return hashlib.sha256((prompt or load_prompt()).encode('utf-8')).hexdigest()[:8]


def input_hash(listing: dict, version: str) -> str:
    payload = {k: listing.get(k) for k in _HASH_FIELDS}
    payload['_prompt'] = version
    raw = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]


# ── listing → message ─────────────────────────────────────────────────────────

def _age_days(first_seen: Any, now: datetime) -> int | None:
    try:
        ts = datetime.fromisoformat(str(first_seen).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0, (now - ts).days)


def _rand(v) -> str:
    return 'R' + f'{int(v):,}'.replace(',', ' ')


def build_user_message(l: dict, now: datetime) -> str:
    rent = l.get('listing_type') == 'rent'
    lines = [
        f"City: {CITY_NAMES.get(l.get('city'), l.get('city') or 'unknown')}",
        f"Suburb: {l.get('suburb') or l.get('suburb_normalised') or 'unknown'}",
        f"Listing type: {'to rent (monthly rent)' if rent else 'for sale'}",
        f"Property type: {l.get('property_type') or 'unknown'}",
        f"Price: {_rand(l['price'])}{' per month' if rent else ''}",
        f"Bedrooms: {l['bedrooms'] if l.get('bedrooms') is not None else 'not stated'}",
        f"Bathrooms: {l['bathrooms'] if l.get('bathrooms') is not None else 'not stated'}",
    ]
    if l.get('size_m2'):
        lines.append(f"Floor size: {l['size_m2']:g} m2")
    if l.get('erf_size'):
        lines.append(f"Erf (plot) size: {l['erf_size']:g} m2")
    furn = l.get('is_furnished')
    lines.append(f"Furnished: {'yes' if furn is True else 'no' if furn is False else 'not stated'}")
    if l.get('views'):
        lines.append(f"Views: {l['views']}")
    if l.get('parking'):
        lines.append(f"Parking: {l['parking']}")
    if l.get('security'):
        lines.append(f"Security: {l['security']}")
    if l.get('levy_monthly'):
        lines.append(f"Monthly levy: {_rand(l['levy_monthly'])}")
    if l.get('rates_monthly'):
        lines.append(f"Monthly rates: {_rand(l['rates_monthly'])}")
    age = _age_days(l.get('first_seen'), now)
    if age is not None:
        lines.append(f"Listed: {age} days ago")
    lines.append(f"Title: {l.get('title') or ''}")
    lines.append(f"Description: {l.get('description') or 'none given'}")
    return '\n'.join(lines)


# ── reply → validated signal ──────────────────────────────────────────────────

def parse_reply(text: str) -> dict | None:
    """The JSON object in Maddy's reply (tolerates code fences / stray words around it)."""
    if not text:
        return None
    m = re.search(r'\{.*\}', text, re.S)
    if not m:
        return None
    try:
        out = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return out if isinstance(out, dict) else None


def validate_reply(r: dict) -> tuple[dict | None, str | None]:
    """Normalise a reply to the engine's vocabulary. Returns (clean, None) or (None, why)."""
    try:
        score = int(round(float(r.get('homelens_score'))))
    except (TypeError, ValueError):
        return None, 'bad score'
    if not 0 <= score <= 100:
        return None, 'score out of range'
    verdict, badge = r.get('verdict'), r.get('deal_badge')
    if verdict not in VERDICTS:
        return None, f'bad verdict {verdict!r}'
    if badge not in BADGES:
        return None, f'bad badge {badge!r}'
    # verdict and badge are two views of one judgment: more than a step apart means a muddled reply
    if abs(VERDICTS.index(verdict) - BADGES.index(badge)) > 1:
        return None, f'verdict {verdict} contradicts badge {badge}'
    conf = r.get('confidence')
    if conf == 'high':                 # her prompt forbids it; treat as her top allowed level
        conf = 'medium'
    if conf not in CONFIDENCES:
        return None, f'bad confidence {conf!r}'
    label = (r.get('verdict_label') or '').strip()
    reason = (r.get('reason') or '').strip()
    if not label or not reason:
        return None, 'missing label or reason'

    capped = False
    if CAP_LOUD_CLAIMS:
        if verdict == 'strong_deal':
            verdict, capped = 'deal', True
        if badge == 'excellent_deal':
            badge, capped = 'good_deal', True
        if capped:
            score = min(score, SCORE_CAP_WHEN_CAPPED)
            label = 'Below market'
    return {
        'homelens_score': score, 'verdict': verdict, 'verdict_label': label, 'deal_badge': badge,
        'confidence': conf, 'reason': reason,
        'what_it_really_is': (r.get('what_it_really_is') or '').strip() or None,
        'capped': capped,
    }, None


def build_signal_row(listing: dict, clean: dict, h: str, now_iso: str) -> dict:
    """Engine-shaped listing_signals row; every statistical field is left out (NULL)."""
    return {
        'listing_id':        listing['listing_id'],
        'city':              listing.get('city'),
        'suburb_normalised': listing.get('suburb_normalised'),
        'listing_type':      listing.get('listing_type') or 'sale',
        'bedrooms':          listing.get('bedrooms'),
        'property_type':     listing.get('property_type') or 'other',
        'comparison_method': METHOD,
        'listing_price':     int(listing['price']),
        'homelens_score':    clean['homelens_score'],
        'verdict':           clean['verdict'],
        'verdict_label':     clean['verdict_label'],
        'deal_badge':        clean['deal_badge'],
        'confidence':        clean['confidence'],
        'is_luxury':         bool(listing.get('is_luxury')),
        'is_outlier':        False,
        'scored_by':         SCORED_BY,
        'maddy_reason':      clean['reason'],
        'maddy_what_it_is':  clean['what_it_really_is'],
        'input_hash':        h,
        'computed_at':       now_iso,
    }


# ── database (select + upsert/delete of Maddy rows only) ──────────────────────

def _pages(make_query) -> list[dict]:
    out: list[dict] = []
    off = 0
    while True:
        page = make_query().range(off, off + PAGE - 1).execute().data or []
        out += page
        if len(page) < PAGE:
            return out
        off += PAGE


def fetch_active_listings(client) -> list[dict]:
    return _pages(lambda: client.table('listings').select(_LISTING_COLS).eq('is_active', True)
                  .order('listing_id'))


def fetch_signal_state(client) -> tuple[set[str], dict[str, str | None], bool]:
    """(ids with an ENGINE row, {id: input_hash} of Maddy rows, scored_by column exists)."""
    try:
        rows = _pages(lambda: client.table('listing_signals').select('listing_id,scored_by,input_hash')
                      .order('listing_id'))
        has_col = True
    except Exception as e:
        err = str(e).lower()
        if not ('scored_by' in err or 'input_hash' in err or 'does not exist' in err or 'pgrst204' in err
                or '42703' in err):
            raise
        rows = _pages(lambda: client.table('listing_signals').select('listing_id').order('listing_id'))
        has_col = False
    engine = {r['listing_id'] for r in rows if r.get('scored_by', 'engine') != SCORED_BY}
    maddy = {r['listing_id']: r.get('input_hash') for r in rows if r.get('scored_by') == SCORED_BY}
    return engine, maddy, has_col


def select_work(listings: list[dict], engine_ids: set[str], maddy: dict[str, str | None],
                version: str, city: str | None = None) -> dict:
    """Split active listings into: engine-scored / ineligible / unchanged Maddy / to score."""
    out = {'active': 0, 'engine': 0, 'no_price': 0, 'outlier': 0, 'unchanged': 0,
           'todo': [], 'eligible_ids': set(), 'unscored': 0}
    for l in listings:
        if city and l.get('city') != city:
            continue
        out['active'] += 1
        lid = l['listing_id']
        if lid in engine_ids:
            out['engine'] += 1
            continue
        if not l.get('price'):
            out['no_price'] += 1
            continue
        if l.get('is_outlier'):
            out['outlier'] += 1
            continue
        out['unscored'] += 1
        out['eligible_ids'].add(lid)
        h = input_hash(l, version)
        if maddy.get(lid) == h:
            out['unchanged'] += 1
        else:
            out['todo'].append((l, h))
    return out


def _pick_sample(todo: list[tuple[dict, str]], n: int) -> list[tuple[dict, str]]:
    """Small, mixed, repeatable sample: the Camps Bay ~R19,000 1-bed rental first (if present),
    then listings alternating between cities in a stable pseudo-random order."""
    def is_camps_bay_example(l):
        return (l.get('suburb_normalised') == 'camps-bay' and l.get('listing_type') == 'rent'
                and l.get('bedrooms') == 1 and 17_000 <= (l.get('price') or 0) <= 21_000)
    first = [t for t in todo if is_camps_bay_example(t[0])]
    rest = sorted((t for t in todo if t not in first),
                  key=lambda t: hashlib.md5(t[0]['listing_id'].encode()).hexdigest())
    by_city: dict[str, list] = {}
    for t in rest:
        by_city.setdefault(t[0].get('city') or '', []).append(t)
    mixed: list = []
    while any(by_city.values()):
        for c in list(by_city):
            if by_city[c]:
                mixed.append(by_city[c].pop(0))
    return (first + mixed)[:n]


def drop_listings_with_engine_rows(client, ids: list[str]) -> set[str]:
    """Re-check just before writing: ids that gained an ENGINE row since we started."""
    gained: set[str] = set()
    for i in range(0, len(ids), IN_CHUNK):
        chunk = ids[i:i + IN_CHUNK]
        try:
            rows = client.table('listing_signals').select('listing_id,scored_by').in_('listing_id', chunk).execute().data or []
        except Exception:
            return set()
        gained |= {r['listing_id'] for r in rows if r.get('scored_by', 'engine') != SCORED_BY}
    return gained


def _signals_have_city(client) -> bool:
    """True once supabase/migrations/signals_city.sql has been applied."""
    try:
        client.table('listing_signals').select('city').limit(1).execute()
        return True
    except Exception as e:
        err = str(e).lower()
        if any(k in err for k in ('pgrst204', '42703', 'schema cache', 'could not find', 'does not exist')):
            return False
        raise


def write_rows(client, rows: list[dict]) -> int:
    if not _signals_have_city(client):
        logger.warning('listing_signals.city missing — run supabase/migrations/signals_city.sql')
        rows = [{k: v for k, v in r.items() if k != 'city'} for r in rows]
    written = 0
    for i in range(0, len(rows), WRITE_BATCH):
        batch = rows[i:i + WRITE_BATCH]
        try:
            client.table('listing_signals').upsert(batch, on_conflict='listing_id').execute()
            written += len(batch)
        except Exception as e:
            logger.warning('Maddy batch write failed (%s) — retrying row by row', e)
            for r in batch:
                try:
                    client.table('listing_signals').upsert(r, on_conflict='listing_id').execute()
                    written += 1
                except Exception as e2:
                    logger.warning('Maddy row write failed (%s): %s', r.get('listing_id'), e2)
    return written


def delete_stale_maddy_rows(client, maddy_ids: list[str], eligible_ids: set[str]) -> int:
    """Maddy rows for listings that are no longer active/priced/unflagged: remove."""
    stale = [i for i in maddy_ids if i not in eligible_ids]
    for i in range(0, len(stale), IN_CHUNK):
        client.table('listing_signals').delete().eq('scored_by', SCORED_BY) \
              .in_('listing_id', stale[i:i + IN_CHUNK]).execute()
    return len(stale)


# ── the model call ────────────────────────────────────────────────────────────

def ask_maddy(ai, system: str, user: str) -> tuple[dict | None, int, int, str | None]:
    """One listing → (reply dict, input_tokens, output_tokens, error)."""
    try:
        resp = ai.messages.create(
            model=MODEL, max_tokens=MAX_TOKENS, system=system,
            messages=[{'role': 'user', 'content': user}],
            extra_body={'output_config': {'effort': 'low'}},     # a quick read, not a research task
        )
    except Exception as e:
        return None, 0, 0, f'{type(e).__name__}: {e}'
    tin, tout = resp.usage.input_tokens, resp.usage.output_tokens
    text = ''.join(b.text for b in resp.content if getattr(b, 'type', None) == 'text')
    if getattr(resp, 'stop_reason', None) == 'refusal':
        return None, tin, tout, 'refusal'
    reply = parse_reply(text)
    return (reply, tin, tout, None) if reply else (None, tin, tout, 'unparseable reply')


def estimate_cost(tokens_in: int, tokens_out: int) -> float:
    return tokens_in / 1e6 * PRICE_PER_MTOK_IN + tokens_out / 1e6 * PRICE_PER_MTOK_OUT


# ── the pass ──────────────────────────────────────────────────────────────────

def run_fallback(client, ai, limit: int | None = None, sample: int | None = None,
                 city: str | None = None, dry_run: bool = False) -> dict:
    """
    Score the active listings the engine could not. `limit` caps how many are sent to Maddy
    this run; `sample` picks a small mixed set (for a first look). `dry_run` calls Maddy but
    writes nothing. Returns the report dict (also logged by the caller).
    """
    system = load_prompt()
    version = prompt_version(system)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    listings = fetch_active_listings(client)
    engine_ids, maddy, has_col = fetch_signal_state(client)
    work = select_work(listings, engine_ids, maddy, version, city)
    if not has_col:
        logger.warning('listing_signals.scored_by missing — run supabase/migrations/maddy_fallback.sql '
                       'before writing. Dry run only.')
        dry_run = True

    todo = work['todo']
    if sample:
        todo = _pick_sample(todo, sample)
    elif limit:
        todo = todo[:limit]

    scored: list[dict] = []          # signal rows
    detail: list[dict] = []          # for the printed sample
    failures: Counter = Counter()
    tin = tout = 0
    if todo:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futs = {pool.submit(ask_maddy, ai, system, build_user_message(l, now)): (l, h) for l, h in todo}
            for fut in as_completed(futs):
                l, h = futs[fut]
                reply, i_tok, o_tok, err = fut.result()
                tin += i_tok
                tout += o_tok
                if reply is None:
                    failures[err or 'no reply'] += 1
                    continue
                clean, why = validate_reply(reply)
                if clean is None:
                    failures[why or 'invalid'] += 1
                    continue
                row = build_signal_row(l, clean, h, now_iso)
                scored.append(row)
                detail.append({**row, 'city': l.get('city'), 'suburb': l.get('suburb_normalised'),
                               'title': l.get('title'), 'price': l.get('price'),
                               'bathrooms': l.get('bathrooms'), 'size_m2': l.get('size_m2'),
                               'capped': clean['capped']})

    written = skipped_engine = removed = 0
    if scored and not dry_run:
        gained = drop_listings_with_engine_rows(client, [r['listing_id'] for r in scored])
        skipped_engine = len(gained)
        written = write_rows(client, [r for r in scored if r['listing_id'] not in gained])
    if not dry_run and not (limit or sample):
        removed = delete_stale_maddy_rows(client, list(maddy), work['eligible_ids'])

    return {
        'dry_run': dry_run, 'model': MODEL, 'prompt_version': version, 'city': city,
        'active': work['active'], 'engine_scored': work['engine'],
        'previously_unscored': work['unscored'], 'no_price': work['no_price'], 'outlier': work['outlier'],
        'skipped_unchanged': work['unchanged'], 'sent_to_maddy': len(todo),
        'maddy_scored': len(scored), 'written': written, 'lost_to_engine_row': skipped_engine,
        'capped': sum(1 for d in detail if d['capped']), 'failed': dict(failures),
        'stale_removed': removed, 'tokens_in': tin, 'tokens_out': tout,
        'est_cost_usd': round(estimate_cost(tin, tout), 4), 'rows': detail,
        'remaining_after': max(0, len(work['todo']) - len(todo)),
    }


def format_report(rep: dict, show: int = 40) -> str:
    L = [f"MADDY FALLBACK ({'DRY RUN, nothing written' if rep['dry_run'] else 'written'}"
         f"{', ' + rep['city'] if rep['city'] else ''}) model {rep['model']} prompt {rep['prompt_version']}",
         f"  total active listings         {rep['active']:>6}",
         f"  scored by the engine          {rep['engine_scored']:>6}",
         f"  previously unscored           {rep['previously_unscored']:>6}"
         f"   (+{rep['no_price']} with no price, +{rep['outlier']} flagged outlier: not scored)",
         f"  skipped, unchanged since last {rep['skipped_unchanged']:>6}",
         f"  sent to Maddy this run        {rep['sent_to_maddy']:>6}",
         f"  Maddy newly scored            {rep['maddy_scored']:>6}"
         f"   ({rep['capped']} had a 'strong/excellent' claim capped)",
         f"  written to listing_signals    {rep['written']:>6}"
         f"   ({rep['lost_to_engine_row']} dropped: engine scored them meanwhile; {rep['stale_removed']} stale rows removed)",
         f"  failed / rejected             {sum(rep['failed'].values()):>6}   {rep['failed'] or ''}",
         f"  left for a later run          {rep['remaining_after']:>6}",
         f"  tokens in/out                 {rep['tokens_in']:,} / {rep['tokens_out']:,}",
         f"  estimated cost                ${rep['est_cost_usd']:.4f}"]
    if rep['maddy_scored']:
        avg = rep['est_cost_usd'] / rep['maddy_scored']
        L.append(f"  = ${avg:.5f} per listing; all {rep['previously_unscored']} unscored would cost about "
                 f"${avg * rep['previously_unscored']:.2f}")
    rows = rep['rows']
    if rows:
        L += ['', f'SAMPLE ({min(show, len(rows))} of {len(rows)} Maddy-scored rows)']
        for d in rows[:show]:
            L.append(f"- [{d['city']}] {d['suburb']} | {d['listing_type']} {d['bedrooms']}bd {d['property_type']}"
                     f" | R{int(d['price']):,} | {d['title'][:50] if d['title'] else ''}")
            L.append(f"    score {d['homelens_score']}  verdict {d['verdict']} ({d['verdict_label']})  "
                     f"badge {d['deal_badge']}  confidence {d['confidence']}  scored_by {d['scored_by']}"
                     f"{'  [capped]' if d['capped'] else ''}")
            L.append(f"    {d['maddy_reason']}")
            if d['maddy_what_it_is']:
                L.append(f"    note: {d['maddy_what_it_is']}")
    return '\n'.join(L)
