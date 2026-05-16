import os
import json
import re
from typing import Dict, List, Optional
import boto3
from datetime import datetime
from config import openai_client

from helpers.db_helpers import customers_table, conversations_table, get_customer_id

# ─────────────────────────────────────────────
# CRM follow-up
# ─────────────────────────────────────────────

def get_missing_crm_fields(customer: Dict) -> List[str]:
    """Return a list of CRM fields that are missing from the customer record."""
    missing = []
    if not customer.get('contact_name'):
        missing.append('name')
    if not customer.get('email'):
        missing.append('email')
    if not customer.get('lead_intent') or customer.get('lead_intent') == 'Unknown':
        missing.append('intent')
    return missing


def build_crm_followup_message(missing_fields: List[str]) -> Optional[str]:
    """Build a natural follow-up SMS asking for missing CRM info."""
    if not missing_fields:
        return None

    asks = []
    if 'name' in missing_fields:
        asks.append("your name")
    if 'email' in missing_fields:
        asks.append("your email address")
    if 'intent' in missing_fields:
        asks.append("whether you're looking to buy for yourself or as an investment")

    if len(asks) == 1:
        ask_str = asks[0]
    elif len(asks) == 2:
        ask_str = f"{asks[0]} and {asks[1]}"
    else:
        ask_str = ", ".join(asks[:-1]) + f", and {asks[-1]}"

    return f"One quick thing — could you share {ask_str}? It helps me find you the best options. 😊"


def extract_crm_reply(message: str, missing_fields: List[str]) -> Dict:
    """Use GPT to extract CRM field values from the user's reply to the follow-up question."""
    print(f"[OPENAI] Extracting CRM reply for missing fields: {missing_fields}")
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            max_tokens=200,
            messages=[
                {
                    "role": "system",
                    "content": f"""The user was asked to provide: {', '.join(missing_fields)}.
Extract these values from their reply and return ONLY valid JSON with these keys (use empty string if not found):
- contact_name (string)
- email (string)
- lead_intent (string): one of 'Buying', 'Renting', 'Investing', 'Unknown'"""
                },
                {"role": "user", "content": message}
            ]
        )
        text = re.sub(r'```json|```', '', response.choices[0].message.content.strip()).strip()
        parsed = json.loads(text)
        print(f"[OPENAI] Extracted CRM reply: {parsed}")
        return parsed
    except Exception as e:
        print(f"[OPENAI] ERROR in extract_crm_reply: {e}")
        return {}


# def update_customer_crm(phone: str, crm_data: Dict):
#     """Update only the CRM fields on an existing customer record without touching listings."""
#     print(f"[DYNAMO] Updating CRM fields for {phone}: {crm_data}")
#     try:
#         parts = []
#         # Use ExpressionAttributeNames for ALL fields since 'name' and 'email'
#         # are reserved words in DynamoDB
#         expr_names  = {
#             '#contact_name': 'contact_name',
#             '#email':        'email',
#             '#lead_intent':  'lead_intent',
#             '#updated_at':   'updated_at',
#         }
#         expr_values = {
#             ':updated_at': datetime.now().isoformat()
#         }

#         if crm_data.get('contact_name') and crm_data['contact_name'] != 'Unknown':
#             parts.append('#contact_name = :contact_name')
#             expr_values[':contact_name'] = crm_data['contact_name']

#         if crm_data.get('email') and crm_data['email'] != 'Unknown':
#             parts.append('#email = :email')
#             expr_values[':email'] = crm_data['email']

#         if crm_data.get('lead_intent') and crm_data['lead_intent'] not in ('Unknown', ''):
#             parts.append('#lead_intent = :lead_intent')
#             expr_values[':lead_intent'] = crm_data['lead_intent']

#         if not parts:
#             print("[DYNAMO] No CRM fields to update")
#             return

#         parts.append('#updated_at = :updated_at')
#         update_expr = "SET " + ", ".join(parts)

#         print(f"[DYNAMO] UpdateExpression: {update_expr}")
#         print(f"[DYNAMO] ExpressionAttributeValues: {expr_values}")

#         customers_table.update_item(
#             Key={'customer_id': phone},
#             UpdateExpression=update_expr,
#             ExpressionAttributeNames=expr_names,
#             ExpressionAttributeValues=expr_values
#         )
#         print(f"[DYNAMO] CRM fields updated successfully")
#     except Exception as e:
#         print(f"[DYNAMO] ERROR in update_customer_crm: {e}")
#         import traceback
#         print(traceback.format_exc())

def update_customer_crm(phone: str, updates: Dict):
    """
    Updates only allowed fields provided in the 'updates' dict.
    """
    # 1. Define strictly what the AI/Function is allowed to change
    ALLOWED_KEYS = {
        'contact_name': 'contact_name',
        'email': 'email',
        'customer_type': 'customer_type',
        'lead_intent': 'lead_intent',
        'summary': 'summary',
        'enquiry_bedrooms': 'enquiry_bedrooms',
        'status': 'status',
        'enquiry_max_price': 'enquiry_max_price',
    }

    parts = []
    expr_names = {}
    expr_values = {':now': datetime.now().isoformat()}

    # 2. Loop through the incoming data
    for key, value in updates.items():
        if key in ALLOWED_KEYS and value not in (None, 'Unknown', ''):
            clean_key = f"#{key}"
            clean_val = f":{key}"
            
            parts.append(f"{clean_key} = {clean_val}")
            expr_names[clean_key] = ALLOWED_KEYS[key]
            expr_values[clean_val] = value

    if not parts:
        return "No valid fields to update."

    customer_id = get_customer_id(phone)
    if not customer_id:
        print(f"[DYNAMO] ERROR in update_customer_crm: no customer found for {phone}")
        return "Customer not found."

    # 3. Always update the timestamp
    parts.append("#upd = :now")
    expr_names["#upd"] = "updated_at"

    # 4. Perform the "Surgical" Update
    customers_table.update_item(
        Key={'customer_id': customer_id},
        UpdateExpression="SET " + ", ".join(parts),
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values
    )

def parse_email_enquiry(body) -> dict:
    """
    Multi-format email parser — handles Moneypenny, Lee (voice agent), and generic/Loop.
    Returns keys compatible with the /first-message handler.
    Ported from Zapier JS extraction step.
    """
    if isinstance(body, dict):
        body = str(body)
    body = body.replace("\\n", "\n")

    first_name = last_name = email = address = simple_address = ''
    phone = postcode = comments = transaction_type = buyer_or_renter = responsible_agent_email = ''

    if "Moneypenny" in body:
        m = re.search(r'Caller Name\s*:\s*([A-Z][a-z]+)\s+([A-Z][a-z]+)', body, re.IGNORECASE)
        if m:
            first_name, last_name = m.group(1).strip(), m.group(2).strip()
        else:
            fn = re.search(r'(?:First Name|Hi|Hello)[:\s]+([A-Z][a-z]+)', body, re.IGNORECASE)
            if fn: first_name = fn.group(1).strip()
            ln = re.search(r'(?:Surname|Last Name)[:\s]+([A-Z][a-z]+)', body, re.IGNORECASE)
            if ln: last_name = ln.group(1).strip()

        em = re.search(r'Email Address[:\s]+([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', body, re.IGNORECASE)
        if em: email = em.group(1).strip()
        else:
            fb = re.search(r'([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', body, re.IGNORECASE)
            if fb: email = fb.group(1).strip()

        ph = re.search(r'(?:Contact Number|Caller Number|Phone|Tel|Mobile)[:\s]+([\d\s\-+()]{7,})', body, re.IGNORECASE)
        if not ph: ph = re.search(r'(0\d{3,4}[\s\-]?\d{3}[\s\-]?\d{3,4})', body, re.IGNORECASE)
        if ph: phone = ph.group(1).strip()

        addr = re.search(r'(?:Property Address|Address)[:\s]*([^\n]+)', body, re.IGNORECASE)
        if addr:
            address = addr.group(1).strip()
            sa = re.search(r'(?:\w+\s+){1,3}(?:Road|Street|Avenue|Ave|Lane|Close|Drive|Way|Place|Crescent|Gardens?|Grove|Terrace|Court|Walk|Rise|Hill|Mews)\b', address, re.IGNORECASE)
            if sa: simple_address = re.sub(r'^\d+\s+', '', sa.group(0).strip())

        pc = re.search(r'postcode:\s*([A-Z]{1,2}\d{1,2}\s*\d[A-Z]{2})', body, re.IGNORECASE)
        if not pc: pc = re.search(r'([A-Z]{1,2}\d{1,2}[A-Z]?\s*\d[A-Z]{2})', body, re.IGNORECASE)
        if not pc: pc = re.search(r'\b([A-Z]{1,2}\d{1,2}[A-Z]?)\b', body, re.IGNORECASE)  # district fallback e.g. SE22, SW2
        if pc: postcode = pc.group(1).upper().strip()

        cm = re.search(r'(?:Comment|Enquiry)\s*:\s*([^\n]+)', body, re.IGNORECASE)
        if cm: comments = cm.group(1).strip()

        is_lettings = 'LETTENQ' in body or bool(re.search(r'Sales\s+or\s+Lettings[:\s]+Lettings', body, re.IGNORECASE))
        transaction_type, buyer_or_renter = ('lettings', 'renter') if is_lettings else ('sales', 'buyer')

    elif "Sent by Lee (voice agent)" in body:
        clean = re.sub(r'\*([^*]+)\*', r'\1', body).replace('* * *', '')

        m = re.search(r'Caller\s*:\s*(\S+)(?:\s+(\S+))?', clean, re.IGNORECASE)
        if m:
            first_name = m.group(1).strip()
            last_name = m.group(2).strip() if m.group(2) else ''

        em = re.search(r'^Email(?:\s*Address)?\s*:\s*([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', clean, re.IGNORECASE | re.MULTILINE)
        if em: email = em.group(1).strip()
        else:
            body_only = re.sub(r'^(From|To|Cc|Bcc):.*$', '', clean, flags=re.IGNORECASE | re.MULTILINE)
            fb = re.search(r'([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', body_only, re.IGNORECASE)
            if fb: email = fb.group(1).strip()

        ph = re.search(r'(?:Contact Number|Caller Number|Phone|Tel|Mobile)\s*:\s*([\+\d][\d\s\-()]{6,})', clean, re.IGNORECASE)
        if not ph: ph = re.search(r'(\+44[\d\s]{9,})', clean, re.IGNORECASE)
        if not ph: ph = re.search(r'(0\d{3,4}[\s\-]?\d{3}[\s\-]?\d{3,4})', clean, re.IGNORECASE)
        if ph: phone = re.sub(r'\s', '', ph.group(1)).strip()

        addr = re.search(r'(?:Property Address|Property|Address)\s*:\s*([^\n(]+)', clean, re.IGNORECASE)
        if addr:
            address = addr.group(1).strip()
            sa = re.search(r'(?:\w+\s+){1,3}(?:Road|Street|Avenue|Ave|Lane|Close|Drive|Way|Place|Crescent|Gardens?|Grove|Terrace|Court|Walk|Rise|Hill|Mews)\b', address, re.IGNORECASE)
            if sa: simple_address = re.sub(r'^\d+\s+', '', sa.group(0).strip())

        pc = re.search(r'postcode:\s*([A-Z]{1,2}\d{1,2}\s*\d[A-Z]{2})', clean, re.IGNORECASE)
        if not pc: pc = re.search(r'([A-Z]{1,2}\d{1,2}[A-Z]?\s*\d[A-Z]{2})', clean, re.IGNORECASE)
        if not pc: pc = re.search(r'\b([A-Z]{1,2}\d{1,2}[A-Z]?)\b', clean, re.IGNORECASE)  # district fallback e.g. SE22, SW2
        if pc: postcode = pc.group(1).upper().strip()

        notes = re.search(r'Notes\s*:\s*\n+\s*([^\n]+)', clean, re.IGNORECASE)
        summary = re.search(r'Summary\s*:\s*\n+\s*([^\n]+)', clean, re.IGNORECASE)
        if notes and notes.group(1).strip(): comments = notes.group(1).strip()
        elif summary and summary.group(1).strip(): comments = summary.group(1).strip()

        is_lettings = bool(re.search(r'renter|tenant|letting|rental', clean, re.IGNORECASE))
        transaction_type, buyer_or_renter = ('lettings', 'renter') if is_lettings else ('sales', 'buyer')

    else:
        # Generic / Loop fallback
        fn = re.search(r'(?:Name|First Name|Caller Name|Hi|Hello)[:\s]+([A-Z][a-z]+)', body, re.IGNORECASE)
        if fn: first_name = fn.group(1).strip()
        ln = re.search(r'(?:Surname|Last Name)[:\s]+([A-Z][a-z]+)', body, re.IGNORECASE)
        if ln: last_name = ln.group(1).strip()

        em = re.search(r'^Email(?:\s*Address)?\s*:\s*([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', body, re.IGNORECASE | re.MULTILINE)
        if em: email = em.group(1).strip()
        else:
            body_only = re.sub(r'^(From|To|Cc|Bcc):.*$', '', body, flags=re.IGNORECASE | re.MULTILINE)
            fb = re.search(r'([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', body_only, re.IGNORECASE)
            if fb: email = fb.group(1).strip()

        ph = re.search(r'(?:Phone Daytime|Phone Evening|Contact Number|Phone|Tel|Mobile|Telephone|Caller Number)[:\s]+([\d\s\-+()]{7,})', body, re.IGNORECASE)
        if not ph: ph = re.search(r'(\+\d{1,3}[\s\-]?\d[\d\s\-()]{7,})', body, re.IGNORECASE)
        if not ph: ph = re.search(r'(0\d{3,4}[\s\-]?\d{3}[\s\-]?\d{3,4})', body, re.IGNORECASE)
        if not ph: ph = re.search(r'((?:\d[\d\s\-()]{7,})|(?:\([0-9]\)[\d\s\-]{6,}))', body, re.IGNORECASE)
        if ph: phone = ph.group(1).strip()

        addr = re.search(r'(?:Address|Property Address)[:\s]*([^\n]+)', body, re.IGNORECASE)
        if addr:
            address = addr.group(1).strip()
            sa = re.search(r'(?:\w+\s+){1,3}(?:Road|Street|Avenue|Ave|Lane|Close|Drive|Way|Place|Crescent|Gardens?|Grove|Terrace|Court|Walk|Rise|Hill|Mews)\b', address, re.IGNORECASE)
            if sa: simple_address = re.sub(r'^\d+\s+', '', sa.group(0).strip())

        pc = re.search(r'postcode:\s*([A-Z]{1,2}\d{1,2}\s*\d[A-Z]{2})', body, re.IGNORECASE)
        if not pc: pc = re.search(r'([A-Z]{1,2}\d{1,2}[A-Z]?\s*\d[A-Z]{2})', body, re.IGNORECASE)
        if not pc: pc = re.search(r'\b([A-Z]{1,2}\d{1,2}[A-Z]?)\b', body, re.IGNORECASE)  # district fallback e.g. SE22, SW2
        if pc: postcode = pc.group(1).upper().strip()

        cm = re.search(r'(?:Comment|Enquiry):\s*([^\n]+)', body, re.IGNORECASE)
        if cm: comments = cm.group(1).strip()

        is_lettings = 'LETTENQ' in body or bool(re.search(r'Sales\s+or\s+Lettings[:\s]+Lettings', body, re.IGNORECASE))
        transaction_type, buyer_or_renter = ('lettings', 'renter') if is_lettings else ('sales', 'buyer')

        all_emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', body)
        responsible_agent_email = all_emails[-1] if all_emails else ''

    phone = re.sub(r'\s', '', phone).strip()
    address = address.rstrip(',').strip()

    result = {
        'First Name':              first_name,
        'Last Name':               last_name,
        'Email Address':           email,
        'Phone Number':            phone,
        'Postcode':                postcode,
        'Simple Address':          simple_address,
        'Address':                 address,
        'Customer Type':           buyer_or_renter,
        'Responsible Agent Email': responsible_agent_email,
        'Comments':                comments,
        'transaction_type':        transaction_type,
    }
    print("Parsed email enquiry:", result)
    return result


# Parse the email body from Loop to get enquiry details from the user
def parse_loop_enquiry(body) -> dict:
    if isinstance(body, dict):
        body = str(body)

    body = body.replace("\\n", "\n")
    parts = [p.strip() for p in body.split("\n") if p.strip()]

    result = {}
    for part in parts:
        if ':' in part:
            key, _, value = part.partition(':')
            result[key.strip()] = value.strip()

    print("Parsed Loop Enquiry:", result)
    return result