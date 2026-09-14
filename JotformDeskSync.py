# ===============================================================
# AUTHOR DETAILS
# ===============================================================
# Author      : Ankit.Kumar
# Organization: Daalchini Technologies
# Purpose     : Jotform → Faveo Ticket Synchronization Automation
# Created     : September 2026
# ===============================================================

import requests
import time
import re
import json
import os

from datetime import datetime
from zoneinfo import ZoneInfo

import gspread
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ============================================================
# CONFIGURATION (LOADED VIA ENVIRONMENT VARIABLES)
# ============================================================

JOTFORM_API_KEY = os.environ["JOTFORM_API_KEY"]
FAVEO_TOKEN = os.environ["FAVEO_TOKEN"]
FORM_ID = "251590768630059"

SHEET_ID = "1aEyTSdIgxwgDl9Zp_Fr_ztW9OT4asoImcipWdiWlSeY"
WORKSHEET_NAME = "Closure Data"
INDEX_WORKSHEET_NAME = "Faveo Ticket Index"

BASE_URL = "https://desk.daalchini.co.in"

CLOSED_STATUS_ID = 3
START_LEAD_NO = 2826

# Target Faveo Form 8 & Creator ID 15
FAVEO_CATEGORY_ID = 8
FAVEO_CREATOR_ID = 15

JOTFORM_PAGE_SIZE = 100
FAVEO_TICKET_LIST_LIMIT = 100

REQUEST_TIMEOUT = 30
REQUEST_DELAY = 0.25

SHEET_HEADERS = [
    "LEAD ID",
    "LEAD SUBMISSION ID",
    "OPERATION",
    "STAGE OF OPERATION",
    "FAVEO DATA ID",
    "FAVEO TICKET ID",
    "TICKET CREATED AT",
    "TICKET AUTO CLOSED AT"
]

INDEX_HEADERS = [
    "FAVEO ID",
    "TICKET NUMBER",
    "LEAD ID",
    "TITLE",
    "CREATED AT (IST)",
    "RAW CREATED AT",
    "IS CLOSED"
]


# ============================================================
# GOOGLE SERVICE ACCOUNT (LOADED VIA ENVIRONMENT VARIABLE)
# ============================================================

SERVICE_ACCOUNT_INFO = json.loads(os.environ["SERVICE_ACCOUNT_INFO"])


# ============================================================
# HTTP SESSION
# ============================================================

def create_session():
    retry_strategy = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


SESSION = create_session()


# ============================================================
# TIME & PARSING HELPERS (STRICT UTC -> IST)
# ============================================================

def get_ist_time():
    return datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d-%m-%Y %H:%M:%S")


def format_faveo_created_at(raw_value):
    """
    Converts Faveo UTC timestamps (e.g. '2026-09-02T11:20:27.000000Z')
    strictly into Asia/Kolkata IST format ('%d-%m-%Y %H:%M:%S').
    Rejects 'FALSE', 'TRUE', or boolean values.
    """
    if not raw_value or isinstance(raw_value, bool):
        return ""

    val = str(raw_value).strip("\"' ")
    if not val or val.upper() in {"FALSE", "TRUE", "NONE", "NULL"}:
        return ""

    # Parse UTC string ending in 'Z'
    if val.endswith("Z"):
        try:
            dt_utc = datetime.fromisoformat(val.replace("Z", "+00:00"))
            dt_ist = dt_utc.astimezone(ZoneInfo("Asia/Kolkata"))
            return dt_ist.strftime("%d-%m-%Y %H:%M:%S")
        except Exception:
            pass

    # Parse ISO offset string (+05:30, etc.)
    if ("+" in val[10:]) or ("-" in val[10:]):
        try:
            dt = datetime.fromisoformat(val)
            dt_ist = dt.astimezone(ZoneInfo("Asia/Kolkata"))
            return dt_ist.strftime("%d-%m-%Y %H:%M:%S")
        except Exception:
            pass

    # Standard SQL / DateTime formats
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%d-%m-%Y %H:%M:%S",
        "%d/%m/%Y %H:%M:%S"
    ]

    for fmt in formats:
        try:
            parsed = datetime.strptime(val, fmt)
            return parsed.strftime("%d-%m-%Y %H:%M:%S")
        except ValueError:
            pass

    return val


def parse_event_datetime(value):
    if value is None or isinstance(value, bool):
        return None

    val = str(value).strip("\"' ")
    if not val or val.upper() in {"FALSE", "TRUE", "NONE", "NULL"}:
        return None

    if val.endswith("Z"):
        try:
            dt_utc = datetime.fromisoformat(val.replace("Z", "+00:00"))
            return dt_utc.astimezone(ZoneInfo("Asia/Kolkata"))
        except Exception:
            pass

    if ("+" in val[10:]) or ("-" in val[10:]):
        try:
            dt = datetime.fromisoformat(val)
            return dt.astimezone(ZoneInfo("Asia/Kolkata"))
        except Exception:
            pass

    formats = [
        "%d-%m-%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%d/%m/%Y %H:%M:%S"
    ]

    for fmt in formats:
        try:
            return datetime.strptime(val, fmt).replace(tzinfo=ZoneInfo("Asia/Kolkata"))
        except ValueError:
            pass

    return None


def clean_answer(value):
    if value is None:
        return ""

    if isinstance(value, dict):
        value = (
            value.get("answer")
            or value.get("text")
            or value.get("value")
            or value.get("datetime")
            or ""
        )

    if isinstance(value, list):
        return ", ".join(str(x) for x in value if x is not None)

    return str(value).strip()


def get_answer(answers, question_id):
    if not isinstance(answers, dict):
        return ""
    lead_answer = answers.get(str(question_id), {})
    if isinstance(lead_answer, dict):
        return clean_answer(lead_answer.get("answer", ""))
    return clean_answer(lead_answer)


def extract_lead_number(value):
    if not value:
        return None
    match = re.search(r"\d+", str(value))
    return int(match.group()) if match else None


# ============================================================
# JOTFORM ELIGIBILITY GATEKEEPER
# ============================================================

def check_jotform_eligibility(content):
    thread = content.get("thread", [])
    if not isinstance(thread, list):
        return False, "", ""

    for event in reversed(thread):
        if event.get("actionType") != "WEBHOOK_SUCCESS":
            continue

        action_details = event.get("actionDetails", {})
        response_content = action_details.get("responseContent", {})

        if isinstance(response_content, str):
            try:
                response_content = json.loads(response_content)
            except Exception:
                response_content = {}

        response_data = response_content.get("data", {}) if isinstance(response_content, dict) else {}

        f_data_id = str(response_data.get("id", "") or "").strip()
        f_ticket_no = str(response_data.get("ticket_number", "") or "").strip()

        if f_data_id:
            return True, f_data_id, f_ticket_no

    return False, "", ""


# ============================================================
# GOOGLE SHEET TICKET INDEX MANAGEMENT (WITH RESILIENT MAPPING)
# ============================================================

def get_or_create_index_worksheet(spreadsheet):
    try:
        ws = spreadsheet.worksheet(INDEX_WORKSHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        print(f"Creating '{INDEX_WORKSHEET_NAME}' tab in Google Sheets...")
        ws = spreadsheet.add_worksheet(title=INDEX_WORKSHEET_NAME, rows=1000, cols=10)
        ws.append_row(INDEX_HEADERS)
    return ws


def load_cached_index_from_sheet(index_ws):
    all_rows = index_ws.get_all_values()
    lead_ticket_map = {}
    known_ticket_ids = set()
    max_known_id = 0

    if len(all_rows) <= 1:
        return lead_ticket_map, known_ticket_ids, max_known_id

    header_row = [str(col).strip().upper() for col in all_rows[0]]
    col_idx = {name: idx for idx, name in enumerate(header_row)}

    faveo_id_col = col_idx.get("FAVEO ID", 0)
    ticket_no_col = col_idx.get("TICKET NUMBER", 1)
    lead_id_col = col_idx.get("LEAD ID", 2)
    title_col = col_idx.get("TITLE", 3)
    
    created_at_col = col_idx.get("CREATED AT (IST)", col_idx.get("CREATED AT", 4))
    raw_created_col = col_idx.get("RAW CREATED AT", 5)
    is_closed_col = col_idx.get("IS CLOSED", 6)

    for row in all_rows[1:]:
        if not row:
            continue

        tid = str(row[faveo_id_col]).strip() if faveo_id_col < len(row) else ""
        if not tid or not tid.isdigit():
            continue

        tnum = str(row[ticket_no_col]).strip() if ticket_no_col < len(row) else ""
        lead_key = str(row[lead_id_col]).strip().upper() if lead_id_col < len(row) else ""
        t_title = str(row[title_col]).strip() if title_col < len(row) else ""
        
        raw_created_at = str(row[raw_created_col]).strip() if raw_created_col < len(row) else ""
        created_at_cell = str(row[created_at_col]).strip() if created_at_col < len(row) else ""

        # Reject boolean string corruption
        if created_at_cell.upper() in {"FALSE", "TRUE"}:
            created_at_cell = ""
        if raw_created_at.upper() in {"FALSE", "TRUE"}:
            raw_created_at = ""

        created_at_fmt = format_faveo_created_at(raw_created_at or created_at_cell)
        is_closed_val = (str(row[is_closed_col]).strip().lower() == "true") if is_closed_col < len(row) else False

        ticket_item = {
            "f_data_id": tid,
            "f_ticket_no": tnum,
            "title": t_title,
            "created_at": created_at_fmt,
            "raw_created_at": raw_created_at or created_at_cell,
            "is_closed_in_faveo": is_closed_val
        }

        known_ticket_ids.add(tid)
        if int(tid) > max_known_id:
            max_known_id = int(tid)

        if lead_key:
            if lead_key not in lead_ticket_map:
                lead_ticket_map[lead_key] = []
            lead_ticket_map[lead_key].append(ticket_item)

    print(f"Loaded {len(known_ticket_ids)} cached ticket(s) across {len(lead_ticket_map)} lead(s) from '{INDEX_WORKSHEET_NAME}'.")
    return lead_ticket_map, known_ticket_ids, max_known_id


def sync_faveo_index_incremental(spreadsheet, index_ws):
    lead_ticket_map, known_ticket_ids, max_known_id = load_cached_index_from_sheet(index_ws)

    url = f"{BASE_URL}/v3/api/agent/ticket-list"
    headers = {
        "Authorization": f"Bearer {FAVEO_TOKEN}",
        "Accept": "application/json",
    }

    print()
    print("=" * 110)
    print("UPDATING FAVEO INDEX INCREMENTALLY...")
    print("=" * 110)

    page = 1
    new_tickets_to_append = []
    stop_paginating = False

    while not stop_paginating:
        params = [
            ("category", str(FAVEO_CATEGORY_ID)),
            ("creator-ids[]", str(FAVEO_CREATOR_ID)),
            ("limit", str(FAVEO_TICKET_LIST_LIMIT)),
            ("page", str(page)),
            ("sort-order", "desc")
        ]

        try:
            res = SESSION.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
            if res.status_code != 200:
                print(f"       ⚠️ Ticket list returned HTTP {res.status_code} on page {page}")
                break

            payload = res.json()
            data_block = payload.get("data", {})
            tickets = data_block.get("tickets", []) if isinstance(data_block, dict) else []

            if not tickets:
                break

            for t in tickets:
                if not isinstance(t, dict):
                    continue

                tid = clean_answer(t.get("id"))
                if not tid:
                    continue

                if tid in known_ticket_ids:
                    if max_known_id > 0:
                        stop_paginating = True
                        break
                    continue

                t_title = clean_answer(t.get("title") or t.get("subject"))

                # Extract Lead ID directly from title
                match = re.search(r"DC-LEAD-\d+", t_title, re.IGNORECASE)
                if not match:
                    match = re.search(r"DC-LEAD-\d+", str(t), re.IGNORECASE)

                lead_key = match.group().upper() if match else ""

                raw_created = clean_answer(t.get("created_at"))
                created_at_fmt = format_faveo_created_at(raw_created)

                status_dict = t.get("status") if isinstance(t.get("status"), dict) else {}
                is_closed = (status_dict.get("id") == CLOSED_STATUS_ID) or (
                    clean_answer(status_dict.get("name")).lower() in {"closed", "close"}
                )

                ticket_item = {
                    "f_data_id": str(tid),
                    "f_ticket_no": clean_answer(t.get("ticket_number")),
                    "title": t_title,
                    "created_at": created_at_fmt,
                    "raw_created_at": raw_created,
                    "is_closed_in_faveo": is_closed
                }

                known_ticket_ids.add(tid)
                if lead_key:
                    if lead_key not in lead_ticket_map:
                        lead_ticket_map[lead_key] = []
                    lead_ticket_map[lead_key].append(ticket_item)

                new_tickets_to_append.append([
                    str(tid),
                    clean_answer(t.get("ticket_number")),
                    lead_key,
                    t_title,
                    created_at_fmt,
                    raw_created,
                    str(is_closed)
                ])

            next_page_url = data_block.get("next_page_url")
            if not next_page_url or stop_paginating:
                break

            page += 1
            time.sleep(REQUEST_DELAY)

        except Exception as e:
            print(f"       ❌ Error during index sync on page {page}: {e}")
            break

    if new_tickets_to_append:
        print(f"Saving {len(new_tickets_to_append)} new ticket(s) to '{INDEX_WORKSHEET_NAME}'...")
        index_ws.append_rows(new_tickets_to_append, value_input_option="USER_ENTERED")
    else:
        print("Index is up to date. No new tickets found on Faveo.")

    return lead_ticket_map


# ============================================================
# FAVEO TICKET DETAILS (FOR DIRECT FALLBACK LOOKUP)
# ============================================================

def get_faveo_ticket_details(ticket_id):
    if not ticket_id:
        return None

    url = f"{BASE_URL}/v3/api/agent/ticket-details/{ticket_id}"
    headers = {
        "Authorization": f"Bearer {FAVEO_TOKEN}",
        "Accept": "application/json"
    }

    try:
        response = SESSION.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        return None

    if response.status_code != 200:
        return None

    try:
        payload = response.json()
    except Exception:
        return None

    ticket = payload.get("data", {}).get("ticket", {})
    if not isinstance(ticket, dict):
        return None

    status = ticket.get("status")
    status_id = ""
    status_name = ""
    if isinstance(status, dict):
        status_id = clean_answer(status.get("id"))
        status_name = clean_answer(status.get("name"))
    elif isinstance(status, str):
        status_name = status.strip()

    raw_created = clean_answer(ticket.get("created_at"))
    created_at_fmt = format_faveo_created_at(raw_created)

    is_closed = (status_id == str(CLOSED_STATUS_ID)) or (status_name.lower() in {"closed", "close"})

    return {
        "f_data_id": clean_answer(ticket.get("id")),
        "f_ticket_no": clean_answer(ticket.get("ticket_number")),
        "created_at": created_at_fmt,
        "raw_created_at": raw_created,
        "is_closed_in_faveo": is_closed
    }


def close_faveo_ticket(ticket_id):
    if not ticket_id:
        return False

    url = f"{BASE_URL}/v3/api/ticket/change-status/{ticket_id}/{CLOSED_STATUS_ID}"
    headers = {
        "Authorization": f"Bearer {FAVEO_TOKEN}",
        "Accept": "application/json"
    }

    try:
        response = SESSION.post(url, headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        print(f"       ❌ Close API failed for ticket {ticket_id}: {e}")
        return False

    return response.status_code == 200


# ============================================================
# JOTFORM SUBMISSIONS (FIXED PARAMS & VISIBILITY)
# ============================================================

def fetch_eligible_submissions(start_lead_no=START_LEAD_NO):
    eligible_ids = []
    offset = 0

    print()
    print(f"Scanning Jotform submissions for Leads >= {start_lead_no}...")

    while True:
        url = f"https://api.jotform.com/form/{FORM_ID}/submissions"
        params = {
            "apiKey": JOTFORM_API_KEY,
            "limit": JOTFORM_PAGE_SIZE,
            "offset": offset
        }

        try:
            response = SESSION.get(url, params=params, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            print(f"JotForm request failed: {e}")
            break

        if response.status_code != 200:
            print(f"JotForm API returned HTTP {response.status_code}")
            break

        try:
            data = response.json()
        except Exception:
            break

        submissions = data.get("content", [])
        if not submissions:
            break

        for submission in submissions:
            submission_id = submission.get("id")
            if not submission_id:
                continue

            answers = submission.get("answers", {})
            lead_answer = answers.get("251", {})
            lead_value = (
                clean_answer(lead_answer.get("answer", ""))
                if isinstance(lead_answer, dict)
                else clean_answer(lead_answer)
            )
            lead_number = extract_lead_number(lead_value)

            if lead_number is not None and lead_number >= start_lead_no:
                eligible_ids.append(str(submission_id))

        if len(submissions) < JOTFORM_PAGE_SIZE:
            break

        offset += JOTFORM_PAGE_SIZE
        time.sleep(REQUEST_DELAY)

    print(f"Found {len(eligible_ids)} submissions to process.")
    return eligible_ids[::-1]


def get_submission_data(submission_id):
    url = f"https://www.jotform.com/API/inbox/submission/{submission_id}"
    params = {
        "addWorkflowStatus": "1",
        "addThread": "1"
    }

    headers = {
        "APIKEY": JOTFORM_API_KEY,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": f"https://www.jotform.com/{FORM_ID}",
        "Origin": "https://www.jotform.com"
    }

    try:
        response = SESSION.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            print(f"       ⚠️ Submission {submission_id} returned HTTP {response.status_code}")
            return None
        payload = response.json()
    except Exception as e:
        print(f"       ❌ Error reading submission {submission_id}: {e}")
        return None

    content = payload.get("content", {})
    if not isinstance(content, dict):
        return None

    answers = content.get("answers", {})

    # Extract Stage directly from JotForm Question 9 (exact API string)
    current_stage_from_api = get_answer(answers, 9)

    return {
        "lead_id": get_answer(answers, 251),
        "submission_id": str(submission_id),
        "operation": get_answer(answers, 3),
        "operation_type": get_answer(answers, 4),
        "stage": current_stage_from_api,
        "created_at": get_answer(answers, 104),
        "raw_content": content
    }


# ============================================================
# STATE & GOOGLE SHEET MANAGEMENT
# ============================================================

def build_lead_state(all_rows):
    lead_state = {}
    for row_number, row in enumerate(all_rows[1:], start=2):
        if not row:
            continue
        while len(row) < 8:
            row.append("")

        lead_id = str(row[0]).strip()
        if not lead_id:
            continue

        row_state = {
            "row_number": row_number,
            "submission_id": str(row[1]).strip(),
            "operation": str(row[2]).strip(),
            "stage": str(row[3]).strip(),
            "f_data_id": str(row[4]).strip(),
            "f_ticket_no": str(row[5]).strip(),
            "created_at": str(row[6]).strip(),
            "closure_status": str(row[7]).strip()
        }

        if lead_id not in lead_state:
            lead_state[lead_id] = []
        lead_state[lead_id].append(row_state)

    return lead_state


def find_existing_sheet_row(lead_state, lead_id, stage, f_data_id):
    existing_rows = lead_state.get(lead_id, [])
    target_stage = str(stage).strip()
    target_faveo = str(f_data_id).strip()

    for row in existing_rows:
        row_stage = str(row.get("stage", "")).strip()
        row_faveo = str(row.get("f_data_id", "")).strip()
        if row_stage == target_stage and row_faveo == target_faveo:
            return row

    return None


def update_closure_time(worksheet, row_number, closure_time):
    worksheet.update_cell(row_number, 8, closure_time)


def append_new_row(worksheet, row_data):
    worksheet.append_row(row_data, value_input_option="USER_ENTERED")


# ============================================================
# MAIN SYNC ENGINE
# ============================================================

def run_sync():
    print()
    print("=" * 110)
    print("FAVEO TICKET-LIST → EXACT API STAGE & ACCURATE IST MULTI-TICKET SYNC")
    print("=" * 110)
    print(f"Started: {get_ist_time()}")

    # 1. Connect Google Sheets
    try:
        gc = gspread.service_account_from_dict(SERVICE_ACCOUNT_INFO)
        spreadsheet = gc.open_by_key(SHEET_ID)
        worksheet = spreadsheet.worksheet(WORKSHEET_NAME)
    except Exception as e:
        print(f"Google Sheet connection failed: {e}")
        return

    # Load Main Closure Data Sheet & Headers
    try:
        all_rows = worksheet.get_all_values()
    except Exception as e:
        print(f"Failed to load Closure Data: {e}")
        return

    if not all_rows:
        print("Sheet is completely empty. Adding headers...")
        worksheet.append_row(SHEET_HEADERS)
        all_rows = [SHEET_HEADERS]

    lead_state = build_lead_state(all_rows)

    # 2. Sync / Load Index from 'Faveo Ticket Index' Tab
    index_ws = get_or_create_index_worksheet(spreadsheet)
    faveo_lead_map = sync_faveo_index_incremental(spreadsheet, index_ws)

    # 3. Scan JotForm Submissions
    submission_ids = fetch_eligible_submissions()
    if not submission_ids:
        print("No eligible submissions found.")
        return

    new_rows_count = 0
    closed_tickets_count = 0
    already_processed_count = 0
    skipped_no_faveo_data = 0

    for index, sub_id in enumerate(submission_ids, start=1):
        sub = get_submission_data(sub_id)
        if not sub:
            continue

        lead_id = sub["lead_id"]
        operation = sub["operation"]
        stage_from_api = sub["stage"]

        if not lead_id:
            continue

        print()
        print("-" * 100)
        print(f"[{index}/{len(submission_ids)}] Checking Lead: {lead_id}")
        print(f"       Operation: {operation or '[BLANK]'} | Stage from API: {stage_from_api or '[BLANK]'}")

        # ----------------------------------------------------
        # STEP 1: JOTFORM ELIGIBILITY GATEKEEPER
        # ----------------------------------------------------
        is_eligible, webhook_tid, webhook_tnum = check_jotform_eligibility(sub["raw_content"])

        if not is_eligible:
            skipped_no_faveo_data += 1
            print("       → NO FAVEO DATA (Skipped - 0 Faveo calls)")
            continue

        print(f"       → ELIGIBLE LEAD! Active Webhook Ticket: {webhook_tnum or webhook_tid} (ID: {webhook_tid})")

        # ----------------------------------------------------
        # STEP 2: RETRIEVE ALL TICKETS FOR THIS LEAD FROM CACHED INDEX
        # ----------------------------------------------------
        lead_key = lead_id.strip().upper()
        tickets_for_lead = faveo_lead_map.get(lead_key, [])

        # Fallback: if webhook ticket is missing or has an invalid timestamp, fetch directly from Faveo
        if webhook_tid:
            found_ticket = next((t for t in tickets_for_lead if str(t.get("f_data_id")) == str(webhook_tid)), None)
            if not found_ticket or not found_ticket.get("created_at"):
                details = get_faveo_ticket_details(webhook_tid)
                if details:
                    if found_ticket:
                        found_ticket["created_at"] = details["created_at"]
                        found_ticket["raw_created_at"] = details["raw_created_at"]
                    else:
                        tickets_for_lead.append(details)

        # Re-verify any tickets in the list with missing or invalid timestamps
        for t in tickets_for_lead:
            c_at = t.get("created_at", "")
            if not c_at or c_at.upper() in {"FALSE", "TRUE", "NONE", ""}:
                details = get_faveo_ticket_details(t.get("f_data_id"))
                if details:
                    t["created_at"] = details["created_at"]
                    t["raw_created_at"] = details["raw_created_at"]

        if not tickets_for_lead:
            print(f"       → No Form 8 tickets found in index for Lead {lead_id}.")
            continue

        print(f"       → Retrieved {len(tickets_for_lead)} Form 8 Ticket(s) for {lead_id}:")
        for t in tickets_for_lead:
            print(f"         - Ticket: {t['f_ticket_no']} (ID: {t['f_data_id']}) | Created At (IST): {t['created_at']}")

        # ----------------------------------------------------
        # STEP 3: SORT CHRONOLOGICALLY (Higher ID = more recent)
        # ----------------------------------------------------
        def ticket_sort_key(t):
            nid = int(t["f_data_id"]) if str(t.get("f_data_id", "")).isdigit() else 0
            dt = parse_event_datetime(t.get("raw_created_at") or t.get("created_at")) or datetime.min.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
            return (nid, dt)

        sorted_tickets = sorted(tickets_for_lead, key=ticket_sort_key)
        total_tickets = len(sorted_tickets)
        latest_ticket = sorted_tickets[-1]
        latest_faveo_id = latest_ticket["f_data_id"]

        # ----------------------------------------------------
        # STEP 4: CLOSE PREVIOUS ACTIVE ROWS IN GOOGLE SHEET
        # ----------------------------------------------------
        existing_sheet_rows = lead_state.get(lead_id, [])
        closure_timestamp = get_ist_time()

        for old_row in existing_sheet_rows:
            old_faveo = old_row.get("f_data_id", "")
            old_ticket = old_row.get("f_ticket_no", "")
            old_status = old_row.get("closure_status", "")

            # If it's an older ticket and currently Pending, close it
            if old_faveo and str(old_faveo) != str(latest_faveo_id):
                if not old_status or old_status.upper() == "PENDING":
                    print(f"       → Closing older active ticket {old_ticket or old_faveo} in Faveo...")
                    if close_faveo_ticket(old_faveo):
                        print(f"       → CLOSED Ticket {old_ticket or old_faveo} in Faveo at {closure_timestamp}")
                        closed_tickets_count += 1
                    time.sleep(REQUEST_DELAY)

                    row_num = old_row["row_number"]
                    update_closure_time(worksheet, row_num, closure_timestamp)
                    old_row["closure_status"] = closure_timestamp
                    if row_num - 1 < len(all_rows):
                        all_rows[row_num - 1][7] = closure_timestamp
                    print(f"       → Updated Row {row_num} (Ticket {old_ticket or old_faveo}) Closed At: {closure_timestamp}")

        # ----------------------------------------------------
        # STEP 5: APPEND TICKETS TO GOOGLE SHEET
        # ----------------------------------------------------
        for idx, t in enumerate(sorted_tickets):
            is_latest = (idx == total_tickets - 1)
            t_faveo_id = t["f_data_id"]
            t_ticket_no = t["f_ticket_no"]
            t_created_at = t["created_at"] or get_ist_time()
            t_stage = stage_from_api

            existing_row = find_existing_sheet_row(lead_state, lead_id, t_stage, t_faveo_id)

            if is_latest:
                if existing_row:
                    already_processed_count += 1
                    print(f"       → Latest Ticket {t_ticket_no} already in Sheet (Row {existing_row['row_number']}) → Skipped.")
                    continue

                closure_status = "Already Closed" if t["is_closed_in_faveo"] else "Pending"

                row_payload = [
                    lead_id,
                    sub["submission_id"],
                    operation,
                    t_stage,
                    t_faveo_id,
                    t_ticket_no,
                    t_created_at,
                    closure_status
                ]

                append_new_row(worksheet, row_payload)
                all_rows.append(row_payload)
                new_row_number = len(all_rows)

                if lead_id not in lead_state:
                    lead_state[lead_id] = []
                lead_state[lead_id].append({
                    "row_number": new_row_number,
                    "submission_id": sub["submission_id"],
                    "operation": operation,
                    "stage": t_stage,
                    "f_data_id": t_faveo_id,
                    "f_ticket_no": t_ticket_no,
                    "created_at": t_created_at,
                    "closure_status": closure_status
                })

                new_rows_count += 1
                print(f"       → [LATEST TICKET APPENDED] Row {new_row_number}: {t_ticket_no} | Stage: '{t_stage}' | Created At (IST): {t_created_at} | Status: {closure_status}")

            else:
                if t_faveo_id and t_faveo_id != latest_faveo_id:
                    if not t["is_closed_in_faveo"]:
                        print(f"       → Closing older ticket {t_ticket_no} (ID: {t_faveo_id}) in Faveo...")
                        if close_faveo_ticket(t_faveo_id):
                            print(f"       → CLOSED Ticket {t_ticket_no} in Faveo at {closure_timestamp}")
                            closed_tickets_count += 1
                        time.sleep(REQUEST_DELAY)

                if existing_row:
                    already_processed_count += 1
                    continue

                row_payload = [
                    lead_id,
                    sub["submission_id"],
                    operation,
                    t_stage,
                    t_faveo_id,
                    t_ticket_no,
                    t_created_at,
                    closure_timestamp
                ]

                append_new_row(worksheet, row_payload)
                all_rows.append(row_payload)
                new_row_number = len(all_rows)

                if lead_id not in lead_state:
                    lead_state[lead_id] = []
                lead_state[lead_id].append({
                    "row_number": new_row_number,
                    "submission_id": sub["submission_id"],
                    "operation": operation,
                    "stage": t_stage,
                    "f_data_id": t_faveo_id,
                    "f_ticket_no": t_ticket_no,
                    "created_at": t_created_at,
                    "closure_status": closure_timestamp
                })

                new_rows_count += 1
                print(f"       → [OLDER TICKET APPENDED] Row {new_row_number}: {t_ticket_no} | Stage: '{t_stage}' | Created At (IST): {t_created_at} | Closed At: {closure_timestamp}")

    # ========================================================
    # FINAL SUMMARY
    # ========================================================
    print()
    print("=" * 110)
    print("FINAL SUMMARY")
    print("=" * 110)
    print(f"Submissions scanned          : {len(submission_ids)}")
    print(f"Skipped (No Faveo Data)      : {skipped_no_faveo_data}")
    print(f"New rows added to sheet      : {new_rows_count}")
    print(f"Older tickets closed via API : {closed_tickets_count}")
    print(f"Rows already present/skipped : {already_processed_count}")
    print(f"Finished                     : {get_ist_time()}")
    print("=" * 110)


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    run_sync()
