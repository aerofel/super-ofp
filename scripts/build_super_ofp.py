#!/usr/bin/env python3
"""super-ofp: Convert an Aircalin OFP PDF into an interactive HTML 'super-OFP'.

The output is a single self-contained HTML file that:
  - Preserves the original OFP layout (monospaced text, page breaks, all 63 pages)
  - Overlays editable inputs on selected fields (DISCR, ZFW)
  - Recomputes derived values live (REQUESTED FUEL, TOF, TOW, EBO, LAW, UNDERLOAD)

Usage:  build_super_ofp.py <ofp.pdf> [-o output.html]
"""
from __future__ import annotations
import sys, subprocess, html, re, json, base64, tempfile
from pathlib import Path

# Delegate PDF→column-accurate text extraction to the ofp-extract skill
# (single source of truth). See ~/.claude/skills/ofp-extract/SKILL.md for
# the rationale and algorithm.
sys.path.insert(0, str(Path.home() / '.claude' / 'skills' / 'ofp-extract' / 'scripts'))
from extract_ofp import extract_ofp as _ofp_extract  # noqa: E402

USAGE = "Usage: build_super_ofp.py <ofp.pdf> [-o output.html]"

# --- Anchors / regexes ---------------------------------------------------
FUEL_HEADER_RE   = re.compile(r'^\s+E\.FUEL\s+ACT\.FUEL\s+E\.TME')
WT_HEADER_RE     = re.compile(r'^\s+E\.WT\s+ACT\.\s*WEIGHTS\s+STRUC/OPS LIMITS')
BURN_COEFF_RE    = re.compile(r'FUEL BURN INCREASE PER 1000\s+KGS ADDITIONAL TAKEOFF WT\s+(\d+)\s*KGS')
NAVLOG_HEADER_RE = re.compile(r'^WPT\s+AMC\s+TCD\s+FLT\s+')
DOTS4_RE         = re.compile(r'\.{4}')   # 4 consecutive dots (navlog "...." placeholder)
PLACEHOLDER      = '. . . . .'            # the 9-char dotted slot in the FUEL/WT block
PAGE_FOOTER_RE   = re.compile(r'^\s+PAGE\s+\d+\s+OF\s+\d+\s*$')   # OFP page footer
PAGE_HEADER_RE   = re.compile(r'AIRCALEDONIE INTERNATIONAL BRIEF\s+PAGE\s+(\d+)\s+OF\s+\d+')
FORM_FEED        = '\x0c'

# OFP-header time anchors (for planned-takeoff / lateness computation)
ETD_HDR_RE       = re.compile(r'ETD\s*:\s*(\d+)\s+(\d{4})Z')
ETA_HDR_RE       = re.compile(r'ETA\s*:\s*(\d+)\s+(\d{4})Z')
TDV_HDR_RE       = re.compile(r'TDV\s*:\s*(\d+)\.(\d{2})')

# ETOPS INFORMATION block anchors
SCENARIO_RE      = re.compile(
    r'^(1EO DEPRESS|2ENG DEPRESS|1EO DRIFTDOWN)\s+A\d{3}(?:-\d{3})?\s+([A-Z]{4})/([A-Z]{4})\s*$')
ETP_LINE_RE      = re.compile(r'^ETP\s+\S+\s+\S+\s+\S+\s+\S+\s*/\s*\d+\s+NM/\s*(\d{2}\.\d{2})/')
DIVERT_TIME_RE   = re.compile(r'TIME/\s*(\d{2}\.\d{2})\b')
TOTAL_CRIT_RE    = re.compile(r'TOTAL CRITICAL DIV FUEL\s+(\d{5})')
EST_FR_RE        = re.compile(r'EST F/R OVER ETP\s*/\s*(\d+)')
HOLDING_RE       = re.compile(r'HOLDING\s*/\s*1500\s*FT\s+\d{2}\.\d{2}\s+(\d+)')
# ETP label appearing INSIDE the navlog (one per scenario per ETP), e.g.:
#   "ETP   1EO DRIFTDOWN A330-941"
ETP_NAVLOG_LABEL_RE = re.compile(
    r'^ETP\s+(1EO DEPRESS|2ENG DEPRESS|1EO DRIFTDOWN)\s+A\d{3}(?:-\d{3})?\s*$')

# Single-word uppercase/alphanumeric label (WPT/EEP/EXP). FIR boundary lines
# have several words and are excluded by their multi-token structure plus an
# explicit \bFIR\b check.
WPT_LABEL_RE = re.compile(r'^[A-Z0-9]{2,8}\s*$')

# Weather section anchors
WEATHER_LABEL_RE = re.compile(r'^(DEPARTURE|ARRIVAL|OTHER)\s*:\s*.+$')
METAR_LINE_RE    = re.compile(r'^METAR\s+([A-Z]{4})\b')
TAF_LINE_RE      = re.compile(r'^TAF(?:\s+AMD)?\s+([A-Z]{4})\b')
# Header captures for selective bolding: keyword, ICAO, issue time (DDHHMMZ).
METAR_HDR_RE     = re.compile(r'^(METAR)\s+([A-Z]{4})\s+(\d{6}Z)')
TAF_HDR_RE       = re.compile(r'^(TAF(?:\s+AMD)?)\s+([A-Z]{4})\s+(\d{6}Z)')
# TAF time tokens: DDHH/DDHH ranges and FM DDHHMM single times
TAF_RANGE_RE     = re.compile(r'\b(\d{2})(\d{2})/(\d{2})(\d{2})\b')
TAF_FM_RE        = re.compile(r'\bFM(\d{2})(\d{2})(\d{2})\b')
# SUITABILITY DEST/ALT and FUEL ERA — VALIDITY WINDOW with ETA at end
VALIDITY_WITH_ETA_RE = re.compile(
    r'^([A-Z]{4})\s+VALIDITY WINDOW\s+(\d{2}):(\d{2})Z\s+TO\s+(\d{2}):(\d{2})Z'
    r'\s+ETA\s+(\d{2}):(\d{2})Z\s*$')

# PDF pages whose content is mostly graphics (perf charts, NOTAM maps) and
# is therefore lost by text-only extraction. We render them as base64 PNG
# pages and inline them into the HTML so the document is self-contained.
IMAGE_PAGES_DEFAULT = list(range(24, 32)) + list(range(64, 67))
IMAGE_PAGE_DPI      = 120

# --- Crew-briefing header (page 2) -------------------------------------
# Value-highlights and label rewrites at the top of the CREW BRIEFING. Each
# regex's *odd* capture groups are static labels/gaps and *even* groups are
# the values we wrap with .sofp-hdr-val. Two exceptions: CFP_CI rewrites
# the literal "CFP N" → "OFP #" (same 5-char width — column-safe), and the
# FUEL BURN INCREASE/DECREASE pair drives the strike-through rule.
HDR_AIRCRAFT_RE = re.compile(
    r'^(AIRCRAFT TYPE\s*:\s+)(\S+)(.*?)(REG\s*:\s+)(\S+)(.*?)(DATE\s*:\s+)(\S+)')
HDR_FLIGHT_RE = re.compile(
    r'^(FLIGHT NUMBER\s*:\s+)(\S+)(.*?)(DEP\s*:\s+)(\S+)(.*?)(ARR\s*:\s+)(\S+)')
HDR_CREW_RE = re.compile(r'^(CREW\s*:\s+)(\S+)')
HDR_CFP_CI_RE = re.compile(
    r'^(CFP N)(\s+:\s+)(\d+)(.*?)(CI\s*:\s+)(\S+)')
HDR_ETOPS_TNK_RE = re.compile(
    r'^(ETOPS\s*:\s+)(\d+\s*MIN\s*/\s*\d+\s*NM)(.*?)(TNK\s*:\s+)(\S+)')
HDR_TDV_ETD_ETA_RE = re.compile(
    r'^(TDV\s*:\s+)(\d+\.\d+)(.*?)(ETD\s*:\s+)(\d+\s+\d{4}Z)(.*?)(ETA\s*:\s+)(\d+\s+\d{4}Z)')
HDR_EROPS_RE       = re.compile(r'(EROPS\s*:\s+)(\d+)')
HDR_DEST_ALTN_RE   = re.compile(
    r'^(DEST ALTN\s*:\s+)(\S+)(.*?)(ALTN FUEL\s*:\s+)(\d+)')
HDR_ERA_RE         = re.compile(r'^(FUEL ERA AERODROME\s*:\s+)(\S+)')
FUEL_BURN_INC_RE   = re.compile(
    r'^FUEL BURN INCREASE PER 1000\s+KGS ADDITIONAL TAKEOFF WT\s+(\d+)\s*KGS')
FUEL_BURN_DEC_RE   = re.compile(
    r'^FUEL BURN DECREASE PER 1000\s+KGS SUBTRACT\s+TAKEOFF WT\s+(\d+)\s*KGS')

# DSP COMMENTS bullet-point lines on page 3, format
# "      LABEL :          * VALUE".
DSP_FIELD_RE = re.compile(
    r'^\s+(?P<label>DOWC|EQUIPAGE|PREVI PAX|CODE CGO|MEL/CDL)'
    r'\s*:\s*\*\s+(?P<value>.+?)\s*$')
# PREVI PAX format: "21J-14W-187Y +5INF" — sum the J/W/Y counts, infants
# stay separate per Aircalin convention (loaded in different rows).
PREVI_SEG_RE = re.compile(r'(\d+)[JWY]')
PREVI_INF_RE = re.compile(r'\+(\d+)\s*INF')

# Single-failure scenarios eligible for in-flight comparison (1EO DEPRESS is
# excluded because it is a *combined* failure, not a single one).
INFLIGHT_SCENARIOS = ('2ENG DEPRESS', '1EO DRIFTDOWN')

# SUITABILITY ETOPS header + per-altn VALIDITY WINDOW line (no trailing ETA —
# distinguishes from SUITABILITY DEST/ALT lines which have ETA at end).
SUITABILITY_HDR_RE = re.compile(r'^SUITABILITY ETOPS\s*$')
VALIDITY_LINE_RE   = re.compile(
    r'^([A-Z]{4})\s+VALIDITY WINDOW\s+(\d{2}):(\d{2})Z\s+TO\s+(\d{2}):(\d{2})Z\s*$')

FUEL_ROWS = ['DEST', 'RTE.R', 'ALT.R', 'FIN.R', 'ADD.R', 'EROPS', 'EXTRA',
             'TANKERING', 'T/O FUEL', 'TAXI', 'TOTAL FUEL', 'DISCR', 'REQUESTED FUEL']
WT_ROWS   = ['DOW', 'PLD', 'ZFW', 'TOF', 'TOW', 'EBO', 'LAW']

# --- Extraction ----------------------------------------------------------
def extract(pdf: Path) -> str:
    """Delegate to the ofp-extract skill (single source of truth)."""
    return _ofp_extract(pdf)

def parse_blocks(text: str) -> dict:
    lines = text.split('\n')
    fuel_hdr = next((i for i, l in enumerate(lines) if FUEL_HEADER_RE.match(l)), None)
    wt_hdr   = next((i for i, l in enumerate(lines) if WT_HEADER_RE.match(l)), None)
    burn_m   = BURN_COEFF_RE.search(text)
    burn     = int(burn_m.group(1)) if burn_m else 0

    fuel: dict[str, int] = {}
    if fuel_hdr is not None:
        for line in lines[fuel_hdr + 1: fuel_hdr + 20]:
            for row in FUEL_ROWS:
                m = re.match(rf'^{re.escape(row)}\b.*?(\d{{6}})\s+\.', line)
                if m:
                    fuel[row] = int(m.group(1))
                    break

    wt_est: dict[str, int] = {}
    wt_lim: dict[str, int] = {}
    if wt_hdr is not None:
        for line in lines[wt_hdr + 1: wt_hdr + 15]:
            for row in WT_ROWS:
                m = re.match(rf'^{re.escape(row)}\s+(\d{{6}})\s+\.', line)
                if m:
                    wt_est[row] = int(m.group(1))
                    lim = re.search(
                        rf'{re.escape(row)}\s+\.\s\.\s\.\s\.\s\.\s+(\d{{6}})', line)
                    if lim:
                        wt_lim[row] = int(lim.group(1))
                    break

    # Column where "STRUC/OPS LIMITS" begins on the WT header line — used to
    # drop the UNDERLOAD overlay on the DOW row directly below it.
    struc_col = 48
    if wt_hdr is not None:
        c = lines[wt_hdr].find('STRUC/OPS LIMITS')
        if c >= 0:
            struc_col = c

    # OFP-header times for lateness computation
    etd_m = ETD_HDR_RE.search(text)
    eta_m = ETA_HDR_RE.search(text)
    tdv_m = TDV_HDR_RE.search(text)
    etd_day  = int(etd_m.group(1)) if etd_m else None
    eta_day  = int(eta_m.group(1)) if eta_m else None
    eobt_min = _hhmm_str_to_min(etd_m.group(2)) if etd_m else None
    eta_min  = _hhmm_str_to_min(eta_m.group(2)) if eta_m else None
    tdv_min  = (int(tdv_m.group(1)) * 60 + int(tdv_m.group(2))) if tdv_m else None
    # taxi-out = ETA(printed, landing) - EOBT - TDV  (in minutes, mod 1440 for day rollover)
    if None not in (eobt_min, eta_min, tdv_min):
        diff = eta_min - eobt_min
        if diff < 0:
            diff += 1440
        taxi_out_min = diff - tdv_min
        planned_takeoff_min = (eobt_min + taxi_out_min) % 1440
    else:
        taxi_out_min = 20
        planned_takeoff_min = (eobt_min + taxi_out_min) % 1440 if eobt_min is not None else None

    return {
        'fuel': fuel,
        'wt_est': wt_est,
        'wt_lim': wt_lim,
        'burn_coeff_per_1000kg': burn,
        'header_line_fuel':    fuel_hdr,
        'header_line_wt':      wt_hdr,
        'struc_col':           struc_col,
        'eobt_min':            eobt_min,
        'eta_min':             eta_min,
        'tdv_min':             tdv_min,
        'etd_day':             etd_day,
        'eta_day':             eta_day,
        'taxi_out_min':        taxi_out_min,
        'planned_takeoff_min': planned_takeoff_min,
    }

def _hhmm_str_to_min(s: str) -> int:
    """Convert 'HHMM' string to minutes-since-midnight."""
    v = int(s)
    return (v // 100) * 60 + (v % 100)

# --- HTML transformation -------------------------------------------------
# Editable cells are contenteditable <span>s, not <input>s. Spans participate
# literally in the monospace text flow inside <pre> — "4ch" is exactly 4 Menlo
# cells, with no UA form-control padding/font overrides to push the column
# downstream. The placeholder is drawn via CSS :empty::before so an empty span
# looks identical to the PDF's "....": no width, no gap, no shift.
def make_marker(kind: str, key: str) -> str:
    if kind == 'input':
        return (f'<span class="sofp-in" contenteditable="true" tabindex="0" '
                f'data-target="{key}" data-maxlen="6" '
                f'data-placeholder=". . . . ."></span>')
    if kind == 'output':
        return f'<span class="sofp-out" data-source="{key}">. . . . .</span>'
    if kind == 'underload':
        return f'<span class="sofp-ul" data-source="UNDERLOAD">UL . . . . .</span>'
    raise ValueError(kind)

def make_narrow_input(key: str, maxlen: int = 4, ctme: str = None) -> str:
    # Narrow cells (4 cells) flow as **inline** text — width = literal text
    # width in the monospace grid. Initial content is the PDF placeholder so
    # the cell is exactly 4 cells wide from the start; JS swaps it for the
    # user's value on first keypress.
    ctme_attr = f' data-ctme="{ctme}"' if ctme else ''
    return (f'<span class="sofp-in sofp-w4 is-placeholder" '
            f'contenteditable="true" tabindex="0" '
            f'data-target="{key}" data-maxlen="{maxlen}" '
            f'data-placeholder="...."{ctme_attr}>....</span>')

def make_eta_span(ctme: str) -> str:
    # ETA span also inline — content is always exactly 4 chars (placeholder or HHMM).
    return f'<span class="sofp-eta" data-ctme="{ctme}">....</span>'

def make_diff_span(wpt_n: int, efu: str) -> str:
    # DIFF span — computed = AFU(user) − EFU(printed). EFU embedded as data-efu
    # so JS doesn't need to know about waypoint pairing.
    return (f'<span class="sofp-diff" data-target="DIFF_{wpt_n}" '
            f'data-afu="AFU_{wpt_n}" data-efu="{efu}">....</span>')

def make_etops_eta_marker(tetp: str, divert: str, altns: str) -> str:
    """Inline span appended at end of an ETOPS-scenario header line.
    Holds CTME-to-ETP and divert TIME (both as HHMM strings). JS computes
    the LATEST possible arrival time at the altn (= ETP-crossing time +
    divert time) for this scenario, once ATA_FIRST is entered. The two
    altns of a pair share the same time by ETP definition, so we display
    a single value labelled LATEST."""
    return (f'  <span class="sofp-etops-eta" '
            f'data-tetp="{tetp}" data-divert="{divert}" data-altns="{altns}">'
            f'LATEST --:--</span>')

def build_etops_index(lines: list) -> dict:
    """Map line-index → tagged tuple describing what to do with that line.

    Two kinds of entries are produced:
      • Scenario-header line:
          ('scenario', scenario_name, altn_a, altn_b, tetp_str, divert_str)
        The follow-up lines (ETP S..., TIME/ ...) are scanned to extract
        the CTME-to-ETP and the divert duration (both as 'HHMM' strings).
      • TOTAL CRITICAL DIV FUEL line for the lowest-fuel in-flight scenario:
          ('crit_lowest',)
        For each ETP altn-pair, we compare TOTAL CRITICAL DIV FUEL across
        the two in-flight scenarios (2ENG DEPRESS, 1EO DRIFTDOWN — 1EO
        DEPRESS is excluded because it's a combined, not single, failure)
        and flag the lower one. transform_line wraps its fuel value with
        a highlight span."""
    idx: dict[int, tuple] = {}
    blocks_by_pair: dict[str, list] = {}            # altns -> [(scenario, total_line_idx, fuel)]
    scenarios_by_pair: dict[str, dict] = {}         # altns -> {scenario: (tetp_min, divert_min)}

    for i, line in enumerate(lines):
        m = SCENARIO_RE.match(line)
        if not m:
            continue
        scenario, altn_a, altn_b = m.groups()
        tetp = divert = None
        total_line_idx = None
        total_fuel = None
        est_fr = None
        holding = None

        for j in range(i + 1, min(i + 25, len(lines))):
            l2 = lines[j]
            if tetp is None:
                em = ETP_LINE_RE.match(l2)
                if em:
                    tetp = em.group(1).replace('.', '')
            if est_fr is None:
                fm = EST_FR_RE.search(l2)
                if fm:
                    est_fr = int(fm.group(1))
            if divert is None:
                dm = DIVERT_TIME_RE.search(l2)
                if dm:
                    divert = dm.group(1).replace('.', '')
            if holding is None:
                hm = HOLDING_RE.search(l2)
                if hm:
                    holding = int(hm.group(1))
            if total_line_idx is None:
                tm = TOTAL_CRIT_RE.search(l2)
                if tm:
                    total_line_idx = j
                    total_fuel = int(tm.group(1))
                    break   # TOTAL CRITICAL DIV FUEL is the last line we need

        if tetp and divert:
            idx[i] = ('scenario', scenario, altn_a, altn_b, tetp, divert)
            altns = f'{altn_a}/{altn_b}'
            tetp_min   = int(tetp[:2])  * 60 + int(tetp[2:])
            divert_min = int(divert[:2]) * 60 + int(divert[2:])
            scenarios_by_pair.setdefault(altns, {})[scenario] = (tetp_min, divert_min)
            if total_line_idx is not None and total_fuel is not None:
                blocks_by_pair.setdefault(altns, []).append(
                    (scenario, total_line_idx, total_fuel, est_fr, holding))

    # For each ETP altn-pair, highlight the LOWEST critical fuel among the
    # in-flight scenarios (2ENG DEPRESS vs 1EO DRIFTDOWN). 1EO DEPRESS is
    # excluded per Aircalin in-flight planning convention. Also compute the
    # F/R-OVER-ETP margin: diff = EST F/R OVER ETP − TOTAL CRITICAL DIV FUEL.
    # If diff < HOLDING/1500FT value → orange (margin tighter than 15-min
    # alternate-hold reserve), otherwise green (comfortable margin).
    for altns, scenarios in blocks_by_pair.items():
        elig = [t for t in scenarios if t[0] in INFLIGHT_SCENARIOS]
        if len(elig) >= 2:
            lowest = min(elig, key=lambda x: x[2])
            _sc, lowest_line_idx, lowest_fuel, lowest_fr, lowest_hold = lowest
            diff = (lowest_fr - lowest_fuel) if (lowest_fr is not None) else None
            is_warn = (diff is not None and lowest_hold is not None
                       and diff < lowest_hold)
            idx[lowest_line_idx] = ('crit_lowest', diff, is_warn)

    # ── In-flight VALIDITY WINDOW recalc ───────────────────────────────────
    # Per-ETP delta = (in-flight scenario LATEST) − (1EO DEPRESS LATEST).
    # The in-flight scenario is the one with the **lowest TOTAL CRITICAL
    # DIV FUEL** among single failures (2ENG DEPRESS, 1EO DRIFTDOWN) —
    # 1EO DEPRESS is excluded as a combined failure. Static (no ATA dep).
    # Mapped to altns positionally in the SUITABILITY ETOPS list:
    #   altn[i] uses ETP[min(i, n_etps − 1)]
    def _etp_delta(pair_scens, pair_blocks):
        if '1EO DEPRESS' not in pair_scens:
            return None
        eligible = [(t[0], t[2]) for t in pair_blocks
                    if t[0] in INFLIGHT_SCENARIOS and t[0] in pair_scens]
        if not eligible:
            return None
        lowest_sc, _ = min(eligible, key=lambda x: x[1])
        orig_latest = sum(pair_scens['1EO DEPRESS'])
        new_latest  = sum(pair_scens[lowest_sc])
        return (new_latest - orig_latest, lowest_sc)

    etp_deltas = [_etp_delta(scens, blocks_by_pair.get(pair, []))
                  for pair, scens in scenarios_by_pair.items()]
    etp_deltas = [d for d in etp_deltas if d is not None]

    section = next((i for i, l in enumerate(lines)
                    if SUITABILITY_HDR_RE.match(l)), None)
    if section is not None and etp_deltas:
        altn_pos = 0
        for i in range(section + 1, min(section + 12, len(lines))):
            l = lines[i]
            if not l.strip():
                continue
            m = VALIDITY_LINE_RE.match(l)
            if not m:
                break   # left the SUITABILITY ETOPS section
            start_min = int(m.group(2)) * 60 + int(m.group(3))
            end_min   = int(m.group(4)) * 60 + int(m.group(5))
            delta, sc = etp_deltas[min(altn_pos, len(etp_deltas) - 1)]
            # Stash the raw inputs — JS will recompute live as ATA_FIRST changes.
            idx[i] = ('validity_inflight', sc, start_min, end_min, delta)
            altn_pos += 1

    # ── Highlight in-flight selected ETP scenario in the navlog ────────────
    # The navlog contains 3 ETP rows per ETP pair, each prefixed with a label
    # like "ETP   1EO DRIFTDOWN A330-941" followed by 2 data lines (N-coord,
    # E-coord). For each ETP pair (in route order), highlight the label + 2
    # data lines that correspond to the in-flight selected scenario.
    nav_label_groups: list[list] = []
    cur_group: list = []
    last_label_i = -10
    for i, line in enumerate(lines):
        m = ETP_NAVLOG_LABEL_RE.match(line)
        if m:
            if i - last_label_i > 5 and cur_group:
                nav_label_groups.append(cur_group)
                cur_group = []
            cur_group.append((i, m.group(1)))
            last_label_i = i
    if cur_group:
        nav_label_groups.append(cur_group)

    for grp_pos, group in enumerate(nav_label_groups):
        if grp_pos >= len(etp_deltas):
            break
        selected_sc = etp_deltas[grp_pos][1]
        for label_i, scenario in group:
            if scenario == selected_sc:
                idx[label_i]     = ('etp_navlog_highlight',)        # label line
                idx[label_i + 1] = ('etp_navlog_highlight_data',)   # N-coord
                idx[label_i + 2] = ('etp_navlog_highlight_data',)   # E-coord
                break
    return idx

# --- Weather section ----------------------------------------------------
# Builds annotations for the WEATHER pages of the OFP:
#   • DEPARTURE / ARRIVAL / OTHER labels get a light-brown highlight and an
#     appended time (ETD / ETA / ETOPS window) per the airport's role.
#   • METAR lines are bolded under DEPARTURE; TAF lines are bolded under
#     ARRIVAL/OTHER. The relevant-time substrings inside the TAF (the main
#     validity, plus any BECMG/TEMPO/PROB/FM tokens whose window covers the
#     time of interest) are wrapped with .sofp-taf-period.
# A separate weather_info map (built from SUITABILITY ETOPS / DEST & ALT /
# FUEL ERA) carries per-airport ETAs/windows used both for the appended
# annotation and for TAF-period selection.

def _parse_weather_info(lines: list) -> dict:
    """ICAO -> {kind: 'etops'|'dest'|'era',
                window: (start_min, end_min) | None,
                eta:    hhmm_min | None}.
    'kind' is the source of the most-authoritative window for that airport:
    ETOPS > DEST/ERA. Eta is the printed scheduled ETA at that altn (only
    present for DEST&ALT and FUEL ERA entries)."""
    info: dict = {}
    section = None   # 'etops' | 'dest' | 'era' | None
    for line in lines:
        s = line.strip()
        if s.startswith('SUITABILITY ETOPS'):
            section = 'etops'; continue
        if s.startswith('SUITABILITY DEST'):
            section = 'dest'; continue
        if s.startswith('SUITABILITY FUEL ERA'):
            section = 'era'; continue
        if not s:
            continue
        if section == 'etops':
            m = VALIDITY_LINE_RE.match(line)
            if m:
                icao = m.group(1)
                start = int(m.group(2)) * 60 + int(m.group(3))
                end   = int(m.group(4)) * 60 + int(m.group(5))
                d = info.setdefault(icao, {})
                d['kind']   = 'etops'
                d['window'] = (start, end)
        elif section in ('dest', 'era'):
            m = VALIDITY_WITH_ETA_RE.match(line)
            if m:
                icao = m.group(1)
                start = int(m.group(2)) * 60 + int(m.group(3))
                end   = int(m.group(4)) * 60 + int(m.group(5))
                eta   = int(m.group(6)) * 60 + int(m.group(7))
                d = info.setdefault(icao, {})
                if d.get('kind') != 'etops':
                    d['kind']   = section
                    d['window'] = (start, end)
                d['eta'] = eta
    return info

def _next_icao(lines: list, start: int, limit: int = 12) -> str | None:
    """Find the first METAR/TAF ICAO within *limit* lines after *start*."""
    for j in range(start + 1, min(start + 1 + limit, len(lines))):
        mm = METAR_LINE_RE.match(lines[j])
        if mm:
            return mm.group(1)
        tm = TAF_LINE_RE.match(lines[j])
        if tm:
            return tm.group(1)
        # Stop if we hit the next label
        if WEATHER_LABEL_RE.match(lines[j]):
            return None
    return None

def build_weather_decorations(lines: list, parsed: dict) -> dict:
    """line_idx → tag tuple for weather-section decorations.

    Tags:
      ('weather_label', label_type, icao, info_dict)
      ('weather_metar', bold:bool, icao)
      ('weather_taf',   bold:bool, icao, times:list[(start_min,end_min)])
        times = the time(s) of interest for *this* airport, used to
        highlight matching TAF period tokens. Each entry is a half-open
        absolute-minute range [day*1440 + min, day*1440 + min) — a point
        ETA is stored as (t, t+1) and a window as (t_start, t_end).
    """
    info_by_icao = _parse_weather_info(lines)
    day_d = parsed.get('etd_day') or 1
    day_a = parsed.get('eta_day') or day_d
    eobt  = parsed.get('eobt_min')
    eta   = parsed.get('eta_min')

    deco: dict = {}
    current_label = None     # 'DEPARTURE' | 'ARRIVAL' | 'OTHER'
    current_icao  = None
    current_times: list = []

    for i, line in enumerate(lines):
        m = WEATHER_LABEL_RE.match(line)
        if m:
            label_type = m.group(1)
            icao = _next_icao(lines, i)
            ai = info_by_icao.get(icao, {}) if icao else {}
            times: list = []
            if label_type == 'DEPARTURE' and eobt is not None:
                times.append((day_d * 1440 + eobt, day_d * 1440 + eobt + 1))
            elif label_type == 'ARRIVAL' and eta is not None:
                times.append((day_a * 1440 + eta, day_a * 1440 + eta + 1))
            elif label_type == 'OTHER':
                kind = ai.get('kind')
                if kind == 'etops' and 'window' in ai:
                    ws, we = ai['window']
                    times.append((day_a * 1440 + ws, day_a * 1440 + we))
                elif 'eta' in ai:
                    e = ai['eta']
                    times.append((day_a * 1440 + e, day_a * 1440 + e + 1))
            current_label = label_type
            current_icao  = icao
            current_times = times
            deco[i] = ('weather_label', label_type, icao, ai)
            continue
        if METAR_LINE_RE.match(line):
            deco[i] = ('weather_metar', current_label == 'DEPARTURE', current_icao)
            continue
        if TAF_LINE_RE.match(line):
            deco[i] = ('weather_taf',
                       current_label in ('ARRIVAL', 'OTHER'),
                       current_icao, list(current_times))
            continue
    return deco

def _taf_period_spans(line: str, times: list) -> list:
    """Return [(start, end, html)] for time-tokens in *line* whose window
    overlaps any range in *times*. Uses 30-day month wraparound rules: a
    TAF range with end day < start day rolls to next month. The reference
    day from *times* is used to disambiguate which month a TAF token
    refers to."""
    if not times:
        return []
    ref_day = times[0][0] // 1440
    spans: list = []

    def _overlap(a_start, a_end, b_start, b_end):
        return not (a_end <= b_start or a_start >= b_end)

    def _abs_min(dd, hh, mm=0):
        # Snap dd to the same month as ref_day (TAF day-of-month → absolute min).
        if dd < ref_day - 5:        # wrapped to next month
            dd += 31
        return dd * 1440 + hh * 60 + mm

    for m in TAF_RANGE_RE.finditer(line):
        d1, h1, d2, h2 = (int(g) for g in m.groups())
        a = _abs_min(d1, h1)
        b = _abs_min(d2, h2)
        if b <= a:
            b += 31 * 1440   # shouldn't happen with above remap, but safe
        if any(_overlap(a, b, t0, t1) for (t0, t1) in times):
            spans.append((m.start(), m.end(), m.group(0)))

    for m in TAF_FM_RE.finditer(line):
        d, h, mn = (int(g) for g in m.groups())
        a = _abs_min(d, h, mn)
        # FM is an instant; treat as covering until the next FM/end-of-validity.
        # We don't have look-ahead context here, so treat FM as a 6h block —
        # generous but a useful visual cue when ETA is just after FM.
        b = a + 6 * 60
        if any(_overlap(a, b, t0, t1) for (t0, t1) in times):
            spans.append((m.start(), m.end(), m.group(0)))

    # Deduplicate overlapping spans (TAF_FM and TAF_RANGE shouldn't collide
    # by construction, but be safe).
    spans.sort()
    out: list = []
    last_end = -1
    for s, e, txt in spans:
        if s >= last_end:
            out.append((s, e, f'<span class="sofp-taf-period">{html.escape(txt)}</span>'))
            last_end = e
    return out

def _fmt_hhmm(mins: int) -> str:
    return f'{(mins // 60) % 24:02d}:{mins % 60:02d}'

def _wx_header_bold_spans(line: str, hdr_re) -> list:
    """[(s, e, html)] bolding the keyword, ICAO and DDHHMMZ issue time only."""
    m = hdr_re.match(line)
    if not m:
        return []
    spans = []
    for i in (1, 2, 3):
        s, e = m.start(i), m.end(i)
        spans.append((s, e, f'<span class="sofp-wx-bold">{html.escape(line[s:e])}</span>'))
    return spans

# --- Crew-briefing header decorations -----------------------------------
def _hl_val(text: str) -> str:
    return f'<span class="sofp-hdr-val">{html.escape(text)}</span>'

def _previ_pax_sum(value: str) -> str:
    """'21J-14W-187Y +5INF' → '= 222 PAX (+5 INF)'.
    Infants are tracked separately because they don't occupy a seat —
    keep them visible but not folded into the seat-count total."""
    pax = sum(int(m.group(1)) for m in PREVI_SEG_RE.finditer(value))
    inf_m = PREVI_INF_RE.search(value)
    inf = int(inf_m.group(1)) if inf_m else 0
    return f'= {pax} PAX (+{inf} INF)' if inf else f'= {pax} PAX'

def _briefing_value_spans(line: str) -> list:
    """Return [(start, end, html)] for known briefing-header patterns.
    Each match yields one or more value-highlight replacements; for the
    CFP/CI line we ALSO rewrite the label "CFP N" → "OFP #" (same 5-char
    width — column alignment preserved)."""
    # Each entry: (regex, value-group-indices, optional label-rewrite-tuple)
    # label-rewrite = (group_index, replacement_html)
    table = [
        (HDR_AIRCRAFT_RE,    (2, 5, 8), None),
        (HDR_FLIGHT_RE,      (2, 5, 8), None),
        (HDR_CREW_RE,        (2,),      None),
        (HDR_CFP_CI_RE,      (3, 6),
            (1, '<span class="sofp-hdr-label">OFP #</span>')),
        (HDR_ETOPS_TNK_RE,   (2, 5),    None),
        (HDR_TDV_ETD_ETA_RE, (2, 5, 8), None),
        (HDR_DEST_ALTN_RE,   (2, 5),    None),
        (HDR_ERA_RE,         (2,),      None),
    ]
    for rx, val_groups, label in table:
        m = rx.match(line)
        if not m:
            continue
        spans = []
        for g in val_groups:
            s, e = m.start(g), m.end(g)
            spans.append((s, e, _hl_val(line[s:e])))
        if label is not None:
            g, repl = label
            spans.append((m.start(g), m.end(g), repl))
        return spans
    # EROPS lives on the FOB line — use search() (not match) and only that one.
    m = HDR_EROPS_RE.search(line)
    if m:
        s, e = m.start(2), m.end(2)
        return [(s, e, _hl_val(line[s:e]))]
    # DSP COMMENTS bullet fields (page 3): DOWC, EQUIPAGE, PREVI PAX (with
    # sum), CODE CGO, MEL/CDL — same yellow value-highlight style. The
    # value comes after "<label> : * " and runs to end-of-line; we don't
    # touch anything to the right so the trailing-newline column isn't an
    # issue and downstream lines stay untouched.
    m = DSP_FIELD_RE.match(line)
    if m:
        label = m.group('label')
        s, e  = m.start('value'), m.end('value')
        value = line[s:e]
        if label == 'PREVI PAX':
            repl = (_hl_val(value)
                    + '  <span class="sofp-pax-sum">'
                    + html.escape(_previ_pax_sum(value)) + '</span>')
        else:
            repl = _hl_val(value)
        return [(s, e, repl)]
    return []

def build_briefing_decorations(lines: list) -> dict:
    """Tag briefing-header lines for value highlights + CFP→OFP rewrite,
    plus the strike-through rule on the FUEL BURN DECREASE line when its
    coefficient equals the INCREASE value (can't be both true)."""
    deco: dict = {}
    inc_val = None
    dec_line = None
    dec_val  = None
    for i, line in enumerate(lines):
        spans = _briefing_value_spans(line)
        if spans:
            deco[i] = ('value_highlights', spans)
            continue
        m = FUEL_BURN_INC_RE.match(line)
        if m:
            inc_val = m.group(1); continue
        m = FUEL_BURN_DEC_RE.match(line)
        if m:
            dec_line = i; dec_val = m.group(1); continue
    if dec_line is not None and inc_val is not None and inc_val == dec_val:
        deco[dec_line] = ('strike_line',)
    return deco

def apply_replacements(line: str, replacements: list) -> str:
    """Replace non-overlapping ``(start, end, html)`` slices in *line*, escaping
    the surrounding text. Replacements may be passed in any order."""
    replacements = sorted(replacements, key=lambda r: r[0])
    out, cursor = [], 0
    for start, end, new in replacements:
        out.append(html.escape(line[cursor:start]))
        out.append(new)
        cursor = end
    out.append(html.escape(line[cursor:]))
    return ''.join(out)

def build_navlog_index(lines: list, parsed: dict) -> dict:
    """Scan the *first* navlog block and tag every data line as 'n_data'
    (3 trailing '....' — A.RF / AFU / DIFF) or 'e_data' (2 trailing '....'
    sandwiched between numeric anchors — ETA / ATA). Each entry stores the
    CTME of the row and an ``is_first`` flag for the departure waypoint.

    Walks **every** navlog block in the OFP — the main (VTBS→NWWW) plus any
    alternate navlog (e.g. NWWW→YBBN). For alternate navlogs, each row's
    CTME is offset-adjusted by the main navlog's destination CTME, so the
    JS recompute (`ETA = ATA_FIRST + CTME`) yields the correct ETA for
    alternate waypoints anchored on the main destination's ETA. Only the
    main navlog's first waypoint gets the editable `ATA_FIRST` input;
    alternate first waypoints are computed (their time = main destination
    ETA, which is itself ATA_FIRST + main destination CTME)."""
    headers = [i for i, l in enumerate(lines) if NAVLOG_HEADER_RE.match(l)]
    if not headers:
        return {}

    def _hhmm_min(s: str) -> int:
        return int(s[:2]) * 60 + int(s[2:])

    def _min_hhmm(m: int) -> str:
        return f'{m // 60:02d}{m % 60:02d}'

    idx: dict[int, tuple] = {}
    main_dest_ctme_min = 0   # captured at end of main navlog, reused for alt offsets
    wpt_n = 0                # global waypoint counter (unique across all navlogs)

    def _peek_efu(start_i: int, stop_i: int) -> str | None:
        """Look ahead from an n_data line for the matching e_data line and
        return its EFU (last 4-digit token)."""
        for j in range(start_i, min(start_i + 5, stop_i)):
            ts = lines[j].split()
            if (len(ts) >= 4 and ts[-4:-2] == ['....', '....']
                    and re.fullmatch(r'\d{4}', ts[-2])
                    and re.fullmatch(r'\d{4}', ts[-1])):
                return ts[-1]
        return None

    for nav_i, start in enumerate(headers):
        end       = headers[nav_i + 1] if nav_i + 1 < len(headers) else len(lines)
        is_main   = (nav_i == 0)
        offset    = 0 if is_main else main_dest_ctme_min
        # is_first now means "main navlog's first waypoint" specifically — that
        # cell uses ATA_0 as the anchor for all other ETA computations.
        pending_first = is_main
        last_ctme_str = None
        last_ctme_min = 0
        pending_wpt_n = wpt_n   # set on n_data, reused on the matching e_data

        for i in range(start + 1, end):
            toks = lines[i].split()
            if len(toks) < 4:
                continue
            # N-coord (or coord-less) data line: ends with 3 "...." preceded by 4-digit CTME
            if toks[-3:] == ['....', '....', '....'] and re.fullmatch(r'\d{4}', toks[-4]):
                raw_ctme = toks[-4]
                last_ctme_min = _hhmm_min(raw_ctme)
                # For alt navlogs, anchor each CTME on the main destination ETA
                last_ctme_str = _min_hhmm(offset + last_ctme_min)
                efu = _peek_efu(i + 1, end)
                pending_wpt_n = wpt_n
                idx[i] = ('n_data', last_ctme_str, pending_first, wpt_n, efu)
                continue
            # E-coord continuation line: 2 "...." then two 4-digit numbers (E.RF, EFU)
            if (toks[-4:-2] == ['....', '....']
                    and re.fullmatch(r'\d{4}', toks[-2])
                    and re.fullmatch(r'\d{4}', toks[-1])):
                idx[i] = ('e_data', last_ctme_str, pending_first, pending_wpt_n)
                if pending_first:
                    pending_first = False
                wpt_n += 1   # one wpt per N+E pair

        # After main navlog, remember its destination CTME so alts start from it
        if is_main:
            main_dest_ctme_min = last_ctme_min
    return idx

def build_navlog_decorations(lines: list, navlog_idx: dict) -> dict:
    """Returns line_idx → tag for navlog-label decorations:
      ('eep_label', ctme_str)     — EEP row gets light-brown bg + '-30 MIN: HH:MM' annotation
      ('exp_label',)              — EXP row gets light-brown bg
      ('special_data_highlight',) — N/E data lines following EEP/EXP (same bg)
      ('wpt_label_bold',)         — single-word waypoint label (not FIR)
    """
    deco: dict[int, tuple] = {}
    n_data_lines = sorted(i for i, info in navlog_idx.items() if info[0] == 'n_data')
    for n_i in n_data_lines:
        ctme = navlog_idx[n_i][1]   # offset-adjusted HHMM string
        # Look back past at most 3 blank lines for the wpt label
        for back in range(1, 4):
            j = n_i - back
            if j < 0:
                break
            l = lines[j]
            if not l.strip():
                continue
            if l.strip().startswith('-'):
                break       # separator → no wpt label for this n_data
            stripped = l.strip()
            if stripped == 'EEP':
                deco[j]       = ('eep_label', ctme)
                deco[n_i]     = ('special_data_highlight',)
                # E-coord row (= next line that navlog_idx tagged as e_data)
                if (n_i + 1) in navlog_idx and navlog_idx[n_i + 1][0] == 'e_data':
                    deco[n_i + 1] = ('special_data_highlight',)
                break
            if stripped == 'EXP':
                deco[j]       = ('exp_label',)
                deco[n_i]     = ('special_data_highlight',)
                if (n_i + 1) in navlog_idx and navlog_idx[n_i + 1][0] == 'e_data':
                    deco[n_i + 1] = ('special_data_highlight',)
                break
            if re.search(r'\bFIR\b', l):
                break       # FIR boundary line — leave plain
            if WPT_LABEL_RE.match(stripped):
                deco[j] = ('wpt_label_bold',)
            break
    return deco

def render_navlog_line(line: str, info: tuple) -> str:
    positions = [m.start() for m in DOTS4_RE.finditer(line)]
    rep: list = []
    kind = info[0]
    if kind == 'n_data':
        _, ctme, _is_first, wpt_n, efu = info
        # A.RF, AFU on ALL waypoints; DIFF span (computed = AFU − EFU)
        if len(positions) >= 1:
            rep.append((positions[0], positions[0] + 4, make_narrow_input(f'ARF_{wpt_n}')))
        if len(positions) >= 2:
            rep.append((positions[1], positions[1] + 4, make_narrow_input(f'AFU_{wpt_n}')))
        if len(positions) >= 3 and efu is not None:
            rep.append((positions[2], positions[2] + 4, make_diff_span(wpt_n, efu)))
    elif kind == 'e_data':
        _, ctme, _is_first, wpt_n = info
        # ETA (computed) on every line; ATA input on ALL waypoints (was first-only)
        if ctme is not None and len(positions) >= 1:
            rep.append((positions[0], positions[0] + 4, make_eta_span(ctme)))
        if len(positions) >= 2:
            rep.append((positions[1], positions[1] + 4,
                        make_narrow_input(f'ATA_{wpt_n}', ctme=ctme)))
    if not rep:
        return html.escape(line)
    return apply_replacements(line, rep)

def transform_line(line: str, parsed: dict, navlog_idx: dict,
                   etops_idx: dict, idx: int) -> str:
    fuel_hdr  = parsed['header_line_fuel']
    wt_hdr    = parsed['header_line_wt']
    struc_col = parsed['struc_col']

    marker = None
    if fuel_hdr is not None and fuel_hdr < idx < fuel_hdr + 20:
        if   re.match(r'^DISCR\b', line):          marker = ('input',  'DISCR')
        elif re.match(r'^REQUESTED FUEL\b', line): marker = ('output', 'REQUESTED_FUEL')
        elif re.match(r'^TAXI\b', line):           marker = ('input',  'TAXI')

    if wt_hdr is not None and wt_hdr < idx < wt_hdr + 15:
        for row in WT_ROWS:
            if re.match(rf'^{re.escape(row)}\b', line):
                if   row == 'ZFW':                        marker = ('input',  'ZFW')
                elif row in ('TOF', 'TOW', 'EBO', 'LAW'): marker = ('output', row)
                break

    # Inline marker (replace the first '. . . . .' placeholder if present)
    if marker is not None:
        pos = line.find(PLACEHOLDER)
        if pos >= 0:
            out = (html.escape(line[:pos])
                   + make_marker(marker[0], marker[1])
                   + html.escape(line[pos + len(PLACEHOLDER):]))
        else:
            out = html.escape(line)
    elif idx in navlog_idx:
        out = render_navlog_line(line, navlog_idx[idx])
        # Selected-ETP-scenario data line — wrap whole rendered line in highlight
        info = etops_idx.get(idx)
        if info and info[0] == 'etp_navlog_highlight_data':
            out = f'<span class="sofp-etp-highlight">{out}</span>'
        elif info and info[0] == 'special_data_highlight':
            out = f'<span class="sofp-special-highlight">{out}</span>'
    elif idx in etops_idx:
        info = etops_idx[idx]
        if info[0] == 'scenario':
            _, scenario, altn_a, altn_b, tetp, divert = info
            out = html.escape(line) + make_etops_eta_marker(tetp, divert, f'{altn_a}/{altn_b}')
        elif info[0] == 'crit_lowest':
            # Highlight the first 5-digit number after "TOTAL CRITICAL DIV FUEL"
            # and insert the F/R-margin annotation INSIDE the trailing spaces
            # so the per-altn columns to the right stay aligned.
            _, diff, is_warn = info
            m = TOTAL_CRIT_RE.search(line)
            if m:
                start = line.index(m.group(1), m.start())
                end   = start + len(m.group(1))
                # Find where the next column starts (end of the gap)
                next_pos = end
                while next_pos < len(line) and line[next_pos] == ' ':
                    next_pos += 1
                gap_len = next_pos - end
                if diff is not None:
                    sign = '+' if diff >= 0 else '−'
                    ann  = f'{sign}{abs(diff)}'
                    cls  = 'sofp-fr-warn' if is_warn else 'sofp-fr-ok'
                    # 1 space before, annotation, then enough trailing spaces
                    # to keep total width == original gap (so following cols
                    # don't shift). Fall back to 1-space gap if annotation is
                    # wider than the original gap.
                    consumed = 1 + len(ann)
                    spaces_after = max(1, gap_len - consumed)
                    middle = (' ' + f'<span class="{cls}">{ann}</span>'
                              + ' ' * spaces_after)
                else:
                    middle = ' ' * gap_len
                out = (html.escape(line[:start])
                       + f'<span class="sofp-crit-lowest">{m.group(1)}</span>'
                       + middle
                       + html.escape(line[next_pos:]))
            else:
                out = html.escape(line)
        elif info[0] == 'etp_navlog_highlight':
            # Label line ("ETP   1EO DRIFTDOWN A330-941") for the selected scenario
            out = f'<span class="sofp-etp-highlight">{html.escape(line)}</span>'
        elif info[0] == 'etp_navlog_highlight_data':
            # Data line falls here only if it wasn't already in navlog_idx
            out = f'<span class="sofp-etp-highlight">{html.escape(line)}</span>'
        elif info[0] == 'eep_label':
            _, ctme = info
            # Light-brown highlight + bold + appended '-30 MIN: HH:MM' (blue, live)
            out = (f'<span class="sofp-eep-label">{html.escape(line)}</span>'
                   f' <span class="sofp-eep-warn" data-ctme="{ctme}">-30 MIN: --:--</span>')
        elif info[0] == 'exp_label':
            out = f'<span class="sofp-exp-label">{html.escape(line)}</span>'
        elif info[0] == 'wpt_label_bold':
            out = f'<span class="sofp-wpt-name">{html.escape(line)}</span>'
        elif info[0] == 'special_data_highlight':
            # Data line tagged but not in navlog_idx — rare; just wrap
            out = f'<span class="sofp-special-highlight">{html.escape(line)}</span>'
        elif info[0] == 'validity_inflight':
            _, sc, orig_start, orig_end, delta = info
            # JS reads data-* and writes the formatted text on every
            # recompute(). Initial body shows the static planned-takeoff
            # window so something sensible appears before ATA_FIRST is entered.
            out = (html.escape(line)
                   + '   '
                   + f'<span class="sofp-validity-inflight" '
                   + f'data-scenario="{sc}" data-orig-start="{orig_start}" '
                   + f'data-orig-end="{orig_end}" data-delta="{delta}">'
                   + f'{sc}: …</span>')
        elif info[0] == 'weather_label':
            _, label_type, icao, ai = info
            ann = ''
            if label_type == 'DEPARTURE' and parsed.get('eobt_min') is not None:
                ann = f'   <span class="sofp-weather-time">ETD {_fmt_hhmm(parsed["eobt_min"])}Z</span>'
            elif label_type == 'ARRIVAL' and parsed.get('eta_min') is not None:
                ann = f'   <span class="sofp-weather-time">ETA {_fmt_hhmm(parsed["eta_min"])}Z</span>'
            elif label_type == 'OTHER' and ai:
                kind = ai.get('kind')
                if kind == 'etops' and 'window' in ai:
                    ws, we = ai['window']
                    ann = (f'   <span class="sofp-weather-time">ETOPS '
                           f'{_fmt_hhmm(ws)}–{_fmt_hhmm(we)}Z</span>')
                elif 'eta' in ai:
                    label = 'ALTN' if kind == 'dest' else ('ERA' if kind == 'era' else 'ETA')
                    ann = (f'   <span class="sofp-weather-time">{label} ETA '
                           f'{_fmt_hhmm(ai["eta"])}Z</span>')
            out = f'<span class="sofp-weather-label">{html.escape(line)}</span>{ann}'
        elif info[0] == 'weather_metar':
            _, bold, _icao = info
            spans = _wx_header_bold_spans(line, METAR_HDR_RE) if bold else []
            out = apply_replacements(line, spans) if spans else html.escape(line)
        elif info[0] == 'weather_taf':
            _, bold, _icao, times = info
            spans = _taf_period_spans(line, times)
            if bold:
                spans += _wx_header_bold_spans(line, TAF_HDR_RE)
            out = apply_replacements(line, spans) if spans else html.escape(line)
        elif info[0] == 'value_highlights':
            out = apply_replacements(line, info[1])
        elif info[0] == 'strike_line':
            out = f'<span class="sofp-strike-line">{html.escape(line)}</span>'
        else:
            out = html.escape(line)
    else:
        out = html.escape(line)

    # UNDERLOAD overlay: on the DOW row, anchored under STRUC/OPS LIMITS header.
    if (wt_hdr is not None and wt_hdr < idx < wt_hdr + 15
            and re.match(r'^DOW\b', line)):
        content_len = len(line.rstrip())
        pad = max(1, struc_col - content_len)
        out += (' ' * pad) + make_marker('underload', 'UNDERLOAD')

    return out

def build_image_pages_html(pdf: Path, pages: list = None) -> dict:
    """Render each PDF page in *pages* as a base64 PNG embedded in a
    self-contained block. Returns {pdf_page_num: html}.

    Silently returns {} on missing pdftoppm or other failures — text-only
    output still works. Pages are grouped into contiguous ranges to
    minimize pdftoppm invocations."""
    if pages is None:
        pages = IMAGE_PAGES_DEFAULT
    out: dict = {}
    if not pages:
        return out
    pages = sorted(set(pages))
    ranges: list = []
    s = e = pages[0]
    for p in pages[1:]:
        if p == e + 1:
            e = p
        else:
            ranges.append((s, e)); s = e = p
    ranges.append((s, e))

    for first, last in ranges:
        with tempfile.TemporaryDirectory() as tmpd:
            prefix = Path(tmpd) / 'p'
            try:
                subprocess.run(
                    ['pdftoppm', '-png', '-r', str(IMAGE_PAGE_DPI),
                     '-f', str(first), '-l', str(last),
                     str(pdf), str(prefix)],
                    check=True, capture_output=True)
            except (FileNotFoundError, subprocess.CalledProcessError):
                continue
            for png in sorted(Path(tmpd).glob('p-*.png')):
                try:
                    n = int(png.stem.rsplit('-', 1)[-1])
                except ValueError:
                    continue
                b64 = base64.b64encode(png.read_bytes()).decode('ascii')
                out[n] = (
                    f'<div class="page pdf-image">'
                    f'<div class="pdf-image-label">PDF page {n}</div>'
                    f'<img src="data:image/png;base64,{b64}" alt="PDF page {n}">'
                    f'</div>')
    return out

def render_body(text: str, parsed: dict, pdf: Path = None) -> str:
    """Split into logical OFP pages and emit one ``<pre class="page">`` per page.

    Page breaks land directly after each ``PAGE N OF 63`` footer line (the OFP's
    own page numbering), so a logical page always starts with the next
    ``AIR CALEDONIE ... PAGE N+1 OF 63`` header. This is more robust than
    splitting on form-feed characters (which sit at PDF page boundaries and can
    misalign with the OFP's logical pagination).

    If *pdf* is given, image-only pages (perf charts, NOTAM maps — see
    IMAGE_PAGES_DEFAULT) are rendered as base64 PNG blocks and substituted
    in at the corresponding OFP page position. PDF pages beyond the OFP's
    own pagination (e.g. trailing chart inserts) are appended at the end."""
    full_lines  = text.split('\n')
    navlog_idx  = build_navlog_index(full_lines, parsed)
    etops_idx   = build_etops_index(full_lines)
    # Navlog decorations (EEP/EXP highlight, WPT name bold) live alongside
    # the etops tags — different tag types, same dispatch in transform_line.
    etops_idx.update(build_navlog_decorations(full_lines, navlog_idx))
    etops_idx.update(build_weather_decorations(full_lines, parsed))
    etops_idx.update(build_briefing_decorations(full_lines))
    transformed = [
        transform_line(l, parsed, navlog_idx, etops_idx, i)
        for i, l in enumerate(full_lines)
    ]
    # Pair each text page with its OFP page number, parsed from the
    # "AIRCALEDONIE INTERNATIONAL BRIEF PAGE N OF M" header that opens every
    # page. We split on HEADER (not footer): image-only pages — perf charts,
    # NOTAM maps — survive text extraction only as their own header line with
    # no footer of their own, so footer-based splitting silently merged them
    # into the next text page. Header-splitting gives each OFP page its own
    # output slot, which the image-interleave logic below uses to substitute.
    out_pages: list = []           # [(page_num_or_None, html)]
    current: list = []
    current_num: int | None = None
    for i, html_line in enumerate(transformed):
        # Form-feed control chars hide inside the AIR CALEDONIE header lines —
        # strip them so they don't render as a literal control character.
        if FORM_FEED in full_lines[i]:
            html_line = html_line.replace(FORM_FEED, '')
        hm = PAGE_HEADER_RE.search(full_lines[i])
        if hm:
            # Flush the previous page (everything up to but not including
            # this header) and start a new page tagged with the header's
            # OFP page number.
            if current:
                out_pages.append((current_num,
                                  '<pre class="page">' + '\n'.join(current) + '</pre>'))
            current = [html_line]
            current_num = int(hm.group(1))
        else:
            current.append(html_line)
    if current:
        out_pages.append((current_num,
                          '<pre class="page">' + '\n'.join(current) + '</pre>'))

    image_html = build_image_pages_html(pdf) if pdf is not None else {}

    final: list = []
    seen: set = set()
    for num, h in out_pages:
        if num is not None and num in image_html:
            final.append(image_html[num])
            seen.add(num)
        else:
            final.append(h)
    # Append image pages that lie beyond the OFP's own pagination (e.g. 64-66).
    for n in sorted(image_html):
        if n not in seen:
            final.append(image_html[n])
    return '\n'.join(final)

# --- Template ------------------------------------------------------------
TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root {
  --bg: #efeae0;
  --paper: #fffdf6;
  --ink: #1c1c1c;
  --accent: #b03021;
  --input-bg: #fff5b8;
  --output-bg: #d8f0cc;
  --output-ink: #145c14;
  --ul-bg: #cfe4ff;
  --ul-ink: #0a4a8a;
  --warn-bg: #ffd0d0;
  --warn-ink: #a01010;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 18px;
  background: var(--bg);
  font-family: 'Menlo', 'Consolas', 'Courier New', monospace;
  color: var(--ink);
  font-size: 11px;
  line-height: 1.22;
}
.toolbar {
  position: sticky;
  top: 6px;
  z-index: 10;
  background: #fff;
  border: 1px solid #bbb;
  border-radius: 6px;
  padding: 8px 14px;
  margin: 0 auto 14px;
  max-width: 960px;
  display: flex;
  gap: 14px;
  align-items: center;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  font-size: 13px;
  box-shadow: 0 2px 6px rgba(0,0,0,.08);
}
.toolbar strong { color: var(--accent); letter-spacing: .5px; }
.toolbar .spacer { flex: 1; }
.toolbar button {
  font: inherit;
  padding: 4px 10px;
  background: #f4f4f4;
  border: 1px solid #aaa;
  border-radius: 4px;
  cursor: pointer;
}
.toolbar button:hover { background: #e8e8e8; }
.page {
  max-width: 960px;
  margin: 0 auto 14px;
  background: var(--paper);
  padding: 28px 30px;
  border: 1px solid #c5c0b3;
  box-shadow: 0 2px 8px rgba(0,0,0,.08);
  white-space: pre;
  overflow-x: auto;
  break-after: page;
  font-size: 11px;
}
/* Image-only PDF pages (perf charts, NOTAM maps) rendered as inline PNGs.
   Drop the pre-formatted text styling, center the image, scale to page width. */
.page.pdf-image {
  white-space: normal;
  text-align: center;
  padding: 14px;
}
.page.pdf-image img {
  max-width: 100%;
  height: auto;
  display: block;
  margin: 0 auto;
}
.pdf-image-label {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  font-size: 11px;
  color: #888;
  margin-bottom: 8px;
  text-align: right;
}
/* Strategy
   --------
   * WIDE cells (fuel/wt) use display:inline-block + min-width:9ch so the slot
     stays 9 cells wide even when content is only 6 digits.
   * NARROW cells (navlog) flow as **inline text with NO box** — no display
     rule, no background, no bold weight. The text content (always 4 chars,
     "...." or HHMM) IS the slot. This makes the cell render at literally
     the same width as the 4 cells of plain "...." it replaced.
   * No bold on narrow cells. font-weight:700 in some browsers triggers
     subpixel-padding that the background fills, visually widening the cell.
   * Backgrounds appear on hover/focus only — interaction feedback without
     altering the default layout.
*/
.sofp-in, .sofp-out, .sofp-ul, .sofp-eta {
  font-family: inherit;
  font-size: inherit;
  line-height: inherit;
  border-radius: 1px;
  margin: 0;
  padding: 0;
  border: 0;
}

/* ---------- Wide editable cells (DISCR, TAXI, ZFW — 9-cell slot) ---------- */
.sofp-in:not(.sofp-w4) {
  display: inline-block;
  min-width: 9ch;
  vertical-align: baseline;
  text-align: right;
  box-sizing: content-box;
  background: var(--input-bg);
  color: var(--accent);
  font-weight: 700;
  cursor: text;
  outline: none;
  box-shadow: inset 0 -1px 0 0 #b8862c;
}
.sofp-in:not(.sofp-w4):hover { box-shadow: inset 0 0 0 1px #b8862c; }
.sofp-in:not(.sofp-w4):focus { background: #fff4a3; box-shadow: inset 0 0 0 2px var(--accent); }
.sofp-in:not(.sofp-w4):empty::before {
  content: attr(data-placeholder);
  color: #b8862c; opacity: 0.55; font-weight: 400;
}
.sofp-in:not(.sofp-w4):focus:empty::before { content: ''; }

/* ---------- Narrow editable cells (ATA, A.RF, AFU — 4-cell inline) ---------- */
.sofp-in.sofp-w4 {
  color: var(--accent);
  font-weight: inherit;          /* match surrounding <pre> weight */
  background: transparent;       /* no bg = no overflow artifact */
  cursor: text;
  outline: none;
  border-bottom: 1px dashed #b8862c;   /* editable hint, doesn't affect width */
}
.sofp-in.sofp-w4.is-placeholder {
  color: #b8862c;
  opacity: 0.55;
}
.sofp-in.sofp-w4:hover { background: #fff8d2; }
.sofp-in.sofp-w4:focus {
  background: #fff4a3;
  outline: 1px solid var(--accent);
  outline-offset: 0;
}

/* ---------- Outputs and badges ---------- */
.sofp-out {
  display: inline-block;
  min-width: 9ch;
  background: var(--output-bg);
  color: var(--output-ink);
  font-weight: 700;
  text-align: right;
  vertical-align: baseline;
  box-sizing: content-box;
}
.sofp-ul {
  display: inline-block;
  background: var(--ul-bg);
  color: var(--ul-ink);
  font-weight: 700;
  vertical-align: baseline;
}
.sofp-warn { background: var(--warn-bg) !important; color: var(--warn-ink) !important; }
/* Computed ETA: inline, color only (text always 4 chars) */
.sofp-eta {
  color: #0a4a8a;
  font-weight: inherit;
}
/* ETOPS scenario ETA — inline, color-only, appended at end of scenario header */
.sofp-etops-eta {
  color: #0a4a8a;
  font-weight: 700;
  background: #e3eaff;
  padding: 0 2px;
  border-radius: 2px;
}
/* Per-ETP lowest TOTAL CRITICAL DIV FUEL among in-flight scenarios
   (2ENG DEPRESS vs 1EO DRIFTDOWN) — 1EO DEPRESS excluded.
   No padding — must keep exact monospace column width. */
.sofp-crit-lowest {
  background: #d4f7c5;
  color: #145c14;
  font-weight: 700;
  border-radius: 1px;
}
/* F/R-OVER-ETP margin vs critical fuel.
   ok   = diff ≥ HOLDING/1500FT value (≥ 15-min hold reserve at altn)
   warn = diff < HOLDING/1500FT (margin tighter than 15-min hold)
   No padding — must keep exact column width so following per-altn
   columns stay aligned with HOLDING/IAP/APU rows above. */
.sofp-fr-ok {
  background: #d4f7c5;
  color: #145c14;
  font-weight: 700;
  border-radius: 1px;
}
.sofp-fr-warn {
  background: #ffd9a8;
  color: #b35900;
  font-weight: 700;
  border-radius: 1px;
}
/* Recalculated in-flight VALIDITY WINDOW end (appended next to original) */
.sofp-validity-inflight {
  background: #fff3c4;
  color: #6b4d00;
  font-weight: 700;
  padding: 0 4px;
  border-radius: 2px;
}
/* In-flight selected ETP scenario rows in the navlog (label + N/E data lines) */
.sofp-etp-highlight {
  background: #d4f7c5;
  border-radius: 2px;
}
/* EEP / EXP labels and their N/E data lines — light brown */
.sofp-eep-label, .sofp-exp-label {
  background: #e8d5b5;
  color: #6b4d00;
  font-weight: 700;
  padding: 0 4px;
  border-radius: 2px;
}
.sofp-special-highlight {
  background: #e8d5b5;
  border-radius: 2px;
}
/* "-30 MIN: HH:MM" pre-ETOPS check annotation next to EEP */
.sofp-eep-warn {
  color: #0a4a8a;
  font-weight: 700;
  background: transparent;
}
/* Plain WPT name labels (non-FIR, non-EEP/EXP) — just bold */
.sofp-wpt-name {
  font-weight: 700;
}
/* ATA cell color: red if late, green if early (vs ATA_0 + CTME) */
.sofp-in.sofp-w4.ata-late  { color: #b00020; background: #ffe5e5; }
.sofp-in.sofp-w4.ata-early { color: #0a6b0a; background: #e0f7e0; }
/* DIFF = AFU − EFU: red if positive (burned more), green if negative */
.sofp-diff {
  color: #555;
  font-weight: inherit;
  border-radius: 1px;
}
.sofp-diff.diff-pos  { color: #b00020; background: #ffe5e5; font-weight: 700; }
.sofp-diff.diff-neg  { color: #0a6b0a; background: #e0f7e0; font-weight: 700; }
.sofp-diff.diff-zero { color: #555; font-weight: 700; }
.sofp-late {
  font-weight: 700;
  margin-left: 8px;
}
.sofp-late.late-pos  { color: #a01010; }      /* late = red */
.sofp-late.late-neg  { color: #145c14; }      /* early = green */
.sofp-late.late-zero { color: #555; }
/* Weather section: DEPARTURE/ARRIVAL/OTHER labels, METAR/TAF bolding,
   inline ETA/ETD/window annotation, TAF period highlight. */
.sofp-weather-label {
  background: #e8d5b5;
  color: #6b4d00;
  font-weight: 700;
  padding: 0 4px;
  border-radius: 2px;
}
.sofp-weather-time {
  background: #fff0b8;
  color: #6b4d00;
  font-weight: 700;
  padding: 0 4px;
  border-radius: 2px;
}
/* Bolded only on the METAR/TAF keyword + ICAO + DDHHMMZ issue time —
   the rest of the report stays normal weight for readability. */
.sofp-wx-bold {
  font-weight: 700;
}
/* Crew-briefing header values (page 2): flight number, registration, date,
   dep/arr, crew, OFP #, CI, ETOPS, TNK, TDV, ETD, ETA, EROPS, DEST ALTN,
   ALTN FUEL, FUEL ERA AERODROME. */
.sofp-hdr-val {
  background: #fff0b8;
  color: #6b4d00;
  font-weight: 700;
  border-radius: 2px;
}
.sofp-hdr-label {
  font-weight: 700;
}
/* FUEL BURN DECREASE line — struck through when its coefficient equals the
   INCREASE coefficient (a 1-to-1 burn coefficient applies in only one
   direction at a time per Aircalin convention). */
.sofp-strike-line {
  text-decoration: line-through;
  color: #888;
}
/* PREVI PAX sum annotation appended after the highlighted value. */
.sofp-pax-sum {
  color: #0a4a8a;
  font-weight: 700;
}
.sofp-taf-period {
  background: #fff0b8;
  color: #6b4d00;
  font-weight: 700;
  border-radius: 2px;
}
@media print {
  body { background: #fff; padding: 0; }
  .toolbar { display: none; }
  .page { box-shadow: none; border: none; max-width: none; margin: 0; padding: 0; page-break-after: always; }
  .sofp-in, .sofp-out, .sofp-ul, .sofp-eta { background: transparent; box-shadow: none; color: #000; border-bottom: none; }
  .sofp-in:not(.sofp-w4):empty::before { color: #000; opacity: 1; }
}
</style>
</head>
<body>
<div class="toolbar">
  <strong>SUPER&#x2011;OFP</strong>
  <span>__PDF_NAME__</span>
  <span class="spacer"></span>
  <span id="sofp-status" style="font-size:12px;color:#555"></span>
  <span id="sofp-lateness" class="sofp-late" style="font-size:13px"></span>
  <button onclick="sofpReset()">Reset</button>
  <button onclick="window.print()">Print</button>
  <button onclick="sofpShare()" title="Share this OFP via AirDrop / system share sheet">Share</button>
</div>
__BODY__
<script id="sofp-config" type="application/json">
__CONFIG_JSON__
</script>
<script>
(function() {
  var CONFIG = JSON.parse(document.getElementById('sofp-config').textContent);
  function fmt6(n) {
    if (n === null || n === undefined || isNaN(n)) return '. . . . .';
    var v = Math.round(n);
    var s = (v < 0 ? '-' : '') + String(Math.abs(v)).padStart(6 - (v<0?1:0), '0');
    return s;
  }
  function fmtUL(n) {
    if (n === null || n === undefined || isNaN(n)) return 'UL . . . . .';
    var sign = n < 0 ? '-' : '+';
    return 'UL ' + sign + String(Math.abs(Math.round(n))).padStart(6, '0');
  }
  // Editable cells are contenteditable <span>s; value lives in textContent.
  // Narrow navlog cells always have placeholder text "...." when "empty",
  // tagged via .is-placeholder so read() can distinguish from a typed value.
  function isEmpty(el) {
    return el.classList.contains('is-placeholder') ||
           el.textContent.replace(/\s+/g, '').trim() === '';
  }
  // localStorage key prefix, per OFP file → each OFP keeps its own values.
  var LS_PREFIX = 'sofp:' + (CONFIG.pdf_name || 'unknown') + ':';
  function lsSave(key, value) {
    try { localStorage.setItem(LS_PREFIX + key, value); } catch (e) {}
  }
  function lsLoad(key) {
    try { return localStorage.getItem(LS_PREFIX + key); } catch (e) { return null; }
  }
  function lsClearAll() {
    try {
      // Remove every key for this OFP
      for (var i = localStorage.length - 1; i >= 0; i--) {
        var k = localStorage.key(i);
        if (k && k.indexOf(LS_PREFIX) === 0) localStorage.removeItem(k);
      }
    } catch (e) {}
  }
  function read(key) {
    var el = document.querySelector('.sofp-in[data-target="' + key + '"]');
    if (!el || isEmpty(el)) return 0;
    var v = parseInt(el.textContent.replace(/\s+/g, ''), 10);
    return isNaN(v) ? 0 : v;
  }
  function readOr(key, fallback) {
    var el = document.querySelector('.sofp-in[data-target="' + key + '"]');
    if (!el || isEmpty(el)) return fallback;
    var v = parseInt(el.textContent.replace(/\s+/g, ''), 10);
    return isNaN(v) ? fallback : v;
  }
  // Time helpers — HHMM (clock) and CTME (cumulative HHMM, HH may exceed 23)
  function parseHHMM(s) {
    if (s === null || s === undefined) return null;
    s = String(s).trim();
    if (!s) return null;
    var v = parseInt(s, 10);
    if (isNaN(v)) return null;
    var hh = Math.floor(v / 100), mm = v % 100;
    if (hh < 0 || hh > 23 || mm < 0 || mm > 59) return null;
    return hh * 60 + mm;
  }
  function parseCTME(s) {
    if (s === null || s === undefined) return null;
    var v = parseInt(s, 10);
    if (isNaN(v)) return null;
    return Math.floor(v / 100) * 60 + (v % 100);
  }
  function fmtHHMM(totalMin) {
    var m = ((totalMin % 1440) + 1440) % 1440;
    return String(Math.floor(m / 60)).padStart(2, '0')
         + String(m % 60).padStart(2, '0');
  }
  function getAtaFirstMin() {
    // First waypoint's ATA = the anchor for every other time on the page.
    var ataEl = document.querySelector('.sofp-in[data-target="ATA_0"]');
    return (ataEl && !isEmpty(ataEl)) ? parseHHMM(ataEl.textContent) : null;
  }
  // ATA color: green if early, red if late vs the expected ETA for that wpt.
  // The first ATA (= takeoff anchor) is colored only via the toolbar lateness.
  function recomputeATAColors() {
    var anchor = getAtaFirstMin();
    document.querySelectorAll('.sofp-in[data-target^="ATA_"]').forEach(function(el) {
      el.classList.remove('ata-late', 'ata-early');
      if (el.getAttribute('data-target') === 'ATA_0') return;       // anchor — skip
      if (el.classList.contains('is-placeholder')) return;
      if (anchor === null) return;
      var ctme = parseCTME(el.getAttribute('data-ctme'));
      var ata  = parseHHMM(el.textContent);
      if (ctme === null || ata === null) return;
      var expected = (anchor + ctme) % 1440;
      var d = ata - expected;
      if (d >  720) d -= 1440;
      if (d < -720) d += 1440;
      if (d > 0) el.classList.add('ata-late');
      else if (d < 0) el.classList.add('ata-early');
    });
  }
  // DIFF = AFU(user) − EFU(printed). Sign + color.
  function recomputeDIFFs() {
    document.querySelectorAll('.sofp-diff').forEach(function(el) {
      var afuKey = el.getAttribute('data-afu');
      var efuStr = el.getAttribute('data-efu');
      var efu = parseInt(efuStr, 10);
      var afuEl = document.querySelector('.sofp-in[data-target="' + afuKey + '"]');
      el.classList.remove('diff-pos', 'diff-neg', 'diff-zero');
      if (isNaN(efu) || !afuEl || isEmpty(afuEl)) {
        el.textContent = '....';
        return;
      }
      var afu = parseInt(afuEl.textContent.replace(/\s+/g, ''), 10);
      if (isNaN(afu)) { el.textContent = '....'; return; }
      var diff = afu - efu;
      var sign = diff > 0 ? '+' : (diff < 0 ? '−' : '±');
      // Right-pad to 4 chars so the column stays aligned
      var n = Math.min(Math.abs(diff), 999);
      el.textContent = sign + String(n).padStart(3, '0');
      if (diff > 0) el.classList.add('diff-pos');
      else if (diff < 0) el.classList.add('diff-neg');
      else el.classList.add('diff-zero');
    });
  }
  function recomputeETAs() {
    var ataMin = getAtaFirstMin();
    document.querySelectorAll('.sofp-eta').forEach(function(el) {
      var ctme = parseCTME(el.getAttribute('data-ctme'));
      if (ataMin === null || ctme === null) {
        el.textContent = '....';
      } else {
        el.textContent = fmtHHMM(ataMin + ctme);
      }
    });
  }
  // Pre-ETOPS check: ETA-at-EEP minus 30 min. Displayed in blue next to EEP.
  function recomputeEepWarn() {
    var ataMin = getAtaFirstMin();
    document.querySelectorAll('.sofp-eep-warn').forEach(function(el) {
      var ctme = parseCTME(el.getAttribute('data-ctme'));
      if (ataMin === null || ctme === null) {
        el.textContent = '-30 MIN: --:--';
      } else {
        var t = (((ataMin + ctme - 30) % 1440) + 1440) % 1440;
        var hhmm = fmtHHMM(t);
        el.textContent = '-30 MIN: ' + hhmm.slice(0, 2) + ':' + hhmm.slice(2);
      }
    });
  }
  // ETOPS-scenario ETAs: each header line carries a span with
  //   data-tetp="HHMM"   = CTME from V1 to the ETP for this scenario
  //   data-divert="HHMM" = OEI divert TIME from that ETP to the altn
  //   data-altns="AAAA/BBBB"
  // ETA at ETP   = ATA_FIRST + tetp
  // ETA at altn  = ETA at ETP + divert    (same for both altns — equal-time-point)
  function recomputeEtopsEtas() {
    var ataMin = getAtaFirstMin();
    document.querySelectorAll('.sofp-etops-eta').forEach(function(el) {
      var tetp   = parseCTME(el.getAttribute('data-tetp'));
      var divert = parseCTME(el.getAttribute('data-divert'));
      if (ataMin === null || tetp === null || divert === null) {
        el.textContent = 'LATEST --:--';
        return;
      }
      // LATEST possible arrival at altn (worst-case divert decision = at ETP)
      //   = ATA_FIRST + CTME-to-ETP + divert TIME + 2 min turn-back allowance
      var altnMin  = ataMin + tetp + divert + 2;
      var hhmm     = fmtHHMM(altnMin);
      el.textContent = 'LATEST ' + hhmm.slice(0, 2) + ':' + hhmm.slice(2);
    });
  }
  // In-flight VALIDITY WINDOW recompute. Each span carries:
  //   data-orig-start  = printed window start (minutes since midnight)
  //   data-orig-end    = printed window end   (minutes since midnight)
  //   data-delta       = scenario shift (single-failure LATEST − 1EO DEPRESS LATEST), in min
  //   data-scenario    = which single-failure scenario is binding (display label)
  // The in-flight window =
  //     start: orig_start + lateness
  //     end:   orig_end   + lateness + delta
  // where lateness = ATA_FIRST − (EOBT + 20).
  function recomputeValidityInflight() {
    var ataMin   = getAtaFirstMin();
    var plan     = (CONFIG.eobt_min != null) ? (CONFIG.eobt_min + 20) : null;
    var lateness = (ataMin !== null && plan !== null) ? (ataMin - plan) : 0;
    // wrap lateness in case of midnight crossing
    if (lateness >  720) lateness -= 1440;
    if (lateness < -720) lateness += 1440;
    document.querySelectorAll('.sofp-validity-inflight').forEach(function(el) {
      var os    = parseInt(el.getAttribute('data-orig-start'), 10);
      var oe    = parseInt(el.getAttribute('data-orig-end'),   10);
      var delta = parseInt(el.getAttribute('data-delta'),      10);
      var sc    = el.getAttribute('data-scenario');
      if (isNaN(os) || isNaN(oe) || isNaN(delta)) return;
      var ns = ((os + lateness) % 1440 + 1440) % 1440;
      var ne = ((oe + lateness + delta) % 1440 + 1440) % 1440;
      function f(m) { return String(Math.floor(m/60)).padStart(2,'0')+':'+String(m%60).padStart(2,'0'); }
      var sign = delta > 0 ? '+' : (delta < 0 ? '−' : '±');
      var lateTag = lateness ? '  (late ' + (lateness > 0 ? '+' : '−') + Math.abs(lateness) + ')' : '';
      el.textContent = sc + ': ' + f(ns) + 'Z TO ' + f(ne)
                     + 'Z (' + sign + Math.abs(delta) + ')' + lateTag;
    });
  }
  // Lateness vs planned takeoff (= EOBT + estimated taxi-out).
  // Displayed in the toolbar.
  function updateLateness() {
    var el = document.getElementById('sofp-lateness');
    if (!el) return;
    var ataMin   = getAtaFirstMin();
    var planMin  = CONFIG.planned_takeoff_min;
    if (ataMin === null || planMin === null || planMin === undefined) {
      el.textContent = '';
      el.classList.remove('late-pos','late-neg','late-zero');
      return;
    }
    var diff = ataMin - planMin;
    if (diff > 720)  diff -= 1440;          // wrap (in case of midnight crossing)
    if (diff < -720) diff += 1440;
    var sign = diff > 0 ? '+' : (diff < 0 ? '−' : '±');
    el.textContent = 'late ' + sign + Math.abs(diff) + ' min';
    el.classList.toggle('late-pos',  diff > 0);
    el.classList.toggle('late-neg',  diff < 0);
    el.classList.toggle('late-zero', diff === 0);
  }
  function setOut(key, value, warn) {
    var el = document.querySelector('.sofp-out[data-source="' + key + '"], .sofp-ul[data-source="' + key + '"]');
    if (!el) return;
    el.textContent = (key === 'UNDERLOAD') ? fmtUL(value) : fmt6(value);
    el.classList.toggle('sofp-warn', !!warn);
  }
  function recompute() {
    var discr   = read('DISCR');
    var zfwAct  = read('ZFW');
    var eTaxi   = CONFIG.fuel.TAXI || 0;
    var taxiAct = readOr('TAXI', eTaxi);
    var eTof    = CONFIG.wt_est.TOF || 0;
    var eEbo    = CONFIG.wt_est.EBO || 0;
    var eTow    = CONFIG.wt_est.TOW || 0;
    var mzfw    = CONFIG.wt_lim.ZFW || Infinity;
    var mtow    = CONFIG.wt_lim.TOW || Infinity;
    var mlw     = CONFIG.wt_lim.LAW || Infinity;
    var coeff   = (CONFIG.burn_coeff_per_1000kg || 0) / 1000;

    // FUEL block
    //   T/O FUEL (act) = T/O FUEL (est) + DISCR             (DISCR sits on top of T/O FUEL)
    //   REQUESTED FUEL = ceil_100( T/O FUEL (act) + TAXI )  (uplift, rounded up to nearest 100 kg)
    var reqFuelRaw = eTof + discr + taxiAct;
    var reqFuel    = Math.ceil(reqFuelRaw / 100) * 100;
    setOut('REQUESTED_FUEL', reqFuel);

    // WT block — TAXI is burned before V1 so it does not affect TOF/TOW/EBO/LAW
    var tofAct = eTof + discr;
    setOut('TOF', tofAct);

    var towAct, eboAct, lawAct, ul;
    if (zfwAct > 0) {
      towAct = zfwAct + tofAct;
      var dTow = towAct - eTow;
      eboAct = eEbo + dTow * coeff;
      lawAct = towAct - eboAct;
      // UNDERLOAD = remaining payload capacity vs the most constraining limit.
      // 1 kg payload -> +1 kg ZFW, +1 kg TOW, +1 kg LAW (no extra burn from payload alone)
      ul = Math.min(mzfw - zfwAct, mtow - towAct, mlw - lawAct);
    }
    setOut('TOW', towAct, towAct !== undefined && towAct > mtow);
    setOut('EBO', eboAct);
    setOut('LAW', lawAct, lawAct !== undefined && lawAct > mlw);
    setOut('UNDERLOAD', ul, ul !== undefined && ul < 0);

    // NAVLOG ETAs (= first ATA + CTME for each waypoint)
    recomputeETAs();
    // Per-wpt ATA late/early colors + DIFF (= AFU − EFU) colors
    recomputeATAColors();
    recomputeDIFFs();
    // -30 MIN pre-ETOPS check next to EEP
    recomputeEepWarn();
    // ETOPS scenario ETAs (= ATA + time-to-ETP + divert)
    recomputeEtopsEtas();
    // In-flight VALIDITY WINDOW recalc (shifts with lateness + scenario diff)
    recomputeValidityInflight();
    // Lateness vs planned takeoff (= EOBT + estimated taxi-out)
    updateLateness();

    var st = document.getElementById('sofp-status');
    if (st) {
      var bits = [];
      bits.push('coeff ' + (CONFIG.burn_coeff_per_1000kg || 0) + ' kg/1000kg');
      if (ul !== undefined) bits.push('UL ' + Math.round(ul) + ' kg');
      st.textContent = bits.join(' · ');
    }
  }
  // Editable cells pre-filled from estimated values at load and on reset.
  var PREFILLS = { TAXI: CONFIG.fuel.TAXI, ZFW: CONFIG.wt_est.ZFW };
  function clearToEmptyState(el) {
    // Narrow cells must always contain text so they occupy exactly 4 cells
    // inline. Wide cells can be truly empty — :empty::before shows the hint.
    if (el.classList.contains('sofp-w4')) {
      el.textContent = el.getAttribute('data-placeholder') || '';
      el.classList.add('is-placeholder');
    } else {
      el.textContent = '';
    }
  }
  function setValue(el, value) {
    el.textContent = String(value);
    el.classList.remove('is-placeholder');
  }
  function applyPrefills() {
    Object.keys(PREFILLS).forEach(function(key) {
      var el = document.querySelector('.sofp-in[data-target="' + key + '"]');
      if (el && PREFILLS[key] != null) setValue(el, PREFILLS[key]);
    });
  }
  function sofpReset() {
    lsClearAll();   // wipe any saved overrides for this OFP
    document.querySelectorAll('.sofp-in[contenteditable]').forEach(clearToEmptyState);
    applyPrefills();
    recompute();
  }
  window.sofpReset = sofpReset;

  // Share the current HTML (with the user's edits embedded — contenteditable
  // changes reflect into the DOM, so document.documentElement.outerHTML is the
  // up-to-date snapshot) via the Web Share API. On macOS Safari and iOS Safari
  // this opens the native share sheet, which includes AirDrop. Other browsers
  // either lack file sharing or only support text/url — we fall back to a
  // plain download so the user can drag the file into AirDrop from Finder.
  async function sofpShare() {
    var baseName = (CONFIG && CONFIG.pdf_name)
      ? CONFIG.pdf_name.replace(/\.pdf$/i, '') + '.super.html'
      : (document.title || 'ofp') + '.html';
    try {
      var snapshot = '<!DOCTYPE html>\n' + document.documentElement.outerHTML;
      var blob = new Blob([snapshot], { type: 'text/html;charset=utf-8' });
      var file = new File([blob], baseName, { type: 'text/html' });
      if (navigator.canShare && navigator.canShare({ files: [file] })) {
        await navigator.share({ files: [file], title: baseName });
        return;
      }
      // Fallback: trigger a download (use Finder → right-click → Share → AirDrop)
      var url = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = url; a.download = baseName;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      var s = document.getElementById('sofp-status');
      if (s) s.textContent = 'Downloaded ' + baseName + ' — AirDrop from Finder';
    } catch (e) {
      if (e && e.name === 'AbortError') return;   // user cancelled
      console.error('share failed', e);
      alert('Share failed: ' + (e && e.message ? e.message : e));
    }
  }
  window.sofpShare = sofpShare;
  // Restore any user values previously saved in localStorage for this OFP.
  // Runs after applyPrefills so saved values win over OFP-derived prefills.
  function restoreFromLocalStorage() {
    document.querySelectorAll('.sofp-in[contenteditable][data-target]').forEach(function(el) {
      var key = el.getAttribute('data-target');
      var saved = lsLoad(key);
      if (saved !== null && saved !== '') {
        setValue(el, saved);
      }
    });
  }

  // Wire per-cell handlers: digit-only filter, maxlen, placeholder management.
  document.querySelectorAll('.sofp-in[contenteditable]').forEach(function(el) {
    var maxlen = parseInt(el.getAttribute('data-maxlen'), 10) || 6;
    var placeholder = el.getAttribute('data-placeholder') || '';
    var isNarrow = el.classList.contains('sofp-w4');

    function selectAll() {
      var sel = window.getSelection();
      var range = document.createRange();
      range.selectNodeContents(el);
      sel.removeAllRanges();
      sel.addRange(range);
    }
    function caretToEnd() {
      var sel = window.getSelection();
      var range = document.createRange();
      range.selectNodeContents(el);
      range.collapse(false);
      sel.removeAllRanges();
      sel.addRange(range);
    }

    el.addEventListener('focus', function() {
      // If showing placeholder, select it so the first keypress overwrites.
      if (isNarrow && el.classList.contains('is-placeholder')) selectAll();
    });
    el.addEventListener('blur', function() {
      // If user cleared a narrow cell, restore the "...." placeholder so the
      // 4-cell slot stays occupied in the inline flow.
      if (isNarrow && el.textContent.replace(/\s+/g, '').trim() === '') {
        clearToEmptyState(el);
      }
    });
    el.addEventListener('input', function() {
      var raw = el.textContent;
      var digits = raw.replace(/\D/g, '').slice(0, maxlen);
      if (digits !== raw) {
        el.textContent = digits;
        if (digits.length) caretToEnd();
      }
      if (el.textContent.replace(/\s+/g, '').trim()) {
        el.classList.remove('is-placeholder');
      } else if (isNarrow) {
        // User deleted everything — keep slot occupied while editing.
        // (Will restore real placeholder on blur.)
      }
      // Persist every keystroke (empty string = "no override; use prefill on next reload")
      var key = el.getAttribute('data-target');
      if (key) lsSave(key, el.textContent.replace(/\s+/g, '').trim());
      recompute();
    });
    el.addEventListener('keydown', function(e) {
      if (e.key === 'Enter') { e.preventDefault(); el.blur(); }
    });
  });

  applyPrefills();
  restoreFromLocalStorage();   // overrides prefills with any saved values
  recompute();
})();
</script>
</body>
</html>
"""

# --- Driver --------------------------------------------------------------
def build_html(pdf: Path, parsed: dict, body: str) -> str:
    config = {
        'fuel':                  parsed['fuel'],
        'wt_est':                parsed['wt_est'],
        'wt_lim':                parsed['wt_lim'],
        'burn_coeff_per_1000kg': parsed['burn_coeff_per_1000kg'],
        'eobt_min':              parsed.get('eobt_min'),
        'taxi_out_min':          parsed.get('taxi_out_min'),
        'planned_takeoff_min':   parsed.get('planned_takeoff_min'),
        'pdf_name':              pdf.name,
    }
    return (TEMPLATE
            .replace('__TITLE__',       html.escape(f'Super-OFP — {pdf.name}'))
            .replace('__PDF_NAME__',    html.escape(pdf.name))
            .replace('__BODY__',        body)
            .replace('__CONFIG_JSON__', json.dumps(config, indent=2)))

def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] in ('-h', '--help'):
        print(USAGE); return 0
    pdf = Path(argv[1])
    out: Path | None = None
    if '-o' in argv:
        out = Path(argv[argv.index('-o') + 1])
    if not pdf.exists():
        print(f'PDF not found: {pdf}', file=sys.stderr); return 1

    raw    = extract(pdf)
    parsed = parse_blocks(raw)
    body   = render_body(raw, parsed, pdf=pdf)
    out    = out or pdf.with_name(pdf.stem + '.super.html')
    out.write_text(build_html(pdf, parsed, body), encoding='utf-8')

    print(f'OK    : {out}')
    print(f'pages : {sum(1 for l in raw.splitlines() if PAGE_FOOTER_RE.match(l))}')
    print(f'fuel  : {len(parsed["fuel"])} rows')
    print(f'wt_est: {len(parsed["wt_est"])} / wt_lim: {len(parsed["wt_lim"])}')
    print(f'coeff : {parsed["burn_coeff_per_1000kg"]} kg/1000kg')
    return 0

if __name__ == '__main__':
    sys.exit(main(sys.argv))
