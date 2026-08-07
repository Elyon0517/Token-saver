# Coding Approach

## Think Before Coding

- State assumptions explicitly when they matter.
- If multiple interpretations exist, name them instead of choosing silently.
- If the smallest correct fix turns into a large structural or architectural change, stop and ask with a short plan.
- Define what success looks like before implementing.

## Mandatory Basic UI Change Gate

For every code change that affects rendered UI, layout, controls, styling, editor chrome, or runtime HUD, apply all six rules before implementation:

1. **Align shared frames:** peer regions that share a template or read as one visual template use the same outer edges, row starts, gutters, and intended height. Unmatched left/right stacks or a tall panel beside a short peer fail unless the asymmetry communicates a clear priority.
2. **Use one row when it fits:** keep related labels, values, inputs, buttons, and compact controls on one line at widths where they fit without overlap, clipping, or unreadable compression. Wrap only at a measured narrow breakpoint; contain long text inside its own bounded area so it does not push neighboring controls down.
3. **Group once:** use one visual boundary for one logical group. Do not add a nested card, panel, fieldset, background, heading, or classification when the parent already supplies that grouping.
4. **Add information on the alignment grid:** put supporting information inside the panel that owns it. If it is a genuinely separate row or category, make the row explicit and align its outer edges and gutters with the surrounding layout; do not append a floating or offset row.
5. **Keep geometry stable:** reserve or contain space for expected loading, status, error, and optional-content states so controls and surrounding panels do not jump or resize unpredictably.
6. **Make state semantics truthful:** visible color, text, icon, badge, and enabled/disabled state must match the real lifecycle or application state.

Accessibility, localization, readable target sizes, and narrow viewports may require wrapping or deliberate asymmetry. Treat each exception as an explicit layout decision, preserve the alignment system after reflow, and send the rendered desktop and narrow states to Ending Real verification.

## Prefer The Simplest Viable Solution

- Write the minimum code that solves the actual request.
- Do not add unrequested features, abstractions, configurability, fallbacks, compatibility layers, or defensive branches.
- Trust declared inputs, outputs, formats, and contracts. Fix the producer or contract instead of adding consumer-side repair logic.
- For exactly two mutually exclusive outcomes, use a plain `if`/`else`.
- For three or more outcomes, use the language's switch-style construct: Python `match`/`case`, C# `switch`/`case`, switch expressions, or guarded switch cases.

## Keep Edits Surgical

- Touch only what the request requires.
- Match local style unless this skill or a tighter repo instruction says otherwise.
- Remove only unused imports, variables, or helpers made obsolete by your own change.
- Identify the authoritative source path before editing, copying, moving, or generating files.
- Do not mix sibling packages, caches, clones, or workspaces just because filenames look similar.

## Naming

- Use correct English spelling for new identifiers, file names, keys, comments, and prompt labels.
- Keep an existing misspelled name only when required by an external API, persisted schema, third-party contract, or compatibility boundary.
- Use clear full-word names instead of unexplained abbreviations.
