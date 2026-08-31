```text
FONT RECONSTRUCTION PIPELINE
============================

INPUT
  URL / family / style / weight
  optional:
    vietnamese = true|false

  │
  ▼

EXACT CACHE / VALID BINARY
  │
  ├─ exact valid TTF/OTF exists
  │     → RETURN immediately
  │
  ├─ exact canonical FontModel exists
  │     → build missing TTF/OTF only
  │     → structural check
  │     → RETURN
  │
  └─ cache miss
        │
        ▼

FAST METADATA BOOTSTRAP
  │
  ├─ native Chrome --dump-dom
  │     extract:
  │       family
  │       style
  │       weight
  │       font/style identifiers
  │       MD5 if present
  │       raster/CDN metadata
  │
  └─ metadata incomplete
        → start ONE persistent Chromium/Playwright session
        → complete session/cookies/metadata
        → keep browser alive for:
            measureText
            feature probes
            canvas-atlas fallback
            Vietnamese validation/inference

  │
  ▼

EXACT FONT IDENTITY
  │
  ├─ MD5 available
  │     → use MD5
  │
  └─ MD5 unavailable
        → metadata/Algolia lookup
        → resolve MD5 if possible
        → otherwise use stable source identity
          with lower identity-confidence

  │
  ▼

HTTP GLYPH DISCOVERY
  │
  │  use HTTP directly, NOT browser crawling
  │
  │  enumerate pages/batches:
  │    page 1
  │    page 2
  │    page 3
  │    ...
  │
  │  stop on:
  │    explicit end
  │    empty result
  │    repeated page/layout/hash
  │
  ▼

GlyphManifest
  contains:
    Unicode
    raster identifiers/URLs
    available raster parameters
    metadata
    state
    observations
    metrics
    confidence
    cache identity

  │
  ├───────────────────────────────────────────────┐
  │                                               │
  ▼                                               ▼

ASYNC HTTP RASTER PRODUCER                  METRICS PRODUCER
  HTTP/2                                    persistent Chromium
  keep-alive                               batched JS calls
  concurrency ≈ 8                          one browser session
  exact cookies/session
                                            measureText at:
                                            512
                                            1024
                                            2048

  │                                         collect:
  │                                           width
  │                                           actual bbox
  │                                           ascent/descent
  │                                           font bbox
  │
  │                                         multi-size regression
  │                                         normalize to UPEM=1000
  │
  └──────────────────────┬────────────────────────┘
                         │
                         ▼

PROGRESSIVE GLYPH RECONSTRUCTION
  every glyph starts in FAST LANE

  acquire:
    1024 x_phase=0
    2048 x_phase=0
    y_phase=0

  source priority for every requested observation:

    exact raster cache
         ↓ miss
    direct HTTP/CDN raster
         ↓ unavailable/not attestable
    Chromium CANVAS ATLAS
         ↓
    smaller canvas atlas pages if allocation fails
         ↓
    raster failure

  IMPORTANT:
    no screenshot
    no per-glyph CDP rendering
    no browser navigation per glyph

  Chromium atlas operates only on missing observations:

    group requests by:
      font identity
      size
      x_phase
      y_phase

    build bounded atlas page
      target 64–96 MB
      hard max 128 MB
      one atlas page in memory

    draw all requested glyphs into canvas
         ↓
    one canvas readback
         ↓
    crop cells in memory
         ↓
    feed same raster pipeline as HTTP

  │
  ▼

FAST GEOMETRY PASS

  observations
      ↓
  decode alpha
      ↓
  normalize coordinate system
      ↓
  align observations
      ↓
  merge alpha observations
      ↓
  SDF
      ↓
  zero-distance / ~50% alpha contour
      ↓
  subpixel contour extraction
      ↓
  topology cleanup
      ↓
  cubic Bézier fitting
      ↓
  error-bounded simplification
      ↓
  candidate GlyphModel

  Python only orchestrates.
  Pixel/SDF/contour heavy work should use native/compiled code.

  │
  ▼

CHEAP PER-GLYPH CONFIDENCE CHECK

  check:
    finite coordinates
    closed contours
    sane bbox
    sane advance
    component count consistency
    hole count consistency
    topology consistency
    normalized 1024↔2048 edge agreement
    Bézier residual
    metrics regression residual
    catastrophic self-intersection
    degenerate segments

  │
  ├─ confidence PASS
  │     → FREEZE glyph immediately
  │     → persist:
  │         final Bézier/GlyphModel
  │         metrics
  │     → release alpha/SDF/contour RAM
  │
  └─ confidence FAIL
        │
        ▼

REFINEMENT LEVEL 1
  acquire only missing:
    1024 x=.5
    2048 x=.5

  HTTP if source truly supports that observation
  otherwise Chromium atlas

  reuse previous x=0 observations
      ↓
  merge
      ↓
  SDF
      ↓
  local Bézier refit
      ↓
  confidence check

  ├─ PASS → FREEZE
  │
  └─ FAIL
        │
        ▼

REFINEMENT LEVEL 2
  add:
    512 x=0
    512 x=.5

  now glyph has normal maximum preset:
    512/1024/2048 × x{0,.5}

  reuse all previous observations
      ↓
  reconstruct/refit
      ↓
  confidence

  ├─ PASS → FREEZE
  │
  └─ FAIL
        │
        ▼

REFINEMENT LEVEL 3
  add:
    4096 x=0
    4096 x=.5

  reuse 512/1024/2048
      ↓
  local reconstruction/refit
      ↓
  confidence

  ├─ PASS → FREEZE
  │
  └─ FAIL
        │
        ▼

REFINEMENT LEVEL 4
  add only new phases:

    2048 x=.25
    2048 x=.75
    4096 x=.25
    4096 x=.75

  DO NOT reacquire x=0/.5

  merge all useful observations
      ↓
  SDF
      ↓
  subpixel contour
      ↓
  local cubic Bézier refit
      ↓
  confidence

  ├─ PASS → FREEZE
  │
  └─ FAIL
        │
        ▼

LOCAL OPTIMIZER
  scope = failing glyph ONLY

  may optimize:
    Bézier control points
    local contour geometry
    advance
    side bearings

  objective uses:
    SDF/edge error
    contour error
    bbox error
    metric error

  NEVER optimize entire font.

  choose best structurally valid candidate
      ↓
  if valid:
    FREEZE
  else:
    FAILED_GLYPH

  one difficult glyph must never cause a whole-font reconstruction pass.

  │
  ▼

STREAMING FontModel ASSEMBLY

  while HTTP is downloading later glyphs:
    earlier glyphs are already:
      decoded
      reconstructed
      checked
      frozen
      inserted into FontModel

  no:
    download-all → process-all barrier

  canonical representation:

    UPEM = 1000
    outline = cubic Bézier

  FontModel:
    glyphs
    cmap
    metrics
    components
    anchors
    kerning
    features
    metadata

  │
  ▼

SELECTIVE TYPOGRAPHY INFERENCE

  KERNING
    generate useful candidate pairs only:
      A-Z × A-Z
      A-Z × a-z
      common lowercase pairs
      punctuation
      digits
      known sensitive pairs

    browser batch measure:
      width(left)
      width(right)
      width(pair)

    kern_delta =
      width(pair)
      - width(left)
      - width(right)

    retain only:
      abs(delta) > epsilon

    build GPOS kern

  FEATURES
    priority:
      kern
      liga
      calt

    browser behavioral probe:
      feature ON
      vs
      feature OFF

    compare:
      width
      rendering
      glyph behaviour

    create feature rules only when an observable difference exists.

    no exhaustive ss01..ss20 probing on normal fast path.

  │
  ▼

OPTIONAL VIETNAMESE EXTENSION

  if vietnamese=false:
      skip directly to binary validation

  if vietnamese=true:

      coverage audit:
        Vietnamese NFC
        Vietnamese NFD

      for every target:

        existing glyph?
          YES
            → preserve

          NO
            ↓

        deterministic component synthesis first

        examples:
          A + breve + acute
          A + circumflex + grave
          O + horn + hook
          U + horn + dotbelow

        reuse:
          existing base outlines
          existing accent geometry
          inferred upper/lower anchors
          component transformations

        build:
          precomposed NFC glyphs
          anchors
          mark
          mkmk

        cheap validation:
          accent collision
          stack spacing
          bbox
          advance consistency
          outline integrity
          NFC/NFD geometry equivalence

        ├─ PASS
        │    → freeze Vietnamese glyph/class
        │
        └─ FAIL
             ↓

        deterministic geometry search:
          anchor dx/dy
          accent scale
          component spacing
          collision avoidance

        ├─ PASS
        │    → freeze
        │
        └─ FAIL
             ↓

        AI STYLE FALLBACK

          AI runs ONLY on failed glyph classes.
          AI NEVER generates raster.

          analyse font style once, then reuse StyleProfile.

          AI input:
            target class
            base vector outlines
            existing accents
            neighbouring glyphs
            metrics
            anchor candidates
            validation error
            StyleProfile

          AI may output only:
            component selection
            anchor adjustment
            dx/dy
            scale
            limited local Bézier adjustment

          operate by class, not one request/glyph.

          examples:
            uppercase A+circumflex+tone class
            lowercase O+horn+tone class
            lowercase U+horn+tone class

          special Đ/đ:
            derive crossing stroke from D/d and font style
            AI only if deterministic confidence is low

          AI candidate
              ↓
          local geometry optimizer
              ↓
          cheap validation

          valid → freeze
          invalid → FAILED_VIETNAMESE_CLASS

  │
  ▼

CANONICAL FONTMODEL COMPLETE

  cubic model is the single source of truth.

  DO NOT independently optimize TTF and OTF.

  │
  ▼

BUILD TEMPORARY TTF

  cubic FontModel
       ↓
  cu2qu
       ↓
  TTF/glyf

  temporary TTF is used for the ONE heavy validation route.

  │
  ▼

FINAL HEAVY VALIDATION
  run ONCE after all bounded glyph refinement,
  typography and optional Vietnamese are complete.

  FontTools:
    open/load
    mandatory table sanity
    cmap
    glyph count
    metrics
    outline loading

       ↓

  HarfBuzz:
    shaping
    glyph sequence
    positioning
    kerning
    liga/calt behaviour

       ↓

  FreeType:
    load glyphs
    rasterize representative corpus
    check rendering

  base corpus includes:
    representative glyphs
    digits
    punctuation
    AVATAR
    Hamburgefontsiv
    VA WA To Ta Te Ty
    fi fl ff ffi ffl

  when vietnamese=true also validate:
    NFC vs NFD
    mark/mkmk
    Vietnamese corpus
    stacked Vietnamese accents
    Ắ Ằ Ẳ Ẵ Ặ
    Ấ Ầ Ẩ Ẫ Ậ
    Ế Ề Ể Ễ Ệ
    Ố Ồ Ổ Ỗ Ộ
    Ớ Ờ Ở Ỡ Ợ
    Ứ Ừ Ử Ữ Ự

  │
  ├─ FAIL structural/load/shaping/render
  │     → pipeline FAIL
  │
  └─ PASS
        │
        ▼

FINAL OUTPUT BUILD

  canonical cubic FontModel
       │
       ├─→ OTF / CFF
       │
       └─→ cu2qu → TTF / glyf

  run cheap FontTools structural/load check
  on BOTH final binaries.

  no second full shaping/render validation
  unless debug mode explicitly requests it.

  │
  ▼

CACHE FINAL RESULT

  persistent:
    compressed source raster observations
    measurements
    final Bézier GlyphModels
    canonical FontModel
    final TTF
    final OTF
    validation report

  ephemeral only:
    decoded alpha
    SDF
    temporary contours
    merge buffers

  checkpoint:
    every 16 frozen glyphs
    OR completed atlas page
    OR graceful shutdown

  no fsync/checkpoint after every glyph.

  │
  ▼

RETURN
  TTF
  OTF
  validation report


PRODUCTION DEFAULTS
===========================================

browser_sessions     = 1
http_concurrency      = 8
glyph_workers         = 2
atlas_pages_in_memory = 1
atlas_target_mb       = 96
atlas_max_mb          = 128
checkpoint_batch      = 16

Performance:
  ordinary ORIGINAL target ≈ <=10 minutes
  execution budget = 15 minutes

At budget limit:
  stop further optional refinement.

  if a valid result exists:
    finalize best valid result.

  if structural/shaping/rendering validity
  cannot be achieved:
    FAILED.


CRITICAL PATH
=============

exact cache/binary
 → dump-dom
 → resolve exact identity/MD5
 → HTTP discovery
 → stream HTTP raster
 → 1024+2048 x0
 → cheap confidence
 → freeze easy glyphs immediately
 → only uncertain glyphs receive x.5
 → only remaining failures receive 512
 → only remaining failures receive 4096
 → only remaining failures receive .25/.75
 → only remaining failures receive local optimizer
 → canonical FontModel
 → selective kern/liga/calt
 → optional deterministic Vietnamese
 → AI only on failed Vietnamese classes
 → temporary TTF
 → ONE FontTools+HarfBuzz+FreeType heavy validation
 → final TTF + OTF
 → cache
 → return
```
