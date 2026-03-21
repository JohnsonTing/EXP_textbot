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
TWILIO_FROM = os.environ['TWILIO_FROM_NUMBER']  # e.g. +14155551234

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
    response = customers_table.get_item(Key={'customer_id': phone})
    item = response.get('Item')
    print(f"[DYNAMO] Customer found: {item is not None}")
    return item


def save_customer(phone: str, enquiry: Dict, listings: List[Dict]):
    print(f"[DYNAMO] Saving customer: {phone} with {len(listings)} listings")
    customers_table.put_item(Item={
        'customer_id':        phone,
        'enquiry_postcode':   enquiry.get('postcode', ''),
        'enquiry_bedrooms':   enquiry.get('bedrooms', 0),
        'enquiry_max_price':  enquiry.get('max_price', 0),
        'enquiry_prop_type':  enquiry.get('prop_type', 'Any property type'),
        'scraped_listings':   json.dumps(listings),
        'created_at':         datetime.now().isoformat(),
        'status':             'active'
    })
    print(f"[DYNAMO] Customer saved successfully")


def get_conversation_history(phone: str) -> List[Dict]:
    print(f"[DYNAMO] Fetching conversation history for: {phone}")
    response = conversations_table.query(
        IndexName='phone_number-timestamp-index',
        KeyConditionExpression=boto3.dynamodb.conditions.Key('phone_number').eq(phone),
        ScanIndexForward=True
    )
    items = response.get('Items', [])
    print(f"[DYNAMO] Found {len(items)} messages in history")
    return [{'role': item['role'], 'content': item['message']} for item in items]


def save_message(phone: str, role: str, message: str):
    print(f"[DYNAMO] Saving message | role={role} | preview={message[:80]}")
    conversations_table.put_item(Item={
        'message_id':   str(uuid.uuid4()),
        'phone_number': phone,
        'role':         role,
        'message':      message,
        'timestamp':    datetime.now().isoformat()
    })
    print(f"[DYNAMO] Message saved")


def reset_customer(phone: str):
    print(f"[RESET] Resetting customer: {phone}")
    customers_table.delete_item(Key={'customer_id': phone})
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


# ─────────────────────────────────────────────
# Scraper
# ─────────────────────────────────────────────

def extract_postcode(address: str) -> Optional[str]:
    match = re.search(r'\b([A-Z]{1,2}\d{1,2}[A-Z]?\s?\d[A-Z]{2})\b', address.upper())
    return match.group(1) if match else None


def build_search_url(postcode: str, max_price: int, min_beds: int, prop_type: str, radius: float = 4) -> str:
    clean_postcode = postcode.strip().upper().replace(' ', '')
    url = f"https://exp.uk.com/properties-for-sale/results/?action=search&location={clean_postcode}&search_radii={radius}"

    if max_price > 0:
        url += f"&max-price={max_price}"
    else:
        url += "&max-price=0"

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

        print(f"[SCRAPER] Returning {len(results)} properties")
        return results

    except Exception as e:
        print(f"[SCRAPER] ERROR: {e}")
        logger.error(f"Scraping error: {e}")
        return []


# ─────────────────────────────────────────────
# ChatGPT helpers
# ─────────────────────────────────────────────

def extract_enquiry_details(message: str) -> Dict:
    print(f"[OPENAI] Extracting enquiry details from message: {message[:100]}")
    response = openai_client.chat.completions.create(
        model="gpt-4o",
        max_tokens=300,
        messages=[
            {
                "role": "system",
                "content": """Extract property search details from the message. 
                Return ONLY valid JSON with these keys:
                - postcode (string, UK postcode or area e.g. 'M4' or 'SW11')
                - bedrooms (integer, 0 if not mentioned)
                - max_price (integer, 0 if not mentioned, no commas)
                - prop_type (string, one of: 'Any property type', 'Semi-detached house', 'Detached house', 'Flat', 'Bungalow', 'Cottage')
                If you cannot find a postcode or area, set postcode to empty string."""
            },
            {"role": "user", "content": message}
        ]
    )
    try:
        text = response.choices[0].message.content.strip()
        print(f"[OPENAI] Raw response: {text}")
        text = re.sub(r'```json|```', '', text).strip()
        parsed = json.loads(text)
        print(f"[OPENAI] Parsed enquiry: {parsed}")
        return parsed
    except Exception as e:
        print(f"[OPENAI] ERROR parsing enquiry: {e}")
        logger.error(f"Failed to parse enquiry details: {e}")
        return {"postcode": "", "bedrooms": 0, "max_price": 0, "prop_type": "Any property type"}


def get_ai_response(phone: str, user_message: str, listings: List[Dict]) -> str:
    print(f"[OPENAI] Getting AI response for {phone} | message: {user_message[:100]}")
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


# ─────────────────────────────────────────────
# Send SMS directly via Twilio API
# ─────────────────────────────────────────────

def send_sms(to: str, body: str):
    """Send an SMS proactively via Twilio REST API (not via TwiML webhook return)."""
    print(f"[TWILIO] Sending SMS to {to} | preview: {body[:80]}")
    if len(body) <= 1600:
        messages = [body]
    else:
        chunks = [body[i:i+1550] for i in range(0, len(body), 1550)]
        messages = [f"({i+1}/{len(chunks)}) {chunk}" for i, chunk in enumerate(chunks)]

    for msg in messages:
        twilio_client.messages.create(to=to, from_=TWILIO_FROM, body=msg)
    print(f"[TWILIO] Sent {len(messages)} message(s)")


# ─────────────────────────────────────────────
# TwiML response builder
# ─────────────────────────────────────────────

def twiml_response(messages):
    if isinstance(messages, str):
        messages = [messages]
    msg_tags = "".join(f"<Message>{m}</Message>" for m in messages)
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{msg_tags}</Response>'


def parse_body(event):
    print(f"[PARSE] Raw event body: {str(event.get('body', ''))[:300]}")
    raw = event.get('body', '')
    if event.get('isBase64Encoded'):
        import base64
        raw = base64.b64decode(raw).decode('utf-8')
        print(f"[PARSE] Decoded base64 body: {raw[:300]}")
    params = dict(p.split('=', 1) for p in raw.split('&') if '=' in p)
    from urllib.parse import unquote_plus
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

    # Health check
    if event.get('requestContext', {}).get('http', {}).get('method') == 'GET':
        print(f"[HANDLER] Health check request")
        return {'statusCode': 200, 'body': 'Property bot is running ✅'}

    parsed  = parse_body(event)
    phone   = parsed['from']
    message = parsed['body'].strip()

    print(f"[HANDLER] phone={phone!r} | message={message!r}")

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

            # ── Immediately acknowledge so Twilio doesn't time out ──
            ack = "Got it! Searching for properties now, I'll send you the results in a moment... 🏡"
            save_message(phone, 'assistant', ack)
            print(f"[HANDLER] Sending acknowledgement via TwiML...")
            # We return the ack immediately, then Twilio closes the webhook.
            # The rest of the work happens before we return — Lambda keeps running.
            # To truly async this you'd need a second Lambda, but for now we
            # send the ack via Twilio API and return an empty TwiML response.
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
            return {
                'statusCode': 200,
                'headers': {'Content-Type': 'text/xml'},
                'body': '<Response></Response>'
            }

        else:
            print(f"[HANDLER] Returning customer flow")
            save_message(phone, 'user', message)
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