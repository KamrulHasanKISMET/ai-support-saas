# Queues

Placeholder for Redis-backed background job queues (e.g. BullMQ).

Planned jobs, in build order:

1. `knowledge-ingestion` — chunk + embed uploaded documents (calls Python API)
2. `memory-consolidation` — periodically summarize short-term into long-term memory
3. `channel-webhook-retry` — retry failed outbound sends to WhatsApp/Facebook
4. `notification` — internal alerts (e.g. escalation to a human agent)

Do not add a queue until a real background job needs one — see
architecture doc section 30 ("do not start with unnecessary enterprise
complexity").
