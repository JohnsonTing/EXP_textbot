import json
import logging
from datetime import datetime

from openai_helpers import enquire_openai, extract_enquiry_details, detect_new_search, get_ai_response
from scrape_helpers import scrape_exp, find_specific_agent_listing_from_loop_postcode
from crm_helpers import get_missing_crm_fields, build_crm_followup_message, extract_crm_reply, parse_loop_enquiry
from db_helpers import customers_table, get_customer, save_customer, save_message, reset_customer
from twilio_helpers import send_sms, twiml_response, parse_send_message_body
from general_helpers import format_scraped_properties_into_listings_message
# ─────────────────────────────────────────────
# Setup
# ─────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

import os

# ─────────────────────────────────────────────
# Lambda handler
# ─────────────────────────────────────────────

def handler(event, context):
    print(f"[HANDLER] Invoked. Event keys: {list(event.keys())}")
    print("event:", event)
    
    path = event.get('requestContext', {}).get('http', {}).get('path', '')
    phone = None
    message = None

    #______________________________________________
    # Health check
    #______________________________________________
    if event.get('requestContext', {}).get('http', {}).get('method') == 'GET' and path != '/first-message':
        print(f"[HANDLER] Health check request")
        return {'statusCode': 200, 'body': 'Property bot is running ✅'}
    if path != '/first-message':
        if event.get('isBase64Encoded') == False:
            print(f"[HANDLER] Not base64 encoded, using body directly")
            parsed = event.get("body")
            if isinstance(parsed, str) and parsed.strip():
                try:
                    parsed = json.loads(parsed)
                    phone = parsed.get("phone")
                    message = parsed.get("message")
                    print(f"[HANDLER] Parsed -> from={phone} | body={message}")
                    print(f"[HANDLER] phone={phone!r} | message={message!r}")
                except json.JSONDecodeError:
                    pass  # leave parsed as the original string     

        else:
            parsed  = parse_send_message_body(event)
            print(f"[HANDLER] Parsed Message-Phone Json: {parsed}")
            phone   = parsed['from']
            print("")
            message = parsed['body'].strip()
            print(f"[HANDLER] phone={phone!r} | message={message!r}")


    # ─────────────────────────────────────────────
    # Manual send route — POST /send
    # ─────────────────────────────────────────────
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
    # /first-message First Message to send across multiple properties similar to their enquired one
    # ─────────────────────────────────────────────
    if path == "/first-message":
        if event.get('requestContext', {}).get('http', {}).get('method') == 'GET':
            print(f"[HANDLER] Testing First Message Endpoint")
            return {'statusCode': 200, 'body': 'The first message endpoint is up and running ✅'}

        print(f"[HANDLER] Send first message to engage on interested properties")
        
        try:
            email_body = event.get("body","")
            print(f"[HANDLER] Email body received {email_body!r}")

            if not email_body:
                return {
                    'statusCode': 400,
                    'body': json.dumps({'error': 'email body is required'})
                }
            enquiry_details = parse_loop_enquiry(email_body)
            print("Native Loop Enquiry Details:", enquiry_details)

            # Tidying up phone number and email and other keys
            personal_phone = enquiry_details['Phone Evening']
            raw_email = enquiry_details["Email Address"]
            try:
                email = raw_email.split("[")[1].split("]")[0]
            except IndexError:
                email = raw_email.strip()  # fall back to using it as-is
            enquiry_details['Email Address'] = email
            enquiry_details['email'] = email
            enquiry_details['created_at'] = datetime.now().isoformat()
            enquiry_details['updated_at'] = datetime.now().isoformat()
            if not personal_phone:
                return {'statusCode': 400, 'body': json.dumps({'error': 'Phone number missing'})}
            enquiry_details['customer_id'] = "+44" + personal_phone[1:] if personal_phone.startswith("0") else personal_phone
            enquiry_details['contact_name'] = enquiry_details['First Name']
            enquiry_details['enquiry_postcode'] = enquiry_details["Postcode"]
            enquiry_details['status'] = 'active'

            # Find the specific property they enquired about and then add to the DB the number of bedrooms    
            enquired_property = find_specific_agent_listing_from_loop_postcode(enquiry_details["Postcode"])
            print("Enquired Property: ", enquired_property)
            enquiry_details['enquiry_bedrooms'] = enquired_property.get("bedrooms", "")
            enquiry_details['enquired_property'] = enquired_property.get("url", "")
            enquiry_max_price = int(enquired_property["price"].replace("£","").replace(",","")) * 1.2
            from decimal import Decimal
            enquiry_details['enquiry_max_price'] = Decimal(str(enquiry_max_price))

            # Scrape the list of similar properties save it on their database
            listings = scrape_exp(
                postcode=enquiry_details["Postcode"],
                max_price=enquiry_max_price, #just assume that their max budget is the current enquired property price upwards of 20%
                min_beds=enquired_property.get('bedrooms', 0),
                prop_type=enquired_property.get('prop_type', 'Any property type')
            )
            enquiry_details['scraped_listings'] = json.dumps(listings)
            
            customers_table.put_item(Item=enquiry_details)
            print(f"[HANDLER] Enquiry details extracted: {enquiry_details}")


            # All details are extracted from their enquiry. Send them a first message.

            greet_user_message = f"Hi {enquiry_details['First Name']}! I’m Chloe from EXP. I see you enquired about {enquiry_details['Address']}. How are you doing today?"
            send_first_message = send_sms(enquiry_details['customer_id'], greet_user_message)
            save_message(enquiry_details['customer_id'], 'assistant', greet_user_message)

            # Send them the list of similar properties we found
            listing_message = format_scraped_properties_into_listings_message(listings, 5)
            send_sms(enquiry_details['customer_id'], listing_message)
            save_message(enquiry_details['customer_id'], 'assistant', listing_message)

            # properties = json.loads(enquiry_details['scraped_listings'])[:5]
            # print(f"[HANDLER] Getting AI to list properties...")
            # ai_prompt = f"Say that below is a list of properties you think they might be interested in. List ALL {len(properties)} properties you found with their address, price, bed count, type, and URL. Do not summarise — list every single one."
            # reply = get_ai_response(enquiry_details['customer_id'], ai_prompt, properties)
            # print(f"[HANDLER] Sending the list of properties...")
            # send_sms(enquiry_details['customer_id'], reply)
            # save_message(enquiry_details['customer_id'], 'assistant', reply)


            return {
                'statusCode': 200,
                'headers': {
                    'Access-Control-Allow-Origin': '*',  # or your specific Replit URL
                    'Access-Control-Allow-Headers': 'Content-Type',
                    'Access-Control-Allow-Methods': 'POST, OPTIONS'
                },
                'body': json.dumps({'success': True, 
                                    'Enquiry Details': f"{enquiry_details}",
                                    'Send First Message Response': f"{send_first_message}"
                                    })
            }

        except Exception as e:
            print(f"[HANDLER] ERROR in first message: {e}")
            import traceback
            print(traceback.format_exc())
            return {
                'statusCode': 500,
                'body': json.dumps({'error': 'Internal server error'})
            }


    # ─────────────────────────────────────────────
    # Automatic AI response to SMS messages
    # ─────────────────────────────────────────────
    if path != '/send' and path != '/first-message':
        print(f"[HANDLER] SMS message received from {phone}")
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
                        'max_price': detection.get('max_price', customer.get('max_price', 0)),
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