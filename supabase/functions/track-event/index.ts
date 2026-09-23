// Homelens — track-event Edge Function
//
// Receives analytics events from the Lovable frontend and writes them to
// user_events. Uses service role key so it can bypass RLS.
//
// Deploy: supabase functions deploy track-event
//
// POPIA: rejects any event that contains personal data fields.
// Session IDs are anonymous UUIDs — not linked to any real person.

import { serve } from 'https://deno.land/std@0.168.0/http/server.ts'
import { createClient } from 'https://esm.sh/@supabase/supabase-js@2'

// ── CORS headers ─────────────────────────────────────────────
const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type',
  'Access-Control-Allow-Methods': 'POST, OPTIONS',
}

// ── Allowed event types (all others are rejected) ────────────
const ALLOWED_EVENTS = new Set([
  // Navigation
  'page_view', 'tab_switch', 'app_open', 'app_close',

  // Home screen
  'home_tile_tap',

  // Renter flow
  'rent_checker_started', 'rent_suburb_selected', 'rent_bedrooms_selected',
  'rent_amount_entered', 'rent_check_submitted', 'rent_result_viewed',
  'rent_result_above', 'rent_result_below', 'rent_increase_checked',
  'rent_letter_downloaded',

  // Buyer flow
  'buyer_suburb_chip_tapped', 'buyer_filter_opened', 'buyer_filter_applied',
  'buyer_filter_cleared', 'buyer_sort_changed', 'buyer_listing_viewed',
  'buyer_listing_scrolled', 'buyer_page_changed',

  // Investor flow
  'investor_browse_opened', 'investor_analyse_opened',
  'investor_suburb_selected', 'investor_filter_applied',
  'investor_listing_viewed', 'investor_breakdown_opened',
  'investor_bond_adjusted', 'investor_calculator_used',
  'investor_page_changed',

  // Listing detail
  'listing_opened', 'listing_score_viewed', 'listing_comparables_viewed',
  'listing_investment_viewed', 'listing_breakdown_opened',
  'listing_agency_clicked',   // ← lead conversion event
  'listing_shared', 'listing_saved', 'listing_scrolled_full',

  // Alerts and conversion
  'alert_form_opened', 'alert_submitted',
  'waitlist_form_opened', 'waitlist_submitted',

  // Agent section
  'agent_section_viewed', 'agent_form_opened', 'agent_form_submitted',

  // Search and filters
  'search_performed', 'filter_suburb_selected', 'filter_suburb_deselected',
  'filter_bedrooms_changed', 'filter_price_changed', 'filter_type_changed',
  'filter_size_changed',

  // Scroll depth milestones
  'scroll_25', 'scroll_50', 'scroll_75', 'scroll_100',

  // Time on page milestones
  'time_30s', 'time_60s', 'time_120s', 'time_300s',
])

// ── Fields that must never appear (POPIA) ────────────────────
const FORBIDDEN_FIELDS = ['email', 'phone', 'name', 'id_number', 'passport']

// ---------------------------------------------------------------------------

serve(async (req: Request) => {
  // Preflight
  if (req.method === 'OPTIONS') {
    return new Response('ok', { status: 200, headers: CORS })
  }

  if (req.method !== 'POST') {
    return json({ error: 'Method not allowed' }, 405)
  }

  let body: Record<string, unknown>
  try {
    body = await req.json()
  } catch {
    return json({ error: 'Invalid JSON' }, 400)
  }

  // ── Validate event type ──────────────────────────────────────
  if (!body.event_type || !ALLOWED_EVENTS.has(body.event_type as string)) {
    return json({ error: 'Invalid event type' }, 400)
  }

  // ── POPIA guard — reject any personal data ───────────────────
  for (const field of FORBIDDEN_FIELDS) {
    if (body[field] !== undefined) {
      return json({ error: `Field not allowed: ${field}` }, 400)
    }
    const meta = body.metadata as Record<string, unknown> | undefined
    if (meta && meta[field] !== undefined) {
      return json({ error: `Field not allowed in metadata: ${field}` }, 400)
    }
  }

  // ── Require session_id ───────────────────────────────────────
  if (!body.session_id) {
    return json({ error: 'session_id required' }, 400)
  }

  // ── Write to Supabase using service role ─────────────────────
  const supabase = createClient(
    Deno.env.get('SUPABASE_URL')!,
    Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!,
  )

  const { error } = await supabase.from('user_events').insert({
    session_id:           String(body.session_id),
    event_type:           String(body.event_type),
    event_category:       String(body.event_category  ?? 'general'),
    page:                 strOrNull(body.page),
    suburb:               strOrNull(body.suburb),
    listing_id:           strOrNull(body.listing_id),
    listing_type:         strOrNull(body.listing_type),
    bedrooms:             intOrNull(body.bedrooms),
    price:                intOrNull(body.price),
    homelens_score:       intOrNull(body.homelens_score),
    investment_score:     intOrNull(body.investment_score),
    verdict:              strOrNull(body.verdict),
    input_rent:           intOrNull(body.input_rent),
    input_suburb:         strOrNull(body.input_suburb),
    input_price:          intOrNull(body.input_price),
    scroll_depth_pct:     intOrNull(body.scroll_depth_pct),
    time_on_page_seconds: intOrNull(body.time_on_page_seconds),
    filter_type:          strOrNull(body.filter_type),
    filter_value:         strOrNull(body.filter_value),
    sort_by:              strOrNull(body.sort_by),
    device:               strOrNull(body.device),
    screen_width:         intOrNull(body.screen_width),
    referrer:             strOrNull(body.referrer),
    metadata:             (body.metadata as object) ?? {},
    occurred_at:          new Date().toISOString(),
  })

  if (error) {
    console.error('Insert error:', error.message)
    return json({ error: 'Database error' }, 500)
  }

  return json({ ok: true })
})

// ── Helpers ──────────────────────────────────────────────────

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...CORS },
  })
}

function strOrNull(v: unknown): string | null {
  return v != null ? String(v) : null
}

function intOrNull(v: unknown): number | null {
  if (v == null) return null
  const n = Number(v)
  return isFinite(n) ? Math.round(n) : null
}
