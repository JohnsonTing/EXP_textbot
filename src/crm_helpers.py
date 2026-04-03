import os
import json
import re
from typing import Dict, List, Optional
import boto3
from datetime import datetime
from openai_helpers import openai_client

from db_helpers import customers_table, conversations_table

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


def update_customer_crm(phone: str, crm_data: Dict):
    """Update only the CRM fields on an existing customer record without touching listings."""
    print(f"[DYNAMO] Updating CRM fields for {phone}: {crm_data}")
    try:
        parts = []
        # Use ExpressionAttributeNames for ALL fields since 'name' and 'email'
        # are reserved words in DynamoDB
        expr_names  = {
            '#contact_name': 'contact_name',
            '#email':        'email',
            '#lead_intent':  'lead_intent',
            '#updated_at':   'updated_at',
        }
        expr_values = {
            ':updated_at': datetime.now().isoformat()
        }

        if crm_data.get('contact_name') and crm_data['contact_name'] != 'Unknown':
            parts.append('#contact_name = :contact_name')
            expr_values[':contact_name'] = crm_data['contact_name']

        if crm_data.get('email') and crm_data['email'] != 'Unknown':
            parts.append('#email = :email')
            expr_values[':email'] = crm_data['email']

        if crm_data.get('lead_intent') and crm_data['lead_intent'] not in ('Unknown', ''):
            parts.append('#lead_intent = :lead_intent')
            expr_values[':lead_intent'] = crm_data['lead_intent']

        if not parts:
            print("[DYNAMO] No CRM fields to update")
            return

        parts.append('#updated_at = :updated_at')
        update_expr = "SET " + ", ".join(parts)

        print(f"[DYNAMO] UpdateExpression: {update_expr}")
        print(f"[DYNAMO] ExpressionAttributeValues: {expr_values}")

        customers_table.update_item(
            Key={'customer_id': phone},
            UpdateExpression=update_expr,
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values
        )
        print(f"[DYNAMO] CRM fields updated successfully")
    except Exception as e:
        print(f"[DYNAMO] ERROR in update_customer_crm: {e}")
        import traceback
        print(traceback.format_exc())

# Parse the email body from Loop to get enquiry details from the user
def parse_loop_enquiry(body) -> dict:
    if isinstance(body, dict):
        body = str(body)
    start = body.index("# New Enquiry") + len("# New Enquiry")
    end = body.index("For further information")
    section = body[start:end]
    print("section", section)

    # Clean and split
    parts = [p.strip() for p in section.split("\\n") if p.strip()]

    result = {}
    for i, part in enumerate(parts):
        if part.endswith(":"):  # it's a key
            key = part.rstrip(":")
            next_part = parts[i + 1] if i + 1 < len(parts) else None
            # If next part is also a key, this field is empty
            if next_part is not None and not next_part.endswith(":"):
                result[key] = next_part
            else:
                continue
 

    return result