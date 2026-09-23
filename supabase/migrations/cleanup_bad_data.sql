-- =============================================================================
-- Homelens — Data Quality Cleanup Migration
-- Run once after the initial bulk scrape to fix known data issues.
--
-- What this does:
--   1. Deactivate non-Cape-Town listings that slipped through eXp Realty's
--      geo bounding box (geo_lat/geo_lon is null for some JHB/PE/Durban records).
--   2. Remove listings with obviously bad suburb slugs (numbers, empty, etc.).
--   3. Normalise known duplicate suburb slug variants to the canonical form.
--   4. Null out size_m2 values that are clearly ERF/plot sizes, not floor areas.
--   5. Null out price values outside the valid SA residential range.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- 1. Deactivate non-Cape-Town eXp Realty listings
--    These slipped through because geo_lat/geo_lon is NULL in their Supabase
--    DB, so PostgREST's geo bounding box filter passes them.
--    The exprealty scraper now has a whitelist, but old records need cleanup.
-- ---------------------------------------------------------------------------

WITH ct_suburbs AS (
    SELECT unnest(ARRAY[
        -- City Bowl & CBD
        'cape-town', 'city-bowl', 'bo-kaap', 'de-waterkant', 'foreshore',
        'gardens', 'oranjezicht', 'tamboerskloof', 'vredehoek', 'higgovale',
        'zonnebloem', 'woodstock', 'salt-river', 'observatory', 'maitland',
        -- Atlantic Seaboard
        'sea-point', 'green-point', 'three-anchor-bay', 'mouille-point',
        'fresnaye', 'bantry-bay', 'clifton', 'camps-bay', 'bakoven',
        'llandudno', 'hout-bay', 'imizamo-yethu',
        -- Southern Suburbs
        'claremont', 'newlands', 'rondebosch', 'mowbray', 'pinelands',
        'wynberg', 'plumstead', 'kenilworth', 'lansdowne', 'ottery',
        'bishop-court', 'bishopscourt', 'constantia', 'tokai', 'bergvliet',
        'meadowridge', 'retreat', 'heathfield', 'diep-river', 'dieprivier',
        'westlake', 'lakeside', 'muizenberg', 'kalk-bay', 'st-james',
        'fish-hoek', 'clovelly', 'simon-town', 'simonstown', 'glencairn',
        'sunnydale', 'ocean-view', 'kommetjie', 'scarborough', 'noordhoek',
        'capri', 'sunvalley', 'sun-valley', 'marina-da-gama',
        -- Northern Suburbs
        'bellville', 'tyger-valley', 'tygervalley', 'durbanville',
        'brackenfell', 'kraaifontein', 'kuilsrivier', 'kuils-river',
        'parow', 'goodwood', 'monte-vista', 'edgemead', 'bothasig',
        'parklands', 'blouberg', 'bloubergrand', 'bloubergsrand',
        'tableview', 'table-view', 'sunningdale', 'milnerton',
        'richmond-estate', 'richwood', 'welgemoed', 'de-tijger',
        'kenridge', 'pinehurst', 'eversdal',
        -- Helderberg
        'somerset-west', 'strand', 'gordon-bay', 'gordons-bay',
        'firgrove', 'morgenster', 'lourensford',
        -- West Coast
        'melkbosstrand', 'big-bay', 'west-beach', 'wave-crest',
        'lagoon-beach', 'flamingo-vlei', 'sandown',
        -- Cape Flats
        'mitchells-plain', 'athlone', 'hanover-park', 'belhar', 'delft',
        'blue-downs', 'khayelitsha', 'gugulethu', 'langa', 'crossroads',
        'philippi', 'grassy-park', 'lavender-hill', 'lotus-river',
        'steenberg', 'parkwood', 'pelican-park', 'strandfontein',
        -- Winelands (within Cape metro)
        'stellenbosch'
    ]) AS slug
)
UPDATE listings
SET    is_active = FALSE,
       updated_at = NOW()
WHERE  source = 'exprealty'
  AND  suburb_normalised IS NOT NULL
  AND  suburb_normalised NOT IN (SELECT slug FROM ct_suburbs);


-- ---------------------------------------------------------------------------
-- 2. Deactivate listings with clearly invalid suburb slugs
--    - purely numeric (e.g. '123', '72')
--    - very short (single character or empty)
--    - known non-geographic values
-- ---------------------------------------------------------------------------

UPDATE listings
SET    is_active = FALSE,
       updated_at = NOW()
WHERE  is_active = TRUE
  AND  suburb_normalised IS NOT NULL
  AND  (
      suburb_normalised ~ '^\d+$'           -- purely numeric
   OR LENGTH(suburb_normalised) < 2         -- too short (< 2 chars)
   OR suburb_normalised IN (
          'south-africa', 'western-cape', 'gauteng',
          'kwazulu-natal', 'eastern-cape', 'limpopo',
          'mpumalanga', 'north-west', 'free-state', 'northern-cape',
          'johannesburg', 'pretoria', 'durban', 'ekurhuleni',
          'tshwane', 'sandton', 'randburg', 'roodepoort',
          'port-elizabeth', 'gqeberha', 'bloemfontein'
      )
  );


-- ---------------------------------------------------------------------------
-- 3. Normalise duplicate suburb slug variants → canonical form
--    These are common alternate spellings that should map to one canonical slug.
-- ---------------------------------------------------------------------------

-- gordon-bay / gordons-bay → gordons-bay
UPDATE listings
SET    suburb_normalised = 'gordons-bay',
       updated_at = NOW()
WHERE  suburb_normalised = 'gordon-bay'
  AND  is_active = TRUE;

-- bloubergsrand / bloubergrand → bloubergsrand
UPDATE listings
SET    suburb_normalised = 'bloubergsrand',
       updated_at = NOW()
WHERE  suburb_normalised = 'bloubergrand'
  AND  is_active = TRUE;

-- table-view / tableview → table-view
UPDATE listings
SET    suburb_normalised = 'table-view',
       updated_at = NOW()
WHERE  suburb_normalised = 'tableview'
  AND  is_active = TRUE;

-- tyger-valley / tygervalley → tyger-valley
UPDATE listings
SET    suburb_normalised = 'tyger-valley',
       updated_at = NOW()
WHERE  suburb_normalised = 'tygervalley'
  AND  is_active = TRUE;

-- dieprivier → diep-river
UPDATE listings
SET    suburb_normalised = 'diep-river',
       updated_at = NOW()
WHERE  suburb_normalised = 'dieprivier'
  AND  is_active = TRUE;

-- simonstown → simon-town
UPDATE listings
SET    suburb_normalised = 'simon-town',
       updated_at = NOW()
WHERE  suburb_normalised = 'simonstown'
  AND  is_active = TRUE;

-- bishopscourt → bishop-court
UPDATE listings
SET    suburb_normalised = 'bishop-court',
       updated_at = NOW()
WHERE  suburb_normalised = 'bishopscourt'
  AND  is_active = TRUE;

-- kuils-river / kuilsrivier → kuils-river
UPDATE listings
SET    suburb_normalised = 'kuils-river',
       updated_at = NOW()
WHERE  suburb_normalised = 'kuilsrivier'
  AND  is_active = TRUE;

-- sun-valley / sunvalley → sun-valley
UPDATE listings
SET    suburb_normalised = 'sun-valley',
       updated_at = NOW()
WHERE  suburb_normalised = 'sunvalley'
  AND  is_active = TRUE;


-- ---------------------------------------------------------------------------
-- 4. Null out size_m2 values that are clearly ERF/plot sizes (not floor area)
--    Thresholds by property type:
--      apartment: >400m²  almost always ERF
--      townhouse: >600m²  almost always ERF
--      house:    >2000m²  almost always ERF
--    For listings with no property_type: cap at 1500m² as a safe default.
-- ---------------------------------------------------------------------------

-- Apartments
UPDATE listings
SET    size_m2 = NULL,
       price_per_m2 = NULL,
       updated_at = NOW()
WHERE  property_type = 'apartment'
  AND  size_m2 > 400;

-- Townhouses
UPDATE listings
SET    size_m2 = NULL,
       price_per_m2 = NULL,
       updated_at = NOW()
WHERE  property_type = 'townhouse'
  AND  size_m2 > 600;

-- Houses
UPDATE listings
SET    size_m2 = NULL,
       price_per_m2 = NULL,
       updated_at = NOW()
WHERE  property_type = 'house'
  AND  size_m2 > 2000;

-- Unknown property type — conservative cap
UPDATE listings
SET    size_m2 = NULL,
       price_per_m2 = NULL,
       updated_at = NOW()
WHERE  property_type IS NULL
  AND  size_m2 > 1500;


-- ---------------------------------------------------------------------------
-- 5. Null out prices outside the valid SA residential range
--    Below R50,000: data entry error / placeholder
--    Above R500,000,000: almost certainly a parsing error (wrong units)
-- ---------------------------------------------------------------------------

UPDATE listings
SET    price = NULL,
       price_per_m2 = NULL,
       updated_at = NOW()
WHERE  price IS NOT NULL
  AND  (price < 50000 OR price > 500000000);


-- ---------------------------------------------------------------------------
-- 6. Recompute price_per_m2 where both price and size_m2 are present
--    but price_per_m2 is NULL (covers rows fixed above + historical gaps).
-- ---------------------------------------------------------------------------

UPDATE listings
SET    price_per_m2 = ROUND((price::NUMERIC / size_m2)::NUMERIC, 2),
       updated_at   = NOW()
WHERE  price     IS NOT NULL
  AND  size_m2   IS NOT NULL
  AND  size_m2   > 0
  AND  price_per_m2 IS NULL;


-- ---------------------------------------------------------------------------
-- Verification queries (run manually after migration)
-- ---------------------------------------------------------------------------

-- Total active by source:
-- SELECT source, COUNT(*) AS active
-- FROM listings WHERE is_active = TRUE
-- GROUP BY source ORDER BY active DESC;

-- eXp Realty non-CT still active (should be 0):
-- SELECT COUNT(*) FROM listings
-- WHERE source = 'exprealty' AND is_active = TRUE
--   AND suburb_normalised NOT IN (
--     'cape-town','sea-point','camps-bay','constantia', ...
--   );

-- Bad slugs still active (should be 0):
-- SELECT DISTINCT suburb_normalised FROM listings
-- WHERE is_active = TRUE AND suburb_normalised ~ '^\d+$';
