# STRATO·GCS — Mobile Layout Plan

Target: **iPhone 13 Pro (390×844)** primary; degrade cleanly across 320–430 portrait
phones; tablets (≥768) keep desktop layout (with the small touch-target shims).
Same URL, same React tree — the layout decides per-viewport whether to render the
mobile shell or the desktop shell. **Desktop is untouched at ≥1024.**

---

## 1. Mobile detection — hybrid

* A tiny `useIsMobile()` hook (in `src/lib/useIsMobile.ts`) drives **layout-level
  component swaps** via `matchMedia('(max-width: 767px)')`. Synchronous initial
  read so SSR isn't relevant (this is a Vite SPA) and the first paint already
  picks the right shell — no flash.
* CSS (Tailwind `md:` breakpoints, safe-area padding) handles all sizing/spacing.
* Tablets (768–1023) keep desktop layout but inherit the larger tap targets via
  CSS (`@media (pointer: coarse)` adds 6px to button padding via index.css).
* Justification: a phone shell isn't just smaller — bottom-sheet, edge-only
  chrome, and gesture handlers are wildly different from the floating-panel
  desktop. JS component swaps keep both shells clean.

## 2. Files added / modified

### New (mobile-only)

| Path | Why |
|---|---|
| `src/lib/useIsMobile.ts` | Single source of truth for the breakpoint. |
| `src/components/mobile/MobileShell.tsx` | Top-level mobile composition (renders by App.tsx when isMobile). |
| `src/components/mobile/MobileTopBar.tsx` | Safe-area top: link status, vehicle pill, 2D/3D, reports, more. |
| `src/components/mobile/MobileHud.tsx` | Compact glanceable HUD strip + tap-to-expand instrument card. |
| `src/components/mobile/MobileCommandSheet.tsx` | Bottom sheet with drag handle: primary row (ARM/TAKEOFF/LAND/RTL/HOLD/BRAKE) + secondary tabs (Mode/Mission/Autotune/Force-Disarm). |
| `src/components/mobile/MobilePttFab.tsx` | Large floating PTT button (LARGE — primary mobile interaction). |
| `src/components/mobile/MobileVideoSheet.tsx` | Bottom sheet variant for video feeds (collapses to a small toolbar tab). |
| `src/components/mobile/MobileToast.tsx` | Snackbar host fed by the existing pushLog stream (command failures, voice errors). |
| `docs/PLAN.md` | This document. |

### Modified

| Path | Why |
|---|---|
| `src/App.tsx` | At mount, swap to `<MobileShell />` when `useIsMobile()`. Desktop branch untouched. |
| `src/index.css` | Safe-area CSS vars + tap-target shim under `pointer: coarse`. **Removes `overflow:hidden` on `html/body` ONLY on mobile** so the bottom sheet can ignore the URL bar; desktop unchanged. |
| `index.html` | viewport meta: add `viewport-fit=cover` so `env(safe-area-inset-*)` reports notch insets. |

### Untouched

* `MapView.tsx`, `Map3DView.tsx`, `Hud.tsx`, `CommandBar.tsx`, `VoiceButton.tsx`,
  `VideoPanel.tsx`, `SecondFeedPanel.tsx`, `AutotunePanel.tsx`,
  `FleetSurveyPanel.tsx`, etc. — all desktop components live and are rendered on
  desktop. Mobile shell **reuses** `MapView` (the canvas), `Map3DView`, and the
  underlying store/api (so feature parity is automatic). It does NOT render
  desktop floating panels (CommandBar, StatusBar, Hud, ConversationPanel,
  Console, FleetSurveyPanel, AutotunePanel) — replaced by their mobile siblings.

## 3. Mobile layout — wireframes

### Portrait (iPhone 13 Pro 390×844) — home / closed sheet

```
┌─────────────────────────────────────┐ ← safe-area top (notch ~47pt)
│ [LIVE] [Overwatch ▾ 45%·POSCTL]  ⋯ │ ← MobileTopBar (44pt min)
│ [2D]                                │
├─────────────────────────────────────┤
│                                     │
│                                     │
│            G O O G L E              │
│              M A P                  │
│         (full canvas,               │
│      gestureHandling=greedy)        │
│                                     │
│            🚁  (drone)              │
│                                     │
│  ▲ HUD strip  ──────────────────    │
│  POSCTL · 12.3m · 4.1m/s · 14sats   │ ← MobileHud collapsed strip
│  ◯ ← tap to expand instrument card  │
├─────────────────────────────────────┤
│ ━━━  (drag handle)                  │ ← MobileCommandSheet (collapsed)
│ [ARM]  [TAKEOFF]  [HOLD]  [LAND]    │ ← primary row, 56pt buttons
│ [RTL]  [BRAKE]  [MORE…]   (PTT►)    │
└─────────────────────────────────────┘ ← safe-area bottom (home indic. 34pt)
                                  ⬤    ← MobilePttFab (88pt, floating)
```

### Portrait — bottom sheet OPEN (drag-up)

```
┌─────────────────────────────────────┐
│ [LIVE] [Overwatch ▾]            ⋯  │
├─────────────────────────────────────┤
│         (map dimmed 40%)            │
│                                     │
├─────────────────────────────────────┤ ← sheet pulled to ~70% viewport
│ ━━━                                 │
│ ┌─ NAV ─ SURVEY ─ TRACK ─┐  (modes) │
│ [ARM] [TAKEOFF↑15m] [HOLD] [BRAKE]  │
│ [RTL] [LAND]    [FORCE-DISARM ⚠]    │
│                                     │
│ ▼ Autotune   ▼ Mission   ▼ Vehicle  │ ← secondary accordions
│ ▼ Console (last 5 lines)            │
│ ▼ Conversation (live, last 3 turns) │
└─────────────────────────────────────┘
```

### Landscape (844×390) — split layout

```
┌──────────────────────────┬──────────────────────┐
│                          │ [LIVE] [Overwatch ▾] │
│                          ├──────────────────────┤
│        M A P             │ HUD instrument       │
│   (left 60%)             │ (full card)          │
│                          ├──────────────────────┤
│                          │ [ARM] [TKO]  [RTL]   │
│                          │ [HOLD][BRAKE][LAND]  │
│   ⬤ PTT (bottom-left)    │ [FORCE]  (PTT◯)      │
└──────────────────────────┴──────────────────────┘
```

### Tablet portrait (≥768) — desktop layout retained

The desktop shell is rendered as-is. Buttons inherit the `pointer: coarse` shim
(+6px padding) so touch targets clear 44pt. No structural rework.

## 4. Feature → mobile location map

| Feature | Mobile placement |
|---|---|
| Map pan/zoom/tap-to-goto | Full canvas (existing `MapView`). |
| Map long-press = goto | `MapView` already opens an "Action card" on tap with Goto/Orbit/Home/POI. Reused. |
| Telemetry HUD | `MobileHud` strip + tap-to-expand. |
| ARM/DISARM | Primary row of the bottom sheet. |
| Takeoff (with altitude) | Primary row; tapping reveals an inline altitude stepper. |
| Land/RTL/Hold/Brake | Primary row. |
| Force-disarm (emergency) | Secondary tray of the sheet, behind a sticky red confirm card (same gate logic as desktop CommandBar). |
| Voice / PTT | `MobilePttFab` — large 88pt floating button bottom-right (re-implements VoiceButton's PTT/Open logic; sharing voice session lib unchanged). |
| Vehicle switcher | `MobileTopBar` vehicle pill — tap to open a vehicle list sheet. |
| Mode (NAV/SURVEY/TRACK) | Secondary tray of the sheet. |
| Live video | `MobileVideoSheet` — accessible from MobileTopBar's "⋯" menu → "Video". Renders the existing `VideoPanel` content inside a half-sheet (drag down to dismiss). |
| Mission report | MobileTopBar ⋯ → "Reports". Reuses `ReportPage` (already fullscreen). |
| Console / Conversation | Secondary tray of the sheet, last N lines. |
| Autotune | Secondary tray of the sheet, drives existing `AutotunePanel` logic (we'll reuse it in a "compact" wrapper). |
| Failure feedback | `MobileToast` — subscribes to `useGcs.log` and shows the last `severity<=3` line as a snackbar for 4s. |

## 5. Bottom sheet mechanics

* Three snap points: **closed (88pt)**, **half (~50vh)**, **full (~85vh)**.
* Drag handle area is the entire header row (60pt tall) so it's easy to grab.
* `framer-motion` `<motion.div drag="y" dragConstraints={...} onDragEnd>`
  snaps to the nearest point using a velocity threshold.
* The primary row (ARM/TKO/LAND/RTL/HOLD/BRAKE) is **always visible**, even
  closed — that's the emergency mode.
* Half opens secondary tray (modes, force, mission, vehicle).
* Full opens secondary tray + console + conversation.
* Esc / tap-on-dimmed-map closes back to half.
* No swipe-to-dismiss (we never want to fully hide the primary commands).

## 6. Gestures

| Gesture | Effect |
|---|---|
| Tap on map | Existing — opens Goto/Orbit/Home/POI action card. |
| Long-press on map | Same as tap — already drops action card; we just don't add separate handling. |
| Double-tap on map | Recenter on drone (calls existing `followVehicle` toggle). New in `MobileShell` via map click-counter. |
| Drag handle up/down | Snap sheet to closed/half/full. |
| Tap dimmed map (when sheet open) | Snap sheet to closed. |
| Hold PTT FAB | Start voice (existing logic). Release to end. |
| Tap PTT mode pill | Toggle PTT / Open. |
| Tap vehicle pill | Open vehicle picker sheet. |

## 7. Safe-area + viewport handling

* `index.html` viewport meta: `width=device-width, initial-scale=1.0, viewport-fit=cover`.
* `body` keeps `overflow:hidden` on desktop. On mobile, the MobileShell uses
  `100dvh` (dynamic viewport — accounts for Safari's URL bar shifting), and
  explicit `padding-top: env(safe-area-inset-top)` /
  `padding-bottom: env(safe-area-inset-bottom)` on the shell wrappers.
* PTT FAB sits at `bottom: calc(env(safe-area-inset-bottom) + 96px)` so it
  never falls under the home indicator nor the sheet's primary row.
* Top bar: `padding-top: env(safe-area-inset-top)` plus a 12px gap.

## 8. Touch targets

* All buttons: `min-height: 44px; min-width: 44px;` via a `.tap` utility class
  added to `index.css`.
* PTT FAB: 88×88pt (Apple recommends 88+ for primary action surfaces).
* Sheet drag handle: 60pt tall hit area, even though the visual is a 4pt bar.

## 9. Failure feedback ("no silent command discards")

* `MobileToast` subscribes to the `log` array in the store.
* When a new entry with `severity <= 3` appears, show a snackbar for 4s
  (auto-dismiss; tap to dismiss earlier).
* The existing `CommandBar` `run()` wrapper already calls `pushLog("error", …)`
  on rejection — the mobile sheet's button handlers use the **same** `run()`
  pattern, so the existing error pipeline lights up the toast for free.
* For ARM denial, the existing `armToggle` already reads
  `{ ok, armed, reason }` and pushes `"Arm denied: <reason>"` — that flows into
  the toast.

## 10. Design language

* Dark theme preserved. `glass` class reused everywhere.
* Accent teal `#22e3c4` for confirms, primary state, voice "live".
* Warn amber `#ffb020` for HOLD/BRAKE/RTL.
* Danger red `#ff4d5e` for LAND, DISARM-when-armed, FORCE.
* Monospace `tnum` on every numeric telemetry value.

## 11. Landscape note (post-test)

iPhone landscape (844×390) sits ABOVE the 767 px mobile breakpoint, so it falls
into the **desktop** layout. The Playwright test (see /tmp/mobile_landscape.png)
confirms the desktop chrome fits, with a compressed but functional CommandBar.
A dedicated landscape phone shell with the split-screen sketched above is
deferred — landscape isn't a typical phone-cockpit posture and the desktop
fallback is usable. Switching to a `(max-width: 932px)` query would catch
landscape too, at the cost of also pulling tablet portrait into the mobile
shell — not the right trade.

## 12. What we explicitly punt

* **3D map mobile UX**: render `<Map3DView />` exactly as on desktop when the
  user taps 3D in the top bar. Its overlays were not redesigned for portrait;
  acceptable since 3D is a "look-at" view, rarely the operator's working canvas.
* **Click-to-track box drawing on Outrider feed in mobile sheet**: drawing a
  precise box on a phone is fiddly. The video sheet exposes the feed + the
  Acquire-by-text path; box-draw remains a desktop feature.
* **Survey polygon editing on mobile**: tap-to-add-vertex works (UI mode
  switches), but multi-vertex polygon editing isn't optimized for thumb. Mobile
  shows the planned polygon and can commit/cancel; precise polygon edits are a
  desktop workflow.
* **Tablet redesign**: tablet inherits desktop with the tap-target shim. A
  dedicated tablet layout is future work.

## 13. Testing checklist (manual, iPhone 13 Pro)

* [ ] Top bar fully visible below notch; no clipping.
* [ ] Bottom sheet closed → ARM, TAKEOFF, HOLD, LAND, RTL, BRAKE, FORCE-disarm reachable.
* [ ] Drag handle snaps to closed/half/full.
* [ ] PTT FAB depresses on touch, releases on lift, speech reaches the voice model.
* [ ] Vehicle pill opens picker, swap works.
* [ ] 2D ↔ 3D toggle works.
* [ ] Map tap shows goto/orbit card with thumb-friendly buttons.
* [ ] Force-disarm shows confirm card; cancel and confirm both reachable.
* [ ] Failed command (e.g. ARM while disconnected) shows red toast.
* [ ] Landscape: layout reflows to left-map / right-controls split.
* [ ] Desktop ≥1024px: pixel-identical to before (visual diff).
