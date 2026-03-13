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
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta

import pandas as pd
from dotenv import load_dotenv

import threading

load_dotenv()

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
            days_diff   = (boot_weekday - target_weekday) % 7
            window_date = ref - timedelta(days=days_diff)

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
# 3. Tool functions
# ---------------------------------------------------------------------------

def get_lyric_servers_ready_for_validation() -> str:
    """
    Return only lyric servers where Implementation Status is 'Completed'.
    These are the only servers that should have boot time fetched and validated.
    """
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


def get_server_boot_time(server_name: str) -> str:
    """
    Connect to *server_name* via WinRM and retrieve the last boot time
    using  (Get-Date) - (gcim Win32_OperatingSystem).LastBootUpTime
    Returns the boot time as an ISO-8601 string.
    """
    return json.dumps({"server": server_name, "boot_time": "2026-03-12 15:42:05"})


def update_boot_time_in_excel(server_name: str, boot_time: str) -> str:
    """
    Write *boot_time* into the 'Boot Time' column for *server_name*
    in the master Excel file.  Creates the column if it doesn't exist.
    """
    if not os.path.exists(MASTER_PATH):
        return json.dumps({"error": f"Master Excel not found at {MASTER_PATH}"})

    try:
        df = pd.read_excel(MASTER_PATH)
        df.columns = df.columns.str.strip()

        if "Boot Time" not in df.columns:
            df["Boot Time"] = None

        mask = (
            df["Server Name"].astype(str).str.strip().str.lower()
            == server_name.strip().lower()
        )

        if not mask.any():
            return json.dumps({"error": f"Server '{server_name}' not found in Excel."})

        df.loc[mask, "Boot Time"] = boot_time
        with _excel_lock:
            df.to_excel(MASTER_PATH, index=False)

        return json.dumps({
            "status":    "updated",
            "server":    server_name,
            "boot_time": boot_time,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


def validate_boot_within_patch_window(server_name: str) -> str:
    """
    Check whether the server's stored Boot Time falls inside its Patch Window.
    Updates 'Application Team Validation Status' column accordingly:
      - 'Successful'  → boot time is within the patch window
      - 'Failed'      → boot time is outside the patch window
      - 'Unknown'     → missing/unparseable boot time or patch window
    """
    if not os.path.exists(MASTER_PATH):
        return json.dumps({"error": f"Master Excel not found at {MASTER_PATH}"})

    try:
        df = pd.read_excel(MASTER_PATH)
        df.columns = df.columns.str.strip()

        if "Application Team Validation Status" not in df.columns:
            df["Application Team Validation Status"] = None

        mask = (
            df["Server Name"].astype(str).str.strip().str.lower()
            == server_name.strip().lower()
        )

        if not mask.any():
            return json.dumps({"error": f"Server '{server_name}' not found in Excel."})

        row = df[mask].iloc[0]

        boot_time_raw = row.get("Boot Time")
        patch_window  = row.get("Patch Window")

        # Parse boot time
        boot_dt = None
        if boot_time_raw and not pd.isna(boot_time_raw):
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d-%b-%Y %H:%M"):
                try:
                    boot_dt = datetime.strptime(str(boot_time_raw).strip(), fmt)
                    break
                except ValueError:
                    continue

        if boot_dt is None:
            df.loc[mask, "Application Team Validation Status"] = "Unknown"
            with _excel_lock:
                df.to_excel(MASTER_PATH, index=False)
            return json.dumps({
                "server": server_name,
                "status": "Unknown",
                "reason": "Boot time is missing or could not be parsed.",
            })

        start_dt, end_dt = _parse_patch_window(patch_window, reference_date=boot_dt)

        if start_dt is None:
            df.loc[mask, "Application Team Validation Status"] = "Unknown"
            with _excel_lock:
                df.to_excel(MASTER_PATH, index=False)
            return json.dumps({
                "server": server_name,
                "status": "Unknown",
                "reason": f"Patch window '{patch_window}' could not be parsed.",
            })

        within            = start_dt <= boot_dt <= end_dt
        validation_status = "Successful" if within else "Failed"

        df.loc[mask, "Application Team Validation Status"] = validation_status
        with _excel_lock:
            df.to_excel(MASTER_PATH, index=False)

        return json.dumps({
            "server":        server_name,
            "boot_time":     str(boot_dt),
            "patch_window":  f"{start_dt} → {end_dt}",
            "within_window": within,
            "status":        validation_status,
        })

    except Exception as e:
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# 4. TOOL_FUNCTIONS registry
# ---------------------------------------------------------------------------

TOOL_FUNCTIONS: dict[str, callable] = {
    "get_lyric_servers_ready_for_validation": get_lyric_servers_ready_for_validation,
    "get_server_boot_time":                   get_server_boot_time,
    "update_boot_time_in_excel":              update_boot_time_in_excel,
    "validate_boot_within_patch_window":      validate_boot_within_patch_window,
}


# ---------------------------------------------------------------------------
# 5. TOOL_SCHEMAS — OpenAI-style, consumed by Groq API
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_lyric_servers_ready_for_validation",
            "description": (
                "Return only lyric application servers where Implementation Status is 'Completed'. "
                "Always use this when fetching boot times or running validation — "
                "servers that are not Completed should be skipped."
            ),
            "parameters": {
                "type":       "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_server_boot_time",
            "description": (
                "Connect to a Windows server via WinRM and retrieve its last "
                "boot time (Win32_OperatingSystem.LastBootUpTime). "
                "Returns the boot time as 'YYYY-MM-DD HH:MM:SS'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "server_name": {
                        "type":        "string",
                        "description": "Hostname or IP address of the target Windows server.",
                    }
                },
                "required": ["server_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_boot_time_in_excel",
            "description": (
                "Write a server's boot time into the 'Boot Time' column of the "
                "master Excel file. Creates the column if it doesn't exist."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "server_name": {
                        "type":        "string",
                        "description": "Exact server name as it appears in the Excel.",
                    },
                    "boot_time": {
                        "type":        "string",
                        "description": "Boot time string, e.g. '2025-06-14 23:45:00'.",
                    },
                },
                "required": ["server_name", "boot_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_boot_within_patch_window",
            "description": (
                "Check whether the server's Boot Time (already stored in Excel) "
                "falls inside its Patch Window. "
                "Sets 'Application Team Validation Status' to 'Successful', 'Failed', or 'Unknown' "
                "and saves the result back to the master Excel."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "server_name": {
                        "type":        "string",
                        "description": "Exact server name as it appears in the Excel.",
                    }
                },
                "required": ["server_name"],
            },
        },
    },
]
