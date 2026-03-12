import os
import re
import json
import base64
import requests
import threading
import pandas as pd
from pathlib import Path
from dotenv import load_dotenv
from msal import PublicClientApplication, SerializableTokenCache
from flask import Flask, request, jsonify
from google import genai
from google.genai import types
from datetime import datetime, timedelta, timezone
from ValidationAgent import run_agent as run_validation_agent
from SharedExcelLock import excel_lock

load_dotenv()

#Gemini
API_KEY    = os.getenv("GEMINI_API_KEY")
MODEL      = os.getenv("GEMINI_MODEL")

#Excels

EXCELS_FOLDER    = os.getenv("EXCELS_FOLDER", "Excels")
EXCEL_EXTENSIONS = {".xlsx", ".xls", ".xlsm", ".xlsb", ".csv"}
os.makedirs(os.path.join(EXCELS_FOLDER, "Maintenance"), exist_ok=True)
os.makedirs(os.path.join(EXCELS_FOLDER, "Rescheduled"), exist_ok=True)
os.makedirs(os.path.join(EXCELS_FOLDER, "ImplementationStatus"), exist_ok=True)
IMPORTANT_COLUMNS = [
    "Server Name",
    "Application Name",
    "Patch Window",
    "Reboot Required",
    "Implementation Status"
]


# Microsoft Graph
CLIENT_ID   = os.getenv("CLIENT_ID")
AUTHORITY   = os.getenv("AUTHORITY")
SCOPES      = [os.getenv("SCOPES")]
CACHE_FILE  = os.getenv("CACHE_FILE")
FOLDER_NAME = os.getenv("FOLDER_NAME")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

os.makedirs(EXCELS_FOLDER, exist_ok=True)

client = genai.Client(api_key=API_KEY)


cache = SerializableTokenCache()
if os.path.exists(CACHE_FILE):
    cache.deserialize(open(CACHE_FILE, "r").read())

app_auth = PublicClientApplication(CLIENT_ID, authority=AUTHORITY, token_cache=cache)


def get_access_token():
    accounts = app_auth.get_accounts()
    result = app_auth.acquire_token_silent(SCOPES, account=accounts[0]) if accounts else None
    if not result:
        result = app_auth.acquire_token_interactive(scopes=SCOPES)
    if cache.has_state_changed:
        open(CACHE_FILE, "w").write(cache.serialize())
    return result["access_token"]


def get_headers():
    return {
        "Authorization": "Bearer " + get_access_token(),
        "Content-Type": "application/json"
    }



PREDEFINED_PROMPTS = {

    "lyric_servers_patch":
        "Find all lyric application servers and tell me their patch day window.",

    "full_summary":
        "Give me a complete summary of all servers: total count, how many need reboots, "
        "unique patch windows, and any servers with downtime.",

    "mail_and_patch_check":
        "Get the latest email, extract any server names mentioned in the subject or body, "
        "then check the Excel data to see if those servers have patch windows defined.",
}



def load_excel():
    # full_path = os.path.join(EXCEL_DIR, EXCEL_FILE)
    # if not os.path.exists(full_path):
    #     print(f"File not found: {full_path}")
    #     return None
    # return pd.read_excel(full_path)
    master_path = os.path.join(EXCELS_FOLDER, "master_patch_data.xlsx")

    if not os.path.exists(master_path):
        return build_master_excel()

    return pd.read_excel(master_path)


def delete_old_excels():
    cutoff = datetime.now() - timedelta(days=7)
    for file_path in Path(EXCELS_FOLDER).iterdir():
        if file_path.is_file():
            file_age = datetime.fromtimestamp(file_path.stat().st_mtime)
            if file_age < cutoff:
                file_path.unlink()
                print(f"Deleted old file: {file_path.name}")

def get_latest_excel(folder):
    files = [
        f for f in Path(folder).iterdir()
        if f.suffix.lower() in EXCEL_EXTENSIONS
    ]

    if not files:
        return None

    latest = max(files, key=lambda f: f.stat().st_mtime)
    return latest


def build_master_excel(default_impl_status="Pending"):
    folders  = ["Maintenance", "Rescheduled", "ImplementationStatus"]
    priority = {"Maintenance": 1, "Rescheduled": 2, "ImplementationStatus": 3}
    dfs = []
 
    master_path = os.path.join(EXCELS_FOLDER, "master_patch_data.xlsx")
 
    # ── Preserve ValidationAgent columns before rebuilding ────────────────────
    preserved_cols = ["Boot Time", "Application Team Validation Status"]
    preserved_data = {}
    if os.path.exists(master_path):
        try:
            existing_df = pd.read_excel(master_path)
            existing_df.columns = existing_df.columns.str.strip()
            cols_to_save = ["Server Name"] + [c for c in preserved_cols if c in existing_df.columns]
            if len(cols_to_save) > 1:
                preserved_data = existing_df[cols_to_save].set_index("Server Name").to_dict(orient="index")
        except Exception as e:
            print(f"Could not preserve validation columns: {e}")
 
    for folder in folders:
        folder_path = os.path.join(EXCELS_FOLDER, folder)
        latest_file = get_latest_excel(folder_path)
        if not latest_file:
            continue
        try:
            df = (
                pd.read_excel(latest_file)
                if latest_file.suffix.lower() != ".csv"
                else pd.read_csv(latest_file)
            )
            df.columns      = df.columns.str.strip()
            df["Source_Folder"] = folder
            df["Source_File"]   = latest_file.name
            df["Source_Mtime"]  = latest_file.stat().st_mtime
            dfs.append(df)
        except Exception as e:
            print(f"Error reading {latest_file}: {e}")
 
    if not dfs:
        print("No Excel files found.")
        return None
 
    required_cols = ["Server Name", "Application Name", "Patch Window",
                     "Reboot Required", "Implementation Status"]
    for df in dfs:
        for col in required_cols:
            if col not in df.columns:
                df[col] = None
 
    master_df = pd.concat(dfs, ignore_index=True)
 
    # ImplementationStatus (priority=3) is source of truth → keep="last"
    master_df["priority"] = master_df["Source_Folder"].map(priority)
    master_df = master_df.sort_values(
        by=["priority", "Source_Mtime"],
        ascending=[True, True]          # lowest priority first → keep="last" wins highest
    )
    master_df = master_df.drop_duplicates(subset=["Server Name"], keep="last")
 
    master_df["Implementation Status"] = master_df["Implementation Status"].fillna(default_impl_status)
 
    # Drop internal helper columns
    master_df = master_df.drop(columns=["priority", "Source_Mtime"], errors="ignore")
 
    # ── Patch preserved ValidationAgent columns back in ───────────────────────
    if preserved_data:
        for col in preserved_cols:
            master_df[col] = master_df["Server Name"].map(
                lambda s: preserved_data.get(s, {}).get(col)
            )
 
    with excel_lock:
        master_df.to_excel(master_path, index=False)
    print(f"Master Excel updated: {master_path}")
 
    return master_df

def get_latest_mail(folder_name: str = "") -> str:
    """
    Fetch the most recent email from the monitored mail folder.
    Returns subject, sender, body preview, and any saved Excel attachments.
    If the subject is 'Implementation Status', also triggers the Validation Agent.
    """
    target_folder = folder_name or FOLDER_NAME
 
    # Resolve folder ID
    folder_url = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/childFolders"
    folders    = requests.get(folder_url, headers=get_headers()).json()
    folder_id  = None
    for f in folders.get("value", []):
        if f["displayName"] == target_folder:
            folder_id = f["id"]
 
    if not folder_id:
        return json.dumps({"error": f"Folder '{target_folder}' not found."})
 
    # Fetch latest message
    msgs_url = (
        f"https://graph.microsoft.com/v1.0/me/mailFolders/{folder_id}/messages"
        f"?$top=1&$orderby=receivedDateTime desc"
    )
    msgs = requests.get(msgs_url, headers=get_headers()).json()
 
    if not msgs.get("value"):
        return json.dumps({"error": "No messages found in folder."})
 
    mail       = msgs["value"][0]
    message_id = mail["id"]
    subject    = mail.get("subject", "")
    sender     = mail["from"]["emailAddress"]["address"]
    body       = mail.get("bodyPreview")
    received   = mail.get("receivedDateTime")
 
    attachments_saved = []
    is_impl_status    = "Implementation Status" in subject
 
    if (
        "Maintenance Notification" in subject
        or "Reschedule Maintenance"  in subject
        or is_impl_status
    ):
        if mail.get("hasAttachments"):
            attach_url      = f"https://graph.microsoft.com/v1.0/me/messages/{message_id}/attachments"
            attach_response = requests.get(attach_url, headers=get_headers()).json()
 
            for att in attach_response.get("value", []):
                file_name = att["name"]
                ext       = Path(file_name).suffix.lower()
 
                if ext not in EXCEL_EXTENSIONS:
                    continue
 
                if "Maintenance Notification" in subject:
                    folder    = os.path.join(EXCELS_FOLDER, "Maintenance")
                    save_path = os.path.join(folder, "maintenance_latest.xlsx")
 
                elif "Reschedule Maintenance" in subject:
                    folder    = os.path.join(EXCELS_FOLDER, "Rescheduled")
                    save_path = os.path.join(folder, "rescheduled_latest.xlsx")
 
                elif is_impl_status:
                    folder    = os.path.join(EXCELS_FOLDER, "ImplementationStatus")
                    save_path = os.path.join(folder, "implementation_latest.xlsx")
 
                else:
                    continue
 
                os.makedirs(folder, exist_ok=True)
                file_data = base64.b64decode(att["contentBytes"])
                with open(save_path, "wb") as f:
                    f.write(file_data)
 
                attachments_saved.append(save_path)
                print(f"  [Mail Tool] Saved: {save_path}")
 
    if attachments_saved:
        build_master_excel()
        if is_impl_status:
            print("\n[Mail Tool] Implementation Status mail — starting Validation Agent...")
            threading.Thread(
                target=run_validation_agent,
                args=(
                    "Get all lyric servers where Implementation Status is Completed, "
                    "connect to each via WinRM to fetch the boot time, save it to Excel, "
                    "then validate if it's within the patch window and update the "
                    "Application Team Validation Status for every server.",
                ),
                kwargs={"silent": True},
                daemon=True,
            ).start()
        else:
            print(f"\n[Mail Tool] '{subject}' mail processed — validation agent not triggered.")
 
    return json.dumps({
        "message_id":        message_id,
        "subject":           subject,
        "from":              sender,
        "received":          received,
        "body_preview":      body,
        "attachments_saved": attachments_saved,
        "validation_triggered": is_impl_status and bool(attachments_saved),
    })




def search_mails_by_subject(keyword: str) -> str:
    """
    Search emails in the monitored folder whose subject contains a keyword.
    """
    folder_url = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/childFolders"
    folders = requests.get(folder_url, headers=get_headers()).json()
    folder_id = None
    for f in folders.get("value", []):
        if f["displayName"] == FOLDER_NAME:
            folder_id = f["id"]

    if not folder_id:
        return json.dumps({"error": f"Folder '{FOLDER_NAME}' not found."})

    msgs_url = (
        f"https://graph.microsoft.com/v1.0/me/mailFolders/{folder_id}/messages"
        f"?$filter=contains(subject,'{keyword}')"
        f"&$select=subject,from,receivedDateTime,hasAttachments,bodyPreview"
        f"&$top=10&$orderby=receivedDateTime desc"
    )
    msgs = requests.get(msgs_url, headers=get_headers()).json()

    results = []
    for mail in msgs.get("value", []):
        results.append({
            "subject":      mail.get("subject"),
            "from":         mail["from"]["emailAddress"]["address"],
            "received":     mail.get("receivedDateTime"),
            "body_preview": mail.get("bodyPreview", "")[:200],
        })

    return json.dumps({"keyword": keyword, "count": len(results), "emails": results})



def filter_by_application_name(keyword: str) -> str:
    df = load_excel()
    if df is None:
        return json.dumps({"error": "Could not load Excel file."})
    filtered = df[df["Application Name"].str.contains(re.escape(keyword), case=False, na=False)]
    return json.dumps({"count": len(filtered), "results": filtered.to_dict(orient="records")})


def get_column_names() -> str:
    df = load_excel()
    if df is None:
        return json.dumps({"error": "Could not load Excel file."})
    return json.dumps({"columns": list(df.columns)})


def get_summary_stats(column_name: str) -> str:
    df = load_excel()
    if df is None:
        return json.dumps({"error": "Could not load Excel file."})
    if column_name not in df.columns:
        return json.dumps({"error": f"Column '{column_name}' not found."})
    return json.dumps(df[column_name].describe().to_dict())


def get_unique_values(column_name: str) -> str:
    df = load_excel()
    if df is None:
        return json.dumps({"error": "Could not load Excel file."})
    if column_name not in df.columns:
        return json.dumps({"error": f"Column '{column_name}' not found."})
    return json.dumps({"column": column_name, "unique_values": df[column_name].dropna().unique().tolist()})


def get_row_count() -> str:
    df = load_excel()
    if df is None:
        return json.dumps({"error": "Could not load Excel file."})
    return json.dumps({"total_rows": len(df)})


def filter_by_column_value(column_name: str, value: str) -> str:
    df = load_excel()
    if df is None:
        return json.dumps({"error": "Could not load Excel file."})
    if column_name not in df.columns:
        return json.dumps({"error": f"Column '{column_name}' not found."})
    filtered = df[df[column_name].astype(str).str.contains(re.escape(value), case=False, na=False)]
    return json.dumps({"count": len(filtered), "results": filtered.to_dict(orient="records")})


def get_all_rows() -> str:
    df = load_excel()
    if df is None:
        return json.dumps({"error": "Could not load Excel file."})
    return json.dumps({"count": len(df), "results": df.to_dict(orient="records")})

def get_lyric_servers() -> str:
    df = load_excel()

    filtered = df[df["Application Name"].str.contains("lyric", case=False, na=False)]

    filtered = filtered[IMPORTANT_COLUMNS].head(50)

    return json.dumps({
        "count": len(filtered),
        "results": filtered.to_dict(orient="records")
    })

def lyric_summary():
    df = load_excel()

    lyric = df[df["Application Name"].str.contains("lyric", case=False, na=False)]

    summary = {
        "total_servers": len(lyric),
        "reboot_required": lyric["Reboot Required"].value_counts().to_dict(),
        "patch_windows": lyric["Patch Window"].unique().tolist()
    }

    return json.dumps(summary)

TOOL_FUNCTIONS = {
    # Mail tools
    "get_latest_mail":        get_latest_mail,
    "search_mails_by_subject": search_mails_by_subject,
    # Excel tools
    "filter_by_application_name": filter_by_application_name,
    "get_column_names":        get_column_names,
    "get_summary_stats":       get_summary_stats,
    "get_unique_values":       get_unique_values,
    "get_row_count":           get_row_count,
    "filter_by_column_value":  filter_by_column_value,
    "get_all_rows":            get_all_rows,
    "get_lyric_servers":       get_lyric_servers,
    "lyric_summary":           lyric_summary
}

GEMINI_TOOLS = [
    types.Tool(function_declarations=[

        types.FunctionDeclaration(
            name="get_latest_mail",
            description=(
                "Fetch the single most recent email from the monitored inbox folder. "
                "Returns subject, sender, received time, body preview, and any Excel "
                "attachments that were automatically downloaded and saved."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "folder_name": types.Schema(
                        type="STRING",
                        description="Optional: override the default monitored folder name."
                    )
                }
            )
        ),

        types.FunctionDeclaration(
            name="search_mails_by_subject",
            description="Search emails in the monitored folder by a subject keyword.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "keyword": types.Schema(type="STRING", description="Keyword to search in subject")
                },
                required=["keyword"]
            )
        ),

        # ---- EXCEL ----
        types.FunctionDeclaration(
            name="filter_by_application_name",
            description="Filter Excel rows where Application Name contains a keyword.",
            parameters=types.Schema(
                type="OBJECT",
                properties={"keyword": types.Schema(type="STRING")},
                required=["keyword"]
            )
        ),
        types.FunctionDeclaration(
            name="get_column_names",
            description="Get all column names from the Excel file.",
            parameters=types.Schema(type="OBJECT", properties={})
        ),
        types.FunctionDeclaration(
            name="get_summary_stats",
            description="Get summary statistics for a numeric column.",
            parameters=types.Schema(
                type="OBJECT",
                properties={"column_name": types.Schema(type="STRING")},
                required=["column_name"]
            )
        ),
        types.FunctionDeclaration(
            name="get_unique_values",
            description="Get all unique values in a column.",
            parameters=types.Schema(
                type="OBJECT",
                properties={"column_name": types.Schema(type="STRING")},
                required=["column_name"]
            )
        ),
        types.FunctionDeclaration(
            name="get_row_count",
            description="Get the total number of rows in the Excel file.",
            parameters=types.Schema(type="OBJECT", properties={})
        ),
        types.FunctionDeclaration(
            name="filter_by_column_value",
            description="Filter rows where a specific column contains a value.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "column_name": types.Schema(type="STRING"),
                    "value": types.Schema(type="STRING")
                },
                required=["column_name", "value"]
            )
        ),
        types.FunctionDeclaration(
            name="get_all_rows",
            description="Return all rows. Use when user wants all servers or full data without filtering.",
            parameters=types.Schema(type="OBJECT", properties={})
        ),
        types.FunctionDeclaration(
            name="get_lyric_servers",
            description="Return servers belonging to lyric application.",
            parameters=types.Schema(type="OBJECT", properties={})
        ),
         types.FunctionDeclaration(
            name="lyric_summary",
            description=(
                "Return a summary of lyric application servers including total server count, "
                "reboot required distribution, and unique patch windows."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={}
            )
        ),
    ])
]



def run_agent(user_query: str, silent: bool = False) -> str:

    if not silent:
        print(f"\nUser: {user_query}")

    messages = [types.Content(role="user", parts=[types.Part(text=user_query)])]

    system_prompt = (
        "You are an Mail Agent and have access to email monitoring and server patch data. "
        "You can read the latest emails query an Excel file of server patch schedules. "
        "Use tools to fetch real data — never guess or invent values. "
        "Be concise and professional."
    )

    while True:
        response = client.models.generate_content(
            model=MODEL,
            contents=messages,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                tools=GEMINI_TOOLS,
            )
        )

        parts = response.candidates[0].content.parts
        tool_calls = [p for p in parts if p.function_call]

        if not tool_calls:
            final_text = "".join(p.text for p in parts if p.text)
            # if not silent:
            #     print(f"\nAgent: {final_text}")
            return final_text

        messages.append(types.Content(role="model", parts=parts))

        tool_result_parts = []
        for part in tool_calls:
            fc = part.function_call
            if not silent:
                print(f"  [Tool call] {fc.name}({dict(fc.args)})")
            func = TOOL_FUNCTIONS.get(fc.name)
            result = func(**dict(fc.args)) if func else json.dumps({"error": f"Unknown tool: {fc.name}"})
            if not silent:
                print(f"  [Result]    {result[:200]}{'...' if len(result) > 200 else ''}")

            tool_result_parts.append(types.Part(
                function_response=types.FunctionResponse(
                    name=fc.name,
                    response={"result": result}
                )
            ))
        messages.append(types.Content(role="user", parts=tool_result_parts))



def run_predefined(prompt_key: str, silent: bool = False) -> str:
    """Run a predefined prompt by key. Example: run_predefined('daily_patch_report')"""
    if prompt_key not in PREDEFINED_PROMPTS:
        raise ValueError(f"Unknown prompt: '{prompt_key}'. Available: {list(PREDEFINED_PROMPTS.keys())}")
    return run_agent(PREDEFINED_PROMPTS[prompt_key], silent=silent)





flask_app = Flask(__name__)


processed_messages = {} 

@flask_app.route("/webhook", methods=["GET", "POST"])
def webhook():

    validation_token = request.args.get("validationToken")
    if validation_token:
        print("[Webhook] Validation request received — responding OK")
        return validation_token, 200, {"Content-Type": "text/plain"}

    if request.method == "POST":
        data = request.get_json(silent=True)
        if not data or "value" not in data:
            return jsonify({"status": "ignored"}), 200

       
        for notification in data["value"]:
            message_id = notification.get("resourceData", {}).get("id")
            if not message_id:
                continue

            # Skip if already processed
            if message_id in processed_messages:
                print(f"[Webhook] Skipping duplicate message: {message_id}")
                continue

            processed_messages[message_id] = message_id 

            print(f"[Webhook] New mail notification received: {message_id}")
            
            
            threading.Thread(
            target=_process_notification,
            args=(message_id,),
            daemon=True
            ).start()

        return jsonify({"status": "ok"}), 200

def _process_notification(message_id: str):
    result = run_agent(
        "A new mail notification from the patching team just arrived. "
        "Fetch the latest email and check if the subject contains "
        "'Maintenance Notification', 'Reschedule Maintenance', or 'Implementation Status'. "
        "Download any attached Excel and save it to the correct folder. "
        "Return the subject, attachment info, and lyric server details. "
        "Note: if the subject contains 'Implementation Status', the Validation Agent "
        "will be triggered automatically — you do not need to do anything extra.",
        silent=True, 
    )
    print(f"\n[Agent Auto-Response]\n{result}")

def setup_subscription():
    import time
    time.sleep(2)
    print("\n--- Starting Graph API Setup ---")

    folder_url = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/childFolders"
    folders = requests.get(folder_url, headers=get_headers()).json()
    folder_id = None
    for f in folders.get("value", []):
        if f["displayName"] == FOLDER_NAME:
            folder_id = f["id"]

    if not folder_id:
        print("Folder not found. Check FOLDER_NAME in .env")
        return
    print("Folder ID:", folder_id)

    subs = requests.get("https://graph.microsoft.com/v1.0/subscriptions", headers=get_headers()).json()
    subscription_exists = any(
        f"mailFolders/{folder_id}" in s["resource"]
        for s in subs.get("value", [])
    )

    if subscription_exists:
        print("Subscription already active.")
        return

    expiration = (datetime.now(timezone.utc) + timedelta(minutes=4200)).isoformat()
    resp = requests.post(
        "https://graph.microsoft.com/v1.0/subscriptions",
        headers=get_headers(),
        json={
            "changeType": "created",
            "notificationUrl": WEBHOOK_URL,
            "resource": f"me/mailFolders/{folder_id}/messages",
            "expirationDateTime": expiration,
            "clientState": "secret123"
        }
    )
    print("Subscription response:", resp.status_code)
    print("Subscription created!" if resp.status_code in (200, 201) else resp.json())


if __name__ == "__main__":
    import threading, sys

    if "--webhook" in sys.argv:
        print("Starting Flask webhook server on port 5000...")
        threading.Thread(target=setup_subscription, daemon=True).start()

        # Disable reloader to prevent double invocation
        flask_app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)

    # elif "--run" in sys.argv:
    #     idx = sys.argv.index("--run")
    #     key = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
    #     if not key:
    #         print(f"Usage: python excel_agent.py --run <prompt_key>")
    #         print(f"Available: {list(PREDEFINED_PROMPTS.keys())}")
    #     else:
    #         run_predefined(key)

   
    else:
        print("=" * 55)
        print(" Mail Agent")
        print("=" * 55)
        print(f"  Excel: master_patch_data.xlsx")
        print(f"  Mail folder: {FOLDER_NAME}")
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
                    run_predefined(user_input[5:].strip())
                else:
                    run_agent(user_input)
            except KeyboardInterrupt:
                print("\nExiting!")
                break