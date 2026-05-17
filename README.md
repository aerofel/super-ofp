# super-ofp

**Interactive HTML overlay for OFPs.** Converts a navtech OFP PDF into a single self-contained HTML file that looks exactly like the original — column-accurate monospaced text, all 60+ pages — and overlays a live recompute engine on top.

> Fill in actuals (DISCR, TAXI, ZFW, ATA, AFU, A.RF). Every derived value — REQUESTED FUEL, TOF, TOW, EBO, LAW, UNDERLOAD, every ETA, ETOPS scenario LATEST, in-flight validity windows — shifts in real time.

This is a [Claude Code skill](https://docs.claude.com/en/docs/claude-code/skills). It's not a standalone app — it runs inside Claude Code when you reference an OFP.

---

## What it produces

A single `<pdf-stem>.super.html` file next to the source PDF:

- **Pixel-faithful mirror** of the original OFP (monospaced grid, OFP-native page breaks)
- **Editable cells** overlaid as `<span contenteditable>` inside `<pre>` — zero column drift
- **Live recompute engine** (~250 lines of vanilla JS, embedded)
- **Image-only pages** (perf charts, NOTAM maps) rendered at 120 DPI and base64-embedded
- **Fully offline** — no external CSS, fonts, or fetches. AirDrop-able as one file.
- **Per-OFP localStorage** — your filled-in values persist; each OFP keeps its own state

## What gets overlaid

| Area | Behaviour |
|---|---|
| **Crew briefing header** (p.2) | REG, DATE, flight #, DEP/ARR, CREW, OFP #, CI, ETOPS, TNK, TDV, ETD/ETA, EROPS, ALTN highlighted |
| **DSP COMMENTS** (p.3) | DOWC, EQUIPAGE, PREVI PAX (with auto J/W/Y sum), CODE CGO, MEL/CDL |
| **Fuel & weights** (p.5) | DISCR / TAXI / ZFW editable → REQUESTED FUEL, TOF, TOW, EBO, LAW, UNDERLOAD recompute live. Red when limits bust. |
| **NAVLOG** | Every waypoint gets editable A.RF, AFU, ATA. DIFF (AFU−EFU) shown red/green. ETA = ATA₀ + CTME. Late/early colour per waypoint. |
| **ETOPS** | LATEST appended per scenario. TOTAL CRITICAL DIV FUEL highlighted on lowest single-failure scenario per ETP. F/R margin annotation in green/orange. |
| **In-flight validity windows** | SUITABILITY ETOPS windows recomputed live from ATA₀ + selected in-flight scenario at the bounding ETP. |
| **Weather** | DEP/ARR/OTHER labels tagged with the time of interest; TAF period tokens overlapping that time are highlighted. |
| **Toolbar** | Live `late ±N min` indicator (ATA₀ vs planned takeoff), Reset, Print, Share (native iOS/macOS share sheet with the filled-in snapshot). |

## Recompute formulas

```
TOF (act)        = TOF (est) + DISCR
REQUESTED FUEL   = ceil_100( TOF (est) + DISCR + TAXI )
TOW (act)        = ZFW (act) + TOF (act)
EBO (act)        = EBO (est) + (TOW act − TOW est) × (burn_coeff / 1000)
LAW (act)        = TOW (act) − EBO (act)
UNDERLOAD        = min( MZFW − ZFW,  MTOW − TOW,  MLW − LAW )

LATEST           = ATA_0 + tetp + divert + 2 min
new_window_start = orig_start + lateness
new_window_end   = orig_end   + lateness + scenario_delta
```

`lateness = ATA₀ − (EOBT + 20)`. Taxi burns before V1 → affects REQUESTED FUEL only, not TOF/TOW/EBO/LAW.

## In-flight scenario convention

- **Planning**: critical scenario = **1EO DEPRESS** (combined failure, printed on validity windows).
- **In-flight**: 1EO DEPRESS excluded. Eligible = **2ENG DEPRESS** and **1EO DRIFTDOWN**. Lowest-fuel one wins per ETP — that's what the green highlight tracks and what the recomputed validity windows use.

## How to use

Inside Claude Code, just hand the model an OFP PDF and say *"super ofp"* — the skill auto-activates.

Or run the script directly:

```bash
python3 scripts/build_super_ofp.py path/to/your-ofp.pdf
# → path/to/your-ofp.super.html
```

### Requirements

- Python 3 (stdlib only)
- `pdftotext` and `pdftoppm` from poppler (`brew install poppler`)
- The companion [`ofp-extract`](https://github.com/aerofel/ofp-extract) skill (bbox-based column extraction primitive). Installed automatically as a Claude Code skill alongside `super-ofp`.

## Installation as a Claude Code skill

```bash
git clone https://github.com/aerofel/super-ofp ~/.claude/skills/super-ofp
```

The skill description in `SKILL.md` lets Claude auto-route any OFP PDF or the keywords *"super ofp", "interactive ofp", "ofp overlay", "fill DISCR", "LATEST", "validity window inflight"…* to this skill.

## Files

```
super-ofp/
├── SKILL.md                       # full spec (Claude reads this)
├── README.md                      # this file
├── CHANGELOG.md                   # release notes
└── scripts/
    └── build_super_ofp.py         # single-file converter (~2k lines)
```

## Related

- **[`ofp-extract`](https://github.com/aerofel/ofp-extract)** — bbox-based column-accurate text extraction (always used as step 1)
- **`dez-briefing`** — tabbed crew briefing summary (different output shape; use when you want a separate UI, not an OFP mirror)
- **`read-orlando`** — operator OM-A / MANEX search (authoritative source for OFP field definitions, e.g. SPA.ETOPS.115 § "Fenêtre d'accessibilité")

## License

Internal operator tooling. Not for redistribution.
