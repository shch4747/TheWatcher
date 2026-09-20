---
status: accepted
date: 2026-09-19
---
# Three model tiers: Decision Model, Worker Model, Mentor Model

Ingestion is the token sink, and most of its decisions have a bounded answer space (which thread does this message belong to? is this chatter? is the bot being addressed? which agent should get this?). Those go to a Decision Model (Jev — sub-second, ~$0.0004 per decision, returns probabilities rather than text). Open-ended work (summaries, extraction) goes to a cheap Worker Model, which escalates to a Mentor Model when the Decision Model's confidence on the item is low or the item is high-stakes. A plain "one LLM does everything" design was rejected on cost and latency.

**Consequences**: every bounded decision must be written as a Choice/Noul/Score question with a known option list; when the top probability is below threshold the Worker Model takes the decision instead. Vendor lock-in to Jev is contained behind the Decision Model interface. Concrete model choices per tier are a spec-time decision, not recorded here.
