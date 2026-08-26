# North Star — BNDC Support Agent

## End state
A member who posts a question or problem in #support gets an immediate, evidence-backed
answer from the bot: real Discord community knowledge cited by message links, concrete
workflow help (specific workflows sent, the member's own workflow edited and returned as
JSON), and onboarding into VibeComfy when relevant. Threads feel like one continuous
conversation with the same assistant across every follow-up.

## Enduring principles
- **Evidence over vibes** — answers cite actual Discord messages (jump URLs) from the
  archive/hivemind whenever they exist; never invent community precedent.
- **Concreteness over advice** — when workflow-shaped, send/attach specific workflows and
  edited JSON, not generic guidance.
- **Thread = session** — follow-ups continue the same context; no re-asking, no amnesia.
- **Public-surface safety** — this agent talks to any member publicly: read/research +
  helpful output only; no admin powers (payments, moderation, deletion, social).
- **Silence is failure** — every member post in scope gets either an agent reply or an
  explicit handoff to admin; never a dropped message.

## Anti-patterns to avoid
- Hallucinated "community says X" without a real cited message.
- Generic LLM advice where a specific workflow or JSON answer was possible.
- Parallel re-implementations of existing machinery (admin-chat loop, grants trigger
  patterns, hivemind endpoints) instead of reuse.
- Admin-grade tools exposed on a public channel surface.
- In-memory-only state that silently loses thread continuity on restart without a
  rebuild path.
- Ceremonial abstractions: no new frameworks, layers, or config surfaces beyond what the
  feature needs.

## Aligned progress feels like
Each batch lands one visible member-facing capability end to end (trigger → turn → reply),
validated against the real code paths, with tests defending observable behavior only.
