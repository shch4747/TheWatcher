---
status: accepted
date: 2026-09-19
---
# The Scheduler is separate from the WhatsApp module

The original diagram had a "WhatsApp notification + scheduling manager". Scheduling ("run the Project Agent in 4 hours", "Innovation Agent makes sure the others ran this week") is needed by every agent and has nothing to do with WhatsApp; bundling them would make every agent depend on the WhatsApp module. The Scheduler is its own module with its own interface; the WhatsApp side is reduced to the Gateway (I/O) and the WhatsApp Agent (LLM). What orchestration the Scheduler offers beyond delayed/recurring triggers is still open.
