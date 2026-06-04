# Per-dimension override: security

This file is read ONLY by the security reviewer, on top of its built-in focus and
the shared context.md. Use it for project-specific security rules and severity
calibration. (Same pattern for the other dimensions: reusability.md, quality.md,
efficiency.md, plan-coverage.md, docs.md — create only the ones you need.)

## Extra focus areas
<!-- e.g. "All DB access goes through repo/ — raw SQL anywhere else is a CRITICAL finding." -->
<!-- e.g. "Auth tokens must never be logged; flag any logger call that takes a token." -->

## Severity calibration
<!-- e.g. "Missing input validation on internal-only endpoints is MEDIUM, not HIGH." -->

## Out of scope / known-accepted
<!-- e.g. "The legacy /v1 API is being deprecated; don't report findings there." -->
