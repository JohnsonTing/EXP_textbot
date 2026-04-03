from openai import OpenAI
import json
import re
import os
from typing import Dict, List, Optional
import boto3
import time
from db_helpers import get_conversation_history, get_customer

tools = [
    {
        "type": "function",
        "function": {
            "name": "search_properties",
            "description": "Search for properties based on location and criteria. Call this when the user wants to find properties in a specific area.",
            "parameters": {
                "type": "object",
                "properties": {
                    "postcode": {
                        "type": "string",
                        "description": "UK postcode, postcode district (e.g. SW17, SE8), or area name (e.g. Wimbledon, Greenwich)"
                    },
                    "max_price": {
                        "type": "integer",
                        "description": "Maximum price in GBP, 0 if not mentioned"
                    },
                    "min_beds": {
                        "type": "integer",
                        "description": "Minimum number of bedrooms, 0 if not mentioned"
                    },
                    "prop_type": {
                        "type": "string",
                        "enum": ["Any property type", "Semi-detached house", "Detached house", "Flat", "Bungalow", "Cottage"],
                        "description": "Type of property"
                    }
                },
                "required": ["postcode"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_saved_listings",
            "description": "Retrieve the properties already saved for this customer. Call this when the user asks about a specific property from the list, asks for more details, asks which is cheapest/biggest etc.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "book_viewing",
            "description": "Record a viewing request when the user wants to view a specific property.",
            "parameters": {
                "type": "object",
                "properties": {
                    "property_url": {
                        "type": "string",
                        "description": "URL of the property they want to view"
                    },
                    "property_address": {
                        "type": "string",
                        "description": "Address of the property"
                    },
                    "preferred_time": {
                        "type": "string",
                        "description": "When the user wants to view, e.g. 'Friday at 2pm', empty string if not mentioned"
                    }
                },
                "required": ["property_url", "property_address"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_crm",
            "description": "Update the customer's details when they provide their name, email, or buying intent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "contact_name": {
                        "type": "string",
                        "description": "Customer's full name, empty string if not provided"
                    },
                    "email": {
                        "type": "string",
                        "description": "Customer's email address, empty string if not provided"
                    },
                    "lead_intent": {
                        "type": "string",
                        "enum": ["Buying", "Renting", "Investing", "Unknown"],
                        "description": "What the customer is looking to do"
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "reset_conversation",
            "description": "Reset the conversation and customer data when the user wants to start over or says 'reset'.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    }
]


# ─────────────────────────────────────────────
# OpenAI helpers
# ─────────────────────────────────────────────
openai_client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])

def create_property_listing_summary(properties: Dict) -> str:
    if len(properties) > 5:
        properties = properties[:5]
    prompt = "List ALL {len(listings)} properties you found with their address, price, bed count, type, and URL. Do not summarise — list every single one."

def enquire_openai(message: str):
    print(f"[OPENAI] Sending one-off message to OpenAI: {message[:100]}")
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            max_tokens=300,
            messages=[
                {
                    "role": "system",
                    "content": f'{message}'
                },
                {"role": "user", "content": message}
            ]
        )
        openai_returned_text = response.choices[0].message.content.strip()
        print(f"[OPENAI] Raw response: {openai_returned_text}")
        return(openai_returned_text)
    except Exception as e:
        print(f"[OPENAI] ERROR in sending message: {e}")
        return("")


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

