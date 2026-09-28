# Messy-human scenario pack (Launchproof Chaos skill)

Run scenarios in this order. Stop when `max_chaos_scenarios` is reached even if more remain.

## Scenario pack

1. **rage_cta_before_idle** — Open the start URL. Before `networkidle`, double- or triple-click the primary CTA (Buy, Get started, Sign up, Upgrade). Watch for duplicate navigations, double POSTs, blank screens, or uncaught errors.
2. **spam_nav_while_loading** — Click a nav or footer link, then immediately click 3–5 other links before the first page settles. Note races, aborted fetches, or stuck spinners.
3. **double_form_submit** — Find a visible form (signup, contact, newsletter). Fill required fields with synthetic values only (`chaos@example.com`, `Chaos User`, password `ChaosTest1!`). Click submit twice within 300 ms. Check for duplicate accounts, double success toasts, or no debounce.
4. **click_disabled_or_loading** — Trigger an action that shows a loading/disabled primary button, then keep clicking it. Report if a second request fires or the UI unlocks inconsistently.
5. **back_forward_mid_flow** — Walk 2–3 steps into a flow (pricing → signup → next), then browser Back then Forward. Report lost state, blank pages, or broken history.

## Issue kinds (use exactly)
- `chaos_double_submit`
- `chaos_race_click`
- `chaos_nav_spam`
- `chaos_disabled_click`
- `chaos_history`
- `chaos_crash` (page blank, console error storm, or navigation failure after a messy action)

Severity: critical (data corruption / crash), high (duplicate side effects), medium (broken UX under race), low (cosmetic flicker).

## Interesting states
After each scenario that produced an odd screen, append:
`{ "url", "viewport": "desktop"|"phone", "why", "evidence" }`
so Critic can spend vision budget on those URLs first.
