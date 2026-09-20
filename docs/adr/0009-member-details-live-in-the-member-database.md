---
status: accepted
date: 2026-09-19
---
# Member details live in the member database, not in wiki pages

Contact details, entry numbers, hostels, birthdays, socials and WhatsApp identifiers are needed by agents (nudges, identity resolution) but are PII and would be duplicated across pages. They live only in the ARIES CMS (existing, hosted on Cloudflare), which will expose protected read endpoints for Watcher and, in later phases, write endpoints so agents can update it. The team CSV seed (`~/projects/watcher-seed/`, outside the vault) is the bootstrap data for it. Wiki member pages carry role, status, interests, derived project/task lists and Notes. Team/wing was dropped: role is sufficient.

**Consequences**: the wiki can be shared more freely; the Member Registry (WhatsApp identity ↔ CMS member id) is a Gateway table keyed on the CMS id; anything on a page that looks like a phone number or email is a lint error.
