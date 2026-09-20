---
status: accepted
date: 2026-09-19
---
# gowa is the WhatsApp transport

We need programmatic access to ARIES WhatsApp groups. Options were Baileys (Node library), gowa (go-whatsapp-web-multidevice, a self-contained REST server on whatsmeow), or the official Cloud API (not usable for reading arbitrary groups). We chose gowa: one binary/container, its own persistent chat store with a query API, signed webhooks, quote-reply/mention/poll/reaction support, multi-device, and a built-in MCP endpoint — so nothing about WhatsApp protocol details leaks into our code.

**Consequences**: gowa runs on an unofficial client; Meta can ban the number. The number is therefore treated as disposable — a dedicated SIM (currently a member's own number as a stopgap). All WhatsApp access goes through the Gateway, never to gowa directly from other modules, so swapping the transport later touches one module.
