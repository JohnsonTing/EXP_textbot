import json
import logging
import uuid
from datetime import datetime, timezone, timedelta
from xml.dom.minidom import Attr
import re
import resend
# import requests
from helpers.email_helpers import send_error_alert, send_viewing_request

from helpers.openai_helpers import enquire_openai, extract_enquiry_details, detect_new_search, get_ai_response, extract_available_date, answer_customer_question, detect_viewing_request
from helpers.scrape_helpers import scrape_exp, find_specific_agent_listing_from_loop_postcode, scrape_property_details_from_url, find_responsible_agent_and_listing_from_enquiry_details
from helpers.crm_helpers import get_missing_crm_fields, build_crm_followup_message, extract_crm_reply, parse_loop_enquiry, parse_email_enquiry
from helpers.db_helpers import get_customer, save_customer, save_message, reset_customer, get_agent_by_email, emit_metric
from helpers.twilio_helpers import send_sms, twiml_response, parse_send_message_body
from helpers.general_helpers import format_scraped_properties_into_listings_message

from config import dynamodb, customers_table, conversations_table

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
    print("[HANDLER] Version includes Unit Testing logic and First Message Endpoint.")
    
    path = event.get('requestContext', {}).get('http', {}).get('path', '')
    phone = None
    message = None

    #______________________________________________
    # Unit Testing
    #______________________________________________
    if event.get("localUnitTesting", False):
        print(f"[HANDLER] Unit testing mode")
        property_listing = event.get("url", "")
        print(f"[HANDLER] Testing scrape_property_details_from_url with URL: {property_listing}")
        try:
            details = scrape_property_details_from_url(property_listing)
            print(f"[HANDLER] Scraped details: {details}")
            return {
                'statusCode': 200,
                'body': json.dumps({'success': True, 'details': details})
            }
        except Exception as e:
            import traceback
            print(f"[HANDLER] ERROR in unit test scrape: {e}")
            print(traceback.format_exc())
            return {
                'statusCode': 500,
                'body': json.dumps({'success': False, 'error': str(e)})
            }
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
            print(f"[HANDLER] Full Resend inbound event: {json.dumps(event, default=str)}")
            print(f"[HANDLER] Email body received {email_body!r}")

            if not email_body:
                return {
                    'statusCode': 400,
                    'body': json.dumps({'error': 'email body is required'})
                }

            try:
                parsed_body = json.loads(email_body)
                # Resend inbound webhook — fetch full email body using email_id
                if isinstance(parsed_body, dict) and parsed_body.get("type") == "email.received":
                    data = parsed_body.get("data", {})
                    email_id = data.get("email_id", "")
                    to_addresses = data.get("to", [])
                    print(f"[HANDLER] Resend email_id: {email_id} | to: {to_addresses}")
                    resend.api_key = os.environ.get("RESEND_API_KEY", "")
                    # resend.Emails.get() is outbound-only
                    # full_email = resend.Emails.get(email_id)
                    full_email = resend.Emails.Receiving.get(email_id=email_id)
                    print(f"[HANDLER] Resend full email: {full_email}")
                    email_body = full_email.get("text") or full_email.get("html") or ""
                    print(f"[HANDLER] Extracted email body: {email_body!r}")
                # [OLD — Zapier format]
                # elif isinstance(parsed_body, dict) and "body" in parsed_body:
                #     email_body = parsed_body["body"]
            except (json.JSONDecodeError, TypeError):
                pass

            # [OLD — Loop-only key:value parser]
            # enquiry_details = parse_loop_enquiry(email_body)
            enquiry_details = parse_email_enquiry(email_body)
            print("Parsed email enquiry details:", enquiry_details)

            # Map Resend inbound address → responsible agent email
            # [OLD — no listing agent override]
            # INBOUND_AGENT_MAP = {
            #     "jeroen@doriisoloz.resend.app": "jeroen.hoppe@exp.uk.com",
            #     "johnson@doriisoloz.resend.app": "johnson.ting2022@gmail.com"
            #     # add more agents here: "agentname@doriisoloz.resend.app": "agent@exp.uk.com"
            # }
            # for to_addr in to_addresses:
            #     if to_addr in INBOUND_AGENT_MAP:
            #         responsible_agent_email = INBOUND_AGENT_MAP[to_addr]
            #         enquiry_details['Responsible Agent Email'] = INBOUND_AGENT_MAP[to_addr]
            #         print(f"[HANDLER] Mapped inbound address {to_addr} → {INBOUND_AGENT_MAP[to_addr]}")
            #         break
            #     else:
            #         responsible_agent_email = enquiry_details.get('Responsible Agent Email', '')
            INBOUND_AGENT_MAP = {
                "jeroen@doriisoloz.resend.app": "jeroen.hoppe@exp.uk.com",
                "johnson@doriisoloz.resend.app": "johnson.ting2022@gmail.com",
                # add more agents here: "agentname@doriisoloz.resend.app": "agent@exp.uk.com"
            }
            # Decouples property search from email recipient — test addresses search a real agent's listings
            # but outbound email still goes to whoever INBOUND_AGENT_MAP resolves to
            LISTING_AGENT_OVERRIDE = {
                "johnson@doriisoloz.resend.app": "jeroen.hoppe@exp.uk.com",
            }
            matched_inbound_address = ''
            responsible_agent_email = ''
            for to_addr in to_addresses:
                if to_addr in INBOUND_AGENT_MAP:
                    matched_inbound_address = to_addr
                    responsible_agent_email = INBOUND_AGENT_MAP[to_addr]
                    enquiry_details['Responsible Agent Email'] = responsible_agent_email
                    print(f"[HANDLER] Mapped inbound address {to_addr} → {responsible_agent_email}")
                    break
            if not responsible_agent_email:
                responsible_agent_email = enquiry_details.get('Responsible Agent Email', '')
            listing_agent_email = LISTING_AGENT_OVERRIDE.get(matched_inbound_address, responsible_agent_email)
            print(f"[HANDLER] listing_agent_email={listing_agent_email!r} (responsible_agent_email={responsible_agent_email!r})")
            
            print(f"[HANDLER] Responsible agent email determined as: {responsible_agent_email!r}")

            # Tidying up phone number and email and other keys
            personal_phone = enquiry_details.get('Phone Number') or enquiry_details.get('Phone Daytime', '')
            if not personal_phone or personal_phone.strip() in ('', 'N/A'):
                print(f"[HANDLER] ERROR: Phone number missing or invalid. Parsed enquiry: {enquiry_details}")
                raise ValueError(f"Phone number not found in enquiry. Keys parsed: {list(enquiry_details.keys())}")
            personal_phone = personal_phone.strip().replace(" ", "").replace(")", "")
            print(f"[HANDLER] Extracted phone number: {personal_phone}")
            raw_email = enquiry_details["Email Address"]
            try:
                email = raw_email.split("[")[1].split("]")[0]
            except IndexError:
                email = raw_email.strip()  # fall back to using it as-is
            enquiry_details['Email Address'] = email
            enquiry_details['email'] = email
            
            # responsible_agent_email = enquiry_details['Responsible Agent Email']
            # if responsible_agent_email == '':
            #     enquired_property = find_responsible_agent_and_listing_from_enquiry_details(
            #         postcode=enquiry_details["Postcode"],
            #         listing_type="rental" if enquiry_details.get("Customer Type", "buyer").lower() == "renter" else "sale",
            #         simple_address=enquiry_details.get("Simple Address", ""),
            #         #max_price=int(enquiry_details.get("Max Price", 0)),
            #         #min_beds=int(enquiry_details.get("Bedrooms", 0)),
            #         #prop_type=enquiry_details.get("Property Type", "Any property type")
            #     )
            #     responsible_agent_email = enquired_property.get("agent_email", "")


            customer_type = enquiry_details.get("Customer Type", "buyer").lower()
            print(f"[HANDLER] Customer type: {customer_type}")

            agent = get_agent_by_email(responsible_agent_email)
            responsible_agent_id   = agent.get('agent_id', '')   if agent else ''
            responsible_agent_name = agent.get('name', '') if agent else ''
            print(f"[HANDLER] Responsible agent: id={responsible_agent_id!r} name={responsible_agent_name!r}")

            enquiry_details['created_at'] = datetime.now().isoformat()
            enquiry_details['updated_at'] = datetime.now().isoformat()
            if not personal_phone:
                return {'statusCode': 400, 'body': json.dumps({'error': 'Phone number missing'})}
            enquiry_details['phone'] = "+44" + personal_phone[1:] if personal_phone.startswith("0") else personal_phone
            enquiry_details['contact_name'] = enquiry_details['First Name']
            enquiry_details['enquiry_postcode'] = enquiry_details["Postcode"] if enquiry_details["Postcode"] else ""
            enquiry_details['status'] = 'active'

            # Find the specific property they enquired about and then add to the DB the number of bedrooms    
            # [OLD — used responsible_agent_email for lookup, breaks for test addresses]
            # enquired_property = find_specific_agent_listing_from_loop_postcode(postcode=enquiry_details["Postcode"], simple_address=enquiry_details.get("Simple Address", ""), responsible_agent_email=responsible_agent_email, listing_type="rental" if customer_type == "renter" else "sale")
            enquired_property = find_specific_agent_listing_from_loop_postcode(postcode=enquiry_details["Postcode"], simple_address=enquiry_details.get("Simple Address", ""), responsible_agent_email=listing_agent_email, listing_type="rental" if customer_type == "renter" else "sale")
            print("Enquired Property: ", enquired_property)
            enquiry_details['enquiry_bedrooms'] = enquired_property.get("bedrooms", "")
            enquiry_details['enquired_property'] = enquired_property.get("url", "")

            if not enquired_property:
                raise ValueError(f"Property not found for address '{enquiry_details.get('Address')}' and agent '{responsible_agent_email}'")

            enquiry_max_price = int(enquired_property["price"].replace("£","").replace(",","")) * 1.2
            from decimal import Decimal
            enquiry_details['enquiry_max_price'] = Decimal(str(enquiry_max_price))

            # Scrape the list of similar properties save it on their database
            listing_type = "rental" if customer_type == "renter" else "sale"
            print(f"[HANDLER] Scraping similar properties with criteria - postcode: {enquiry_details['enquiry_postcode']}, max_price: {enquiry_details['enquiry_max_price']}, min_beds: {enquiry_details['enquiry_bedrooms']}, prop_type: {enquiry_details.get('enquiry_prop_type', 'Any property type')}, listing_type: {listing_type}")
            listings = scrape_exp(
                postcode=enquiry_details["Postcode"],
                max_price=enquiry_max_price, #just assume that their max budget is the current enquired property price upwards of 20%
                min_beds=enquired_property.get('bedrooms', 0),
                prop_type=enquired_property.get('prop_type', 'Any property type'),
                listing_type=listing_type
            )
            enquiry_details['scraped_listings'] = json.dumps(listings)

            # Clean up all user details before saving it to the DB
            clean_item = {
                'customer_id':             str(uuid.uuid4()),
                'phone':                   enquiry_details['phone'],
                'contact_name':            enquiry_details.get('First Name', ''),
                'customer_type':           customer_type,
                'First Name':             enquiry_details.get('First Name', ''),
                'Last Name':              enquiry_details.get('Last Name', ''),
                'email':                   enquiry_details.get('email', ''),
                'enquiry_postcode':        enquiry_details.get('Postcode', ''),
                'enquiry_bedrooms':        enquiry_details.get('enquiry_bedrooms', ''),
                'enquiry_max_price':       enquiry_details.get('enquiry_max_price'),
                'enquired_property':       enquiry_details.get('enquired_property', ''),
                'responsible_agent_email': responsible_agent_email,
                'responsible_agent_id':   responsible_agent_id,
                'responsible_agent_name': responsible_agent_name,
                'scraped_listings':        json.dumps(listings),
                'status':                  'active',
                'created_at':              enquiry_details['created_at'],
                'updated_at':              enquiry_details['updated_at'],
                'bot_paused':             False,
                'Comment':                enquiry_details.get('Comments', ''),
            }            
            print(f"[HANDLER] Cleaned item to save to DB: {clean_item}")                                                                            
            customers_table.put_item(Item=clean_item)
            emit_metric('enquiry_received', agent_id=responsible_agent_id, customer_id=clean_item['customer_id'], customer_type=customer_type)

            #customers_table.put_item(Item=enquiry_details)
            print(f"[HANDLER] Enquiry details extracted: {enquiry_details}")


            # All details are extracted from their enquiry. Send them a first message.
            # Strip flat/unit prefixes
            # simple_address = re.sub(r'^(?:Flat|Apartment|Unit|House|Studio|Room)\s+[A-Z0-9]+[,\s]+', '', address, flags=re.IGNORECASE)

            # # Strip leading house number
            # simple_address = re.sub(r'^\d+[,\s]+', '', simple_address, flags=re.IGNORECASE)
            # # address_no_number = re.sub(r’^\d+\s*’, '’, enquiry_details['Address’])

            #If the number is not recognized as a valid UK number after cleaning, send email to Jeroen, Johnson and log an error and raise an exception
            if enquiry_details.get('phone') and not bool(re.match(r'^(\+447|07)\d{9}$', enquiry_details.get('phone', ''))):
                print(f"[HANDLER] ERROR: Invalid phone number format after cleaning. phone={enquiry_details.get('phone')}")

                # [OLD — Zapier]
                # zapier_send_email_url = os.environ["ZAPIER_SEND_EMAIL_URL"]
                # headers = {"Content-Type": "application/json"}
                # payload = {
                #     "customer_name": clean_item.get('First Name', '') + " " + clean_item.get('Last Name', ''),
                #     "customer_phone_number": clean_item.get('phone', ''),
                #     "path": "Number not recognized as a UK mobile number. Send email to notify."
                # }
                # response = requests.post(zapier_send_email_url, json=payload, headers=headers)
                # print(response.status_code)
                # print(response.text)

                send_error_alert(
                    customer_name=clean_item.get('First Name', '') + " " + clean_item.get('Last Name', ''),
                    customer_phone=clean_item.get('phone', ''),
                    error_message="Number not recognized as a valid UK mobile number.",
                )
                raise ValueError(f"Invalid phone number format: {enquiry_details.get('phone')}")

            # If the number is valid, proceed to send the first message
            simple_address = enquiry_details.get('Simple Address', '')
            property_url = enquiry_details.get('enquired_property', '')
            first_name = enquiry_details.get('First Name', '')

            # Scrape property details upfront — used for available date (renters) and to answer any question in the enquiry comment
            property_details = {}
            if property_url:
                try:
                    property_details = scrape_property_details_from_url(property_url)
                    print(f"[HANDLER] Property details scraped for first message context")
                except Exception as e:
                    print(f"[HANDLER] WARNING: Could not scrape property details: {e}")

            if customer_type == "renter":
                available_date = extract_available_date(property_details) if property_details else None
                print(f"[HANDLER] Rental available date extracted: {available_date!r}")

                if available_date:
                    greet_user_message = f"Thank you for enquiring about {simple_address}, {property_url}. It’s available {available_date}, does that work for you?"
                else:
                    greet_user_message = f"Thank you for enquiring about {simple_address}, {property_url}. Would you like to arrange a viewing?"
            else:
                greet_user_message = f"Hi {first_name}! I'm Chloe from EXP. How are you doing today? \n\n I see you enquired about {simple_address}. ({property_url}) \n\n Can you give me a few days and times you are free over the next week? We could arrange for a viewing for the property?"

            # If customer included a question in their enquiry comment, try to answer it from property details
            comment = clean_item.get('Comment', '')
            if comment and property_details:
                question_answer = answer_customer_question(comment, property_details)
                if question_answer:
                    greet_user_message += f"\n\nAlso, to answer your question: {question_answer}"
                    print(f"[HANDLER] Appended question answer to first message: {question_answer!r}")

            # If comment contains a viewing request, email the agent and log the metric
            if comment:
                viewing_request = detect_viewing_request(comment)
                if viewing_request.get('is_viewing_request'):
                    preferred_time = viewing_request.get('preferred_time', '')
                    print(f"[HANDLER] Viewing request detected in enquiry comment | preferred_time={preferred_time!r}")
                    try:
                        send_viewing_request(
                            contact_name=clean_item.get('First Name', '') + ' ' + clean_item.get('Last Name', ''),
                            customer_phone=clean_item['phone'],
                            property_address=simple_address,
                            property_url=property_url,
                            property_price=enquired_property.get('price', ''),
                            agent_name=enquired_property.get('agent_name', ''),
                            agent_phone=enquired_property.get('agent_phone', ''),
                            agent_email=enquired_property.get('agent_email', ''),
                            responsible_agent_email=responsible_agent_email,
                            availability=preferred_time,
                            last_five_messages=f"Enquiry comment: {comment}",
                        )
                    except Exception as e:
                        print(f"[HANDLER] WARNING: Could not send viewing request email: {e}")
                    emit_metric('viewing_booked', agent_id=responsible_agent_id, customer_id=clean_item['customer_id'], customer_type=customer_type, metadata={
                        'property_url':   property_url,
                        'preferred_time': preferred_time,
                        'source':         'enquiry_comment',
                    })

            send_first_message = send_sms(clean_item['phone'], greet_user_message)
            save_message(clean_item['phone'], 'assistant', greet_user_message)
            emit_metric('first_message_sent', agent_id=responsible_agent_id, customer_id=clean_item['customer_id'], customer_type=customer_type)

            # SENDING THE BOOK VIEWING MESSAGE SEPARATELY
            # book_viewing_message = f"I see you enquired about {enquiry_details['Address']}. When would you be free over the next week? We could arrange for a viewing for the property?"
            # send_viewing_message = send_sms(enquiry_details['customer_id'], book_viewing_message)
            # save_message(enquiry_details['customer_id'], 'assistant', book_viewing_message)

            # Send them the list of similar properties we found
            # listing_message = format_scraped_properties_into_listings_message(listings, 5)
            # send_sms(enquiry_details['customer_id'], listing_message)
            # save_message(enquiry_details['customer_id'], 'assistant', listing_message)

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
    # /daily-check — Re-engage inactive customers
    # ─────────────────────────────────────────────
    if path == '/daily-check':
        print(f"[HANDLER] Daily customer check triggered")
        
        try:
            now = datetime.now(timezone.utc)
            cutoff = now - timedelta(days=14)
            reengaged = []
            skipped = []

            # Scan all customers that are still active
            customers_response = customers_table.scan(
                FilterExpression=('#st = :st'),
                ExpressionAttributeNames={'#st': 'status'},
                ExpressionAttributeValues={':st': 'active'}
            )
            customers = customers_response.get('Items', [])
            print(f"[HANDLER] Found {len(customers)} customers to check")

            for customer in customers:
                customer_id = customer.get('customer_id')  # UUID — used for DynamoDB key only
                customer_phone = customer.get('phone', '')  # actual phone — used for SMS and Conversations
                contact_name = customer.get('contact_name') or customer.get('First Name', 'there')
                print(f"[HANDLER] Checking customer {customer_phone} ({contact_name})")

                if not customer_phone:
                    print(f"[HANDLER] Skipping customer with no phone")
                    continue

                if customer.get('bot_paused'):
                    print(f"[HANDLER] Skipping paused customer {customer_phone}")
                    skipped.append(customer_phone)
                    continue

                # Get latest message from Conversations table for this customer
                conversations_response = conversations_table.query(
                    IndexName='phone_number-timestamp-index',
                    KeyConditionExpression='phone_number = :pn',
                    ExpressionAttributeValues={':pn': customer_phone},
                    ScanIndexForward=False,  # newest first
                    Limit=1
                )
                messages = conversations_response.get('Items', [])

                if not messages:
                    print(f"[HANDLER] No messages found for {customer_phone}, skipping")
                    skipped.append(customer_phone)
                    continue

                latest_message = messages[0]
                latest_timestamp_str = latest_message.get('timestamp')
                print(f"[HANDLER] Latest message for {customer_phone}: {messages[0]['message'] if messages else 'No messages found'} at {latest_timestamp_str}")

                if not latest_timestamp_str:
                    print(f"[HANDLER] No timestamp on latest message for {customer_phone}, skipping")
                    skipped.append(customer_phone)
                    continue

                latest_timestamp = datetime.fromisoformat(latest_timestamp_str).replace(tzinfo=timezone.utc)
                days_since = (now - latest_timestamp).days
                print(f"[HANDLER] {customer_phone} — last active {days_since} days ago")

                if latest_timestamp < cutoff:
                    print(f"[HANDLER] {customer_phone} is inactive for more than 14 days, re-engaging")
                    reengaged.append(customer_phone)

                    # Scrape again any new properties based on their original enquiry details and update their record in the DB with the new list of properties
                    # Text them this new list of properties to reengage them and see if they're interested in any of the new ones
                    listing_type = "rental" if customer.get("customer_type") == "renter" else "sale"
                    listings = scrape_exp(
                        postcode=customer.get('enquiry_postcode', ''),
                        max_price=customer.get('enquiry_max_price'),
                        min_beds=customer.get('enquiry_bedrooms', 0),
                        prop_type=customer.get('enquiry_prop_type', 'Any property type'),
                        listing_type=listing_type
                    )
                    rejected = set(customer.get("rejected_listings", []))
                    listings = [l for l in listings if l.get("url") not in rejected]
                    print(f"[HANDLER] Scraped {len(listings)} properties for re-engagement of {customer_phone} (after filtering rejected)")
                    print(f"[HANDLER] Sample scraped property for {customer_phone}: {listings[0] if listings else 'No properties found'}")

                    customers_table.update_item(
                        Key={'customer_id': customer_id},
                        UpdateExpression='SET #sl = :sl',
                        ExpressionAttributeNames={'#sl': 'scraped_listings'},
                        ExpressionAttributeValues={':sl': json.dumps(listings)}
                    )

                    # All details are extracted from their enquiry. Send them a first message.
                    message = f"Hi {contact_name}, just checking in — how are you doing? Still on the lookout for a property?"
                    send_sms(customer_phone, message)
                    save_message(customer_phone, 'assistant', message)

                    # Send them the list of similar properties we found
                    listing_message = format_scraped_properties_into_listings_message(listings, 3)
                    print("[HANDLER] Re-engagement listing message:", listing_message)
                    send_sms(customer_phone, listing_message)
                    save_message(customer_phone, 'assistant', listing_message)
                    emit_metric('reengagement_sent', agent_id=customer.get('responsible_agent_id', ''), customer_id=customer_id, customer_type=customer.get('customer_type', ''))
                    print(f"[HANDLER] Re-engaged {customer_phone}")

                else:
                    skipped.append(customer_phone)
                    print(f"[HANDLER] {customer_phone} is still active, skipping")

            print(f"[HANDLER] Daily check complete. Re-engaged: {len(reengaged)}, Skipped: {len(skipped)}")
            return {
                'statusCode': 200,
                'body': json.dumps({
                    'success': True,
                    'date_checked': now.isoformat(),
                    'reengaged': reengaged,
                    'skipped': skipped
                })
            }

        except Exception as e:
            print(f"[HANDLER] ERROR in daily check: {e}")
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
                'body': twiml_response("Hi sorry we could not identify your phone number. No response will be made.")
            }

        # Handle reset
        if message and message.lower() == 'reset':
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

            # bot_paused check
            if customer and customer.get('bot_paused') == True:
                print(f"[HANDLER] Bot paused for {phone} — logging message only")
                save_message(phone, 'user', message)
                return {
                    'statusCode': 200,
                    'headers': {'Content-Type': 'text/xml'},
                    'body': twiml_response("")
                }

            if not customer:
                print(f"[HANDLER] New customer flow")
                save_message(phone, 'user', message)
                enquiry = {
                    'created_at': datetime.now().isoformat(),
                    'updated_at': datetime.now().isoformat(),
                    'status': 'active',
                    'responsible_agent_email': os.environ.get('SPECIFIC_AGENT_EMAIL', '')
                }
                save_customer(
                    phone=phone,
                    enquiry=enquiry,
                    listings=[]
                )

                print(f"[HANDLER] No existing customer found with phone {phone}. Starting new customer flow by asking for enquiry details.")
                # Immediately acknowledge so Twilio doesn't time out
                ack = "Hi there! I'm Chloe from EXP. How are you doing today? \n\n Just so I can help you better, can you tell me what type of property you're looking for?"
                send_sms(phone, ack)                
                save_message(phone, 'assistant', ack)
                # Ask for the enquiry details we need to find properties for them and save to CRM before we do anything else
                # get_customer_info = ""
                # send_sms(phone, get_customer_info)
                # save_message(phone, 'assistant', get_customer_info)

            else:
                print(f"[HANDLER] Returning customer flow")
                save_message(phone, 'user', message)
                reply = get_ai_response(phone, message)
                send_sms(phone, reply)
                save_message(phone, 'assistant', reply)

            # if len(reply) <= 1600:
            #     sms_messages = [reply]
            # else:
            #     chunks = [reply[i:i+1550] for i in range(0, len(reply), 1550)]
            #     sms_messages = [f"({i+1}/{len(chunks)}) {chunk}" for i, chunk in enumerate(chunks)]

            # print(f"[HANDLER] Sending {len(sms_messages)} SMS message(s)")
            return {
                'statusCode': 200,
                'headers': {'Content-Type': 'text/xml'},
                #'body': f"Successfully processed message and sent reply." #twiml_response(sms_messages) --- IGNORE ---
                'body': twiml_response("")
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