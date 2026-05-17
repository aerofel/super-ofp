# Changelog

All notable changes to the `super-ofp` skill are documented here. Newest entries on top.

## 2026-05-17 — Drop read-orlando / read-olb references

- Removed `read-orlando` from the "Related skills" sections of README and SKILL.md.
- No runtime impact — neither skill was ever called by `build_super_ofp.py`; the mentions were documentation pointers only.

## 2026-05-17 — README scrub

- Removed operator-specific branding from README (no "Aircalin", no sample `ACI-*` filenames).
- Switched all "CFP" mentions to "OFP" for consistency.
- Generalised the in-flight scenario section heading.

## 2026-05-17 — Initial public release

- Single-file converter `scripts/build_super_ofp.py` turning Aircalin navtech OFP PDFs into self-contained interactive HTML.
- Crew briefing header overlays (REG, DATE, flight #, DEP/ARR, CREW, OFP #, CI, ETOPS, TNK, TDV, ETD/ETA, EROPS, ALTN).
- DSP COMMENTS bullet highlights (DOWC, EQUIPAGE, PREVI PAX with J/W/Y auto-sum, CODE CGO, MEL/CDL).
- Editable fuel & weights cells (DISCR, TAXI, ZFW) with live REQUESTED FUEL / TOF / TOW / EBO / LAW / UNDERLOAD recompute and limit-bust colouring.
- Every navlog waypoint (main + alternate) editable: A.RF, AFU, ATA. DIFF (AFU − EFU) coloured. ETA = ATA₀ + CTME with late/early colouring.
- ETOPS overlays: per-scenario LATEST, lowest-fuel single-failure highlight, F/R margin annotation.
- SUITABILITY ETOPS in-flight validity windows recomputed live from selected in-flight scenario.
- Weather block ETD/ETA/window tags and TAF-period highlighting.
- Image-only PDF pages (perf charts, NOTAM maps) rendered at 120 DPI and base64-embedded.
- Per-OFP localStorage persistence; toolbar with live lateness indicator, Reset, Print, and Web-Share-API export with live snapshot.
