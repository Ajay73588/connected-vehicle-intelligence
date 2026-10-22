# ADR-004: AI agent architecture — deterministic tool-using planner, optional LLM narration, hash-chained audit

- Status: accepted
- Date: 2026-10-01

## Context
The Fleet Copilot must answer questions about the fleet using real data, recommend actions, estimate impact, never leak across
tenants, never perform a destructive action without confirmation, and leave an auditable trail. It must also keep working when no
LLM is reachable (offline demo, air-gapped customers) and must be testable and reproducible.

## Decision
`question → input guardrails → intent → tool plan → permission & schema checks → execution (tenant-scoped, RLS) → output validation
→ answer composition → (optional) LLM narration with a grounding check → audit`.

* **Planner** (`agent/planner.py`): keyword-scored intent classification + entity extraction (VIN, counts) selects 1–4 tools from
  a registry of 14 typed tools (`agent/tools.py`). No LLM is needed to choose tools, so behaviour is deterministic and unit-tested.
* **Tools** read the same PostgreSQL/Redis/Mongo data as the REST API with the caller's tenant (RLS + explicit filters) and role
  permissions. Write tools (`create_alert`, `acknowledge_alert`) are *destructive*: the agent only **proposes** them and returns a
  single-use, 5-minute, user-bound confirmation token; execution happens in `POST /agent/confirm`.
* **Responder** (`agent/responder.py`) composes answers from tool outputs only. Numbers shown (probabilities, costs, counts) are the
  tool's numbers; derived arithmetic (e.g. idle cost = idle hours × L/h × price) is performed in tools with the assumptions returned
  next to the result.
* **LLM (optional)**: if `ANTHROPIC_API_KEY` is set the draft answer + tool results are sent to the model *only to improve wording*.
  The narration is accepted only if every number in it can be found in the tool results (`verify_grounding`); otherwise the
  deterministic answer is returned and the rejection is audited. Tool outputs are passed as data, with an instruction to ignore
  instructions inside them (prompt-injection defence in depth).
* **Guardrails**: length/charset limits; prompt-injection and cross-tenant phrase screening; Pydantic validation of every tool
  argument (VIN pattern, bounds); secret-key scrubbing and size cap on tool outputs; per-user rate limit; role-based tool permissions.
* **Audit**: every step (`question`, `intent`, `tool_call`, `tool_result`, `guardrail`, `confirmation`, `answer`) is appended to
  `agent_audit`, a per-tenant SHA-256 hash chain (`hash = H(prev_hash ‖ canonical_json(row))`); the API role has INSERT-only
  privileges, and `GET /agent/audit` verifies the chain (tampering → `chain_valid=false`).

## Alternatives considered
* **LLM-driven tool calling (function calling loop)**: more flexible phrasing and multi-step reasoning; but non-deterministic,
  needs network/keys, harder to guarantee safety and to test. Kept as a future extension behind the same tool registry.
* **RAG-only chatbot**: cannot compute (idle cost, knapsack schedule) or act, and would answer from stale text instead of live data.
* **Text-to-SQL**: powerful but risky (data exfiltration, injection, tenant bypass) and hard to audit; typed tools are safer.

## Trade-offs / consequences
* Language coverage is limited to the intents in the planner; unknown questions fall back to a fleet overview rather than guessing.
* The planner is keyword based (documented limitation); embedding-based intent routing is a straightforward upgrade using the existing
  `embed()` seam.
* Vector retrieval (`search_knowledge_base`, similar incidents) uses a hashed n-gram embedding (offline, deterministic), not a neural
  model — adequate for the small, keyword-rich corpus; the dimension and function are the swap point for a real embedding model.
