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
openai_client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])

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
    response = customers_table.get_item(Key={'customer_id': phone})
    return response.get('Item')


def save_customer(phone: str, enquiry: Dict, listings: List[Dict]):
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


def get_conversation_history(phone: str) -> List[Dict]:
    response = conversations_table.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key('phone_number').eq(phone),
        ScanIndexForward=True  # oldest first
    )
    items = response.get('Items', [])
    return [{'role': item['role'], 'content': item['message']} for item in items]


def save_message(phone: str, role: str, message: str):
    conversations_table.put_item(Item={
        'message_id':   str(uuid.uuid4()),
        'phone_number': phone,
        'role':         role,
        'message':      message,
        'timestamp':    datetime.now().isoformat()
    })


def reset_customer(phone: str):
    """Clears customer record so they can start a new enquiry."""
    customers_table.delete_item(Key={'customer_id': phone})
    # Clear conversations too
    response = conversations_table.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key('phone_number').eq(phone)
    )
    for item in response.get('Items', []):
        conversations_table.delete_item(Key={
            'message_id': item['message_id'],
            'phone_number': phone
        })


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
    logger.info(f"Scraping: {url}")

    try:
        response = requests.get(url, headers=SCRAPE_HEADERS, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        aProperties = []
        for script in soup.find_all('script'):
            if script.string and 'aProperties' in script.string:
                match = re.search(r'const aProperties = (\[.*?\]);', script.string, re.DOTALL)
                if match:
                    aProperties = json.loads(match.group(1))

        if not aProperties:
            logger.warning("No properties found")
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

        logger.info(f"Found {len(results)} properties")
        return results

    except Exception as e:
        logger.error(f"Scraping error: {e}")
        return []


# ─────────────────────────────────────────────
# ChatGPT helpers
# ─────────────────────────────────────────────

def extract_enquiry_details(message: str) -> Dict:
    """Use ChatGPT to pull postcode, bedrooms, price, type from a customer's first message."""
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
        text = re.sub(r'```json|```', '', text).strip()
        return json.loads(text)
    except Exception as e:
        logger.error(f"Failed to parse enquiry details: {e}")
        return {"postcode": "", "bedrooms": 0, "max_price": 0, "prop_type": "Any property type"}


def get_ai_response(phone: str, user_message: str, listings: List[Dict]) -> str:
    """Get ChatGPT response with property listings as context."""

    history = get_conversation_history(phone)

    listings_summary = "\n".join([
        f"- {p['address']} | {p['price']} | {p['bedrooms']} bed {p['property_type']} | {p['status']} | {p['url']}"
        for p in listings[:20]  # Cap at 20 to stay within token limits
    ])

    system_prompt = f"""You are a friendly UK property agent assistant communicating via SMS.
You are helping a customer find properties similar to one they enquired about.

Here are the matching properties we found:
{listings_summary}

Guidelines:
- Keep responses concise (SMS format, under 300 words)
- Be helpful and professional
- When recommending properties, include the price and address
- If asked for more details on a specific property, provide them
- You can suggest the customer reply with 'reset' to start a new search"""

    messages = [{"role": "system", "content": system_prompt}] + history + [{"role": "user", "content": user_message}]

    response = openai_client.chat.completions.create(
        model="gpt-4o",
        max_tokens=500,
        messages=messages
    )

    return response.choices[0].message.content.strip()


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
    params = dict(p.split('=', 1) for p in raw.split('&') if '=' in p)
    from urllib.parse import unquote_plus
    return {
        'body': unquote_plus(params.get('Body', '')),
        'from': unquote_plus(params.get('From', ''))
    }


# ─────────────────────────────────────────────
# Lambda handler
# ─────────────────────────────────────────────

def handler(event, context):
    # Health check
    if event.get('requestContext', {}).get('http', {}).get('method') == 'GET':
        return {'statusCode': 200, 'body': 'Property bot is running ✅'}

    parsed   = parse_body(event)
    phone    = parsed['from']
    message  = parsed['body'].strip()

    logger.info(f"Message from {phone}: {message}")

    # Handle reset
    if message.lower() == 'reset':
        reset_customer(phone)
        return {
            'statusCode': 200,
            'headers': {'Content-Type': 'text/xml'},
            'body': twiml_response("🔄 Conversation reset! Send me a new property enquiry to get started.")
        }

    try:
        customer = get_customer(phone)
        

        # ── New customer: extract details, scrape, save ──
        if not customer:
            save_message(phone, 'user', message)

            # Tell the customer we're working on it
            # (Note: Twilio only gets one response per webhook, so we respond after scraping)

            enquiry = extract_enquiry_details(message)
            logger.info(f"Extracted enquiry: {enquiry}")

            if not enquiry.get('postcode'):
                reply = "Hi! I'm your property assistant. Please send me your enquiry including the postcode or area, number of bedrooms, and budget. E.g. 'Looking for a 2 bed flat in M4 under £250,000'"
                save_message(phone, 'assistant', reply)
                return {
                    'statusCode': 200,
                    'headers': {'Content-Type': 'text/xml'},
                    'body': twiml_response(reply)
                }

            # Run the scrape
            listings = scrape_exp(
                postcode=enquiry['postcode'],
                max_price=enquiry.get('max_price', 0),
                min_beds=enquiry.get('bedrooms', 0),
                prop_type=enquiry.get('prop_type', 'Any property type')
            )

            if not listings:
                reply = f"I searched for properties in {enquiry['postcode']} but couldn't find any matching results. Could you try a different postcode or broaden your criteria?"
                save_message(phone, 'assistant', reply)
                return {
                    'statusCode': 200,
                    'headers': {'Content-Type': 'text/xml'},
                    'body': twiml_response(reply)
                }

            # Save customer + listings
            save_customer(phone, enquiry, listings)

            # Get AI to introduce the results
            intro_prompt = f"Introduce yourself and summarise the {len(listings)} properties you found for their enquiry. Give the top 3 most relevant ones with prices."
            reply = get_ai_response(phone, intro_prompt, listings)

        # ── Returning customer: answer their question ──
        else:
            save_message(phone, 'user', message)
            listings = json.loads(customer.get('scraped_listings', '[]'))
            reply = get_ai_response(phone, message, listings)

        save_message(phone, 'assistant', reply)

        # Split long replies for SMS
        if len(reply) <= 1600:
            sms_messages = [reply]
        else:
            chunks = [reply[i:i+1550] for i in range(0, len(reply), 1550)]
            sms_messages = [f"({i+1}/{len(chunks)}) {chunk}" for i, chunk in enumerate(chunks)]

        return {
            'statusCode': 200,
            'headers': {'Content-Type': 'text/xml'},
            'body': twiml_response(sms_messages)
        }

    except Exception as e:
        logger.error(f"Error: {e}")
        return {
            'statusCode': 200,
            'headers': {'Content-Type': 'text/xml'},
            'body': twiml_response("Sorry, something went wrong. Please try again in a moment.")
        }