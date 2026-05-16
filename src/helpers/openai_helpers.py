from openai import OpenAI
import json
import re
import os
from typing import Dict, List, Optional
import boto3
import time
import requests
from datetime import datetime
from helpers.email_helpers import send_viewing_request, send_inactive_notification
from helpers.db_helpers import get_conversation_history, get_customer
from helpers.scrape_helpers import scrape_exp, scrape_property_details_from_url
from helpers.db_helpers import save_customer, reset_customer, emit_metric, get_agent_by_email, get_customer_id
from helpers.crm_helpers import update_customer_crm
from helpers.general_helpers import make_readable_conversation_history
from config import openai_client, customers_table, conversations_table


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
                    },
                    "customer_type": {
                        "type": "string",
                        "enum": ["buyer", "renter"],
                        "description": "Whether the customer is looking to buy or rent. Infer from context if not explicitly stated."
                    }
                },
                "required": ["postcode"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_property_details",
            "description": "Get detailed information about a specific property including full description, features, and composition. Call this when the user asks for more specific details about a specific property.",
            "parameters": {
                "type": "object",
                "properties": {
                    "property_url": {
                        "type": "string",
                        "description": "The URL of the property to get details for"
                    }
                },
                "required": ["property_url"]
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
            "name": "reject_property",
            "description": "Mark a property as not interested so it won't be shown again. Call this when the user says they don't like, aren't interested in, or want to skip a specific property.",
            "parameters": {
                "type": "object",
                "properties": {
                    "property_url": {
                        "type": "string",
                        "description": "The URL of the property to reject"
                    }
                },
                "required": ["property_url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_user_inactive",
            "description": "Set the user's status to inactive and notify the agent. Call this when the user says they have found a property, are no longer searching, or are not interested in receiving more properties.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "enum": ["found_a_property", "no_longer_searching", "not_interested"],
                        "description": "Why the user is becoming inactive"
                    }
                },
                "required": ["reason"]
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
            "description": "Update the customer's details when they provide their name, email, buying/renting intent, or other profile info.",
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
                    "customer_type": {
                        "type": "string",
                        "enum": ["buyer", "renter"],
                        "description": "Whether the customer is looking to buy or rent. Set as soon as this is clear from the conversation."
                    },
                    "lead_intent": {
                        "type": "string",
                        "description": "What the customer is looking to do"
                    },
                    "summary": {
                        "type": "string",
                        "description": "A one-sentence summary of what the prospect is looking for. Include specific inside details like how likely the prospect is to move, their motivation, and any specific requirements or preferences they have mentioned. This will help the agent understand the customer's needs at a glance."
                    },
                    "enquiry_bedrooms": {
                        "type": "integer",
                        "description": "Number of bedrooms the customer is looking for, 0 if not mentioned"
                    },
                    "enquiry_max_price": {
                        "type": "integer",
                        "description": "Maximum price the customer is looking for, 0 if not mentioned" 
                    },
                    "tags": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "description": "Relevant tags about the customer's preferences and situation, e.g. ['first-time buyer', 'investor', 'upsizing', 'downsizing', 'relocation'], But also ['hot', 'warm', 'cold'] based on how likely they seem to move. Infer these from the conversation context and the customer's tone. This will help the agent prioritise and understand the customer's needs quickly."                                                
                        },
                    }
                },
                "required": []
            }
        }
    },
    # {
    #     "type": "function",
    #     "function": {
    #         "name": "reset_conversation",
    #         "description": "Reset the conversation and customer data when the user wants to start over or says 'reset'.",
    #         "parameters": {
    #             "type": "object",
    #             "properties": {},
    #             "required": []
    #         }
    #     }
    # }
]


# ─────────────────────────────────────────────
# OpenAI helpers
# ─────────────────────────────────────────────

def create_property_listing_summary(properties: Dict) -> str:
    if len(properties) > 5:
        properties = properties[:3]
    prompt = "List ALL {len(listings)} properties you found with their address, price, bed count, type, and URL. Do not summarise — list every single one."

def enquire_openai(system_prompt: str, user_content: str, max_tokens: int = 300) -> str:
    print(f"[OPENAI] enquire_openai | system={system_prompt[:60]!r} | user={user_content[:60]!r}")
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ]
        )
        result = response.choices[0].message.content.strip()
        print(f"[OPENAI] Raw response: {result}")
        return result
    except Exception as e:
        print(f"[OPENAI] ERROR in enquire_openai: {e}")
        return ""


def extract_enquiry_details(message: str) -> Dict:
    print(f"[OPENAI] Extracting enquiry details from message: {message[:100]}")
    system_prompt = """Extract property search details from the message.
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
    try:
        text = re.sub(r'```json|```', '', enquire_openai(system_prompt, message)).strip()
        parsed = json.loads(text)
        print(f"[OPENAI] Parsed enquiry: {parsed}")
        return parsed
    except json.JSONDecodeError as e:
        print(f"[OPENAI] ERROR: Failed to parse JSON response: {e}")
        return {"postcode": "", "bedrooms": 0, "max_price": 0, "prop_type": "Any property type"}
    except Exception as e:
        print(f"[OPENAI] ERROR in extract_enquiry_details: {e}")
        return {"postcode": "", "bedrooms": 0, "max_price": 0, "prop_type": "Any property type"}


def detect_viewing_request(comment: str) -> dict:
    print(f"[OPENAI] Checking if enquiry comment is a viewing request | comment={comment[:80]!r}")
    system_prompt = (
        "You detect viewing requests in property enquiry comments. "
        "Return ONLY valid JSON with two keys: "
        "\"is_viewing_request\" (boolean) — true if the customer is asking to view or visit the property; "
        "\"preferred_time\" (string) — the exact time/date they requested, empty string if not specified."
    )
    try:
        text = re.sub(r'```json|```', '', enquire_openai(system_prompt, comment, max_tokens=80)).strip()
        result = json.loads(text)
        print(f"[OPENAI] Viewing request detection result: {result}")
        return result
    except Exception as e:
        print(f"[OPENAI] ERROR in detect_viewing_request: {e}")
        return {"is_viewing_request": False, "preferred_time": ""}


def answer_customer_question(question: str, property_details: dict) -> Optional[str]:
    print(f"[OPENAI] Attempting to answer customer question from property details | question={question[:80]!r}")
    system_prompt = (
        "You are a property assistant. A customer asked a question in their enquiry about a property. "
        "Answer it using ONLY the information in the property details provided. "
        "If the property details do not contain enough information to answer, return null. "
        "Return ONLY valid JSON with one key: \"answer\" (string or null). "
        "Paraphrase and keep the answer concise: include also the original question — this whole answer will be appended to an SMS message."
    )
    try:
        text = re.sub(r'```json|```', '', enquire_openai(system_prompt, json.dumps({
            "customer_question": question,
            "property_details": property_details
        }), max_tokens=150)).strip()
        answer = json.loads(text).get("answer")
        print(f"[OPENAI] Customer question answer: {answer!r}")
        return answer
    except Exception as e:
        print(f"[OPENAI] ERROR in answer_customer_question: {e}")
        return None


def extract_available_date(property_details: dict) -> Optional[str]:
    system_prompt = (
        "You extract available dates from property listing text.\n"
        "Return ONLY valid JSON with one key: \"available_date\" (string or null).\n"
        "Set \"available_date\" to the exact phrase as written in the listing "
        "(e.g. \"Now\", \"1st June 2025\", \"Immediately\", \"1 July 2025\").\n"
        "If no available date is explicitly stated, return {\"available_date\": null}.\n"
        "Do NOT infer, guess, or make up dates. Only return what is explicitly written."
    )
    try:
        text = re.sub(r'```json|```', '', enquire_openai(system_prompt, json.dumps(property_details), max_tokens=60)).strip()
        available_date = json.loads(text).get("available_date")
        print(f"[OPENAI] Available date extracted: {available_date!r}")
        return available_date
    except Exception as e:
        print(f"[OPENAI] ERROR in extract_available_date: {e}")
        return None


def detect_new_search(message: str, current_postcode: str) -> Dict:
    print(f"[OPENAI] Detecting if new search requested | current_postcode={current_postcode}")
    system_prompt = f"""You are a UK property search assistant. The user is currently viewing listings in: {current_postcode}.
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
    try:
        text = re.sub(r'```json|```', '', enquire_openai(system_prompt, message)).strip()
        parsed = json.loads(text)
        print(f"[OPENAI] New search detection result: {parsed}")
        return parsed
    except json.JSONDecodeError as e:
        print(f"[OPENAI] ERROR: Failed to parse JSON in detect_new_search: {e}")
        return {"new_search": False, "postcode": "", "bedrooms": 0, "max_price": 0, "prop_type": "Any property type"}
    except Exception as e:
        print(f"[OPENAI] ERROR in detect_new_search: {e}")
        return {"new_search": False, "postcode": "", "bedrooms": 0, "max_price": 0, "prop_type": "Any property type"}

def get_ai_response(phone: str, user_message: str) -> str:
    history = get_conversation_history(phone)
    history = history[-10:] # Limit history to last 10 messages for context
    print(f"[OPENAI] Conversation history for {phone}: {history}")
    customer = get_customer(phone)
    responsible_agent_name = customer.get('responsible_agent_name', '') if customer else ''

#     system_prompt = """You are Chloe, a friendly UK property agent assistant communicating via SMS for EXP. Be very sure to stick within the context. Do not entertain any requests or questions that are not relevant to the customer's property search. 
# Help customers find properties, answer questions about listings, and book viewings.
# Always be concise — you are sending SMS messages.
# If the customer wants to search for properties, call search_properties.
# If they ask about properties already found, call get_saved_listings first.
# If they want to view a property, call book_viewing.
# If they share their name, email or buying intent, call update_crm.
# Finally, make sure the end reply is below 1600 characters (so it can fit in one SMS).
# """
    system_prompt = f"""You are Chloe, a friendly and down-to-earth UK property agent assistant texting customers on behalf of EXP's agent {responsible_agent_name}.

You chat like a real person — warm, helpful, and easy to talk to. Keep messages concise and natural, like a quick text, not an email. Avoid sounding robotic or overly formal. You are the kind of person that don't like using exclamation marks or emojis. 
Do not use emojis, exclamation marks, "**" or bolding in your response.
You are not trying to please, you say it as it is.

Your role is to help people find the right property, answer questions about listings, and arrange viewings. Stay focused on their property search and gently steer the conversation back if it goes off track.

Be proactive and thoughtful — ask helpful follow-up questions, show understanding, and make the process feel easy and personal. A little warmth goes a long way (e.g. “That sounds lovely”, “I can help with that”, “Let’s find you something that fits”).

Do not repeat your phrases or sentence structures. Using the conversation history, vary how you say things to sound more human. Avoid overusing emojis, but use them when they fit the tone of the conversation.

No need to reveal you are an AI — just be a friendly assistant who’s there to help. Always keep the customer’s needs and preferences in mind, and use the information they’ve shared to guide your responses.

Guidelines:
- If the customer wants to search for properties:
    Call the tool search_properties
    Show them at most 3 properties at a time and format each property strictly as a single line:
        "{{address}}, {{bedrooms}}-bed {{property_type}} at {{price}}. {{url}}"
    Example: "Woodford Road E18, 4-bed Semi-detached house at £925,000. https://exp.uk.com/..."
    Do not include status, bullet points, or extra labels. One line per property.

- If they ask about saved or previously shared properties: 
    call get_saved_listings
    Do not reveal any agent information in your response. Only share the details of the property that would be relevant to a customer, such as address, price, bed count, type, status, and URL. Do not share any information about the estate agent or how to contact them. 
- If they want more in depth details about a specific property:
    Call get_property_details
    Share the information that would be relevant to a customer, such as description, features, and composition. Do not share any information about the estate agent or how to contact them.
- If they want to book a viewing:
    If the property is not known from the context, ask them which one they're interested in
    Ask them when they are available for a viewing: "Let me get that organised for you, can you let me know a few days and times that you can do so that I can get this booked in for you?"
    Call book_viewing 
    Say "Great, I'll try to arrange that viewing for you with your agent {responsible_agent_name}. They will be in touch to confirm a viewing." Do not ask if they are interested in other properites yet.
- If they share details like name, email, or buying intent => call update_crm
- If the customer says they are not interested in a specific property (e.g. "not that one", "skip that", "don't like it", "not for me") => call reject_property with the property URL. That property will never be shown to them again. Acknowledge briefly, e.g. "No problem, I'll take that one off the list."
- If the customer says they have already found a property => call set_user_inactive with reason "found_a_property", say something like "That's great news, congrats. I'll let {responsible_agent_name} know. All the best with the move."
- If the customer says they are no longer searching (e.g. changed plans, not moving anymore) => call set_user_inactive with reason "no_longer_searching", say something like "No problem at all. I'll let {responsible_agent_name} know. Feel free to get in touch if you start looking again."
- If the customer is not interested in receiving any more properties from us => call set_user_inactive with reason "not_interested"

STRICT FORMAT RULES — never break these:
   - Keep replies concise (SMS style, under 1600 characters)
   - Use only plain punctuation like full stops, commas, question marks — no emojis, exclamation marks, or bolding
   - Stay within the context of helping them find properties, answering questions about listings, and booking viewings. Do not entertain any requests or questions that are not relevant to their property search.


Above all, sound human — like a helpful agent who genuinely wants to find them a great place.
Examples of how Chloe texts (No need to be overly emotional. Do not use any emojis or bolding in your reponse):
User: "I'm looking for a 2 bed in Manchester under 200k"
Chloe: "Nice area. Let me have a look at what's available for you"

User: "Can I book a viewing?"
Chloe: "Of course - When are you free? And is it the same property we were looking at?"
"""

    messages = [{"role": "system", "content": system_prompt}] + history + [{"role": "user", "content": user_message}]

    response = openai_client.chat.completions.create(
        model="gpt-4o",
        max_tokens=1000,
        messages=messages,
        tools=tools,
        tool_choice="auto"
    )

    gpt_message = response.choices[0].message

    # GPT wants to call a tool
    if gpt_message.tool_calls:
        messages.append(gpt_message)  # append assistant message first
    
        for tool_call in gpt_message.tool_calls:
            function_name = tool_call.function.name
            arguments = json.loads(tool_call.function.arguments)
            tool_result = handle_tool_call(function_name, arguments, phone, history)
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": tool_result
            })

        final_response = openai_client.chat.completions.create(
            model="gpt-4o",
            max_tokens=1000,
            messages=messages,
            tools=tools,
            tool_choice="none"
        )
        return final_response.choices[0].message.content

    # GPT just replied normally
    return gpt_message.content

def handle_tool_call(function_name: str, arguments: dict, phone: str, history: list = []) -> str:
    print(f"[TOOLS] Calling {function_name} with {arguments}")

    if function_name == "search_properties":
        existing_customer = get_customer(phone)
        customer_type = arguments.get("customer_type") or (existing_customer.get("customer_type") if existing_customer else None) or "buyer"
        print(f"[TOOLS] Determined customer type: {customer_type}")
        listing_type = "rental" if customer_type == "renter" else "sale"
        radius = 4 if customer_type == "buyer" else 5
        max_price = arguments.get("max_price", 0)
        listings = scrape_exp(
            postcode=arguments["postcode"],
            max_price=max_price,
            min_beds=arguments.get("min_beds", 0),
            prop_type=arguments.get("prop_type", "Any property type"),
            radius=radius,
            listing_type=listing_type
        )
        formatted_listings_for_response = []
        if listings:
            if max_price != 0:
                min_price = max_price * 0.5 if max_price < 500000 else max_price * 0.7
                def parse_price(p):
                    try:
                        return int(p.get("price", "0").replace("£", "").replace(",", ""))
                    except (ValueError, AttributeError):
                        return 0
                listings = [l for l in listings if parse_price(l) >= min_price]
            enquiry = {
                "postcode": arguments["postcode"],
                "bedrooms": arguments.get("min_beds", 0),
                "max_price": arguments.get("max_price", 0),
                "prop_type": arguments.get("prop_type", "Any property type"),
                "customer_type": customer_type
            }
            save_customer(phone, enquiry, listings)
            customer = get_customer(phone)
            rejected = set(customer.get("rejected_listings", []) if customer else [])
            print(f'[TOOLS] Listings retrieved from scrape_exp: {listings[:3]}...')
            unavailable_status = "Let Agreed" if listing_type == "rental" else "Sold STC"
            formatted_listings_for_response = [
                {
                    "address": l["address"],
                    "price": l["price"],
                    "bedrooms": l["bedrooms"],
                    "description": l.get("description", "")[:100],
                    "url": l.get("url", ""),
                    "property_type": l.get("property_type", ""),
                }
                for l in listings
                if l.get("status") != unavailable_status and l.get("url") not in rejected
            ]
        return json.dumps(formatted_listings_for_response)
    elif function_name == "get_property_details":
        print(f"[TOOLS] Getting property details for URL: {arguments['property_url']}")
        details = scrape_property_details_from_url(arguments["property_url"])
        return json.dumps(details)

    elif function_name == "get_saved_listings":
        customer = get_customer(phone)
        if not customer:
            return json.dumps([])
        rejected = set(customer.get("rejected_listings", []))
        listings = json.loads(customer.get("scraped_listings", "[]"))
        return json.dumps([l for l in listings if l.get("url") not in rejected])

    elif function_name == "reject_property":
        property_url = arguments.get("property_url", "")
        print(f"[TOOLS] Rejecting property: {property_url}")
        customers_table.update_item(
            Key={'customer_id': get_customer_id(phone)},
            UpdateExpression='ADD rejected_listings :url SET updated_at = :ua',
            ExpressionAttributeValues={
                ':url': {property_url},
                ':ua': datetime.now().isoformat()
            }
        )
        return json.dumps({"success": True})
    elif function_name == 'set_user_inactive':
        reason = arguments.get("reason", "not_interested")
        customers_table.update_item(
            Key={'customer_id': get_customer_id(phone)},
            UpdateExpression='SET #st = :st, updated_at = :ua, inactive_reason = :r',
            ExpressionAttributeNames={'#st': 'status'},
            ExpressionAttributeValues={
                ':st': 'inactive',
                ':ua': datetime.now().isoformat(),
                ':r': reason
            }
        )
        print(f"[TOOL] Set {phone} to inactive — reason: {reason}")

        customer = get_customer(phone)
        contact_name = customer.get("First Name", "") + " " + customer.get("Last Name", "") if customer and (customer.get("First Name") or customer.get("Last Name")) else (customer.get("contact_name", "") if customer else "")
        last_five_messages = make_readable_conversation_history(history[-5:] if history else [])

        reason_labels = {
            "found_a_property": "Found a property",
            "no_longer_searching": "No longer searching",
            "not_interested": "Not interested"
        }

        # [OLD — Jira webhook]
        # try:
        #     response = requests.post(
        #         os.environ["JIRA_WEBHOOK_URL"],
        #         json={
        #             "event_type": "customer_inactive",
        #             "reason": reason_labels.get(reason, reason),
        #             "customer_name": contact_name,
        #             "customer_phone_number": phone,
        #             "last_five_messages": last_five_messages
        #         },
        #         headers={
        #             "Content-Type": "application/json",
        #             "X-Automation-Webhook-Token": os.environ["JIRA_WEBHOOK_TOKEN"]
        #         }
        #     )
        #     print(f"[TOOL] Jira webhook response: {response.status_code}")
        # except Exception as e:
        #     print(f"[TOOL] ERROR sending Jira webhook for inactive customer: {e}")

        try:
            responsible_agent_email = customer.get("responsible_agent_email", "") if customer else ""
            send_inactive_notification(
                contact_name=contact_name,
                customer_phone=phone,
                reason=reason_labels.get(reason, reason),
                responsible_agent_email=responsible_agent_email,
                last_five_messages=last_five_messages,
            )
        except Exception as e:
            print(f"[TOOL] ERROR sending inactive notification: {e}")

        return "Customer marked as inactive."
    elif function_name == "book_viewing":
        # Save viewing request to DB or notify agent however you like
        print(f"[TOOLS] Viewing request | {arguments['property_address']} | {arguments.get('preferred_time', 'time not specified')}")

        customer = get_customer(phone)
        if not customer:
            customer = json.dumps([])
        contact_name = customer.get("First Name", "") + " " + customer.get("Last Name", "") if customer.get("First Name") or customer.get("Last Name") else customer.get("contact_name", "")
        customer_scraped_listings = json.loads(customer.get("scraped_listings", "[]"))
        property_listing = next((l for l in customer_scraped_listings if l.get("url") == arguments["property_url"]), None)
        price = property_listing.get("price", "") if property_listing else ""
        agent_name = property_listing.get("agent_name", "") if property_listing else ""
        agent_phone = property_listing.get("agent_phone", "") if property_listing else ""
        agent_email = property_listing.get("agent_email", "") if property_listing else ""
        print(f"[TOOLS] Contact name: {contact_name} | Agent info: {agent_name}, {agent_phone}, {agent_email}")
        last_five_messages = history[-5:] if history else []
        last_five_messages = make_readable_conversation_history(last_five_messages)
        print(f"[TOOLS] Last 5 messages for context: {last_five_messages}")

        responsible_agent_email = customer.get("responsible_agent_email", agent_email) if customer else "jeroen.hoppe@exp.uk.com"




        # [OLD — Zapier/Jira]
        #jira_automation_url = os.environ["JIRA_WEBHOOK_URL"]
        zapier_send_email_url = os.environ["ZAPIER_SEND_EMAIL_URL"]
        headers = {"Content-Type": "application/json"}
        payload = {
            "property_listing_url": arguments.get("property_url", ""),
            "property_address": arguments.get("property_address", ""),
            "property_price": price,
            "customer_name": contact_name,
            "customer_phone_number": phone,
            "customer_availability": arguments.get("preferred_time", ""),
            "agent_name": agent_name,
            "agent_phone": agent_phone,
            "agent_email": agent_email,
            "responsible_agent_email": responsible_agent_email,
            "last_five_messages": last_five_messages
        }
        response = requests.post(zapier_send_email_url, json=payload, headers=headers)
        print(response.status_code)
        print(response.text)

        # send_viewing_request(
        #     contact_name=contact_name,
        #     customer_phone=phone,
        #     property_address=arguments.get("property_address", ""),
        #     property_url=arguments.get("property_url", ""),
        #     property_price=price,
        #     agent_name=agent_name,
        #     agent_phone=agent_phone,
        #     agent_email=agent_email,
        #     responsible_agent_email=responsible_agent_email,
        #     availability=arguments.get("preferred_time", ""),
        #     last_five_messages=last_five_messages,
        # )

        responsible_agent_id = customer.get('responsible_agent_id', '') if customer else ''
        customer_type = customer.get('customer_type', '') if customer else ''

        emit_metric('viewing_booked', agent_id=responsible_agent_id, customer_id=phone, customer_type=customer_type, metadata={
            'property_url':   arguments.get('property_url', ''),
            'preferred_time': arguments.get('preferred_time', ''),
        })

        # Referral: customer viewing a property not listed by their responsible agent
        if agent_email and responsible_agent_email and agent_email != responsible_agent_email:
            print(f"[TOOLS] Referral detected — listing agent {agent_email} ≠ responsible agent {responsible_agent_email}")
            emit_metric('referral', agent_id=responsible_agent_id, customer_id=phone, customer_type=customer_type, metadata={
                'referred_to_agent_email': agent_email,
                'property_url':            arguments.get('property_url', ''),
            })

        return json.dumps({"success": True, "message": "Viewing request recorded"})

    elif function_name == "update_crm":
        update_customer_crm(phone, arguments)
        return json.dumps({"success": True})

    elif function_name == "reset_conversation":
        reset_customer(phone)
        return json.dumps({"success": True})

    return json.dumps({"error": "Unknown function"})