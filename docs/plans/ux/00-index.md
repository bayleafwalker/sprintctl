# sprintctl UX plan pack

This pack proposes a UX path for `sprintctl` that improves onboarding, day-to-day flow, and local customization **without weakening the existing local-first, CLI-first workflow model**.

## Files

- `01-ux-north-star-and-guardrails.md`
  - Product framing, UX principles, boundaries, and success criteria.
- `02-journeys-and-phased-delivery.md`
  - End-to-end user journeys and a phased implementation plan.
- `03-local-operator-customization.md`
  - External customization patterns: aliases, wrappers, startup helpers, agent guidance, and repo integration.

## Recommended placement in repo

Suggested target path:

```text
docs/plans/ux/
```

## Recommended adoption order

1. Commit `01-ux-north-star-and-guardrails.md` first to lock the product boundary.
2. Use `02-journeys-and-phased-delivery.md` as the implementation backlog source.
3. Treat `03-local-operator-customization.md` as the companion document for examples, repo templates, and integration guidance.

## Intent

The aim is not to make `sprintctl` “friendlier” by hiding the protocol.
The aim is to make the **default path narrower**, keep advanced coordination optional,
and push convenience to **supplemental local tooling** rather than into the core binary.
