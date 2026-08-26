# Finding: Chunking Edge — Paragraph-Aware Splitting

## Ranked findings (verified)
1. **Paragraph-aware splitter, no fence awareness.** `src/common/discord_utils.py:14-50` prefers `\n\n` → `\n` → hard slice. No ` ``` ` tracking — a 2.5k-char block splits mid-fence, leaving orphan open/close fences.
2. **Support delivery uses it naively.** `src/features/support/support_cog.py:320-325` expands `result.replies` via `split_message` and `thread.send(chunk, view=…)` per chunk; last chunk carries `OutcomeView`. Mid-block split = broken rendering, no fence repair.
3. **Topic editor parallel same gap.** `src/features/summarising/topic_editor.py:8457-8511` (`chunk_text_for_discord`) identical strategy, also fence-unaware.
4. **Staged workflow JSON avoids chunking when via file.** `src/features/support/comfy_tools.py:319-441` `mode=deliver` posts JSON as `.json` attachment via `_post_workflow_file` (`discord.File`), not text chunk. Inline ` ```json` in reply text still hits `split_message`.
5. **Discord 2000 cap at `limit=2000`** (`discord_utils.py:14`); `tests/test_support_cog.py:694-711` asserts `len(part)<=2000` and paragraph-aligned breaks.

## Unknowns
- Does Ox Alpha ever emit workflow JSON inline vs exclusively via `mode=deliver`?
- Prod max reply length; frequency of code block + long text co-occurrence.

## Risks
- Broken fences → garbled view, copy-paste fails (violates concreteness / evidence readability).
- `OutcomeView` attaches to orphan fence chunk, confusing UX.

## Suggested approach (bounded)
- **Fence-aware `split_message`:** track `in_fence` toggling on ` ``` ` lines, avoid splitting inside; if forced, close with ` ``` ` and reopen next chunk. Keep fence lines atomic.
- **Guardrail:** guidance + tool desc: forbid inline workflow JSON >200 chars; require `deliver` file path. Fallback: detect large ` ```json` block in reply, promote to file.
- Keep `2000` limit and paragraph preference; add regression test for split inside ``` block.
