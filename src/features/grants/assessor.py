"""LLM-based grant application assessment."""

import json
import logging
import os
from typing import Optional

from src.features.grants.pricing import GPU_RATES, calculate_grant_cost, max_grant_usd
from src.common.soul import BOT_VOICE
from src.common.llm import get_llm_response

logger = logging.getLogger('DiscordBot')

DEFAULT_GRANTS_LLM_CLIENT = 'openrouter'
DEFAULT_GRANTS_LLM_MODEL = 'meta/muse-spark-1.3-contributor'
DEFAULT_GRANTS_LLM_MAX_TOKENS = 1024
MIN_GRANTS_LLM_MAX_TOKENS = 512


def _render_prompt_template(template: str, community_name: str) -> str:
    return template.replace("our server", f"the {community_name} server")


def _load_prompt(server_config, guild_id: Optional[int], content_key: str, fallback: str) -> str:
    prompt = None
    community_name = "community"
    if server_config and guild_id:
        server = server_config.get_server(guild_id)
        if server:
            community_name = server.get('community_name') or community_name
        prompt = server_config.get_content(guild_id, content_key)
    return _render_prompt_template(prompt or fallback, community_name)


def _fill_prompt_template(prompt: str, gpu_info: str) -> str:
    return (
        prompt
        .replace('{bot_voice}', BOT_VOICE)
        .replace('{gpu_info}', gpu_info)
        .replace('{max_grant_usd:.0f}', f'{max_grant_usd():.0f}')
        .replace('{{', '{')
        .replace('}}', '}')
    )


def _grants_llm_config() -> tuple[str, str]:
    """Resolve the LLM provider used for grant reviews.

    GRANTS_LLM_CLIENT: one of openrouter | claude | deepseek | openai | gemini
    (default openrouter).
    GRANTS_LLM_MODEL: model name; defaults per client when unset.

    Routing through the central dispatcher (src.common.llm.get_llm_response)
    means grants keep working if a provider's key lapses — flip the env vars
    instead of editing code.
    """
    # Deployment templates commonly define optional variables as blank. Treat
    # blank/whitespace-only values as absent rather than producing an invalid
    # dispatcher client or pairing OpenRouter with Claude's default model.
    client = (os.getenv('GRANTS_LLM_CLIENT') or '').strip().lower() or DEFAULT_GRANTS_LLM_CLIENT
    model = (os.getenv('GRANTS_LLM_MODEL') or '').strip()
    if not model:
        model = {
            'openrouter': DEFAULT_GRANTS_LLM_MODEL,
            'claude': 'claude-sonnet-4-5-20250929',
            'deepseek': 'deepseek-v4-flash',
            'openai': 'gpt-4o-mini',
            'gemini': 'gemini-2.0-flash',
        }.get(client, DEFAULT_GRANTS_LLM_MODEL)
    return client, model


def first_timer_max_hours() -> int:
    """Hour cap for first-time applicants ('start small' rule). Env-tunable."""
    try:
        return max(int(os.getenv('GRANTS_FIRST_TIMER_MAX_HOURS', '10')), 1)
    except ValueError:
        return 10


def grants_llm_max_tokens() -> int:
    """Completion budget for grant-review calls, with a safe practical floor."""
    try:
        configured = (os.getenv('GRANTS_LLM_MAX_TOKENS') or '').strip()
        value = int(configured or DEFAULT_GRANTS_LLM_MAX_TOKENS)
        return value if value >= MIN_GRANTS_LLM_MAX_TOKENS else DEFAULT_GRANTS_LLM_MAX_TOKENS
    except ValueError:
        return DEFAULT_GRANTS_LLM_MAX_TOKENS


def min_application_chars() -> int:
    """Minimum readable body length before an application can be reviewed."""
    try:
        return max(int(os.getenv('GRANTS_MIN_APPLICATION_CHARS', '120')), 20)
    except ValueError:
        return 120


def is_application_content_sufficient(thread_name: str, body: str) -> tuple[bool, str]:
    """Gate: is there enough real application text to adjudicate?

    A title alone (or an empty/near-empty body — e.g. a writeup pasted as a
    screenshot) is not an application. The reviewer LLM must never be asked to
    infer a project from the thread title; it fabricates when it does.

    Returns (ok, reason). When not ok, the caller should send ``needs_info``
    WITHOUT calling the LLM.
    """
    body_text = (body or '').strip()
    minimum = min_application_chars()
    if len(body_text) < minimum:
        return False, (
            f"Your application body is missing or too short to review "
            f"({len(body_text)}/{minimum} characters). Please paste your full "
            "writeup as plain text in this thread — screenshots alone can't be "
            "reviewed. Include: what the project does, what the GPU hours are "
            "for, and links to prior work (repos, demos, papers)."
        )
    return True, ''

SYSTEM_PROMPT = """You are a grant reviewer for compute micro-grants (10-50 GPU hours) for open-source AI projects.

You review applications and decide whether to approve, reject, or request more information.

{bot_voice}

## Required Application Info — these are HARD requirements
- Project description: what the project does
- Compute purpose: what the GPU hours will be used for (training, fine-tuning, inference, etc.)
- Links to prior work: GitHub repos, papers, demos, or other evidence of capability. A GitHub *profile page* is not a prior-work link — a specific repo, paper, gist, or demo URL is.

An application missing any of these MUST get "needs_info" asking for the missing pieces — never "approved". A title alone is not an application: never infer project content from the thread title, and never write the application the applicant failed to write. If the body text is empty or near-empty, return "needs_info".

## Approval Criteria
- Evidence over assertion: benchmark numbers, "it works", speedups, or trained models are CLAIMS, not evidence, unless a linked artifact (repo, gist, benchmark log, demo, paper) supports them. Ignore unverifiable claims — do not treat them as demonstrated. Self-reported results without a linked artifact cannot support an approval.
- Project must be open-source (or commit to open-sourcing results)
- Reasonable scope: 10-50 GPU hours should meaningfully advance the project
- First-time applicants (no paid grant in their history) START SMALL: recommended_hours is capped at 10. Default to 10, not a middle value. If a first-timer's request genuinely needs more than the cap, return "needs_review" (with your recommendation) instead of approving above it — a human admin decides.
- Merit and reputation: evaluate their public contributions, previous work in the space, and ability to clearly articulate training goals. A new or anonymous account with no verifiable prior work and no linked evidence is a "needs_info" or "needs_review" case, never an auto-approval.
- Community benefit: project serves the broader AI/ML community

## Available GPU Types and Rates
{gpu_info}

## Budget Cap
The maximum grant is capped at ${max_grant_usd:.0f} USD (equivalent to 50 hours of H100).
The applicant may request a specific GPU type or hours — honour their preference if reasonable, but the total cost must not exceed the cap.
If they don't specify, choose based on project needs.

## Prior Grant History
If the applicant has received grants before, this will be noted below the application.
Be VERY hesitant to approve someone who already has an open/active grant (status: reviewing, awaiting_wallet, payment_requested).
For applicants with past paid grants, apply higher scrutiny — they should demonstrate clear results from previous grants before receiving more.

## Discord Engagement
The applicant's Discord activity will be provided below the application. This shows their total message count in our server and their most recent substantive messages. Low engagement does not automatically disqualify, but it RAISES scrutiny and must not be offset by missing evidence. Message count is not demonstrated community standing or merit — especially when the messages are inside the applicant's own application thread.

## Manual Review Triggers (needs_review)
Use "needs_review" to flag a human admin when: the applicant is new/unknown and requesting more than the first-timer cap, claims are consequential but unverifiable, the request is unusual, or you would hesitate to auto-approve with real money. A "needs_review" verdict with a recommendation is a safe default; auto-approval is not.

## Response Format
Return ONLY valid JSON (no markdown, no code fences) with these exact fields:

{{"reasoning": "your internal analysis of the application (2-4 sentences — project viability, applicant capability, scope assessment)", "decision": "approved" | "rejected" | "needs_info" | "needs_review" | "spam", "response": "message to show the applicant (2-4 sentences — friendly, constructive)", "gpu_type": "H100_80GB" | "H200" | "B200" | null, "recommended_hours": <number 10-50 or null>}}

- "reasoning": your private assessment rationale (stored in DB, not shown to applicant)
- "decision": one of "approved", "rejected", "needs_info", "needs_review", "spam"
- "response": the public-facing message shown to the applicant (not used for spam — thread is deleted)
- "gpu_type": required for "approved", null otherwise. For "needs_review", include your recommended gpu_type and hours if you would approve.
- "recommended_hours": required for "approved" (10-50), null otherwise. For "needs_review", include your recommendation if you would approve.

Use "needs_review" when you're unsure — e.g. borderline applications, unusual requests, or cases where you'd want a human to make the final call. An admin will be tagged to review.

Use "spam" for posts that are clearly not real applications — e.g. test posts, gibberish, jokes, off-topic messages, or obvious low-effort spam. These threads will be silently deleted."""


def _build_system_prompt(server_config=None, guild_id: Optional[int] = None) -> str:
    gpu_info = '\n'.join(f'- {name}: ${rate:.2f}/hr' for name, rate in GPU_RATES.items())
    prompt = _load_prompt(server_config, guild_id, 'prompt_grants_assessor_system', SYSTEM_PROMPT)
    return _fill_prompt_template(prompt, gpu_info)


def _parse_json(text: str) -> dict:
    """Extract JSON from LLM response, stripping markdown fences if present."""
    cleaned = text.strip()
    if cleaned.startswith('```'):
        cleaned = cleaned.split('\n', 1)[1] if '\n' in cleaned else cleaned[3:]
        if cleaned.endswith('```'):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
    return json.loads(cleaned)


def _validate(result: dict, first_grant: bool = False) -> str | None:
    """Validate assessment structure. Returns error string or None if valid.

    ``first_grant`` (no paid grants in the applicant's history) enforces the
    'start small' rule mechanically: approved hours are capped at
    first_timer_max_hours(). The LLM loop feeds the error back so the agent
    adjusts — the agent still makes the decision, within the bound.
    """
    required = ['reasoning', 'decision', 'response']
    for field in required:
        if field not in result or not isinstance(result[field], str) or not result[field].strip():
            return f"Missing or empty required field: '{field}'"

    if result['decision'] not in ('approved', 'rejected', 'needs_info', 'needs_review', 'spam'):
        return f"Invalid decision: '{result['decision']}'. Must be 'approved', 'rejected', 'needs_info', 'needs_review', or 'spam'"

    if result['decision'] == 'approved':
        if not result.get('gpu_type') or result['gpu_type'] not in GPU_RATES:
            return f"Invalid gpu_type: '{result.get('gpu_type')}'. Must be one of {list(GPU_RATES.keys())}"
        hours = result.get('recommended_hours')
        if not hours or not isinstance(hours, (int, float)) or not (10 <= hours <= 50):
            return f"Invalid recommended_hours: {hours}. Must be a number between 10 and 50"
        if first_grant and hours > first_timer_max_hours():
            return (
                f"First-time applicants (no paid grant history) are capped at "
                f"{first_timer_max_hours()} hours (start small). Reduce recommended_hours "
                f"to at most {first_timer_max_hours()}, or return decision 'needs_review' "
                f"with your recommendation if the project genuinely needs more."
            )
        cost = calculate_grant_cost(result['gpu_type'], hours)
        cap = max_grant_usd()
        if cost > cap:
            return f"Grant cost ${cost:.2f} exceeds max ${cap:.2f}. Reduce hours or pick a cheaper GPU"

    return None


ADMIN_REVIEW_PROMPT = """You are processing an admin's decision on a grant application that was flagged for manual review.

The admin has replied in the grant thread. Interpret their message and return a final decision.

{bot_voice}

## Available GPU Types and Rates
{gpu_info}

## Fee Structure
All grants include a 10% fee buffer. So 20hrs of H100 = 20 × $2.50 × 1.1 = $55.00.

## Budget Cap
Maximum grant: ${max_grant_usd:.0f} USD.

## How to interpret the admin's message
- If the admin approves (e.g. "looks good", "approve", "yes", "give them $50"), return decision "approved" with appropriate gpu_type and recommended_hours.
- If the admin specifies a dollar amount (e.g. "$50", "give them 50 bucks"), pick the cheapest GPU and calculate the hours that fit within that budget (including the 10% fee). Round hours to nearest whole number.
- If the admin specifies GPU and/or hours, use those.
- If the admin approves without specifics, use the original LLM recommendation if one was provided. For first-time applicants that recommendation is capped at 10 hours — honour it unless the admin explicitly approves more.
- If the admin rejects (e.g. "no", "reject", "not enough detail"), return decision "rejected".
- If the admin asks a question or their intent is unclear, return decision "needs_review" — this keeps the thread open for further discussion.
- The "response" field is the message shown to the applicant. For approvals, be congratulatory. For rejections, be constructive. For needs_review, explain that the review is still in progress.

## Response Format
Return ONLY valid JSON (no markdown, no code fences):

{{"reasoning": "your interpretation of the admin's intent", "decision": "approved" | "rejected" | "needs_review", "response": "message to show the applicant", "gpu_type": "H100_80GB" | "H200" | "B200" | null, "recommended_hours": <number or null>}}"""


def _build_admin_review_prompt(server_config=None, guild_id: Optional[int] = None) -> str:
    gpu_info = '\n'.join(f'- {name}: ${rate:.2f}/hr' for name, rate in GPU_RATES.items())
    prompt = _load_prompt(server_config, guild_id, 'prompt_grants_admin_review_system', ADMIN_REVIEW_PROMPT)
    return _fill_prompt_template(prompt, gpu_info)


async def interpret_admin_decision(thread_content: str, admin_message: str,
                                   llm_recommendation: dict | None = None,
                                   guild_id: Optional[int] = None,
                                   server_config=None) -> dict:
    """Interpret an admin's natural-language reply on a needs_review grant.

    Returns:
        dict with keys: reasoning, decision, response, gpu_type, recommended_hours

    Raises:
        RuntimeError if all attempts fail
    """
    system_prompt = _build_admin_review_prompt(server_config=server_config, guild_id=guild_id)
    client_name, model = _grants_llm_config()

    user_content = f"## Original Application\n\n{thread_content}"

    if llm_recommendation:
        user_content += (
            f"\n\n## Original LLM Recommendation\n"
            f"- Decision: {llm_recommendation.get('decision', 'needs_review')}\n"
            f"- GPU: {llm_recommendation.get('gpu_type', 'not specified')}\n"
            f"- Hours: {llm_recommendation.get('recommended_hours', 'not specified')}\n"
            f"- Reasoning: {llm_recommendation.get('reasoning', 'none')}"
        )

    user_content += f"\n\n## Admin's Reply\n\n{admin_message}"

    messages = [{'role': 'user', 'content': user_content}]

    max_attempts = 3
    last_error = None

    for attempt in range(max_attempts):
        call_kwargs = {'max_tokens': grants_llm_max_tokens(), 'temperature': 0.2}
        if client_name == 'deepseek':
            # Structured output: DeepSeek reasoning can burn the whole token
            # budget and return no final text. Disable it and request JSON mode
            # so the response is deterministic.
            call_kwargs['thinking_enabled'] = False
            call_kwargs['response_format'] = {'type': 'json_object'}
        elif client_name == 'openrouter':
            # Muse Spark requires reasoning to remain enabled on OpenRouter.
            # JSON mode constrains the final answer without disabling it.
            call_kwargs['thinking_enabled'] = True
            call_kwargs['response_format'] = {'type': 'json_object'}
        response_text = None
        try:
            response_text = await get_llm_response(
                client_name,
                model,
                system_prompt=system_prompt,
                messages=messages,
                **call_kwargs,
            )
        except Exception as e:
            msg = str(e)
            if "402" in msg or "Insufficient Balance" in msg or "insufficient" in msg.lower():
                # Billing is out — retrying 3x just spams channel with raw JSON
                # and burns rate limits. Fail fast with a clean, user-facing
                # error the cog can turn into a single admin DM + cooldown.
                logger.error(f"Grants LLM billing exhausted (402) — failing fast, no retry: {e}")
                raise RuntimeError("LLM billing exhausted (402 Insufficient Balance) — grants reviews paused until credits are refilled") from e
            raise
        try:
            result = _parse_json(response_text)
        except json.JSONDecodeError as e:
            last_error = f"Invalid JSON: {e}"
            logger.warning(f"Admin review interpreter attempt {attempt + 1}: {last_error}")
            messages.append({'role': 'assistant', 'content': response_text})
            messages.append({'role': 'user', 'content': f"Your response was not valid JSON. Error: {e}\n\nPlease return ONLY valid JSON."})
            continue

        # Use same validation — but allow needs_review to come back (admin was unclear)
        validation_error = _validate(result)
        if validation_error:
            last_error = validation_error
            logger.warning(f"Admin review interpreter attempt {attempt + 1}: {last_error}")
            messages.append({'role': 'assistant', 'content': response_text})
            messages.append({'role': 'user', 'content': f"Validation error: {validation_error}\n\nPlease fix and return valid JSON."})
            continue

        logger.info(f"Admin review interpretation succeeded on attempt {attempt + 1}: decision={result['decision']}")
        return result

    # After 3 attempts we still have invalid JSON — the admin message was
    # likely vague like "can you try again" with no decision. Instead of
    # raising and showing "I couldn't process that moderator decision" (which
    # looks like a system error), return a graceful needs_review that asks
    # for a clear approved/rejected + hours. This prevents the 15:47→15:48
    # spam loop when pom retries with a non-decision.
    if last_error and "Invalid JSON" in str(last_error):
        logger.warning(f"Admin review fallback to needs_review after 3 Invalid JSON attempts: {last_error}")
        return {
            "reasoning": f"Admin message {admin_message!r} was vague/empty and the model returned invalid JSON after 3 attempts ({last_error}). Ask for a clear decision.",
            "decision": "needs_review",
            "response": "I didn't catch a clear decision in that message. Please reply with something like `Approved 30 hours on H100` or `Rejected because …` and I'll process it.",
            "gpu_type": None,
            "recommended_hours": None,
        }
    raise RuntimeError(f"Admin review interpretation failed after {max_attempts} attempts. Last error: {last_error}")

async def assess_application(thread_content: str, grant_history: list | None = None,
                             engagement: dict | None = None,
                             guild_id: Optional[int] = None,
                             server_config=None) -> dict:
    """Assess a grant application using the configured LLM with structured output and retry.

    Returns:
        dict with keys: reasoning, decision, response, gpu_type, recommended_hours

    Raises:
        RuntimeError if all attempts fail
    """
    system_prompt = _build_system_prompt(server_config=server_config, guild_id=guild_id)
    client_name, model = _grants_llm_config()

    user_content = f'Please review this grant application:\n\n{thread_content}'

    if grant_history:
        history_lines = []
        for g in grant_history:
            line = f"- {g['created_at'][:10]}: {g['status']}"
            if g.get('gpu_type'):
                line += f" | {g['gpu_type']} {g.get('recommended_hours', '?')}hrs"
            if g.get('total_cost_usd'):
                line += f" | ${g['total_cost_usd']}"
            history_lines.append(line)
        user_content += (
            f"\n\n---\n**PRIOR GRANT HISTORY FOR THIS APPLICANT:**\n"
            + '\n'.join(history_lines)
        )

    if engagement:
        total = engagement.get('total_messages', 0)
        recent = engagement.get('recent_messages', [])
        user_content += f"\n\n---\n**DISCORD ENGAGEMENT:**\nTotal messages in server: {total}\n"
        if recent:
            user_content += f"Last {len(recent)} substantive messages (>50 chars):\n"
            for m in recent:
                user_content += f"- [{m['created_at']}] {m['content']}\n"
        else:
            user_content += "No substantive messages found.\n"

    messages = [
        {'role': 'user', 'content': user_content}
    ]

    max_attempts = 3
    last_error = None
    first_grant = not any(g.get('status') == 'paid' for g in (grant_history or []))

    for attempt in range(max_attempts):
        call_kwargs = {'max_tokens': grants_llm_max_tokens(), 'temperature': 0.3}
        if client_name == 'deepseek':
            # Structured output: DeepSeek reasoning can burn the whole token
            # budget and return no final text. Disable it and request JSON mode
            # so the response is deterministic.
            call_kwargs['thinking_enabled'] = False
            call_kwargs['response_format'] = {'type': 'json_object'}
        elif client_name == 'openrouter':
            # Muse Spark requires reasoning to remain enabled on OpenRouter.
            # JSON mode constrains the final answer without disabling it.
            call_kwargs['thinking_enabled'] = True
            call_kwargs['response_format'] = {'type': 'json_object'}
        try:
            response_text = await get_llm_response(
                client_name,
                model,
                system_prompt=system_prompt,
                messages=messages,
                **call_kwargs,
            )
        except Exception as e:
            msg = str(e)
            if "402" in msg or "Insufficient Balance" in msg or "insufficient" in msg.lower():
                logger.error(f"Grants LLM billing exhausted (402) — failing fast, no retry: {e}")
                raise RuntimeError("LLM billing exhausted (402 Insufficient Balance) — grants reviews paused until credits are refilled") from e
            raise
        # Try to parse
        try:
            result = _parse_json(response_text)
        except json.JSONDecodeError as e:
            last_error = f"Invalid JSON: {e}"
            logger.warning(f"Grant assessor attempt {attempt + 1}: {last_error}")
            # Feed error back for retry
            messages.append({'role': 'assistant', 'content': response_text})
            messages.append({'role': 'user', 'content': f"Your response was not valid JSON. Error: {e}\n\nPlease return ONLY valid JSON with the exact fields specified."})
            continue

        # Validate structure
        validation_error = _validate(result, first_grant=first_grant)
        if validation_error:
            last_error = validation_error
            logger.warning(f"Grant assessor attempt {attempt + 1}: {last_error}")
            # Feed error back for retry
            messages.append({'role': 'assistant', 'content': response_text})
            messages.append({'role': 'user', 'content': f"Your response had a validation error: {validation_error}\n\nPlease fix and return valid JSON."})
            continue

        # Success
        logger.info(f"Grant assessment succeeded on attempt {attempt + 1}: decision={result['decision']}")
        return result

    raise RuntimeError(f"Grant assessment failed after {max_attempts} attempts. Last error: {last_error}")
