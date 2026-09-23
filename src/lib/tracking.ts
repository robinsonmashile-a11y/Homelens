// Homelens — Analytics tracking utility
//
// COPY THIS FILE INTO LOVABLE at: src/lib/tracking.ts
//
// Usage:
//   import { track, initScrollTracking, initTimeTracking } from '@/lib/tracking'
//
//   track('page_view', { page: 'buyer' })
//   track('listing_agency_clicked', { listing_id, price, suburb })
//
// POPIA:
//   - hasConsent() gate means NOTHING fires until user accepts the cookie banner.
//   - Session IDs are random UUIDs that reset every 24 hours.
//   - No email, phone, name, or IP is ever stored.

// ── Types ─────────────────────────────────────────────────────

interface TrackPayload {
  category?:             string
  page?:                 string
  suburb?:               string
  listing_id?:           string
  listing_type?:         string
  bedrooms?:             number
  price?:                number
  homelens_score?:       number
  investment_score?:     number
  verdict?:              string
  input_rent?:           number
  input_suburb?:         string
  input_price?:          number
  scroll_depth_pct?:     number
  time_on_page_seconds?: number
  filter_type?:          string
  filter_value?:         string
  sort_by?:              string
  metadata?:             Record<string, unknown>
  // any event-specific extra fields
  [key: string]: unknown
}

// ── Constants ─────────────────────────────────────────────────

const SESSION_KEY         = 'hl_session'
const CONSENT_KEY         = 'hl_consent'
const SESSION_EXPIRY_HOURS = 24

// Mirror of the Edge Function allow-list — keeps client-side validation fast
const ALLOWED_EVENTS = new Set([
  'page_view', 'tab_switch', 'app_open', 'app_close',
  'home_tile_tap',
  'rent_checker_started', 'rent_suburb_selected', 'rent_bedrooms_selected',
  'rent_amount_entered', 'rent_check_submitted', 'rent_result_viewed',
  'rent_result_above', 'rent_result_below', 'rent_increase_checked',
  'rent_letter_downloaded',
  'buyer_suburb_chip_tapped', 'buyer_filter_opened', 'buyer_filter_applied',
  'buyer_filter_cleared', 'buyer_sort_changed', 'buyer_listing_viewed',
  'buyer_listing_scrolled', 'buyer_page_changed',
  'investor_browse_opened', 'investor_analyse_opened',
  'investor_suburb_selected', 'investor_filter_applied',
  'investor_listing_viewed', 'investor_breakdown_opened',
  'investor_bond_adjusted', 'investor_calculator_used',
  'investor_page_changed',
  'listing_opened', 'listing_score_viewed', 'listing_comparables_viewed',
  'listing_investment_viewed', 'listing_breakdown_opened',
  'listing_agency_clicked',
  'listing_shared', 'listing_saved', 'listing_scrolled_full',
  'alert_form_opened', 'alert_submitted',
  'waitlist_form_opened', 'waitlist_submitted',
  'agent_section_viewed', 'agent_form_opened', 'agent_form_submitted',
  'search_performed', 'filter_suburb_selected', 'filter_suburb_deselected',
  'filter_bedrooms_changed', 'filter_price_changed', 'filter_type_changed',
  'filter_size_changed',
  'scroll_25', 'scroll_50', 'scroll_75', 'scroll_100',
  'time_30s', 'time_60s', 'time_120s', 'time_300s',
])

// ── Session management ────────────────────────────────────────

function getSessionId(): string {
  try {
    const stored = localStorage.getItem(SESSION_KEY)
    if (stored) {
      const { id, created } = JSON.parse(stored)
      const hoursElapsed = (Date.now() - created) / 3_600_000
      if (hoursElapsed < SESSION_EXPIRY_HOURS) return id
    }
  } catch {
    // localStorage unavailable — fall through
  }

  const newId = crypto.randomUUID()
  try {
    localStorage.setItem(SESSION_KEY, JSON.stringify({ id: newId, created: Date.now() }))
  } catch {
    // ignore
  }
  return newId
}

// ── Consent gate (POPIA) ──────────────────────────────────────

export function hasConsent(): boolean {
  try {
    return localStorage.getItem(CONSENT_KEY) === 'accepted'
  } catch {
    return false
  }
}

/** Call this when the user clicks "Accept" on the cookie banner. */
export function grantConsent(): void {
  try {
    localStorage.setItem(CONSENT_KEY, 'accepted')
  } catch {
    // ignore
  }
}

/** Call this when the user clicks "Decline". */
export function revokeConsent(): void {
  try {
    localStorage.setItem(CONSENT_KEY, 'declined')
  } catch {
    // ignore
  }
}

// ── Category inference ────────────────────────────────────────

function inferCategory(event_type: string): string {
  if (event_type.startsWith('rent_'))     return 'renter'
  if (event_type.startsWith('buyer_'))    return 'buyer'
  if (event_type.startsWith('investor_')) return 'investor'
  if (event_type.startsWith('listing_'))  return 'listing'
  if (event_type.startsWith('filter_'))   return 'filter'
  if (event_type.startsWith('scroll_'))   return 'engagement'
  if (event_type.startsWith('time_'))     return 'engagement'
  return 'general'
}

function getDevice(): 'mobile' | 'desktop' {
  return window.innerWidth < 768 ? 'mobile' : 'desktop'
}

// ── Event queue (batched, 3-second flush) ─────────────────────

const queue: Record<string, unknown>[] = []
let flushTimer: ReturnType<typeof setTimeout> | null = null

function scheduleFlush() {
  if (flushTimer) return
  flushTimer = setTimeout(flush, 3_000)
}

async function flush(): Promise<void> {
  flushTimer = null
  if (queue.length === 0) return

  const batch = queue.splice(0)

  try {
    // Write directly to Supabase via anon key (INSERT-only RLS policy on user_events)
    const { createClient } = await import('@supabase/supabase-js')
    const supabase = createClient(
      import.meta.env.VITE_SUPABASE_URL,
      import.meta.env.VITE_SUPABASE_ANON_KEY,
    )
    await supabase.from('user_events').insert(batch)
  } catch {
    // Silent — tracking must never crash the app
  }
}

// Flush on unload so we don't lose queued events
if (typeof window !== 'undefined') {
  window.addEventListener('beforeunload', flush)
  window.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') flush()
  })
}

// ── Main track() export ───────────────────────────────────────

/**
 * Fire an analytics event.
 *
 * @example
 *   track('listing_agency_clicked', { listing_id, price, suburb })
 *   track('rent_check_submitted', { suburb, bedrooms, input_rent: rent })
 */
export function track(event_type: string, data: TrackPayload = {}): void {
  // POPIA gate — never track without explicit consent
  if (!hasConsent()) return

  // Drop unknown events silently (fail safe)
  if (!ALLOWED_EVENTS.has(event_type)) return

  const { category, ...rest } = data

  queue.push({
    session_id:    getSessionId(),
    event_type,
    event_category: category ?? inferCategory(event_type),
    device:        getDevice(),
    screen_width:  window.innerWidth,
    referrer:      document.referrer || '',
    page:          window.location.pathname,
    ...rest,
  })

  scheduleFlush()
}

// ── Scroll depth tracker ──────────────────────────────────────

/**
 * Call on component mount. Returns a cleanup function for useEffect.
 *
 * @example
 *   useEffect(() => initScrollTracking(), [])
 */
export function initScrollTracking(): () => void {
  if (!hasConsent() || typeof window === 'undefined') return () => {}

  const fired = new Set<number>()

  const handler = () => {
    const scrolled   = window.scrollY
    const maxScroll  = document.body.scrollHeight - window.innerHeight
    if (maxScroll <= 0) return

    const pct = Math.round((scrolled / maxScroll) * 100)

    for (const milestone of [25, 50, 75, 100]) {
      if (pct >= milestone && !fired.has(milestone)) {
        fired.add(milestone)
        track(`scroll_${milestone}`, { scroll_depth_pct: milestone })
      }
    }
  }

  window.addEventListener('scroll', handler, { passive: true })
  return () => window.removeEventListener('scroll', handler)
}

// ── Time on page tracker ──────────────────────────────────────

/**
 * Call on component mount. Returns a cleanup function for useEffect.
 *
 * @example
 *   useEffect(() => initTimeTracking(), [])
 */
export function initTimeTracking(): () => void {
  if (!hasConsent() || typeof window === 'undefined') return () => {}

  const start = Date.now()
  const fired = new Set<number>()

  const timer = setInterval(() => {
    const seconds = Math.round((Date.now() - start) / 1_000)
    for (const milestone of [30, 60, 120, 300]) {
      if (seconds >= milestone && !fired.has(milestone)) {
        fired.add(milestone)
        track(`time_${milestone}s`, { time_on_page_seconds: seconds })
      }
    }
  }, 5_000)

  return () => clearInterval(timer)
}
