# Changelog

All notable changes to the `super-ofp` skill are documented here. Newest entries on top.

## 2026-05-18 — Fix in-flight critical-fuel scenario selection

- The per-ETP green-highlighted TOTAL CRITICAL DIV FUEL now picks the **highest** single-failure fuel (`max` over `{2ENG DEPRESS, 1EO DRIFTDOWN}`) instead of the lowest. The highest figure is the worst-case in-flight requirement that the margin must cover.
- Same correction applied to the in-flight validity-window recalculation: the per-ETP delta now uses the highest-fuel single-failure scenario, not the lowest.
- CSS class renamed `sofp-crit-lowest` → `sofp-crit-highest` (and the internal `'crit_lowest'` tag → `'crit_highest'`).

## 2026-05-17 — Initial public release

- Single-file converter `scripts/build_super_ofp.py` turning navtech OFP PDFs into self-contained interactive HTML.
- Crew briefing header overlays (REG, DATE, flight #, DEP/ARR, CREW, OFP #, CI, ETOPS, TNK, TDV, ETD/ETA, EROPS, ALTN).
- DSP COMMENTS bullet highlights (DOWC, EQUIPAGE, PREVI PAX with J/W/Y auto-sum, CODE CGO, MEL/CDL).
- Editable fuel & weights cells (DISCR, TAXI, ZFW) with live REQUESTED FUEL / TOF / TOW / EBO / LAW / UNDERLOAD recompute and limit-bust colouring.
- Every navlog waypoint (main + alternate) editable: A.RF, AFU, ATA. DIFF (AFU − EFU) coloured. ETA = ATA₀ + CTME with late/early colouring.
- ETOPS overlays: per-scenario LATEST, highest-fuel single-failure highlight, F/R margin annotation.
- SUITABILITY ETOPS in-flight validity windows recomputed live from selected in-flight scenario.
- Weather block ETD/ETA/window tags and TAF-period highlighting.
- Image-only PDF pages (perf charts, NOTAM maps) rendered at 120 DPI and base64-embedded.
- Per-OFP localStorage persistence; toolbar with live lateness indicator, Reset, Print, and Web-Share-API export with live snapshot.
