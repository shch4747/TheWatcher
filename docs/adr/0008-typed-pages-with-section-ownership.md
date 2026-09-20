---
status: accepted
date: 2026-09-19
---
# Wiki pages are typed, sections have one owner, and agent regions are fenced

Humans and agents both edit the same markdown. The naive options were "agents own the wiki, humans read" (kills the point of a wiki) or "anyone edits anything and agents re-parse whatever they find" (agents slowly mangle pages, humans lose work). We chose a contract instead: every page has typed frontmatter validated by a per-type schema (`meta/schema/`), sections are located by H2 title and each has exactly one owner class (`human` / `shared` / `managed` / `append` / `derived`), agent-written regions are fenced with HTML comments, line items carry Obsidian block ids so identity survives rewording, and metadata is inline fields. References are wikilinks everywhere, including frontmatter, so renames are safe. Derived data is regenerated, never hand-maintained. Lint reports; it repairs only additive defaults.

**Consequences**: every agent write goes through one parser/serialiser with a round-trip idempotency test; schema changes are a Proposal, not an edit; the format is somewhat verbose (fences, block ids) — accepted for the robustness.
