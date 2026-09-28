# Visual appeal rubric (Launchproof Critic skill)

Score each viewed page, then average into `appeal_score` (0–100 integer).
Spend at most `max_vision_views` views total (one URL × one viewport = one view).
Prefer Chaos `interesting_states` first, then Tester `pages`, then home.

## Rubric dimensions (each roughly 0–100, weight equally)
1. **Hierarchy** — Clear primary headline and visual order; not everything the same size.
2. **Contrast / readability** — Text readable on its background; no grey-on-grey body copy.
3. **Whitespace / clutter** — Breathing room vs cramped stacks of competing blocks.
4. **CTA clarity** — One obvious next action for a first-time visitor.
5. **Mobile density** — On phone viewport: tap targets and stacking feel usable, not crushed.
6. **Trust / polish** — Broken layout, placeholder lorem, uneven alignment, missing favicon feel, generic template smell.

## Issue kinds (use exactly)
- `visual_hierarchy`
- `visual_contrast`
- `visual_clutter`
- `visual_cta`
- `visual_mobile`
- `visual_polish`
- `visual_broken_layout` (overlapping elements, cut-off text, severe misalignment)

Severity guide: high for unreadable / broken layout, medium for weak CTA or clutter, low for polish nits.

## Method
- Re-visit live URLs in your own browser (sandboxes do not share Tester PNGs).
- Capture your own screenshots under `/tmp/critic-shots/` if useful for evidence text; summarize what you see in `detail`.
- Do **not** call Anthropic or any external vision API. Use Brainbase model + browser only.
- If the harness cannot show images, still score from DOM text, layout clues, and Playwright screenshots you describe.

## Output fields
- `appeal_score`: integer 0–100 (average across views, or null if zero views)
- `views_used`: integer
- `visual_issues`: list of `{severity, kind, where, detail}`
- `notes`: short free text
