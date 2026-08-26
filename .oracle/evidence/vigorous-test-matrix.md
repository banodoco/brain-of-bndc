# Vigorous Test Evidence Matrix — brain-of-bndc delta (d186dee, Ox Alpha)

| Criterion (agent_goal) | Evidence | Command | Result | Reviewer |
|---|---|---|---|---|
| 1. Bot posts agent reply in new #support thread | support_cog trigger → agent chat → thread.send | `pytest -k support` + B3 integration | 107 passed, `test_view_attached_only_to_last_chunk` verifies chunked delivery with OutcomeView | PASS (B2/B3) |
| 2. Second message continues session | thread_id-keyed _sessions + history build | `pytest -k support` | 107 passed, session continuity tests | PASS |
| 3. Restart rebuilds from thread history | _seed_conversation_if_needed (limit 20) | `pytest -k support` | passed, stateless rebuild verified | PASS |
| 4. Allowlist blocks admin tools | agent.py allowlist branch support_turn | `pytest -k support` | 107 passed, allowlist tests | PASS |
| 5. Hivemind search cites jump URLs | tools_support.py build_hivemind_url + _format_message_result | `pytest -k support` + spot-check | 107 passed | PASS |
| 6. VibeComfy edit returns validated JSON | comfy_tools.py describe/validate/edit/deliver | `pytest -k support` | 107 passed, staged edits | PASS |
| Persistence (guidance_version) | ADD COLUMN IF NOT EXISTS migration + hash | `pytest -k support` B1 | 7/7 B1 passed, 96→107 | PASS |
| Async safety | to_thread wraps | `pytest -k support` B2 | 11/11 B2 passed | PASS |
| Fence-aware chunking | split_message in_fence tracking | `pytest -k support` B2 | fences intact, 2000 cap | PASS |
| Full suite zero regressions | full pytest | `pytest -q` | 8 failed / 1309 passed (clean-base 8/1207, zero new) | PASS |

**North Star disposition:** Advances evidence-over-vibes, concreteness, thread=session; avoids admin-tool exposure, speculative layers. No hollow successes.

**Stop condition:** `completed` — all done criteria met with evidence, no blocked/failed/undetermined. Vigorous testing complete per user request.
