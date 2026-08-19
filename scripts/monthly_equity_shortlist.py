import argparse
import sys
import os
from datetime import datetime, timedelta
import logging
from typing import List, Dict, Any, Optional
import json
from collections import defaultdict
import asyncio
import traceback
from dotenv import load_dotenv

# Adjust the path to import from the 'src' directory
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
sys.path.insert(0, project_root)

# Load environment variables from .env file
load_dotenv(os.path.join(project_root, '.env'))

from src.common.db_handler import DatabaseHandler
from src.common.llm import get_llm_response

# --- Configuration ---
BATCH_SIZE = 500
DEFAULT_DEV_MODE = False # Set to True if you want to use the dev database by default
LOG_PREVIEW_MESSAGE_COUNT = 3 # How many messages to preview per batch
# --- LLM Configuration ---
LLM1_CLIENT = "openai"
# Using a placeholder 'o3' model name, adjust as needed.
# See src/common/llm/openai_client.py for how 'o' models are handled.
LLM1_MODEL = "o3"
LLM1_SYSTEM_PROMPT = """You are an AI assistant analyzing Discord messages from an open source AI art community. The community allocates monthly 'ownership' to contributors who advance the ecosystem (tools, models, workflows, help, resources).

Your task is to identify potential candidates for this allocation based *solely* on the provided batch of messages. Prioritize individuals who:
1. Create/share tangible, useful open source work (code, models, workflows, guides).
2. Demonstrate significant helpfulness (troubleshooting, detailed explanations).
3. Post messages/content with high positive reactions (indicated by '| N reacts |', focus on >= 2 reacts).
4. Focus on contributions where the person seems to be the primary contributor - as opposed to just sharing someone else's work.

Return your findings ONLY as a valid JSON object. The object should contain a single key "candidates" whose value is a list of JSON objects. Each object in the "candidates" list must have the following keys:
- "handle": The Discord handle/username (string).
- "justification": A concise description justifying their potential eligibility based on criteria observed *in this batch* (string) - don't explicitly mention the number of reactions, just focus on the content and its usefulness.

Example JSON Output:
{
  "candidates": [
    {
      "handle": "some_user_handle",
      "justification": "Shared a link to their new open-source workflow node and explained its usage (high usefulness)."
    },
    {
      "handle": "another_handle",
      "justification": "Provided detailed troubleshooting steps that helped multiple users solve an installation issue (very helpful)."
    },
    {
      "handle": "creative_person",
      "justification": "Posted demonstrations of new techniques that got a lot of response from the community."
    }
  ]
}

Keep a reasonably high-bar. If no candidates are found in this batch, return: {"candidates": []}
Focus only on evidence within this message batch. DO NOT include any text outside the JSON object."""
LLM1_MAX_TOKENS = 99999 # Corresponds to max_completion_tokens for 'o' models
# LLM1_TEMPERATURE = 0.5 # Removed as not supported by model

# LLM2 Configuration
LLM2_CLIENT = "openai"
LLM2_MODEL = "o3" # Can be same or different model
LLM2_SYSTEM_PROMPT = """You are an AI assistant tasked with refining a list of potential community contributors based on justifications gathered from multiple message batches.

You will receive a JSON list of candidates. Each candidate object has a "handle" and a "justification". The "justification" field may contain concatenated text from different sources, separated by '---'.

Your task is to:
1. Review the combined "justification" for each candidate.
2. Rewrite the justification into a single, concise, and coherent paragraph summarizing the key contributions mentioned. Focus on the actions that align with community goals (open source work, helpfulness, high impact/reactions).
3. Return the refined list ONLY as a valid JSON object with the exact same structure as the input: a root object with a single key "candidates", whose value is a list of objects, each having "handle" and "justification" keys.

Example Input Candidate:
{
  "handle": "example_user",
  "justification": "Shared a useful workflow (5 reacts).\n---\nHelped debug a complex issue for another user.\n---\nShared another workflow link."
}

Example Output Candidate (after your refinement):
{
  "handle": "example_user",
  "justification": "Shared multiple useful workflows, one receiving 5 reacts, and provided significant help debugging a complex issue for another user."
}

If the input list is empty, return: {"candidates": []}
DO NOT include any text outside the final JSON object."""
LLM2_MAX_TOKENS = 99999 # Adjust as needed


# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [ShortlistScript] - %(message)s')
logger = logging.getLogger(__name__)

# --- Placeholder LLM Functions ---
# Removed process_batch_with_llm_1 placeholder

# --- Helper Function for Consolidation ---
def _consolidate_candidates(llm1_results: List[Dict]) -> List[Dict]:
    """Consolidates candidate justifications from multiple LLM1 results."""
    consolidated = defaultdict(list)
    logger.info(f"Consolidating candidates from {len(llm1_results)} batch results...")

    for i, result in enumerate(llm1_results):
        if isinstance(result, dict) and "candidates" in result and isinstance(result["candidates"], list):
            candidates = result["candidates"]
            logger.debug(f"Processing {len(candidates)} candidates from batch result {i+1}")
            for cand in candidates:
                if isinstance(cand, dict) and "handle" in cand and "justification" in cand:
                    handle = cand["handle"]
                    justification = cand["justification"]
                    if handle and justification: # Ensure non-empty
                         consolidated[handle].append(justification)
                else:
                    logger.warning(f"Skipping invalid candidate structure in batch result {i+1}: {cand}")
        elif isinstance(result, dict) and ("error" in result or "parsing_error" in result):
             logger.warning(f"Skipping consolidation for batch result {i+1} due to previous error: {result}")
        else:
             logger.warning(f"Skipping unexpected structure in batch result {i+1}: {type(result)}")


    final_list = []
    for handle, justifications in consolidated.items():
        # Join justifications with a separator for LLM2 to process
        merged_justification = "\n---\n".join(justifications)
        final_list.append({"handle": handle, "justification": merged_justification})

    logger.info(f"Consolidated into {len(final_list)} unique candidates.")
    return final_list


# --- LLM 2 Processing Function ---
async def process_candidates_with_llm2(candidates: List[Dict]) -> Dict:
    """Processes the consolidated candidate list with LLM2 for refinement."""
    logger.info(f"--- Starting Final Candidate Refinement (LLM 2) on {len(candidates)} candidates ---")

    if not candidates:
        logger.info("No candidates to process with LLM 2.")
        return {"candidates": []} # Return structure consistent with success

    # Prepare input for LLM2
    # Sending the consolidated list as a JSON string in the user message
    try:
        # Ensure input is serializable
        input_json_str = json.dumps({"candidates": candidates}, indent=2)
    except TypeError as e:
         logger.error(f"Failed to serialize consolidated candidates for LLM2: {e}", exc_info=True)
         return {"error": "Failed to prepare candidates for LLM2 processing"}

    messages_for_llm2 = [{"role": "user", "content": input_json_str}]

    try:
        logger.info(f"Making LLM call to {LLM2_CLIENT} model {LLM2_MODEL} for refinement...")
        if logger.isEnabledFor(logging.DEBUG): # Log input only if debug is enabled
            input_preview = input_json_str[:1000] + ("..." if len(input_json_str) > 1000 else "")
            logger.debug(f"LLM2 Input Preview:\n{input_preview}")

        # Add logging BEFORE the call
        logger.info(f"Calling get_llm_response for LLM2 ({LLM2_CLIENT}/{LLM2_MODEL})...")
        refined_result_text = await get_llm_response(
            client_name=LLM2_CLIENT,
            model=LLM2_MODEL,
            system_prompt=LLM2_SYSTEM_PROMPT,
            messages=messages_for_llm2,
            max_completion_tokens=LLM2_MAX_TOKENS, # Ensure LLM2_MAX_TOKENS is used here
            response_format={"type": "json_object"}, # Request JSON output
            reasoning_effort="high" # Add high reasoning effort
        )
        # Add logging AFTER the call
        logger.info(f"Received response from LLM2 ({LLM2_CLIENT}/{LLM2_MODEL}). Length: {len(refined_result_text)}")

        # --- Add this block to clean the response ---
        cleaned_text = refined_result_text.strip()
        if cleaned_text.startswith("```json\n") and cleaned_text.endswith("\n```"):
            cleaned_text = cleaned_text[len("```json\n"):-len("\n```")]
        elif cleaned_text.startswith("```") and cleaned_text.endswith("```"):
            # Handle cases where it might just be ```...```
            cleaned_text = cleaned_text[3:-3]
        # ---------------------------------------------

        # Attempt to parse the refined result as JSON
        try:
            # Use the cleaned_text variable here
            refined_result = json.loads(cleaned_text)
            # Validate structure
            if not isinstance(refined_result, dict) or "candidates" not in refined_result or not isinstance(refined_result["candidates"], list):
                logger.error(f"LLM2 response parsed as JSON but missing expected structure: {cleaned_text[:200]}...")
                return {"error": "LLM2 response structure invalid", "raw_response": refined_result_text} # Log original raw text

            logger.info(f"LLM 2 refinement successful. Refined {len(refined_result.get('candidates', []))} candidates.")
            return refined_result # Return the parsed, validated JSON

        except json.JSONDecodeError:
            logger.error(f"Failed to parse LLM2 response as JSON. Cleaned text: {cleaned_text[:200]}... Original text: {refined_result_text[:200]}...", exc_info=False)
            return {"error": "LLM2 response was not valid JSON", "raw_response": refined_result_text} # Log original raw text

    except Exception as llm_error:
        logger.error(f"Error during LLM2 call with {LLM2_CLIENT} model {LLM2_MODEL}: {llm_error}", exc_info=True)
        return {"error": f"LLM2 API call failed: {str(llm_error)}"}

# --- Helper Functions --- Added back
def get_month_input(month_arg: str | None) -> str:
    """
    Gets and validates the month input from args or user prompt.
    Defaults to the previous full calendar month if no input is given.
    """
    if month_arg:
        try:
            datetime.strptime(month_arg, '%Y-%m')
            logger.info(f"Using specified month: {month_arg}")
            return month_arg
        except ValueError:
            logger.error("Invalid date format provided via command line. Please use YYYY-MM.")
            sys.exit(1)
    else:
        try:
            month_str = input("Enter the month to process (YYYY-MM) [Default: last full month]: ")
            if not month_str:
                # Calculate previous month
                today = datetime.today()
                first_day_current_month = today.replace(day=1)
                last_day_previous_month = first_day_current_month - timedelta(days=1)
                previous_month_str = last_day_previous_month.strftime('%Y-%m')
                logger.info(f"No month entered, defaulting to last full month: {previous_month_str}")
                return previous_month_str
            else:
                # Validate entered month
                datetime.strptime(month_str, '%Y-%m')
                logger.info(f"Using entered month: {month_str}")
                return month_str
        except ValueError:
            logger.error("Invalid format entered. Please use YYYY-MM format or leave blank for default.")
            sys.exit(1)
        except EOFError:
            logger.error("\nNo input received. Exiting.")
            sys.exit(1)


from refresh_media_urls import get_month_date_range  # noqa: E402


# --- Formatting Helpers ---

def _extract_attachment_urls(raw: Optional[str]) -> Optional[str]:
    """Extract and return a comma-separated string of attachment URLs from raw JSON."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            urls = {item.get("url") for item in data if isinstance(item, dict) and item.get("url")}
            urls.discard(None)
            if urls:
                return ",".join(sorted(urls))
    except Exception:
        pass  # Malformed JSON or unexpected structure -- ignore
    return None


def _build_ancestor_map(messages: List[Dict], msg_map: Dict[int, Dict]) -> Dict[int, int]:
    """Compute the true root ancestor ID for every message, handling cycles.

    Returns a dict mapping each message_id to its ultimate ancestor_id.
    """
    ancestor_map: Dict[int, int] = {}

    def _get_true_ancestor_id(mid: int, visited_path: set[int]) -> int:
        if mid in ancestor_map:
            return ancestor_map[mid]

        # Cycle detection
        if mid in visited_path:
            logger.warning(f"Detected reference cycle during ancestor lookup for message ID: {mid}. Using self as ancestor.")
            ancestor_map[mid] = mid
            return mid

        msg = msg_map.get(mid)
        if not msg:
            ancestor_map[mid] = mid
            return mid

        parent = msg.get("reference_id") or msg.get("thread_id")
        if parent and parent in msg_map:
            visited_path.add(mid)
            true_ancestor = _get_true_ancestor_id(parent, visited_path)
            visited_path.remove(mid)
            ancestor_map[mid] = true_ancestor
            return true_ancestor
        else:
            ancestor_map[mid] = mid
            return mid

    for mid in msg_map:
        if mid not in ancestor_map:
            _get_true_ancestor_id(mid, set())

    return ancestor_map


def _build_depth_map(messages: List[Dict], msg_map: Dict[int, Dict], ancestor_map: Dict[int, int]) -> Dict[int, int]:
    """Compute the nesting depth of every message, handling cycles.

    Returns a dict mapping each message_id to its integer depth (0 = root).
    """
    depth_map: Dict[int, int] = {}

    def _compute_depth(mid: int, visited_path: set[int]) -> int:
        if mid in depth_map:
            return depth_map[mid]

        # Cycle detection
        if mid in visited_path:
            logger.warning(f"Detected reference cycle involving message ID: {mid}. Assigning depth 0.")
            depth_map[mid] = 0
            return 0

        msg = msg_map.get(mid)
        if not msg:
            depth_map[mid] = 0
            return 0

        parent = msg.get("reference_id") or msg.get("thread_id")
        if parent and parent in msg_map:
            visited_path.add(mid)
            parent_depth = _compute_depth(parent, visited_path)
            visited_path.remove(mid)
            depth_map[mid] = parent_depth + 1
        else:
            depth_map[mid] = 0
        return depth_map[mid]

    for mid in msg_map:
        if mid not in depth_map:
            _compute_depth(mid, set())

    return depth_map


def _build_author_cache(messages: List[Dict], db_handler: DatabaseHandler) -> Dict[int, str]:
    """Look up author usernames for all messages.

    Returns a dict mapping author_id to display name (username or stringified id).
    """
    author_ids = {m.get("author_id") for m in messages if m.get("author_id") is not None}
    author_cache: Dict[int, str] = {}
    for aid in author_ids:
        member = db_handler.get_member(aid)
        if member and member.get("username"):
            author_cache[aid] = member["username"]
        else:
            author_cache[aid] = str(aid)
    return author_cache


def format_messages_hierarchical(messages: List[Dict], db_handler: DatabaseHandler) -> List[Dict]:
    """Format messages into an indented, thread-aware structure similar to the SQL example.

    The resulting list contains dictionaries with keys:
        - message_id
        - ancestor_id      (ID of the true root message for ordering)
        - created_at       (original timestamp string)
        - message          (formatted/indented content)
        - author           (username if found, else author_id)
        - reactions        (reaction_count)
        - attachments      (comma-separated list of attachment URLs or None)
    Messages are filtered to roughly match the SQL HAVING clause:
        * replies (reference_id/thread_id not NULL)
        * messages that have replies
        * messages with >=2 reactions
        * messages whose content contains an http link
    They are ordered by ancestor_id then created_at (ASC).
    """

    if not messages:
        return []

    # Build lookup tables ----------------------------------------------------
    msgs_by_id: Dict[int, Dict] = {m["message_id"]: m for m in messages}
    children_map: defaultdict[int, list[int]] = defaultdict(list)
    for m in messages:
        parent_id: Optional[int] = m.get("reference_id") or m.get("thread_id")
        if parent_id:
            children_map[parent_id].append(m["message_id"])

    # Delegate independent lookup-building phases to helpers -----------------
    ancestor_map = _build_ancestor_map(messages, msgs_by_id)
    depth_map = _build_depth_map(messages, msgs_by_id, ancestor_map)
    author_cache = _build_author_cache(messages, db_handler)

    # Has-responses calculation ---------------------------------------------
    has_responses: Dict[int, bool] = {mid: bool(children_map.get(mid)) for mid in msgs_by_id}

    # Build formatted list ---------------------------------------------------
    formatted: List[Dict] = []
    for m in messages:
        mid = m["message_id"]
        depth = depth_map.get(mid, 0)
        indent = " " * (depth * 4)
        if m.get("reference_id") is not None:
            prefix = "↳ "
        elif m.get("thread_id") is not None:
            prefix = "│ "
        else:
            prefix = ""
        content = m.get("content") or ""
        formatted_text = f"{indent}{prefix}{content}"

        include = (
            m.get("reference_id") is not None
            or m.get("thread_id") is not None
            or has_responses.get(mid, False)
            or (m.get("reaction_count", 0) >= 2)
            or ("http" in content.lower())
        )
        if not include:
            continue

        ancestor_id = ancestor_map.get(mid, mid)
        formatted.append(
            {
                "message_id": mid,
                "ancestor_id": ancestor_id,
                "created_at": m.get("created_at"),
                "message": formatted_text,
                "author": author_cache.get(m.get("author_id")),
                "reactions": m.get("reaction_count"),
                "attachments": _extract_attachment_urls(m.get("attachments")),
            }
        )

    # Sort as per SQL ORDER BY true_ancestor_id, created_at ------------------
    formatted.sort(key=lambda x: (x["ancestor_id"], x["created_at"]))
    return formatted

# --- Helper function for saving results ---
def save_results_to_md(file_path: str, data: Any, header: str):
    """Appends formatted data under a header to the specified Markdown file."""
    try:
        # Ensure the directory exists
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        
        with open(file_path, 'a', encoding='utf-8') as f:
            f.write(f"## {header}\n\n")
            if isinstance(data, (dict, list)):
                json_str = json.dumps(data, indent=2)
                f.write(f"```json\n{json_str}\n```\n\n")
            else:
                # Handle raw text or other types
                f.write(f"```\n{str(data)}\n```\n\n")
        logger.info(f"Successfully saved '{header}' results to {file_path}")
    except Exception as e:
        logger.error(f"Failed to save results under header '{header}' to {file_path}: {e}", exc_info=True)

# --- Main Script Logic: Stage Helpers ---

async def _process_batches_llm1(
    batches: List[List[str]],
    llm_config: Dict[str, Any],
    results_file_path: Optional[str],
    args: argparse.Namespace,
) -> List[Dict]:
    """Run LLM1 over each batch and return a list of per-batch result dicts.

    ``llm_config`` must contain the keys ``dev_mode`` (bool) used to gate
    dev-mode logging and batch limits.
    """
    dev_mode = llm_config["dev_mode"]

    llm1_results: List[Dict] = []
    logger.info("Starting LLM1 batch processing...")
    batches_to_process = len(batches)
    if dev_mode:
         # Limit to max 2 batches in dev mode
         batches_to_process = min(len(batches), 2)
         logger.info(f"--- DEV MODE: Limiting processing to first {batches_to_process} batch(es) ---")

    for i, batch in enumerate(batches):
        # --- Dev Mode Batch Limit ---
        # Skip processing if we've hit the dev mode limit
        if dev_mode and i >= batches_to_process:
            logger.info(f"--- DEV MODE: Skipping batch {i+1}/{len(batches)} --- ")
            # Ensure batch_result is defined even when skipping
            batch_result = {"candidates": [], "info": f"Skipped batch {i+1} due to dev mode limit"}
            llm1_results.append(batch_result)
            # Save skipped batch info to results file if enabled
            if results_file_path:
                 save_results_to_md(results_file_path, batch_result, f"LLM1 Batch {i+1} Result (Skipped)")
            continue # Skip this batch

        logger.info(f"--- Processing Batch {i+1}/{len(batches)} --- ('Dev Mode Batch {i+1}' if dev_mode else '')")

        # Log preview of first messages in batch (data sent to LLM 1)
        logger.info(f"Previewing first {min(len(batch), LOG_PREVIEW_MESSAGE_COUNT)} formatted messages sent to LLM 1:")

        for msg_index, line in enumerate(batch[:LOG_PREVIEW_MESSAGE_COUNT]):
            logger.info(f"  Msg {msg_index+1}: {line}")
        if not batch:
            logger.info("  Batch is empty.")
            # Handle empty batch result
            batch_result = {"candidates": [], "info": f"Batch {i+1} was empty"}
            llm1_results.append(batch_result)
             # Save empty batch info to results file if enabled
            if results_file_path:
                 save_results_to_md(results_file_path, batch_result, f"LLM1 Batch {i+1} Result (Empty)")
            continue

        # --- Call LLM 1 ---
        try:
            if args.no_llm1:
                logger.warning(f"--- SKIPPING LLM1 CALL for batch {i+1} due to --no-llm1 flag ---")
                batch_result = {"candidates": [], "info": f"LLM1 skipped for batch {i+1}"}
            else:
                # Format batch content for LLM
                # Sending the whole batch as a single user message content string
                batch_content = "\n".join(batch)
                messages_for_llm = [{"role": "user", "content": batch_content}]

                if dev_mode:
                     content_preview = batch_content[:500] + ("..." if len(batch_content) > 500 else "")
                     logger.info(f"--- DEV MODE: Sending content to LLM (Preview):\n{content_preview}")

                # Make the actual async LLM call
                logger.info(f"Calling get_llm_response for LLM1 ({LLM1_CLIENT}/{LLM1_MODEL}) on batch {i+1}... Size: {len(batch_content)} chars")
                batch_result_text = await get_llm_response(
                    client_name=LLM1_CLIENT,
                    model=LLM1_MODEL,
                    system_prompt=LLM1_SYSTEM_PROMPT,
                    messages=messages_for_llm,
                    # Pass kwargs expected by the client
                    # For 'o' models, openai_client expects 'max_completion_tokens'
                    max_completion_tokens=LLM1_MAX_TOKENS,
                    # Request JSON output if supported by the model/client
                    response_format={"type": "json_object"},
                    reasoning_effort="high" # Add high reasoning effort
                )
                logger.info(f"Received response from LLM1 ({LLM1_CLIENT}/{LLM1_MODEL}) for batch {i+1}. Length: {len(batch_result_text)}")

                # --- Add this block to clean the response ---
                cleaned_text = batch_result_text.strip()
                if cleaned_text.startswith("```json\n") and cleaned_text.endswith("\n```"):
                    cleaned_text = cleaned_text[len("```json\n"):-len("\n```")]
                elif cleaned_text.startswith("```") and cleaned_text.endswith("```"):
                    # Handle cases where it might just be ```...```
                    cleaned_text = cleaned_text[3:-3]
                # ---------------------------------------------

                # Attempt to parse the result as JSON
                try:
                    # Use the cleaned_text variable here
                    batch_result = json.loads(cleaned_text)
                    logger.info(f"Successfully parsed JSON response for batch {i+1}.")
                    # Optional: Validate structure further if needed
                    if not isinstance(batch_result, dict) or "candidates" not in batch_result or not isinstance(batch_result["candidates"], list):
                        logger.warning(f"LLM response parsed as JSON but missing expected structure (batch {i+1}): {cleaned_text[:200]}...")
                        # Store raw text if structure is wrong, but indicate issue
                        batch_result = {"parsing_error": "JSON structure invalid", "raw_response": batch_result_text} # Log original raw text
                    else:
                        logger.info(f"LLM 1 processing for batch {i+1} successful (JSON Parsed).")

                except json.JSONDecodeError:
                    logger.error(f"Failed to parse LLM response as JSON (batch {i+1}). Cleaned text: {cleaned_text[:200]}... Original text: {batch_result_text[:200]}...", exc_info=False)
                    # Store raw text if JSON parsing fails
                    batch_result = {"parsing_error": "Invalid JSON", "raw_response": batch_result_text} # Log original raw text

            # Optional: Log a snippet of the result (now potentially a dict)
            if isinstance(batch_result, dict) and "candidates" in batch_result:
                summary_preview = f"Found {len(batch_result['candidates'])} potential candidates."
            elif isinstance(batch_result, dict) and "raw_response" in batch_result:
                summary_preview = f"Parsing Error. Raw: {batch_result['raw_response'][:70]}..."
            else:
                 # Fallback if it's somehow not a dict after parsing logic
                 summary_preview = str(batch_result)[:100] + "..."

            logger.debug(f"LLM 1 Result Preview (Batch {i+1}): {summary_preview}")
            if dev_mode:
                # Log the potentially structured result
                logger.info(f"--- DEV MODE: Received result from LLM (Parsed/Raw):\n{json.dumps(batch_result, indent=2) if isinstance(batch_result, dict) else batch_result}")

        except Exception as llm_error:
            logger.error(f"Error processing batch {i+1} with {LLM1_CLIENT} model {LLM1_MODEL}: {llm_error}", exc_info=True)
            # Decide how to handle failed batches: skip, retry, use dummy data?
            # Here, we'll store an error indicator.
            batch_result = {"error": f"Failed to process batch {i+1}: {str(llm_error)}"}

        # Save batch result to file if enabled
        if results_file_path:
             save_results_to_md(results_file_path, batch_result, f"LLM1 Batch {i+1} Result")

        llm1_results.append(batch_result)

    return llm1_results


async def _consolidate_and_run_llm2(
    llm1_results: List[Dict],
    llm_config: Dict[str, Any],
    results_file_path: Optional[str],
    args: argparse.Namespace,
) -> Dict:
    """Consolidate LLM1 results and optionally refine via LLM2.

    Returns the final result dict (with a ``candidates`` key).
    """
    # Consolidate results from LLM1
    logger.info("Consolidating results from LLM1...")
    consolidated_candidates = _consolidate_candidates(llm1_results)
    logger.info(f"Consolidation complete. {len(consolidated_candidates)} unique candidates found.")

    # Save consolidated list to file if enabled
    if results_file_path:
        save_results_to_md(results_file_path, consolidated_candidates, "Consolidated LLM1 Candidates")

    # Process consolidated results with the second LLM
    # The result from LLM2 is now our final intended output
    if args.no_llm2:
        logger.warning("--- SKIPPING LLM2 CALL due to --no-llm2 flag ---")
        # Use the consolidated list directly as the final result, but maintain structure
        final_result = {"candidates": consolidated_candidates, "info": "LLM2 skipped"}
    else:
        logger.info("Processing consolidated candidates with LLM2...")
        final_result = await process_candidates_with_llm2(consolidated_candidates)
        logger.info("LLM2 processing complete.")

    # Save final result to file if enabled
    if results_file_path:
        save_results_to_md(results_file_path, final_result, "Final Result (after LLM2)")

    return final_result


# --- Main Orchestrator ---

async def main():
    parser = argparse.ArgumentParser(description="Fetch messages for a specific month, process them in batches with LLMs.")
    parser.add_argument("-m", "--month", help="The month to process in YYYY-MM format.", type=str)
    parser.add_argument("--dev", action='store_true', help="Use the development database.")
    parser.add_argument("--no-llm1", action='store_true', help="Skip LLM1 processing (for debugging).")
    parser.add_argument("--no-llm2", action='store_true', help="Skip LLM2 processing (for debugging).")
    # Add the save-results flag (now defaults to True)
    parser.add_argument("--no-save-results", action='store_true', help="Skip saving intermediate and final results to a Markdown file.")
    args = parser.parse_args()

    dev_mode = args.dev or DEFAULT_DEV_MODE
    year_month = get_month_input(args.month)
    start_date, end_date = get_month_date_range(year_month)

    # Save results by default, unless --no-save-results is specified
    save_results = not args.no_save_results

    logger.info(f"Processing messages for {year_month} (from {start_date} to {end_date})")
    logger.info(f"Using {'development' if dev_mode else 'production'} database.")
    if args.no_llm1: logger.warning("LLM1 processing will be SKIPPED.")
    if args.no_llm2: logger.warning("LLM2 processing will be SKIPPED.")
    if save_results: logger.info("Results will be saved to Markdown file (use --no-save-results to disable).")
    else: logger.info("--no-save-results flag detected. Results will NOT be saved.")
    if dev_mode:
        logger.info("--- DEVELOPMENT MODE ACTIVE ---")

    # --- Prepare results file (enabled by default) ---
    results_file_path = None
    if save_results:
        results_dir = os.path.join(project_root, 'results')
        results_file_path = os.path.join(results_dir, f"{year_month}_shortlist_results.md")
        try:
            # Ensure directory exists
            os.makedirs(results_dir, exist_ok=True)
            # Create/clear the file and write the main header
            with open(results_file_path, 'w', encoding='utf-8') as f:
                f.write(f"# Monthly Equity Shortlist Results - {year_month}\n\n")
            logger.info(f"Initialized results file: {results_file_path}")
        except Exception as e:
             logger.error(f"Failed to initialize results file {results_file_path}: {e}", exc_info=True)
             results_file_path = None # Disable saving if initialization fails
    # ------------------------------------------

    # Config dict shared with stage helpers
    llm_config: Dict[str, Any] = {"dev_mode": dev_mode}

    try:
        # Initialize database handler
        db_handler = DatabaseHandler(dev_mode=dev_mode)
        logger.info("Connected to Supabase database")

        # Fetch messages for the specified month
        logger.info("Fetching messages from the database...")
        raw_messages = db_handler.get_messages_in_range(start_date, end_date)
        logger.info(f"Fetched {len(raw_messages)} messages for {year_month}.")

        if not raw_messages:
            logger.info("No messages found for the specified month. Exiting.")
            # Save info to results file if enabled
            if results_file_path:
                 save_results_to_md(results_file_path, "No messages found for the specified month.", "Status")
            print(json.dumps({"candidates": [], "info": "No messages found for period"})) # Print empty result and exit
            return

        # Format messages hierarchically (indentation, prefixes, etc.)
        logger.info("Formatting messages hierarchically...")
        formatted_records = format_messages_hierarchical(raw_messages, db_handler)
        logger.info(f"After filtering & formatting, {len(formatted_records)} messages remain.")

        # Convert each record to a single string line resembling the SQL output
        logger.info("Converting records to formatted lines...")
        formatted_lines: List[str] = []
        for rec in formatted_records:
            time_str = rec["created_at"]
            attachments_str = rec["attachments"] or ""
            # Format reaction count string
            count = rec['reactions']
            react_str = f"{count} react" + ("s" if count != 1 else "")
            # Put author first
            line = f"{rec['author']}: {rec['message']} | {time_str} | {react_str} | {attachments_str}"
            formatted_lines.append(line)

        # Batch formatted lines
        batches = [formatted_lines[i:i + BATCH_SIZE] for i in range(0, len(formatted_lines), BATCH_SIZE)]
        logger.info(f"Split formatted lines into {len(batches)} batches of up to {BATCH_SIZE} lines each.")

        # Stage 1: LLM1 batch processing
        llm1_results = await _process_batches_llm1(batches, llm_config, results_file_path, args)

        # Stage 2: Consolidation + LLM2 refinement
        final_result = await _consolidate_and_run_llm2(llm1_results, llm_config, results_file_path, args)

        # Output the final result (which is the refined list from LLM2 or an error dict)
        logger.info("--- Final Result --- Preparing to print JSON output.")
        # Pretty print the final JSON result
        final_json_output = json.dumps(final_result, indent=2)
        logger.info(f"Final JSON output generated (length: {len(final_json_output)}). Printing to stdout...")
        print(final_json_output)
        logger.info("Script finished successfully.")

    except Exception as e:
        logger.error(f"An error occurred in main: {e}", exc_info=True) # Log traceback
        # Save error to results file if enabled
        if results_file_path:
            save_results_to_md(results_file_path, f"Script failed: {str(e)}\n{traceback.format_exc()}", "Script Error")
        # Attempt to print an error JSON to stdout so the bot can report it
        error_output = json.dumps({"error": f"Script failed: {str(e)}", "candidates": []})
        print(error_output)
        sys.exit(1)
    finally:
        if 'db_handler' in locals() and db_handler:
            logger.info("Closing database connection.")
            db_handler.close()

if __name__ == "__main__":
    asyncio.run(main())