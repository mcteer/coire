# docs/design

Design handoff for `apps/coire-web`.

- `DESIGN.md` — the specification: principles, shell, surfaces, type, components, per-page anatomy, interaction and accessibility rules. Read this first.
- `tokens.css` — every colour, radius, shadow, size, and spacing value as CSS custom properties. Import at the app root; components consume only these.
- `mockups/` — five static 1440×900 HTML pages (`chat`, `training`, `images`, `settings`, `admin`). Open in a browser. Sample data only; measurements are authoritative.

Behavioural questions (what a picker lists, when a model warms, what an admin may do) are answered by `docs/ARCHITECTURE.md` and the constitution, not here.
