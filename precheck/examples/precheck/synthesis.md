## Project-specific synthesis rules

<!-- These rules are appended to the base synthesis spec. They add to
     (not replace) the default clustering/dedup/severity/format steps. -->

- Any finding touching `src/payments/` is at least HIGH severity.
- Group findings by component (`[api]`, `[worker]`, `[frontend]`) in a
  one-line summary before the severity breakdown.
- Known tech debt: the `LegacyAdapter` pattern in `src/adapters/` is
  intentional — don't flag it as a reusability issue.
