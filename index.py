import json
import re
import time
import uuid
import logging
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from typing import Dict, List, Optional

import boto3
from openai import OpenAI

# ─────────────────────────────────────────────
# Setup
# ─────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

import os
from twilio.rest import Client as TwilioClient

openai_client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])
twilio_client = TwilioClient(os.environ['TWILIO_ACCOUNT_SID'], os.environ['TWILIO_AUTH_TOKEN'])
TWILIO_FROM = os.environ['TWILIO_FROM_NUMBER']

dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
customers_table     = dynamodb.Table('Customers')
conversations_table = dynamodb.Table('Conversations')

SCRAPE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.5",
    "Connection": "keep-alive",
}


# ─────────────────────────────────────────────
# DynamoDB helpers
# ─────────────────────────────────────────────

def get_customer(phone: str) -> Optional[Dict]:
    print(f"[DYNAMO] Getting customer record for: {phone}")
    try:
        response = customers_table.get_item(Key={'customer_id': phone})
        item = response.get('Item')
        print(f"[DYNAMO] Customer found: {item is not None}")
        return item
    except Exception as e:
        print(f"[DYNAMO] ERROR in get_customer: {e}")
        raise


def save_customer(phone: str, enquiry: Dict, listings: List[Dict]):
    print(f"[DYNAMO] Saving customer: {phone} with {len(listings)} listings")
    try:
        customers_table.put_item(Item={
            'customer_id':        phone,
            # ── Contact info ──
            'contact_name':       enquiry.get('contact_name', ''),
            'email':              enquiry.get('email', ''),
            'tags':               enquiry.get('tags', []),
            # ── Lead details ──
            'lead_intent':        enquiry.get('lead_intent', ''),
            'summary':            enquiry.get('summary', ''),
            'property_in_mind':   enquiry.get('property_in_mind', ''),
            # ── Search criteria ──
            'enquiry_postcode':   enquiry.get('postcode', ''),
            'enquiry_bedrooms':   enquiry.get('bedrooms', 0),
            'enquiry_max_price':  enquiry.get('max_price', 0),
            'enquiry_prop_type':  enquiry.get('prop_type', 'Any property type'),
            'scraped_listings':   json.dumps(listings),
            'created_at':         datetime.now().isoformat(),
            'updated_at':         datetime.now().isoformat(),
            'status':             'active'
        })
        print(f"[DYNAMO] Customer saved successfully")
    except Exception as e:
        print(f"[DYNAMO] ERROR in save_customer: {e}")
        raise


def get_conversation_history(phone: str) -> List[Dict]:
    print(f"[DYNAMO] Fetching conversation history for: {phone}")
    try:
        response = conversations_table.query(
            IndexName='phone_number-timestamp-index',
            KeyConditionExpression=boto3.dynamodb.conditions.Key('phone_number').eq(phone),
            ScanIndexForward=True
        )
        items = response.get('Items', [])
        print(f"[DYNAMO] Found {len(items)} messages in history")
        return [{'role': item['role'], 'content': item['message']} for item in items]
    except Exception as e:
        print(f"[DYNAMO] ERROR in get_conversation_history: {e}")
        return []  # Degrade gracefully — lose history but don't crash


def save_message(phone: str, role: str, message: str):
    print(f"[DYNAMO] Saving message | role={role} | preview={message[:80]}")
    try:
        conversations_table.put_item(Item={
            'message_id':   str(uuid.uuid4()),
            'phone_number': phone,
            'role':         role,
            'message':      message,
            'timestamp':    datetime.now().isoformat()
        })
        print(f"[DYNAMO] Message saved")
    except Exception as e:
        print(f"[DYNAMO] ERROR in save_message: {e}")
        # Non-fatal — log and continue


def reset_customer(phone: str):
    print(f"[RESET] Resetting customer: {phone}")
    try:
        customers_table.delete_item(Key={'customer_id': phone})
    except Exception as e:
        print(f"[DYNAMO] ERROR deleting customer: {e}")

    try:
        response = conversations_table.query(
            IndexName='phone_number-timestamp-index',
            KeyConditionExpression=boto3.dynamodb.conditions.Key('phone_number').eq(phone)
        )
        for item in response.get('Items', []):
            conversations_table.delete_item(Key={
                'message_id': item['message_id'],
                'phone_number': phone
            })
        print(f"[RESET] Reset complete")
    except Exception as e:
        print(f"[DYNAMO] ERROR clearing conversation history: {e}")


# ─────────────────────────────────────────────
# Scraper
# ─────────────────────────────────────────────

def extract_postcode(address: str) -> Optional[str]:
    match = re.search(r'\b([A-Z]{1,2}\d{1,2}[A-Z]?\s?\d[A-Z]{2})\b', address.upper())
    return match.group(1) if match else None


def build_search_url(postcode: str, max_price: int, min_beds: int, prop_type: str, radius: float = 4) -> str:
    from urllib.parse import quote_plus
    location = postcode.strip()
    if re.match(r'^[A-Za-z]{1,2}\d', location):
        location = location.upper().replace(' ', '')
    else:
        location = quote_plus(location)
    url = f"https://exp.uk.com/properties-for-sale/results/?action=search&location={location}&search_radii={radius}"

    url += f"&max-price={max_price}" if max_price > 0 else "&max-price=0"

    prop_type_map = {
        "any property type": "0",
        "semi-detached house": "3",
        "detached house": "4",
        "flat": "6",
        "bungalow": "8",
        "cottage": "10"
    }
    url += f"&property-type={prop_type_map.get(prop_type.lower(), '0')}"
    url += f"&min-bedrooms={min_beds if min_beds < 6 else 0}"
    return url


def scrape_exp(postcode: str, max_price: int = 0, min_beds: int = 0, prop_type: str = "Any property type", radius: float = 4) -> List[Dict]:
    url = build_search_url(postcode, max_price, min_beds, prop_type, radius)
    print(f"[SCRAPER] Built URL: {url}")

    try:
        print(f"[SCRAPER] Sending HTTP request...")
        response = requests.get(url, headers=SCRAPE_HEADERS, timeout=15)
        print(f"[SCRAPER] Response status: {response.status_code}")
        response.raise_for_status()
    except requests.exceptions.Timeout:
        print(f"[SCRAPER] ERROR: Request timed out after 15s")
        return []
    except requests.exceptions.HTTPError as e:
        print(f"[SCRAPER] ERROR: HTTP error {e.response.status_code}: {e}")
        return []
    except requests.exceptions.RequestException as e:
        print(f"[SCRAPER] ERROR: Request failed: {e}")
        return []

    try:
        soup = BeautifulSoup(response.text, 'html.parser')
        print(f"[SCRAPER] Page parsed, searching for aProperties...")

        aProperties = []
        for script in soup.find_all('script'):
            if script.string and 'aProperties' in script.string:
                match = re.search(r'const aProperties = (\[.*?\]);', script.string, re.DOTALL)
                if match:
                    aProperties = json.loads(match.group(1))
                    print(f"[SCRAPER] Found aProperties block with {len(aProperties)} items")

        if not aProperties:
            print(f"[SCRAPER] No aProperties found in page scripts")
            return []

        results = []
        for p in aProperties:
            try:
                price_text = BeautifulSoup(p['html_price'], 'html.parser').get_text()
                price_match = re.search(r'£[\d,]+', price_text)
                type_match = re.search(r'\d+\s*bedroom[s]?\s+(.+)', p['description'], re.IGNORECASE)
                prop_type_clean = type_match.group(1).strip() if type_match else "Unknown"
                postcode_clean = extract_postcode(p['display_address']) or ''

                results.append({
                    'address':       p['display_address'],
                    'status':        "For Sale" if p['status_code'] == 2 else "Sold STC",
                    'price':         price_match.group(0) if price_match else 'POA',
                    'postcode':      postcode_clean,
                    'bedrooms':      p['bedrooms'],
                    'bathrooms':     p['bathrooms'],
                    'receptions':    p['receptionrooms'],
                    'property_type': prop_type_clean,
                    'agent_name':    p['agent_name'],
                    'agent_phone':   p['agent_phone'],
                    'agent_email':   p['agent_email'],
                    'url':           "https://exp.uk.com" + p['property_url_part'],
                })
            except Exception as e:
                print(f"[SCRAPER] WARNING: Skipping malformed property entry: {e}")
                continue

        print(f"[SCRAPER] Returning {len(results)} properties")
        return results

    except Exception as e:
        print(f"[SCRAPER] ERROR parsing page content: {e}")
        return []


# ─────────────────────────────────────────────
# OpenAI helpers
# ─────────────────────────────────────────────

def extract_enquiry_details(message: str) -> Dict:
    print(f"[OPENAI] Extracting enquiry details from message: {message[:100]}")
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            max_tokens=300,
            messages=[
                {
                    "role": "system",
                    "content": """Extract property search details from the message.
                    Return ONLY valid JSON with these exact keys:
                    - contact_name (string): the person's name if mentioned, else empty string
                    - email (string): email address if mentioned, else empty string
                    - tags (array of strings): relevant tags e.g. ["first-time buyer", "investor", "upsizing", "downsizing", "relocation"] — infer from context, empty array if unclear
                    - lead_intent (string): one of 'Buying', 'Renting', 'Investing', 'Unknown'
                    - summary (string): a one-sentence summary of what the prospect is looking for
                    - property_in_mind (string): any specific property or address they've mentioned, else empty string
                    - postcode (string): the location to search — can be a full postcode (e.g. 'SW1A 1AA'), a postcode district (e.g. 'SE8', 'M4'), or a plain area name (e.g. 'Wimbledon', 'Essex', 'Greenwich'). Use whatever the user said as-is. Empty string if no location found.
                    - bedrooms (integer, 0 if not mentioned)
                    - max_price (integer, 0 if not mentioned, no commas)
                    - prop_type (string, one of: 'Any property type', 'Semi-detached house', 'Detached house', 'Flat', 'Bungalow', 'Cottage')
                    If you cannot find any location at all, set postcode to empty string."""
                },
                {"role": "user", "content": message}
            ]
        )
        text = response.choices[0].message.content.strip()
        print(f"[OPENAI] Raw response: {text}")
        text = re.sub(r'```json|```', '', text).strip()
        parsed = json.loads(text)
        print(f"[OPENAI] Parsed enquiry: {parsed}")
        return parsed
    except json.JSONDecodeError as e:
        print(f"[OPENAI] ERROR: Failed to parse JSON response: {e}")
        return {"postcode": "", "bedrooms": 0, "max_price": 0, "prop_type": "Any property type"}
    except Exception as e:
        print(f"[OPENAI] ERROR in extract_enquiry_details: {e}")
        return {"postcode": "", "bedrooms": 0, "max_price": 0, "prop_type": "Any property type"}


def detect_new_search(message: str, current_postcode: str) -> Dict:
    print(f"[OPENAI] Detecting if new search requested | current_postcode={current_postcode}")
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            max_tokens=300,
            messages=[
                {
                    "role": "system",
                    "content": f"""You are a UK property search assistant. The user is currently viewing listings in: {current_postcode}.
Determine if the user's message is requesting a search in a DIFFERENT area/location.

Return ONLY valid JSON with:
- new_search (boolean): true if they want listings from a different area, false otherwise
- postcode (string): the new location if new_search is true — can be a full postcode, postcode district, or plain area name like 'Wimbledon' or 'Essex'. Use whatever the user said as-is. Empty string if new_search is false.
- bedrooms (integer): number of bedrooms if mentioned, else 0
- max_price (integer): max price if mentioned, else 0
- prop_type (string): one of 'Any property type', 'Semi-detached house', 'Detached house', 'Flat', 'Bungalow', 'Cottage'

Examples that ARE a new search: "what about se1?", "can you check greenwich", "show me places in e3", "actually look in lewisham"
Examples that are NOT a new search: "tell me more about the first one", "which has a garden?", "what is the cheapest?"
"""
                },
                {"role": "user", "content": message}
            ]
        )
        text = response.choices[0].message.content.strip()
        text = re.sub(r'```json|```', '', text).strip()
        parsed = json.loads(text)
        print(f"[OPENAI] New search detection result: {parsed}")
        return parsed
    except json.JSONDecodeError as e:
        print(f"[OPENAI] ERROR: Failed to parse JSON in detect_new_search: {e}")
        return {"new_search": False, "postcode": "", "bedrooms": 0, "max_price": 0, "prop_type": "Any property type"}
    except Exception as e:
        print(f"[OPENAI] ERROR in detect_new_search: {e}")
        return {"new_search": False, "postcode": "", "bedrooms": 0, "max_price": 0, "prop_type": "Any property type"}


def get_ai_response(phone: str, user_message: str, listings: List[Dict]) -> str:
    print(f"[OPENAI] Getting AI response for {phone} | message: {user_message[:100]}")
    try:
        history = get_conversation_history(phone)
        print(f"[OPENAI] Using {len(history)} messages of history and {len(listings)} listings")

        listings_summary = "\n".join([
            f"- {p['address']} | {p['price']} | {p['bedrooms']} bed {p['property_type']} | {p['status']} | {p['url']}"
            for p in listings[:20]
        ])

        system_prompt = f"""You are a friendly UK property agent assistant communicating via SMS.
You are helping a customer find properties for a buyer.

Here are ALL the matching properties we found:
{listings_summary}

Guidelines:
- List ALL properties above, not just a few — the customer wants to see everything available
- For each property include: address, price, number of beds, property type, and URL
- Be helpful and professional
- If asked for more details on a specific property, provide them
- You can suggest the customer reply with 'reset' to start a new search"""

        messages = [{"role": "system", "content": system_prompt}] + history + [{"role": "user", "content": user_message}]

        response = openai_client.chat.completions.create(
            model="gpt-4o",
            max_tokens=2000,
            messages=messages
        )

        reply = response.choices[0].message.content.strip()
        print(f"[OPENAI] AI reply: {reply[:200]}")
        return reply
    except Exception as e:
        print(f"[OPENAI] ERROR in get_ai_response: {e}")
        raise


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


# ─────────────────────────────────────────────
# Send SMS directly via Twilio API
# ─────────────────────────────────────────────

def send_sms(to: str, body: str):
    print(f"[TWILIO] Sending SMS to {to} | preview: {body[:80]}")
    try:
        if len(body) <= 1600:
            messages = [body]
        else:
            chunks = [body[i:i+1550] for i in range(0, len(body), 1550)]
            messages = [f"({i+1}/{len(chunks)}) {chunk}" for i, chunk in enumerate(chunks)]

        for msg in messages:
            twilio_client.messages.create(to=to, from_=TWILIO_FROM, body=msg)
        print(f"[TWILIO] Sent {len(messages)} message(s)")
    except Exception as e:
        print(f"[TWILIO] ERROR sending SMS to {to}: {e}")
        raise


# ─────────────────────────────────────────────
# TwiML response builder
# ─────────────────────────────────────────────

def twiml_response(messages):
    if isinstance(messages, str):
        messages = [messages]
    msg_tags = "".join(f"<Message>{m}</Message>" for m in messages)
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{msg_tags}</Response>'


def parse_body(event):
    raw = event.get('body', '')
    if event.get('isBase64Encoded'):
        import base64
        raw = base64.b64decode(raw).decode('utf-8')
    else:
        print("Not Base64 Encoded")
        return(event.body)
    content_type = ''
    headers = event.get('headers', {})
    for key in headers:
        if key.lower() == 'content-type':
            content_type = headers[key]
            break

    # Multipart form data
    if 'multipart/form-data' in content_type:
        import re
        boundary = re.search(r'boundary=([^\s;]+)', content_type)
        if boundary:
            boundary_str = boundary.group(1)
            fields = {}
            parts = raw.split('--' + boundary_str)
            for part in parts:
                if 'Content-Disposition' in part:
                    match = re.search(r'name="([^"]+)"\s*\r?\n\r?\n(.*)', part, re.DOTALL)
                    if match:
                        fields[match.group(1)] = match.group(2).strip()
            result = {
                'body': fields.get('message', ''),
                'from': fields.get('phone', '')
            }
        else:
            result = {'body': '', 'from': ''}

    # URL-encoded form data (original path)
    else:
        from urllib.parse import unquote_plus
        params = dict(p.split('=', 1) for p in raw.split('&') if '=' in p)
        result = {
            'body': unquote_plus(params.get('Body', '')),
            'from': unquote_plus(params.get('From', ''))
        }

    print(f"[PARSE] Parsed -> from={result['from']} | body={result['body']}")
    return result
# ─────────────────────────────────────────────
# Lambda handler
# ─────────────────────────────────────────────

def handler(event, context):
    print(f"[HANDLER] Invoked. Event keys: {list(event.keys())}")
    print("event:", event)
    #______________________________________________
    # Health check
    #______________________________________________
    if event.get('requestContext', {}).get('http', {}).get('method') == 'GET':
        print(f"[HANDLER] Health check request")
        return {'statusCode': 200, 'body': 'Property bot is running ✅'}
    
    if event.get('isBase64Encoded') == False:
        print(f"[HANDLER] Not base64 encoded, using body directly")
        parsed = event.get("body")
        parsed = json.loads(parsed)
        phone = parsed.get("phone")
        message = parsed.get("message")
        print(f"[HANDLER] Parsed -> from={phone} | body={message}")
    else:
        parsed  = parse_body(event)
        phone   = parsed['from']
        print("")
        message = parsed['body'].strip()

   


    print(f"[HANDLER] phone={phone!r} | message={message!r}")

    # ─────────────────────────────────────────────
    # Manual send route — POST /send
    # ─────────────────────────────────────────────
    path = event.get('requestContext', {}).get('http', {}).get('path', '')
    if path == '/send':
        print(f"[HANDLER] Manual send route triggered")
        try:
            print(f"[HANDLER] Manual send | phone={phone!r} | message={message!r}")

            if not phone or not message:
                return {
                    'statusCode': 400,
                    'body': json.dumps({'error': 'phone and message are required'})
                }

            send_sms(phone, message)
            save_message(phone, 'assistant', message)

            print(f"[HANDLER] Manual send complete to {phone}")
            return {
                'statusCode': 200,
                'headers': {
                    'Access-Control-Allow-Origin': '*',  # or your specific Replit URL
                    'Access-Control-Allow-Headers': 'Content-Type',
                    'Access-Control-Allow-Methods': 'POST, OPTIONS'
                },
                'body': json.dumps({'success': True, 'phone': phone, 'message': message})
            }

        except Exception as e:
            print(f"[HANDLER] ERROR in manual send: {e}")
            import traceback
            print(traceback.format_exc())
            return {
                'statusCode': 500,
                'body': json.dumps({'error': 'Internal server error'})
            }
    # ─────────────────────────────────────────────
    # Automatic AI response to SMS messages
    # ─────────────────────────────────────────────
    if not phone:
        print(f"[HANDLER] ERROR: Empty phone number, cannot proceed")
        return {
            'statusCode': 200,
            'headers': {'Content-Type': 'text/xml'},
            'body': twiml_response("Could not identify your phone number.")
        }

    # Handle reset
    if message.lower() == 'reset':
        print(f"[HANDLER] Reset requested by {phone}")
        reset_customer(phone)
        return {
            'statusCode': 200,
            'headers': {'Content-Type': 'text/xml'},
            'body': twiml_response("🔄 Conversation reset! Send me a new property enquiry to get started.")
        }

    try:
        print(f"[HANDLER] Checking if customer exists...")
        customer = get_customer(phone)

        if not customer:
            print(f"[HANDLER] New customer flow")
            save_message(phone, 'user', message)

            # Immediately acknowledge so Twilio doesn't time out
            ack = "Got it! Searching for properties now, I'll send you the results in a moment... 🏡"
            save_message(phone, 'assistant', ack)
            send_sms(phone, ack)

            print(f"[HANDLER] Extracting enquiry details...")
            enquiry = extract_enquiry_details(message)
            logger.info(f"Extracted enquiry: {enquiry}")

            if not enquiry.get('postcode'):
                print(f"[HANDLER] No postcode found, asking user")
                reply = "Hi! I'm your property assistant. Please send me your enquiry including the postcode or area, number of bedrooms, and budget. E.g. 'Looking for a 2 bed flat in M4 under £250,000'"
                save_message(phone, 'assistant', reply)
                send_sms(phone, reply)
                return {
                    'statusCode': 200,
                    'headers': {'Content-Type': 'text/xml'},
                    'body': '<Response></Response>'
                }

            print(f"[HANDLER] Scraping listings for postcode={enquiry['postcode']}...")
            listings = scrape_exp(
                postcode=enquiry['postcode'],
                max_price=enquiry.get('max_price', 0),
                min_beds=enquiry.get('bedrooms', 0),
                prop_type=enquiry.get('prop_type', 'Any property type')
            )

            if not listings:
                print(f"[HANDLER] No listings found")
                reply = f"I searched for properties in {enquiry['postcode']} but couldn't find any matching results. Could you try a different postcode or broaden your criteria?"
                save_message(phone, 'assistant', reply)
                send_sms(phone, reply)
                return {
                    'statusCode': 200,
                    'headers': {'Content-Type': 'text/xml'},
                    'body': '<Response></Response>'
                }

            print(f"[HANDLER] Saving customer and {len(listings)} listings...")
            save_customer(phone, enquiry, listings)

            print(f"[HANDLER] Getting AI intro response...")
            intro_prompt = f"Introduce yourself briefly, then list ALL {len(listings)} properties you found with their address, price, bed count, type, and URL. Do not summarise — list every single one."
            reply = get_ai_response(phone, intro_prompt, listings)
            save_message(phone, 'assistant', reply)
            send_sms(phone, reply)

            # ── Send CRM follow-up if info is missing ──
            missing = get_missing_crm_fields(enquiry)
            followup = build_crm_followup_message(missing)
            if followup:
                print(f"[HANDLER] Sending CRM follow-up for missing fields: {missing}")
                save_message(phone, 'assistant', followup)
                send_sms(phone, followup)

            return {
                'statusCode': 200,
                'headers': {'Content-Type': 'text/xml'},
                'body': '<Response></Response>'
            }

        else:
            print(f"[HANDLER] Returning customer flow")
            save_message(phone, 'user', message)
            current_postcode = customer.get('enquiry_postcode', '')

            # ── Check if user is replying to CRM follow-up ──
            missing = get_missing_crm_fields(customer)
            if missing:
                crm_data = extract_crm_reply(message, missing)
                if any(crm_data.get(f) for f in ['contact_name', 'email', 'lead_intent']):
                    print(f"[HANDLER] CRM reply detected, updating fields: {crm_data}")
                    update_customer_crm(phone, crm_data)
                    # Check if anything is still missing after this update
                    updated_customer = {**customer, **crm_data}
                    still_missing = get_missing_crm_fields(updated_customer)
                    if still_missing:
                        followup = build_crm_followup_message(still_missing)
                        save_message(phone, 'assistant', followup)
                        send_sms(phone, followup)
                    else:
                        thanks = "Thanks, I've got all your details! Feel free to ask me anything about the listings. 😊"
                        save_message(phone, 'assistant', thanks)
                        send_sms(phone, thanks)
                    return {
                        'statusCode': 200,
                        'headers': {'Content-Type': 'text/xml'},
                        'body': '<Response></Response>'
                    }

            # Check if user is asking to search a new area
            detection = detect_new_search(message, current_postcode)

            if detection.get('new_search') and detection.get('postcode'):
                new_postcode = detection['postcode']
                print(f"[HANDLER] New area requested: {new_postcode}")

                ack = f"Sure! Searching for properties in {new_postcode} now... 🏡"
                save_message(phone, 'assistant', ack)
                send_sms(phone, ack)

                listings = scrape_exp(
                    postcode=new_postcode,
                    max_price=detection.get('max_price', 0),
                    min_beds=detection.get('bedrooms', 0),
                    prop_type=detection.get('prop_type', 'Any property type')
                )

                if not listings:
                    reply = f"I searched in {new_postcode} but couldn't find any matching results. Try a different area or broaden your criteria?"
                    save_message(phone, 'assistant', reply)
                    send_sms(phone, reply)
                    return {
                        'statusCode': 200,
                        'headers': {'Content-Type': 'text/xml'},
                        'body': '<Response></Response>'
                    }

                enquiry = {
                    'postcode':  new_postcode,
                    'bedrooms':  detection.get('bedrooms', customer.get('enquiry_bedrooms', 0)),
                    'max_price': detection.get('max_price', customer.get('enquiry_max_price', 0)),
                    'prop_type': detection.get('prop_type', customer.get('enquiry_prop_type', 'Any property type')),
                }
                save_customer(phone, enquiry, listings)
                print(f"[HANDLER] Updated customer with {len(listings)} new listings for {new_postcode}")

                intro_prompt = f"The user asked to search a new area: {new_postcode}. List ALL {len(listings)} properties found with address, price, bed count, type, and URL. Do not summarise — list every single one."
                reply = get_ai_response(phone, intro_prompt, listings)
                save_message(phone, 'assistant', reply)
                send_sms(phone, reply)

                # Re-check CRM follow-up in case still missing
                updated_customer = get_customer(phone) or customer
                still_missing = get_missing_crm_fields(updated_customer)
                followup = build_crm_followup_message(still_missing)
                if followup:
                    save_message(phone, 'assistant', followup)
                    send_sms(phone, followup)

                return {
                    'statusCode': 200,
                    'headers': {'Content-Type': 'text/xml'},
                    'body': '<Response></Response>'
                }

            else:
                # Normal follow-up question about existing listings
                listings = json.loads(customer.get('scraped_listings', '[]'))
                print(f"[HANDLER] Loaded {len(listings)} listings from customer record")
                reply = get_ai_response(phone, message, listings)

        save_message(phone, 'assistant', reply)

        if len(reply) <= 1600:
            sms_messages = [reply]
        else:
            chunks = [reply[i:i+1550] for i in range(0, len(reply), 1550)]
            sms_messages = [f"({i+1}/{len(chunks)}) {chunk}" for i, chunk in enumerate(chunks)]

        print(f"[HANDLER] Sending {len(sms_messages)} SMS message(s)")
        return {
            'statusCode': 200,
            'headers': {'Content-Type': 'text/xml'},
            'body': twiml_response(sms_messages)
        }

    except Exception as e:
        print(f"[HANDLER] UNCAUGHT ERROR: {e}")
        import traceback
        print(traceback.format_exc())
        logger.error(f"Error: {e}")
        return {
            'statusCode': 200,
            'headers': {'Content-Type': 'text/xml'},
            'body': twiml_response("Sorry, something went wrong. Please try again in a moment.")
        }