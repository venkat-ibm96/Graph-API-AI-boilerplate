"""
validation_agent.py
-------------------
Core agent loop powered by Groq (openai/gpt-oss-120b).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from dotenv import load_dotenv
from groq import Groq

from validation_tool import TOOL_FUNCTIONS, TOOL_SCHEMAS, MASTER_PATH, WINRM_USER

load_dotenv()
logger = logging.getLogger(__name__)
TOOL_CALL_DELAY_SECONDS: int = int(os.environ.get("TOOL_CALL_DELAY", "6"))

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GROQ_API_KEY: str = os.environ["GROQ_API_KEY"]
GPT_MODEL:    str = os.environ.get("GPT_MODEL", "openai/gpt-oss-120b")

PROMPTS_DIR  : Path = Path(__file__).parent / "prompts" / "Validation Prompt"

_groq_client: Groq = Groq(api_key=GROQ_API_KEY)

SYSTEM_PROMPT: str = (
    "You are a Patch Validation Agent for Lyric application servers.\n\n"
    "TOOL EXECUTION ORDER — follow exactly:\n"
    "  Step 1: get_server_boot_time()\n"
    "          Single batch call. Fetches all Completed Lyric servers from Excel\n"
    "          and retrieves boot times via WinRM. Call once per run.\n"
    "  Step 2: update_boot_time_in_excel(servers=[...all results...])\n"
    "          Single batch call. Pass ALL results from Step 1 at once.\n"
    "  Step 3: validate_boot_within_patch_window(server_name)\n"
    "          Call once per server — INCLUDING servers where WinRM failed.\n"
    "          NEVER skip this step. Every server must be validated.\n\n"
    "RULES:\n"
    "  - Never guess or invent boot times or validation status.\n"
    "  - Never skip a server — every server must have a recorded outcome.\n"
    "  - WinRM failure → pass boot_time=null, error='Could not connect to server' to Step 2,\n"
    "    then STILL call validate_boot_within_patch_window for that server in Step 3.\n"
    "  - Only write to 'Boot Time' and 'Application Team Validation Status' columns.\n"
    "  - Scope: Lyric servers only. Read-only WinRM — never modify servers.\n"
    "  - Validation status values: 'Successful', 'Failed', or 'Unknown' only.\n\n"
    "Be concise and professional. Report outcomes only — no narration of steps."
)

# ---------------------------------------------------------------------------
# Predefined prompts
# ---------------------------------------------------------------------------

PREDEFINED_PROMPTS: dict[str, str] = {
    "full_validation": (
        "Get all lyric servers from Excel, connect to each one via WinRM to fetch "
        "the boot time, save it to Excel, then validate if it's within the patch window "
        "and update the Application Team Validation Status for every server."
    ),
    "boot_times_only": (
        "Get all lyric servers from Excel and fetch the boot time for each server "
        "via WinRM. Save each boot time to the master Excel."
    ),
    "validate_only": (
        "For all lyric servers in the master Excel that already have a Boot Time recorded, "
        "validate whether the boot time is within the patch window and update the "
        "Application Team Validation Status column."
    ),
}

# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------

def _load_prompt(filename: str) -> str:
    path = PROMPTS_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    return path.read_text(encoding="utf-8").strip()


def load_prompts() -> dict[str, str]:
    return {
        "system":    _load_prompt("system_prompt"),
        "developer": _load_prompt("developer_prompt"),
        "response":  _load_prompt("response_prompt"),
    }


_PROMPTS: dict[str, str] = load_prompts()


def _build_system_message() -> str:
    return "\n\n---\n\n".join([
        _PROMPTS["system"],
        _PROMPTS["developer"],
        _PROMPTS["response"],
    ])

# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

def _dispatch_tool_call(tool_name: str, tool_args: dict) -> str:
    func = TOOL_FUNCTIONS.get(tool_name)

    if func is None:
        return json.dumps({"error": f"Unknown tool: '{tool_name}'"})

    try:
        logger.info("[Tool] %s(%s)", tool_name, tool_args)
        result = func(**tool_args)
        logger.info("[Result] %s", result[:200])
        return result
    except Exception as exc:
        logger.error("Tool error: %s", exc)
        return json.dumps({"error": str(exc)})

# ---------------------------------------------------------------------------
# Streaming helper
# ---------------------------------------------------------------------------

def _stream_final_response(stream) -> str:
    full_text = ""
    for chunk in stream:
        delta = chunk.choices[0].delta
        token = delta.content or ""
        if token:
            print(token, end="", flush=True)
            full_text += token
    print()
    return full_text

# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

def run_agent(user_query: str, stream: bool = True) -> str:
    logger.info("User: %s", user_query)

    messages: list[dict] = [
        {"role": "system", "content": _build_system_message()},
        {"role": "user",   "content": user_query},
    ]
    while True:
        time.sleep(6)

        response = _groq_client.chat.completions.create(
            model=GPT_MODEL,
            messages=messages,
            tools=TOOL_SCHEMAS,
            tool_choice="auto",
            temperature=1,
            max_completion_tokens=8192,
            top_p=1,
            reasoning_effort="medium",
            stream=False,
        )

        message = response.choices[0].message
        tool_calls = message.tool_calls or []

        if not tool_calls:
            break

        # Append assistant message
        messages.append({
            "role": "assistant",
            "content": message.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in tool_calls
            ],
        })

        # Execute tool calls
        for tc in tool_calls:
            try:
                arg_str = tc.function.arguments or "{}"
                arg_str = arg_str.strip() if isinstance(arg_str, str) else "{}"
                args = json.loads(arg_str)
                if not isinstance(args, dict):
                    args = {}
            except json.JSONDecodeError:
                logger.warning("Bad JSON args for %s", tc.function.name)
                args = {}

            result = _dispatch_tool_call(tc.function.name, args)

            # Try parsing batch response
            try:
                parsed = json.loads(result)
            except:
                parsed = {}

            # Batch handling for get_server_boot_time
            messages.append({
            "role": "tool",
            "tool_call_id": tc.id,
            "content": result,
            })

            # Log batch size if applicable
            if (
                tc.function.name == "get_server_boot_time"
                and isinstance(parsed, dict)
                and isinstance(parsed.get("results"), list)
            ):
                logger.info("Boot time batch returned %d results", len(parsed["results"]))


            time.sleep(TOOL_CALL_DELAY_SECONDS)

    # Final response
    if stream:
        final_stream = _groq_client.chat.completions.create(
            model=GPT_MODEL,
            messages=messages,
            tools=TOOL_SCHEMAS,
            tool_choice="auto",
            temperature=1,
            max_completion_tokens=8192,
            top_p=1,
            reasoning_effort="medium",
            stream=True,
        )
        return _stream_final_response(final_stream)

    final_response = _groq_client.chat.completions.create(
        model=GPT_MODEL,
        messages=messages,
        tools=TOOL_SCHEMAS,
        tool_choice="auto",
        temperature=1,
        max_completion_tokens=8192,
        top_p=1,
        reasoning_effort="medium",
        stream=False,
    )

    return final_response.choices[0].message.content or ""


def run_predefined(prompt_key: str, stream: bool = True) -> str:
    """
    Run a predefined query by its registry key.

    Args:
        prompt_key: Key from PREDEFINED_PROMPTS (e.g. 'full_validation').
        stream:     Whether to stream the final response.

    Returns:
        The agent's final text response.

    Raises:
        ValueError: If the key does not exist in PREDEFINED_PROMPTS.
    """
    if prompt_key not in PREDEFINED_PROMPTS:
        available = list(PREDEFINED_PROMPTS.keys())
        raise ValueError(f"Unknown prompt key '{prompt_key}'. Available: {available}")

    return run_agent(PREDEFINED_PROMPTS[prompt_key], stream=stream)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 55)
    print(" Patch Validation Agent")
    print("=" * 55)
    print(f"  Master Excel : {MASTER_PATH}")
    print(f"  WinRM user   : {WINRM_USER}")
    print(f"  Groq model   : {GPT_MODEL}")
    print("\n  Predefined prompts (type /run <key>):")
    for key in PREDEFINED_PROMPTS:
        print(f"    /run {key}")
    print("  Type 'exit' to quit\n")

    while True:
        try:
            user_input = input("You: ").strip()
            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit"):
                print("Exiting!")
                break
            if user_input.startswith("/run "):
                key    = user_input[5:].strip()
                result = run_predefined(key)
                print(f"\nAgent: {result}")
            else:
                result = run_agent(user_input)
                print(f"\nAgent: {result}")
        except KeyboardInterrupt:
            print("\nExiting!")
            break
