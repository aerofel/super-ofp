---
name: super-ofp
description: Use when user references an Aircalin OFP PDF (filename pattern ACI-XXXX-YYYY-MM-DD-HHMM-AAAA) or says "super ofp", "super-ofp", "interactive ofp", "ofp overlay", "rebuild ofp", "edit ofp", "recompute ofp", "fill DISCR", "fill ZFW", "fill ATA", "calculate UNDERLOAD", "LATEST", "in-flight window", "validity window inflight", "DIFF", "AFU", "EEP -30 min", or asks for an HTML mirror of an OFP with editable ACT.FUEL / ACT.WEIGHTS / navlog cells that recomputes REQUESTED FUEL, TOF, TOW, EBO, LAW, UNDERLOAD, ETAs, in-flight validity windows, and ETOPS scenario LATEST times live. Distinct from dez-briefing (which produces a tabbed crew summary) — super-ofp keeps the exact OFP layout and overlays interactive form fields on top of the original text. Keywords — OFP, CFP, ACT.FUEL, ACT.WEIGHTS, DISCR, REQUESTED FUEL, ZFW, TOF, EBO, LAW, UNDERLOAD, FUEL BURN INCREASE, navtech, Aircalin, ACI500, ACI501, ACI4, F-ONEO, F-ONEA, F-ONET, ETOPS, EEP, EXP, ETP, LATEST, validity window, AFU, EFU, DIFF, A.RF, lateness, planned takeoff.
user_invocable: true
---

# Super-OFP — Interactive OFP Overlay

Convert an Aircalin navtech OFP PDF into a single self-contained HTML file that **looks exactly like the original** (column-accurate monospaced text, OFP-native page breaks, all 60+ pages) and overlays a live recompute engine that responds to user input. Designed for dispatch + crew use: fill in actuals, see all derived values shift in real time.

## When to use

- User has an OFP PDF (`ACI-XXXX-YYYY-MM-DD-HHMM-AAAA*.pdf`) and wants to fill in actuals (DISCR, TAXI, ZFW, ATA, AFU, A.RF for any waypoint) and see derived values.
- User asks for an "interactive OFP", "super-ofp", "ofp overlay".
- User wants live in-flight ETOPS validity windows / scenario LATEST / EEP -30 min check.
- Not for tabbed briefing summaries — use `dez-briefing` for that.

## How to run

```bash
python3 ~/.claude/skills/super-ofp/scripts/build_super_ofp.py <path-to-ofp.pdf> [-o output.html]
```

Output defaults to `<stem>.super.html` next to the PDF.

PDF text extraction is delegated to the `ofp-extract` skill (bbox-based, column-accurate). Requires `pdftotext` (poppler) in PATH.

## Output naming

`<pdf-stem>.super.html` next to the source PDF (override with `-o`).

## Layout-preservation strategy

1. **Extraction**: delegates to `ofp-extract` which uses `pdftotext -bbox-layout` and snaps each word to its true monospace cell (1 PDF cell = 1 HTML cell, exactly). Auto-detects char width (~6.63 pt) and left margin from word geometry.
2. **Page splitting**: on the OFP's own `PAGE N OF M` footer lines (not PDF form-feeds — the OFP's logical pages don't align with PDF pages).
3. **Editable cells**: rendered as `<span contenteditable="true">` inside `<pre>`, not `<input>` — spans flow literally in the monospace grid with zero column drift. Each cell is exactly the same width as the placeholder it replaces (`....` = 4 cells, `. . . . .` = 9 cells).
4. **Self-contained**: single HTML file, no external CSS/fonts/fetches. Works fully offline, shareable as one file.

## Overlays — what gets injected

### Crew-briefing header (page 2)

The GENERAL INFORMATION and FLIGHT PLAN INFORMATION blocks at the top of CREW BRIEFING get value-only highlights (yellow background, brown bold) — labels stay normal:

- **GENERAL INFO**: aircraft type, REG, DATE, flight number, DEP, ARR, CREW
- **FLIGHT PLAN INFO**: OFP # (label rewritten from `CFP N` → `OFP #`, same 5-char width so the colon position is preserved), CI, ETOPS, TNK, TDV, ETD, ETA, EROPS, DEST ALTN, ALTN FUEL, FUEL ERA AERODROME

**FUEL BURN INCREASE / DECREASE** rule: when both coefficients are equal (`00201 KGS == 00201 KGS`), the DECREASE line is struck through (grey + line-through). Aircalin convention: a single coefficient applies in only one direction at a time — printing both is a quirk of the templating; the strike-through eliminates ambiguity.

### DSP COMMENTS bullet fields (page 3)

Same yellow value-highlight on the dispatcher bullet lines (format: `      LABEL :          * VALUE`):

- **DOWC** — value
- **EQUIPAGE** — value (e.g. `3/8`)
- **PREVI PAX** — value (e.g. `21J-14W-187Y +5INF`) **plus** an inline blue sum annotation `= NNN PAX (+M INF)` summing the J/W/Y seat counts. Infants stay separate (no seat occupancy).
- **CODE CGO** — value
- **MEL/CDL** — value

These use the same `.sofp-hdr-val` class as the page-2 briefing. PREVI PAX additionally injects a `.sofp-pax-sum` span after the highlighted value.

### Fuel & weights block (page 5)

| Cell | Behaviour |
|---|---|
| `DISCR` (ACT.FUEL) | editable, defaults to 0 |
| `TAXI` (ACT.FUEL) | editable, pre-filled with estimated TAXI |
| `ZFW` (ACT.WEIGHTS) | editable, pre-filled with E.WT.ZFW |
| `REQUESTED FUEL` | `ceil_100( TOF_est + DISCR + TAXI )` |
| `TOF / TOW / EBO / LAW` (ACT.WT) | recomputed live, red bg if busts STRUC/OPS |
| `UNDERLOAD` | new overlay on DOW row, right-aligned under STRUC/OPS LIMITS header. `min(MZFW−ZFW, MTOW−TOW, MLW−LAW)`. Red if negative. |

**Formulas:**
```
TOF (act)        = TOF (est) + DISCR
REQUESTED FUEL   = ceil_100( TOF (est) + DISCR + TAXI )
TOW (act)        = ZFW (act) + TOF (act)
ΔTOW             = TOW (act) − TOW (est)
EBO (act)        = EBO (est) + ΔTOW × (burn_coeff_per_1000kg / 1000)
LAW (act)        = TOW (act) − EBO (act)
UNDERLOAD        = min( MZFW − ZFW,  MTOW − TOW,  MLW − LAW )
```

TAXI is burned before V1 → does **not** affect TOF/TOW/EBO/LAW, only REQUESTED FUEL.

### NAVLOG — every waypoint editable

Every navlog waypoint (main + alternate) gets four interactive cells:

| Cell | Behaviour |
|---|---|
| `A.RF` (N-coord line) | editable 4-char input, all wpts |
| `AFU` (N-coord line) | editable 4-char input, all wpts |
| `DIFF` (N-coord line, 3rd `....`) | computed = `AFU − EFU`, signed (e.g. `+003`, `-007`, `±000`). **Red** if positive (burned more than plan), **green** if negative. |
| `ATA` (E-coord line) | editable 4-char input, all wpts. **First wpt = ATA_0 = anchor for every ETA on the page.** Other wpts: **red** if late vs `ATA_0 + CTME`, **green** if early. |
| `ETA` (E-coord line) | computed = `ATA_0 + CTME`, always 4-char display |

**Alternate navlog ETAs** anchor on main destination ETA: alt-waypoint CTME is offset-adjusted by main destination CTME at build time, so `ETA(alt wpt) = ATA_0 + main_dest_CTME + alt_CTME` falls out of the same formula.

Key conventions:
- All times are HHMM (e.g. `0955` for 09:55 Z).
- All ATAs anchor on `ATA_0` — type the wheels-up time once, every downstream ETA updates.

### ETOPS INFORMATION section

For each of the 6 scenario blocks (3 scenarios × 2 ETPs):

1. **LATEST** time appended to each scenario header line, e.g.
   ```
   1EO DEPRESS A330-941   WAAA/YBCS   LATEST 17:24
   ```
   Formula: `LATEST = ATA_0 + tetp + divert + 2 min` (2-min turn-back allowance).

2. **TOTAL CRITICAL DIV FUEL** value highlighted (green) for the **lowest-fuel single-failure scenario** per ETP — i.e. `min(fuel)` over `{2ENG DEPRESS, 1EO DRIFTDOWN}`. **1EO DEPRESS is excluded** (combined failure, not single).

3. **F/R margin annotation** appended right next to the highlighted critical-fuel value:
   ```
   TOTAL CRITICAL DIV FUEL   14613 +20961   14613   14591
   ```
   - `+N = EST F/R OVER ETP − TOTAL CRITICAL DIV FUEL`
   - **Green** if `N ≥ HOLDING/1500FT value` (margin ≥ 15-min alternate hold reserve)
   - **Orange** if `N < HOLDING/1500FT value` (tight margin)
   - The annotation fits inside the original gap of trailing spaces, so per-altn columns to the right stay aligned.

### Navlog highlights tied to ETOPS

- **In-flight selected ETP scenario** rows in the navlog (label + N-coord + E-coord) are highlighted **green** — matches the green TOTAL CRITICAL DIV FUEL highlight, so both signals point to the same scenario.
- **EEP** row (label + N-coord + E-coord) highlighted **light brown**, with appended blue `-30 MIN: HH:MM` annotation = `ATA_0 + EEP_CTME − 30` (pre-ETOPS check time).
- **EXP** row (label + N-coord + E-coord) highlighted **light brown**.
- **WPT name labels** (single uppercase/alphanumeric token, e.g. `VTBS`, `PIDEL`, `13S135E`) rendered **bold**. **FIR boundary** lines (containing `\bFIR\b`) are explicitly excluded — they stay plain. AWY labels (appearing AFTER e-data) are naturally filtered out because the back-search looks only at the line BEFORE each n-data row.

### SUITABILITY ETOPS in-flight window recalc

Next to each `<altn> VALIDITY WINDOW HH:MMZ TO HH:MMZ` line in the SUITABILITY ETOPS section, a live recalculated window is appended:

```
WAAA VALIDITY WINDOW  15:22Z TO 18:36Z   1EO DRIFTDOWN: 15:22Z TO 18:31Z (-5)
```

Formula (per altn):
```
lateness          =  ATA_0 − (EOBT + 20)          (planned takeoff)
scenario_delta    =  LATEST(inflight) − LATEST(1EO DEPRESS)    at the bounding ETP
                                              (in-flight = min-fuel of {2ENG DEPRESS, 1EO DRIFTDOWN})

new_start         =  orig_start  +  lateness
new_end           =  orig_end    +  lateness  +  scenario_delta
```

Altn-to-ETP mapping is positional — `altn[i]` uses `ETP[min(i, n_etps − 1)]`, matching route order. The delta is computed once at build time per ETP, the lateness applies dynamically on every ATA_0 keystroke. If `ATA_0` is empty, lateness = 0 and only the scenario_delta applies. A trailing `(late ±N)` tag shows up when there's lateness.

### Weather section (DEPARTURE / ARRIVAL / OTHER pages)

For each weather block detected by the `(DEPARTURE|ARRIVAL|OTHER): <airport>` label line:

- **Label highlighted** light brown, with an inline yellow annotation tag appended:
  - `DEPARTURE` → `ETD HH:MMZ` (from header EOBT)
  - `ARRIVAL`   → `ETA HH:MMZ` (from header landing ETA)
  - `OTHER` (ETOPS altn) → `ETOPS HH:MM–HH:MMZ` (validity window from SUITABILITY ETOPS)
  - `OTHER` (dest altn)  → `ALTN ETA HH:MMZ` (from SUITABILITY DEST & ALT)
  - `OTHER` (fuel ERA)   → `ERA  ETA HH:MMZ` (from SUITABILITY FUEL ERA)
  - `OTHER` not in any SUITABILITY block → no annotation (e.g. transit altns VVTS/WSSS/WBGB)
- **METAR header** (the literal `METAR`, the airport ICAO, and the `DDHHMMZ` issue time) is **bold** under `DEPARTURE` only. The rest of the report stays normal weight.
- **TAF header** (`TAF` or `TAF AMD`, the airport ICAO, and the `DDHHMMZ` issue time) is **bold** under `ARRIVAL`/`OTHER` only. The rest of the report stays normal weight.
- **TAF time-period tokens** (`DDHH/DDHH` ranges and `FMDDHHMM` instants) that **overlap** the time of interest (the appended ETA / ETD / window) are wrapped with a yellow highlight, drawing the eye to the BECMG/TEMPO/PROB/FM block that applies. The day in DDHH is interpreted relative to the OFP's ETD/ETA day with a small month-wrap window.

The airport ICAO is detected by looking at the first METAR/TAF line within 12 lines after the label.

### Image-only PDF pages (perf charts, NOTAM maps)

Some PDF pages are pure graphics (perf-analysis charts, NOTAM maps, runway diagrams) and produce essentially empty text under `pdftotext`. These are rendered by `pdftoppm` at 120 DPI, base64-embedded, and inlined as `<div class="page pdf-image">` blocks at their correct OFP position. Defaults for Aircalin OFPs: PDF pages **24–31** (perf-analysis charts) and **64–66** (trailing NOTAM/chart inserts). Configurable via `IMAGE_PAGES_DEFAULT` in `scripts/build_super_ofp.py`. Page splitting is anchored on the `AIRCALEDONIE INTERNATIONAL BRIEF PAGE N OF M` **header** (not the footer), so each OFP page — including ones with no footer because all body content is graphics — gets its own output slot that the image can replace.

The result is a fully self-contained HTML — no external image dependencies, ready for AirDrop or any other single-file share.

### Toolbar

- **`SUPER‑OFP`** logo + PDF filename
- **`late ±N min`** indicator — `ATA_0 − (EOBT + 20)`. Red if late, green if early, grey at 0.
- **Reset** button — clears localStorage for this OFP and re-applies prefills.
- **Print** button.
- **Share** button — calls the Web Share API with the **live HTML snapshot** (so the user's filled-in ATA / fuel / weight values travel with the file). On macOS / iOS Safari this opens the native share sheet (AirDrop, Mail, Messages, Files…). On browsers without file-share support it falls back to a download — the user can then AirDrop from Finder.

## Persistence — localStorage

Every editable cell saves on each keystroke under key `sofp:<pdf_name>:<data-target>`. On page load, after applying OFP-derived prefills (TAXI, ZFW), saved values override them. **Each OFP keeps its own values** (PDF filename in the key prevents cross-contamination). The Reset button wipes the OFP's localStorage entries.

## Recompute model — config baked into the HTML

Values read from the OFP at build time and embedded as JSON in the HTML:

- `fuel` — every E.FUEL row (DEST, RTE.R, ALT.R, FIN.R, ADD.R, EROPS, EXTRA, TANKERING, T/O FUEL, TAXI, TOTAL FUEL)
- `wt_est` — every E.WT row (DOW, PLD, ZFW, TOF, TOW, EBO, LAW)
- `wt_lim` — STRUC/OPS limits (MZFW, MTOW, MLW)
- `burn_coeff_per_1000kg` — from `FUEL BURN INCREASE PER 1000 KGS ADDITIONAL TAKEOFF WT XXXXX KGS`
- `struc_col` — column of `STRUC/OPS LIMITS` in the WT header (anchors UL on DOW)
- `eobt_min`, `taxi_out_min`, `planned_takeoff_min` — for the toolbar lateness calc
- `pdf_name` — localStorage namespace key

The JS engine is ~250 lines of vanilla JS embedded in the HTML. Single `recompute()` call on every keystroke fans out to: fuel/wt outputs → underload → navlog ETAs → ATA late/early colors → DIFF colors → EEP -30 MIN → ETOPS scenario LATEST → in-flight validity windows → toolbar lateness.

## In-flight scenario selection convention (Aircalin)

- **At flight planning**: critical scenario is **1EO DEPRESS** (combined failure, used for the printed validity windows and the per-ETP CRITICAL SCENARIO field).
- **In-flight**: 1EO DEPRESS is excluded as a *combined* failure. Eligible scenarios are the two single failures: **2ENG DEPRESS** and **1EO DRIFTDOWN**. The one with the **lowest TOTAL CRITICAL DIV FUEL** is selected per ETP. On the sample CFP both ETPs select 1EO DRIFTDOWN (lower fuel because aircraft cruises at FL160 instead of FL100).

## Sanity check on the BKK→NOU sample (`ACI-0501-2026-05-10-0935-VTBS FULL PACK.pdf`)

With `ATA_0 = 09:55`, defaults TAXI = 400, ZFW = 169700, DISCR = 0:

- `REQUESTED FUEL` = 71200 (raw 71173 → ceil-100)
- E.WT = TOW 240473, EBO 54819, LAW 185654 — all reproduced
- `UNDERLOAD` = +4527 kg (MTOW-bound)
- Main navlog: 50 ETA spans, terminating at NWWW with `data-ctme="0917"` (ETA 19:12 Z)
- Alt navlog: 14 ETA spans, terminating at YBBN with `data-ctme="1130"` (ETA 21:25 Z, anchored on main NWWW)
- 6 ETOPS scenario blocks with LATEST values, 2 green-highlighted critical-fuel values (both 1EO DRIFTDOWN: 14613 and 9687) with +20961 and +11179 margin annotations
- 3 in-flight validity windows: WAAA 15:22 → 18:31 (−5), YBCS 17:18 → 20:30 (−6), NWWW 19:16 → 20:30 (−6), all with `1EO DRIFTDOWN:` scenario label
- EEP row light-brown with `-30 MIN: 13:55`; EXP row light-brown
- 62 bold WPT name labels (FIR lines excluded)

## Quick verification after build

```bash
# fuel/weights overlays
grep -nE 'data-target="(DISCR|TAXI|ZFW)"' <pdf>.super.html
grep -nE 'data-source="(REQUESTED_FUEL|TOF|TOW|EBO|LAW|UNDERLOAD)"' <pdf>.super.html

# navlog
grep -c 'data-target="ATA_[0-9]' <pdf>.super.html   # one per wpt
grep -c 'sofp-diff' <pdf>.super.html                # one per wpt N-coord line

# ETOPS
grep -c 'sofp-etops-eta' <pdf>.super.html           # 6 (3 scenarios × 2 ETPs)
grep -c 'sofp-crit-lowest' <pdf>.super.html         # 2 (1 per ETP) + 1 CSS rule
grep -c 'sofp-fr-ok\|sofp-fr-warn' <pdf>.super.html # 2 + a few CSS rules

# Validity windows
grep -c 'sofp-validity-inflight' <pdf>.super.html   # 3 (WAAA/YBCS/NWWW) + 1 CSS

# EEP/EXP/WPT
grep -c 'sofp-eep-label\|sofp-exp-label' <pdf>.super.html  # 1 each + CSS
grep -c 'sofp-wpt-name' <pdf>.super.html            # ~60 (route-dependent)

# Weather highlights
grep -c 'sofp-weather-label' <pdf>.super.html       # 1 DEP + 1 ARR + N OTHERs (CSS + body)
grep -c 'sofp-weather-time'  <pdf>.super.html       # one per labelled airport with time
grep -c 'sofp-wx-bold'       <pdf>.super.html       # 3 per relevant METAR/TAF line (kwd + ICAO + DDHHMMZ)
grep -c 'sofp-taf-period'    <pdf>.super.html       # TAF tokens covering the time of interest

# Crew-briefing header highlights (page 2)
grep -c 'sofp-hdr-val'       <pdf>.super.html       # ~17 values across GENERAL + FLIGHT PLAN INFO
grep -c 'sofp-hdr-label'     <pdf>.super.html       # 1 (CFP N → OFP # rewrite)
grep -c 'sofp-strike-line'   <pdf>.super.html       # 1 if INCREASE coeff == DECREASE coeff

# PDF-rendered image pages (perf charts, NOTAM maps)
grep -c 'class="page pdf-image"' <pdf>.super.html   # 11 on the BKK→NOU sample (8 + 3)
```

Anything missing means the OFP layout has drifted from the navtech standard — inspect the raw text (`python3 ~/.claude/skills/ofp-extract/scripts/extract_ofp.py <pdf>`) and adjust the anchors in `build_super_ofp.py` (regex constants at top of file).

## Files in this skill

- `scripts/build_super_ofp.py` — single-file converter:
  - Parses fuel/wt/navlog/ETOPS/SUITABILITY blocks
  - Builds line→tag indices (navlog, ETOPS, decorations)
  - Embeds CSS + JS engine
  - Delegates text extraction to `~/.claude/skills/ofp-extract/`

Total: one Python script + this SKILL.md. JS engine and CSS are inlined; no separate template files.

## Related skills

- **`ofp-extract`** — the column-accurate text extraction primitive (bbox-layout based). Always used as the first step of super-ofp; can also be called standalone for any OFP→text workflow that needs precise column alignment.
- **`dez-briefing`** — produces a tabbed crew briefing summary (different output format; use when the user wants a separate UI rather than an OFP mirror).
- **`read-orlando`** — Aircalin OM-A / MANEX search. Useful when you need the authoritative definition of OFP fields (e.g. SPA.ETOPS.115 § "Fenêtre d'accessibilité" gave us the ETP-based validity-window formula).
