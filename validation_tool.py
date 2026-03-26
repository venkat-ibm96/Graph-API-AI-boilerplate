"""
validation_tool.py
------------------
All executable tool functions and their Groq-compatible JSON schemas.

Two registries are exported:
    TOOL_FUNCTIONS : dict[str, callable]
        Maps tool name → Python function.
        Consumed by validation_agent.py for dispatch.

    TOOL_SCHEMAS : list[dict]
        OpenAI-style function definitions passed to the Groq API so the
        model knows which tools exist and what arguments they accept.

Sections:
    1. Configuration & imports
    2. Patch-window parser helper
    3. Tool functions
    4. TOOL_FUNCTIONS registry
    5. TOOL_SCHEMAS declarations

Key behaviours
--------------
* update_boot_time_in_excel
    - If the server already has a Boot Time recorded → SKIP (no overwrite).
    - If the server exists but Boot Time is empty/null → write the new value.
    - If the server does NOT exist in the master Excel → append a new row.
    - Handles multiple implementation-status e-mails safely: each mail covers
      a different subset of servers; data from earlier mails is never lost.
    - Duplicate server across mails: the LATEST value wins because the agent
      processes each mail in order and the skip-if-exists guard is bypassed
      only when the cell is genuinely empty.

* validate_boot_within_patch_window
    - If Application Team Validation Status is already set → SKIP.
    - Otherwise compute and write the status as normal.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta

import pandas as pd
from dotenv import load_dotenv

import threading
import random
import winrm
import logging
import time

load_dotenv()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. Configuration
# ---------------------------------------------------------------------------

EXCELS_FOLDER: str = os.getenv("EXCELS_FOLDER", "Excels")
MASTER_PATH: str   = os.path.join(EXCELS_FOLDER, "master_patch_data.xlsx")

WINRM_USER:      str = os.getenv("WINRM_USER")
WINRM_PASSWORD:  str = os.getenv("WINRM_PASSWORD")
WINRM_PORT:      int = int(os.getenv("WINRM_PORT", "5985"))
WINRM_TRANSPORT: str = os.getenv("WINRM_TRANSPORT", "ntlm")

# Threading lock — prevents concurrent writes to the master Excel
_excel_lock: threading.Lock = threading.Lock()

# ---------------------------------------------------------------------------
# 2. Patch-window parser helper
# ---------------------------------------------------------------------------

def _parse_patch_window(patch_window: str, reference_date: datetime | None = None):
    """
    Tries to parse common patch-window formats, e.g.:
      '2025-06-14 22:00 - 2025-06-15 02:00'
      '14-Jun-2025 22:00 to 15-Jun-2025 02:00'
      '22:00 - 02:00'  (time-only; uses reference_date or today)
      'Sunday-03:00:00 to 07:00:00'
    Returns (start_dt, end_dt) as datetime objects, or (None, None) on failure.
    """
    if not patch_window or pd.isna(patch_window):
        return None, None

    pw  = str(patch_window).strip()
    ref = reference_date or datetime.now()

    # Pattern 1: full datetime range  "YYYY-MM-DD HH:MM - YYYY-MM-DD HH:MM"
    m = re.match(
        r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})\s*[-–to]+\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})",
        pw, re.IGNORECASE
    )
    if m:
        try:
            start = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M")
            end   = datetime.strptime(m.group(2), "%Y-%m-%d %H:%M")
            return start, end
        except ValueError:
            pass

    # Pattern 2: "DD-Mon-YYYY HH:MM to DD-Mon-YYYY HH:MM"
    m = re.match(
        r"(\d{1,2}-[A-Za-z]+-\d{4}\s+\d{2}:\d{2})\s*(?:to|-)\s*(\d{1,2}-[A-Za-z]+-\d{4}\s+\d{2}:\d{2})",
        pw, re.IGNORECASE
    )
    if m:
        try:
            start = datetime.strptime(m.group(1), "%d-%b-%Y %H:%M")
            end   = datetime.strptime(m.group(2), "%d-%b-%Y %H:%M")
            return start, end
        except ValueError:
            pass

    # Pattern 3: time-only "HH:MM - HH:MM"
    m = re.match(r"(\d{2}:\d{2})\s*[-–]\s*(\d{2}:\d{2})", pw)
    if m:
        try:
            start = ref.replace(
                hour=int(m.group(1)[:2]), minute=int(m.group(1)[3:]),
                second=0, microsecond=0
            )
            end = ref.replace(
                hour=int(m.group(2)[:2]), minute=int(m.group(2)[3:]),
                second=0, microsecond=0
            )
            # Handle overnight windows
            if end < start:
                end = end.replace(day=end.day + 1)
            return start, end
        except ValueError:
            pass

    # Pattern 4: "Sunday-03:00:00 to 07:00:00"
    m = re.match(
        r"([A-Za-z]+)-(\d{2}:\d{2})(?::\d{2})?\s*to\s*(\d{2}:\d{2})(?::\d{2})?",
        pw, re.IGNORECASE
    )
    if m:
        try:
            day_name   = m.group(1).strip().lower()
            start_time = m.group(2)
            end_time   = m.group(3)

            day_map = {
                "monday": 0, "tuesday": 1, "wednesday": 2,
                "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
            }

            if day_name not in day_map:
                return None, None

            target_weekday = day_map[day_name]
            boot_weekday   = ref.weekday()

            # Find how many days back the last occurrence of target day was
            days_diff = (target_weekday - boot_weekday) % 7
            # if days_diff == 0:
            #     days_diff = 7
            window_date = ref + timedelta(days=days_diff)

            start = window_date.replace(
                hour=int(start_time[:2]), minute=int(start_time[3:]),
                second=0, microsecond=0
            )
            end = window_date.replace(
                hour=int(end_time[:2]), minute=int(end_time[3:]),
                second=0, microsecond=0
            )

            # Handle overnight window (e.g. 22:00 to 02:00)
            if end < start:
                end = end + timedelta(days=1)

            return start, end
        except ValueError:
            pass

    return None, None


# ---------------------------------------------------------------------------
# 3. Helper utilities
# ---------------------------------------------------------------------------

def _cell_is_empty(value) -> bool:
    """Return True if a cell value is None, NaN, or an empty/whitespace string."""
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return str(value).strip() == ""


def _ensure_columns(df: pd.DataFrame, *columns: str) -> pd.DataFrame:
    for col in columns:
        if col not in df.columns:
            df[col] = None  # scalar None — pandas broadcasts correctly across all rows
        # Always cast to object so strings can be written
        df[col] = df[col].astype(object)
    return df


def _server_mask(df: pd.DataFrame, server_name: str) -> pd.Series:
    """Return a boolean mask for rows whose Server Name matches *server_name* (case-insensitive, stripped)."""
    return (
        df["Server Name"].astype(str).str.strip().str.lower()
        == server_name.strip().lower()
    )


# ---------------------------------------------------------------------------
# 4. Tool functions
# ---------------------------------------------------------------------------

def get_lyric_servers_ready_for_validation() -> str:
    """
    Return only lyric servers where Implementation Status is 'Completed'.
    These are the only servers that should have boot time fetched and validated.
    """
    time.sleep(3)
    if not os.path.exists(MASTER_PATH):
        return json.dumps({"error": f"Master Excel not found at {MASTER_PATH}"})

    try:
        df = pd.read_excel(MASTER_PATH)
        df.columns = df.columns.str.strip()

        lyric_df = df[df["Application Name"].str.contains("lyric", case=False, na=False)]

        ready_df = lyric_df[
            lyric_df["Implementation Status"].str.strip().str.lower() == "completed"
        ]

        cols = [
            c for c in [
                "Server Name", "Application Name", "Patch Window",
                "Reboot Required", "Implementation Status",
            ]
            if c in ready_df.columns
        ]

        return json.dumps({
            "count":   len(ready_df),
            "servers": ready_df[cols].to_dict(orient="records"),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})
    

# def get_server_boot_time(server_names: list[str]) -> str:
#     """
#     Connect to *server_name* via WinRM and retrieve the last boot time.
    
#     Args:
#         server_name: Hostname or IP address of the Windows server
    
#     Returns:
#         JSON string with server, boot_time, and error fields
#         Format: {"server": "...", "boot_time": "YYYY-MM-DD HH:MM:SS", "error": null/str}
    
#     Reads WinRM credentials from environment variables:
#         - WINRM_USER
#         - WINRM_PASSWORD
#         - WINRM_TRANSPORT (default: ntlm)
#     """
    
#     try:
#         time.sleep(5)
#         for server_name in server_names:
#             if not WINRM_USER or not WINRM_PASSWORD:
#                 logger.error(f"{server_name}: WINRM_USER or WINRM_PASSWORD not set in environment")
#                 return json.dumps({
#                     "server": server_name,
#                     "boot_time": None,
#                     "error": "WinRM credentials not configured (WINRM_USER, WINRM_PASSWORD)",
#                 })
            
#             logger.info(f"Connecting to {server_name}...")
#             if "cranckb" in server_name:
#                 # Create WinRM session using winrm.Session
#                 session = winrm.Session(
#                     server_name,
#                     auth=(WINRM_USER, WINRM_PASSWORD),
#                     transport=WINRM_TRANSPORT,
#                 )
#             else:
#                 session = winrm.Session(
#                     server_name,
#                     auth=("azure-server\\subhayan", WINRM_PASSWORD),
#                     transport=WINRM_TRANSPORT,
#                 )
            
#             # Run PowerShell command to get boot time and computer name
#             ps_cmd = (
#                 "(Get-CimInstance -ClassName Win32_OperatingSystem | "
#                 "ForEach-Object { $_.CSName + ' ' + $_.LastBootUpTime })"
#             )
            
#             response = session.run_ps(ps_cmd)
            
#             # Check for command execution errors
#             if response.status_code != 0:
#                 error_msg = response.std_err.decode("utf-8", errors="ignore").strip()
#                 if not error_msg:
#                     error_msg = "Unknown error"
#                 logger.warning(f"PowerShell error on {server_name}: {error_msg}")
#                 return json.dumps({
#                     "server": server_name,
#                     "boot_time": None,
#                     "error": f"PowerShell command failed: {error_msg}",
#                 })
            
#             # Parse output: format is "COMPUTERNAME 3/12/2026 3:42:05 PM"
#             output = response.std_out.decode("utf-8", errors="ignore").strip()
            
#             if not output:
#                 logger.warning(f"{server_name}: No output from PowerShell command")
#                 return json.dumps({
#                     "server": server_name,
#                     "boot_time": None,
#                     "error": "No output from PowerShell command",
#                 })
            
#             # Split output: first part is computer name, rest is the datetime
#             parts = output.split(maxsplit=1)
#             if len(parts) < 2:
#                 logger.warning(f"{server_name}: Invalid output format: {output}")
#                 return json.dumps({
#                     "server": server_name,
#                     "boot_time": None,
#                     "error": f"Invalid output format: {output}",
#                 })
            
#             # Extract boot time (second part onwards)
#             boot_time_str = parts[1].strip()
            
#             # Parse the boot time from Windows format (e.g., "3/12/2026 3:42:05 PM")
#             # Try multiple formats
#             boot_dt = None
#             for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
#                 try:
#                     boot_dt = datetime.strptime(boot_time_str, fmt)
#                     break
#                 except ValueError:
#                     continue
            
#             if boot_dt is None:
#                 logger.warning(f"{server_name}: Could not parse boot time: {boot_time_str}")
#                 return json.dumps({
#                     "server": server_name,
#                     "boot_time": None,
#                     "error": f"Could not parse boot time format: {boot_time_str}",
#                 })
            
#             # Format to standardized format: YYYY-MM-DD HH:MM:SS
#             formatted_boot_time = boot_dt.strftime("%Y-%m-%d %H:%M:%S")
            
#             logger.info(f" {server_name}: boot time = {formatted_boot_time}")
            
#             return json.dumps({
#                 "server": server_name,
#                 "boot_time": formatted_boot_time,
#                 "error": None,
#             })
    
#     except TimeoutError:
#         error_msg = f"Connection timeout to {server_name}"
#         logger.error(error_msg)
#         return json.dumps({
#             "server": server_name,
#             "boot_time": None,
#             "error": "Could not connect to server",
#         })
    
#     except ConnectionError as e:
#         error_msg = f"Connection refused: {str(e)}"
#         logger.error(f"{server_name}: {error_msg}")
#         return json.dumps({
#             "server": server_name,
#             "boot_time": None,
#             "error": "Could not connect to server",
#         })
    
#     except Exception as e:
#         error_msg = f"WinRM error: {str(e)}"
#         logger.error(f"{server_name}: {error_msg}")
#         return json.dumps({
#             "server": server_name,
#             "boot_time": None,
#             "error": "Could not connect to server",
#         })
 

def get_server_boot_time() -> str:
    """
    Reads lyric servers with Implementation Status 'Completed' from the master
    Excel, connects to each via WinRM, and retrieves the last boot time.
    Returns a list of results — one entry per server.
    """
    if not os.path.exists(MASTER_PATH):
        return json.dumps({"error": f"Master Excel not found at {MASTER_PATH}"})

    if not WINRM_USER or not WINRM_PASSWORD:
        return json.dumps({"error": "WinRM credentials not configured"})

    # --- Pull server list from Excel ---
    try:
        df = pd.read_excel(MASTER_PATH)
        df.columns = df.columns.str.strip()

        ready_df = df[
            df["Application Name"].str.contains("lyric", case=False, na=False)
            & (df["Implementation Status"].str.strip().str.lower() == "completed")
        ]

        server_names = ready_df["Server Name"].dropna().str.strip().tolist()
    except Exception as e:
        return json.dumps({"error": f"Failed to read Excel: {e}"})

    if not server_names:
        return json.dumps({"count": 0, "results": [], "message": "No eligible servers found."})

    # --- Loop and fetch boot times ---
    results = []

    for server_name in server_names:
        try:
            time.sleep(2)
            logger.info(f"Connecting to {server_name}...")

            if "cranckb" in server_name:
                session = winrm.Session(
                    server_name,
                    auth=(WINRM_USER, WINRM_PASSWORD),
                    transport=WINRM_TRANSPORT,
                )
            else:
                session = winrm.Session(
                    server_name,
                    auth=("azure-server\\subhayan", WINRM_PASSWORD),
                    transport=WINRM_TRANSPORT,
                )

            ps_cmd = (
                "(Get-CimInstance -ClassName Win32_OperatingSystem | "
                "ForEach-Object { $_.CSName + ' ' + $_.LastBootUpTime })"
            )

            response = session.run_ps(ps_cmd)

            if response.status_code != 0:
                error_msg = response.std_err.decode("utf-8", errors="ignore").strip()
                results.append({
                    "server": server_name,
                    "boot_time": None,
                    "error": f"PowerShell failed: {error_msg or 'Unknown'}",
                })
                continue

            output = response.std_out.decode("utf-8", errors="ignore").strip()

            if not output:
                results.append({"server": server_name, "boot_time": None, "error": "No output"})
                continue

            parts = output.split(maxsplit=1)
            if len(parts) < 2:
                results.append({"server": server_name, "boot_time": None, "error": f"Invalid output: {output}"})
                continue

            boot_time_str = parts[1].strip()
            boot_dt = None
            for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                try:
                    boot_dt = datetime.strptime(boot_time_str, fmt)
                    break
                except ValueError:
                    continue

            if not boot_dt:
                results.append({"server": server_name, "boot_time": None, "error": f"Parse failed: {boot_time_str}"})
                continue

            results.append({
                "server": server_name,
                "boot_time": boot_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "error": None,
            })

        except Exception:
            results.append({"server": server_name, "boot_time": None, "error": "Could not connect to server"})

    return json.dumps({"count": len(results), "results": results})

def update_boot_time_in_excel(servers: list[dict]) -> str:
    """
    Write boot_time and/or error into the master Excel for a list of servers.

    Each item in `servers` should be:
        {"server_name": str, "boot_time": str | None, "error": str | None}

    Rules per server:
    1. Already has Boot Time or Error recorded → SKIP (preserves earlier data).
    2. Exists but Boot Time is empty           → write the new value.
    3. Not found in Excel                      → append a new row.
    """
    time.sleep(3)
    if not os.path.exists(MASTER_PATH):
        return json.dumps({"error": f"Master Excel not found at {MASTER_PATH}"})

    results = []

    try:
        with _excel_lock:
            df = pd.read_excel(MASTER_PATH)
            df.columns = df.columns.str.strip()
            df = _ensure_columns(df, "Boot Time", "Error")
            df["Boot Time"] = df["Boot Time"].astype(object)
            df["Error"]     = df["Error"].astype(object)

            for item in servers:
                server_name = item.get("server", "").strip()
                boot_time   = item.get("boot_time")
                error       = item.get("error")

                if not server_name:
                    results.append({"server": server_name, "status": "error", "reason": "Empty server name"})
                    continue

                mask = _server_mask(df, server_name)

                # Case A: server NOT in Excel → append new row
                if not mask.any():
                    new_row = {col: None for col in df.columns}
                    new_row["Server Name"] = server_name
                    new_row["Boot Time"]   = boot_time
                    new_row["Error"]       = error
                    df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
                    # Recompute mask after concat
                    mask = _server_mask(df, server_name)
                    results.append({"server": server_name, "status": "added", "boot_time": boot_time, "error": error})
                    continue

                # Case B: server found — check if already recorded
                existing_boot  = df.loc[mask, "Boot Time"].iloc[0]
                existing_error = df.loc[mask, "Error"].iloc[0]

                if not _cell_is_empty(existing_boot) or not _cell_is_empty(existing_error):
                    results.append({
                        "server": server_name,
                        "status": "skipped",
                        "reason": f"Already recorded — Boot Time: '{existing_boot}', Error: '{existing_error}'.",
                    })
                    continue

                # Case C: server found, Boot Time empty → safe to write
                df.loc[mask, "Boot Time"] = boot_time
                df.loc[mask, "Error"]     = error
                results.append({"server": server_name, "status": "updated", "boot_time": boot_time, "error": error})

            # Single write for the entire batch
            df.to_excel(MASTER_PATH, index=False)

    except Exception as e:
        return json.dumps({"error": str(e)})

    return json.dumps({"count": len(results), "results": results})


def validate_boot_within_patch_window(server_names: list[str]) -> str:
    """
    Batch validate whether each server's stored Boot Time falls inside its Patch Window.
    Updates 'Application Team Validation Status' for all servers in a single Excel read/write.

    Status values:
      - 'Successful' → boot time within patch window
      - 'Failed'     → boot time outside patch window OR no patch window defined
      - 'Unknown'    → boot time missing/unparseable OR patch window unparseable
    
    Skips servers where status is already recorded.
    """
    time.sleep(3)
    if not os.path.exists(MASTER_PATH):
        return json.dumps({"error": f"Master Excel not found at {MASTER_PATH}"})

    results = []

    try:
        with _excel_lock:
            df = pd.read_excel(MASTER_PATH)
            df.columns = df.columns.str.strip()
            df = _ensure_columns(df, "Application Team Validation Status")

            for server_name in server_names:
                mask = _server_mask(df, server_name)

                if not mask.any():
                    results.append({
                        "server": server_name,
                        "status": "error",
                        "reason": f"Server '{server_name}' not found in Excel.",
                    })
                    continue

                row = df[mask].iloc[0]

                # Skip if already recorded
                existing_status = row.get("Application Team Validation Status")
                if not _cell_is_empty(existing_status):
                    results.append({
                        "server": server_name,
                        "status": "skipped",
                        "reason": f"Already recorded as '{existing_status}'.",
                    })
                    continue

                # Parse boot time
                boot_time_raw = row.get("Boot Time")
                patch_window  = row.get("Patch Window")

                boot_dt = None
                if not _cell_is_empty(boot_time_raw):
                    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d-%b-%Y %H:%M"):
                        try:
                            boot_dt = datetime.strptime(str(boot_time_raw).strip(), fmt)
                            break
                        except ValueError:
                            continue

                if boot_dt is None:
                    df.loc[mask, "Application Team Validation Status"] = "Unknown"
                    results.append({
                        "server": server_name,
                        "status": "Unknown",
                        "reason": "Boot time is missing or could not be parsed.",
                    })
                    continue

                # Parse patch window
                start_dt, end_dt = _parse_patch_window(patch_window, reference_date=boot_dt)

                if start_dt is None:
                    df.loc[mask, "Application Team Validation Status"] = "Unknown"
                    results.append({
                        "server": server_name,
                        "status": "Unknown",
                        "reason": f"Patch window '{patch_window}' could not be parsed.",
                    })
                    continue

                within            = start_dt <= boot_dt <= end_dt
                validation_status = "Successful" if within else "Failed"

                df.loc[mask, "Application Team Validation Status"] = validation_status
                results.append({
                    "server":       server_name,
                    "status":       validation_status,
                    "boot_time":    str(boot_dt),
                    "patch_window": f"{start_dt} → {end_dt}",
                    "within_window": within,
                })

            # Single write for entire batch
            df.to_excel(MASTER_PATH, index=False)

    except Exception as e:
        return json.dumps({"error": str(e)})

    return json.dumps({"count": len(results), "results": results})

# ---------------------------------------------------------------------------
# 5. TOOL_FUNCTIONS registry
# ---------------------------------------------------------------------------

TOOL_FUNCTIONS: dict[str, callable] = {
    # "get_lyric_servers_ready_for_validation": get_lyric_servers_ready_for_validation,
    "get_server_boot_time":                   get_server_boot_time,
    "update_boot_time_in_excel":              update_boot_time_in_excel,
    "validate_boot_within_patch_window":      validate_boot_within_patch_window,
}


# ---------------------------------------------------------------------------
# 6. TOOL_SCHEMAS — OpenAI-style, consumed by Groq API
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict] = [
    # {
    #     "type": "function",
    #     "function": {
    #         "name": "get_lyric_servers_ready_for_validation",
    #         "description": (
    #             "Return only lyric application servers where Implementation Status is 'Completed'. "
    #             "Always use this when fetching boot times or running validation — "
    #             "servers that are not Completed should be skipped."
    #         ),
    #         "parameters": {
    #             "type":       "object",
    #             "properties": {},
    #         },
    #     },
    # },
    {
    "type": "function",
    "function": {
        "name": "get_server_boot_time",
        "description": (
            "Reads all lyric servers with Implementation Status 'Completed' directly "
            "from the master Excel, connects to each via WinRM, and retrieves the last "
            "boot time. No parameters needed — server discovery is handled internally."
        ),
        "parameters": {
            "type": "object",
            "properties": {},  # No parameters
            },
        },
    },
    {
    "type": "function",
    "function": {
        "name": "update_boot_time_in_excel",
        "description": (
            "Write boot time and/or error into the master Excel for a batch of servers "
            "in a single call. SKIPS servers where Boot Time is already recorded. "
            "Appends a new row for servers not found in Excel. "
            "Pass boot_time=null and error=<message> for servers where WinRM failed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "servers": {
                    "type": "array",
                    "description": "List of servers to update.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "server": {
                                "type": "string",
                                "description": "Exact server name as it appears in Excel.",
                            },
                            "boot_time": {
                                "anyOf": [{"type": "string"}, {"type": "null"}],
                                "description": "Boot time e.g. '2026-03-12 15:42:05', or null if unavailable.",
                            },
                            "error": {
                                "anyOf": [{"type": "string"}, {"type": "null"}],
                                "description": "Error message if WinRM failed, or null if successful.",
                            },
                        },
                        "required": ["server"],
                        },
                    }
                },
                "required": ["servers"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_boot_within_patch_window",
            "description": (
                "Batch validate whether each server's Boot Time (already stored in Excel) "
                "falls inside its Patch Window. Pass ALL server names at once. "
                "Sets 'Application Team Validation Status' to 'Successful', 'Failed', or 'Unknown' "
                "for each server in a single Excel read/write. "
                "SKIPS servers where status is already recorded."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "server_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of exact server names as they appear in Excel.",
                    }
                },
                "required": ["server_names"],
            },
        },
    },
]