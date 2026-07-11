# Ballast — UI Specification

**Status:** Draft v0.1
**Companion docs:** PRD.md, TechSpec.md
**Scope:** Dashboard web app (P2) — screens, subscreens, inputs, states, and interactions.

---

## 0. Global shell (present on every screen)

### Layout
- **Left nav rail** (collapsible to icons): Overview · Chaos Lab · Cost & Budget · Event Log · Settings. Dependency Detail is reached by drill-in, not nav.
- **Top bar:** app name/logo · environment chip (`dev` / `prod`, derived from config) · connection status dot (live / reconnecting / stale) · global time-range control.
- **Banner slot** below top bar (priority-stacked): chaos-active → hard-budget-ceiling → connection-lost → demo-mode.
- **Toast stack** bottom-right (max 3 visible, coalescing per UISpec §7 of the component inventory).

### Global inputs
| Input | Type | Options / default | Notes |
|---|---|---|---|
| Time range | Segmented control | **Live** · 15m · 1h · 24h (default: Live) | "Live" = streaming via WebSocket; others = historical query. Persisted per screen in URL. |
| Environment chip | Read-only indicator | dev / prod | Drives chaos confirmation strictness. Never user-editable in UI. |
| Nav collapse | Icon toggle | expanded default | Persisted in localStorage. |

### URL routes (deep-linkable)
```
/                     → Overview
/deps/:name           → Dependency Detail (full page)
/chaos                → Chaos Lab (presets tab)
/chaos/runs/:id       → Chaos run detail
/cost                 → Cost & Budget
/events               → Event Log (filters serialize into query params)
/settings/:tab        → Settings
```

---

## 1. Overview (`/`)

The hero screen. Four regions, top to bottom.

### 1.1 Status strip
Five stat tiles, each with a micro-sparkline (last 5 min):
1. **In-flight requests** — current / max concurrency ("42 / 100").
2. **Queue depth** — current / ceiling; turns amber at 80%.
3. **Requests/sec** — rolling 10s rate.
4. **Shed count** — total this session; red if > 0 in last 60s.
5. **Cost burn** — "$3.20 / $10.00/hr"; amber past soft ceiling, red past hard.

*Interactions:* tiles 1–2 click → Settings › Concurrency (read-only view); tile 5 click → Cost & Budget.

### 1.2 Breaker grid
Responsive card grid, one card per named dependency.

**Card anatomy:** name · state chip (CLOSED / OPEN / HALF-OPEN) · failure-rate bar vs. threshold · p95 latency vs. baseline · fallback-active badge (cache / cheaper-model / static) · last-event timestamp · 30s sparkline.

**Card states:** healthy · degrading (closed, failure rate ≥ 60% of threshold — amber accent) · open · half-open (probe progress "2/3") · fallback-active overlay badge.

**Inputs on this region:**
| Input | Type | Options / default | Notes |
|---|---|---|---|
| Search dependencies | Text field | — | Filters grid live; matches name substring. |
| Sort | Dropdown | **Health (worst first)** · Name A–Z · Traffic | Health-first means an open breaker is always top-left. |
| Card click | — | — | Opens Dependency Detail drawer. |
| Card hover | — | — | Quick-stats popover: last 5 calls as ✓/✗ dots, window stats, cooldown countdown if open. |

### 1.3 Live charts row
Two side-by-side time-series charts (respect global time range):
- **In-flight + queue depth** (dual line, shed events as red dots on the x-axis).
- **Cost burn** (area chart with soft/hard ceiling reference lines).

*Inputs:* hover tooltip (timestamp, value); click a shed-event dot → Event Log pre-filtered to that moment.

### 1.4 Live event feed
Compact scrolling list, newest on top, ~8 rows visible. Row = severity dot · timestamp · event type · dependency · one-line detail.

| Input | Type | Default | Notes |
|---|---|---|---|
| Pause feed | Toggle button | playing | Auto-pauses on hover (resume on mouse-out) so rows don't scroll away mid-read. Shows "n new events" pill while paused. |
| Row click | — | — | Opens event detail drawer. |
| "View all" link | — | — | → Event Log with current time range. |

### 1.5 Empty / first-run state
Replaces regions 1.1–1.4 when no SDK has connected:
- `pip install ballast` + 3-line integration snippet (copy button).
- **"Run built-in demo"** primary button → spins up mock swarm + `api_outage` scenario; screen transitions to live state with demo-mode banner.
- "Waiting for first event…" status with connection instructions expander.

---

## 2. Dependency Detail (`/deps/:name`, also drawer from Overview)

Same content in two containers: right-side drawer (default, keeps Overview visible) and full page (via "open as page" link — shareable). Four tabs.

### 2.1 Tab: Health (default)
- **State timeline band** — horizontal strip for selected range: green/red/amber segments; hover shows transition reason and timestamp; chaos-injected periods get purple hatching so real vs. injected failure is visually distinct.
- **Rolling window internals:** failure-rate gauge vs. threshold · p95 latency vs. baseline (with the multiplier line) · call count in current window.
- **"Why is it in this state?" chip** — click → popover: "Failure rate hit 55% (threshold 50%) over the last 14 calls at 14:02:31."
- **Manual controls** (admin actions, both require confirm dialog):
  | Input | Type | Notes |
  |---|---|---|
  | Force open | Button (danger) | Confirm dialog: "All calls to X will go to fallback until you close it or cooldown logic is re-enabled." |
  | Reset / force close | Button | Confirm dialog. Available only when open/half-open. Emits `manual_override` event. |

### 2.2 Tab: Fallbacks
Ordered chain visualization (cache → cheaper model → static → fail), active rung highlighted. Each rung expands inline:
- **Cache:** hit rate, entry count, TTL config, "clear cache" button (confirm).
- **Cheaper model:** target model name, requests routed count, estimated $ saved, classifier pass rate.
- **Static:** the configured fallback value (truncated, expandable), times served.

### 2.3 Tab: Config
Read-only in v1 (source of truth = SDK `ballast.configure()`): failure threshold · latency multiplier · cooldown · window size · fallback chain. Each field has a tooltip explaining its semantics. Banner: "Configured via SDK — edit in code." Copy-as-Python button emits the `configure()` snippet.

### 2.4 Tab: Events
The Event Log component pre-filtered to this dependency (same filters as §5, dependency locked).

---

## 3. Chaos Lab (`/chaos`)

Three subscreens as tabs, plus a pinned live-experiment panel that appears above tabs whenever a run is active.

### 3.1 Live experiment panel (conditional, pinned)
Visible during any active run, regardless of tab:
- Annotated horizontal timeline filling in real time: `injection started → first failures → breaker tripped (Δ1.4s) → fallback serving → injection ended → half-open → closed`.
- Countdown ("18s remaining") · affected-dependency chips · live pass/fail indicators (detection < 2s target).
- **Stop button** — single click, no confirmation, always reachable. Duplicated in the global banner.

### 3.2 Tab: Presets (default)
Grid of scenario cards: name · plain-English description · affected deps as chips · expected outcome ("breaker trips within ~5 calls") · last-run pass/fail badge with detection latency · **Run** button.

Run flow: Run → confirmation modal → panel 3.1 activates.

**Confirmation modal inputs:**
| Input | Type | Notes |
|---|---|---|
| Blast-radius summary | Read-only | "80% failure on openai_api for 30s — all agents using it will be affected." |
| Type-to-confirm | Text field | Only when env = prod: must type the dependency name. Hidden in dev. |
| "Don't ask again in dev" | Checkbox | dev-only; persisted. |
| Confirm / Cancel | Buttons | Confirm disabled until type-to-confirm matches (prod). |

### 3.3 Tab: Custom injection
Form (can also open as modal from Overview later):
| Input | Type | Options / default / validation | Notes |
|---|---|---|---|
| Dependencies | Multi-select chips | ≥ 1 required | From configured dependency list. |
| Fault type | Radio | **Failure** · Latency · Corruption | Drives which parameter row shows. |
| Failure rate | Slider + numeric | 0–100%, default 80%, step 5 | Shown for Failure/Corruption. |
| Latency multiplier | Slider + numeric | 1.5×–10×, default 3× | Shown for Latency. |
| Duration | Numeric + unit | 5–600 s, default 30 | Hard cap 600s in v1 — no indefinite injection. |
| Ramp | Checkbox | off | If on: rate ramps 0→target over first 25% of duration. |
| Save as preset | Checkbox + name field | off | Name required if checked; appears in 3.2 afterward. |
| Run | Primary button | — | → same confirmation modal as presets. |

Validation errors inline under each field; Run disabled until valid.

### 3.4 Tab: History
Table of past runs: timestamp · scenario/custom label · dependencies · pass/fail · detection latency · duration. Row click → `/chaos/runs/:id` — a frozen replay of the 3.1 timeline plus a link to the event-log slice for that window.

| Input | Type | Notes |
|---|---|---|
| Filter: outcome | Dropdown | All / Passed / Failed |
| Filter: dependency | Multi-select | — |
| Re-run | Button per row | Same confirmation flow. |

### 3.5 Empty states
Presets tab always shows preset cards (they ship built-in); History empty state: "Chaos runs will appear here — try a preset above."

---

## 4. Cost & Budget (`/cost`)

Three regions (single page, no tabs in v1).

### 4.1 Burn overview
- Headline tiles: current burn rate ($/hr) · spend this session · spend vs. budget %.
- Main area chart: cost over time with soft/hard ceiling reference lines; fallback-active periods shaded to visually connect "breaker open" with "burn flattens."

| Input | Type | Options / default |
|---|---|---|
| Timeframe | Segmented | 1h · **24h** · 7d |
| Group by | Dropdown | **Model** · Dependency · Session |

### 4.2 Routing decisions
- Split bar: requests on primary model vs. downgraded vs. cache-served vs. static-fallback.
- **Estimated savings** counter ("fallbacks saved $2.14 during the last incident") — the demo money-shot; computed as (primary-model price − actual price) × downgraded volume.
- Table: per dependency — total calls, downgrade %, cache hit %, savings.

### 4.3 Budget configuration
Read-only in v1 (SDK-configured), shown as a definition list with tooltips: per-hour ceiling · per-run ceiling · soft-ceiling margin % · hard-ceiling behavior (downgrade-first vs. refuse). "Edit in code" banner + copy-as-Python. When Settings becomes writable (post-v1), these become:
| Input | Type | Validation |
|---|---|---|
| Budget $/hr | Numeric | > 0, warn if > 10× current burn |
| Per-run budget $ | Numeric | > 0 |
| Soft ceiling | Percent slider | 50–95%, default 80% |
| Hard-ceiling behavior | Radio | Downgrade-first (default) · Refuse new requests |
Save → budget-edit confirm modal.

---

## 5. Event Log (`/events`)

Single screen: filter bar + virtualized table + detail drawer.

### 5.1 Filter bar
All filters serialize to query params (shareable URLs):
| Input | Type | Options / default |
|---|---|---|
| Search | Text | Free text over detail blob + dependency name. |
| Event type | Multi-select chips | breaker_trip · breaker_close · fallback_used · request_shed · chaos_injected · manual_override (all on by default) |
| Dependency | Multi-select | From known dependencies. |
| Session ID | Text (exact) | — |
| Time range | Uses global control | — |
| Live tail | Toggle | **on** when range = Live; auto-off otherwise. Same hover-pause behavior as Overview feed. |
| Clear filters | Link button | Visible only when any filter active. |

### 5.2 Table
Columns: severity dot · timestamp (relative + absolute on hover) · type · dependency · summary · session ID (click = filter to it). Virtualized scroll (event storms produce thousands of rows). Row click → inline accordion with one-line JSON preview; "expand" → detail drawer.

### 5.3 Detail drawer
Pretty-printed JSON blob · copy button · quick actions: "filter to this session" · "filter to this dependency" · "view dependency" (→ §2).

### 5.4 Export
| Input | Type | Notes |
|---|---|---|
| Export | Split button | CSV · JSON. Exports *current filtered set*, capped at 50k rows with a warning above that. |

### 5.5 Empty state
"Events appear when breakers trip, requests shed, or fallbacks fire." + link to run the demo scenario if no SDK connected.

---

## 6. Settings (`/settings/:tab`)

Five tabs. **v1 posture: read-only mirror of SDK config** with per-field tooltips and "copy as Python" everywhere; the tables below note which inputs activate post-v1.

### 6.1 Tab: Dependencies
Table, one row per dependency: failure threshold · latency multiplier · cooldown · window size · fallback chain summary. Row click → Dependency Detail › Config tab.
*Post-v1 inputs:* inline edit per cell — threshold (0.05–0.95), multiplier (1.5–10), cooldown (1–300s); save per row with validation.

### 6.2 Tab: Concurrency
- Max in-flight (read-only numeric, default 100) · max queue depth (default 500) · shed policy (FIFO v1; priority post-v1).
- Live overlay: current values against these ceilings (small chart) so the numbers have context.

### 6.3 Tab: Chaos safety
| Input | Type | Default | Notes |
|---|---|---|---|
| Chaos enabled | Read-only indicator | from `BALLAST_CHAOS` | UI can never enable chaos; env/config only. Shows how to enable. |
| Require confirmation in dev | Toggle | on | The only writable setting in v1 (localStorage, UI-level only). |
| Max injection duration | Read-only | 600s | — |

### 6.4 Tab: Data
- Event log location + size on disk · retention note.
| Input | Type | Notes |
|---|---|---|
| Purge events older than… | Dropdown + button | 7d / 30d / all. Type-to-confirm modal ("delete 12,431 events — cannot be undone"). |
| Download full DB | Button | Streams the SQLite file. |

### 6.5 Tab: About
Version · SDK version detected · docs link · config file path · WebSocket endpoint (for debugging connection issues).

---

## 7. Cross-cutting behaviors

- **Live vs. historical:** every chart/list respects the global time-range control; "Live" streams over WebSocket, others hit the query API. Switching away from Live pauses streams (no hidden background load).
- **Stale-data honesty:** if the WebSocket drops, all "live" surfaces gray their leading edge and the connection banner shows data age. Never render stale data as current.
- **Chaos visual language:** everything caused by injection (timeline segments, events, banner) uses one distinct color (purple) so injected failure is never confusable with real failure — in the UI or in screenshots.
- **Confirmation hierarchy:** destructive/dangerous = modal (chaos run, force-open, purge; type-to-confirm when irreversible or prod). Ongoing states = banner. Moments = toast. Detail = drawer. Never a modal the system opens on its own.
- **Keyboard:** Esc closes drawer/modal (modal only when safe) · `/` focuses nearest search field. Nothing more in v1.

## 8. Build order (maps to PRD milestones)

| Milestone | Scope |
|---|---|
| M4 | Global shell + banners + toasts (with coalescing) · Overview (all four regions + empty state) · Event Log (filters, table, drawer) · Dependency drawer (Health tab only) |
| M4.5 | Chaos Lab: presets + confirmation modal + live experiment panel + history |
| M5 | Cost & Budget · remaining Dependency tabs (Fallbacks, Config, Events) · Settings (read-only) · custom injection builder |
| Post-v1 | Writable settings + budget editing · priority shed policy UI · saved custom presets sync |
