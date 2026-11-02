# Chat Result Cards Design

## Problem

The banking assistant renders facts, transaction history, and pre-action due diligence inside the same conversation bubble. Dense reviews currently place the subject, registration, qualification, four signal categories, gaps, questions, evidence sources, product details, and notice in one continuous column. This makes the key decision and the supporting evidence hard to scan, especially on a phone.

## Goals

- Make the status and most decision-relevant information apparent at a glance.
- Group related facts consistently across assistant result cards.
- Keep all user-relevant evidence available without presenting every detail at the same visual weight.
- Preserve clear, prominent risk, refusal, missing-evidence, confirmation, and verification states.
- Keep layouts readable at narrow widths and preserve existing keyboard and screen-reader behavior.

## Scope

Update the conversation result cards in the main banking app (`frontend/app.js` and `frontend/styles.css`). Focus on ordinary facts, transaction rows, confirmation/result status, and the pre-action review panel. Do not redesign the service forms, notifications, plan page, staff page, or simulated IM page.

## Proposed UI

- Use a consistent card hierarchy: concise heading/status, primary result, grouped supporting facts, then a visually distinct action area where applicable.
- Render ordinary facts as compact label/value groups with predictable alignment and spacing. Preserve wrapping for long values and values on mobile.
- Render transactions as distinct rows with merchant and date first, followed by category, direction, and amount in a scan-friendly arrangement.
- Structure pre-action review as: decision and subject; registration and qualification; the four review signals; product details when present; always-visible gaps and suggested questions; simulation/source notice.
- Keep blocked decisions, uncovered signals, evidence gaps, and authorization controls visible. Supporting evidence may be progressively disclosed only if no decision-relevant gap or status is hidden.
- Keep product risk level, term, and redemption details visible when present; only the sample-source list may be collapsed behind a descriptive disclosure control.
- Use a responsive grid for signal summaries on wide cards and a single-column layout when space is constrained.
- Keep the current light banking theme and reuse existing CSS variables. Avoid adding a UI dependency.

## Behavior and Data

The change is presentational. It must not alter API requests, authorization semantics, action hashes, card statuses, field sanitization, or money formatting. DOM content continues to be created with text nodes/textContent so untrusted review and transaction strings are not interpreted as HTML. All currently rendered review fields remain available to the user, including product details and evidence sources.

## Accessibility and Responsive Requirements

- Preserve semantic headings, lists, and label/value relationships.
- The expandable sample-source section uses native `details`/`summary`, is keyboard operable, and has a descriptive summary label.
- Maintain visible focus styles and adequate contrast for status indicators.
- At narrow mobile widths, prevent horizontal overflow; stack signal and fact groups as needed.
- Respect reduced-motion preferences; the visual change does not require motion.

## Verification

- Extend the focused frontend tests to verify the review card's information grouping, always-visible blocked/gap content, and availability of supporting details.
- Keep existing rendering, sanitization, authorization, and transaction tests passing.
- Run the frontend test suite and the repository's relevant static checks.
- Inspect the main app at desktop and mobile widths if a local server/browser is available.

## Non-Goals

- Rewriting business copy or due-diligence policy.
- Hiding or removing any current user-facing review data.
- Changing backend response contracts or persistence.
- Deleting or renaming files.
