# Repository guardrails

- Never commit live snapshots, account identifiers, task titles, credentials,
  SQLite databases, or captured responses.
- Demo fixtures must be synthetic and visibly labelled.
- Keep the default bind and upstream loopback-only.
- Preserve the distinction between server observations, retrospective
  explanations, and conditional forecasts.
- A test passing does not mean a forecasting candidate has been adopted.
- UI changes must preserve keyboard access, visible focus, 320 px reflow,
  reduced-motion behavior, chart status text, and runtime table access.
