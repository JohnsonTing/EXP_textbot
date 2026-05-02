import json
import logging
from datetime import datetime, timezone, timedelta
from xml.dom.minidom import Attr

from helpers.openai_helpers import enquire_openai, extract_enquiry_details, detect_new_search, get_ai_response
from helpers.scrape_helpers import scrape_exp, find_specific_agent_listing_from_loop_postcode, scrape_property_details_from_url
from helpers.crm_helpers import get_missing_crm_fields, build_crm_followup_message, extract_crm_reply, parse_loop_enquiry
from helpers.db_helpers import get_customer, save_customer, save_message, reset_customer
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
            print(f"[HANDLER] Email body received {email_body!r}")

            if not email_body:
                return {
                    'statusCode': 400,
                    'body': json.dumps({'error': 'email body is required'})
                }
            enquiry_details = parse_loop_enquiry(email_body)
            print("Native Loop Enquiry Details:", enquiry_details)

            # Tidying up phone number and email and other keys
            personal_phone = enquiry_details['Phone Daytime'] if enquiry_details.get('Phone Daytime') else enquiry_details.get('Phone Evening', '')
            if personal_phone == "N/A" or personal_phone.strip() == "":
                return({'statusCode': 400, 'body': json.dumps({'error': 'Phone number missing'})})
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

            greet_user_message = f"Hi {enquiry_details['First Name']}! I’m Chloe from EXP. How are you doing today?"
            send_first_message = send_sms(enquiry_details['customer_id'], greet_user_message)
            save_message(enquiry_details['customer_id'], 'assistant', greet_user_message)

            book_viewing_message = f"I see you enquired about {enquiry_details['Address']}. When would you be free over the next week? We could arrange for a viewing for the property?"
            send_viewing_message = send_sms(enquiry_details['customer_id'], book_viewing_message)
            save_message(enquiry_details['customer_id'], 'assistant', book_viewing_message)

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
                customer_id = customer.get('customer_id') #customer_id is the phone number in the customers table
                contact_name = customer.get('contact_name') or customer.get('First Name', 'there')
                print(f"[HANDLER] Checking customer {customer_id} ({contact_name})")

                if not customer_id:
                    print(f"[HANDLER] Skipping customer with no customer_id")
                    continue

                # Get latest message from Conversations table for this customer
                conversations_response = conversations_table.query(
                    IndexName='phone_number-timestamp-index',
                    KeyConditionExpression='phone_number = :pn',
                    ExpressionAttributeValues={':pn': customer_id},
                    ScanIndexForward=False,  # newest first
                    Limit=1
                )
                messages = conversations_response.get('Items', [])

                if not messages:
                    print(f"[HANDLER] No messages found for {customer_id}, skipping")
                    skipped.append(customer_id)
                    continue

                latest_message = messages[0]
                latest_timestamp_str = latest_message.get('timestamp')
                print(f"[HANDLER] Latest message for {customer_id}: {messages[0]['message'] if messages else 'No messages found'} at {latest_timestamp_str}")


                if not latest_timestamp_str:
                    print(f"[HANDLER] No timestamp on latest message for {customer_id}, skipping")
                    skipped.append(customer_id)
                    continue

                latest_timestamp = datetime.fromisoformat(latest_timestamp_str).replace(tzinfo=timezone.utc)
                days_since = (now - latest_timestamp).days
                print(f"[HANDLER] {customer_id} — last active {days_since} days ago")

                if latest_timestamp < cutoff:
                    print(f"[HANDLER] {customer_id} is inactive for more than 14 days, re-engaging")
                    reengaged.append(customer_id)

                    # Scrape again any new properties based on their original enquiry details and update their record in the DB with the new list of properties
                    # Text them this new list of properties to reengage them and see if they're interested in any of the new ones
                    listings = scrape_exp(
                        postcode=customer.get('enquiry_postcode', ''),
                        max_price=customer.get('enquiry_max_price'), #just assume that their max budget is the current enquired property price upwards of 20%
                        min_beds=customer.get('enquiry_bedrooms', 0),
                        prop_type=customer.get('enquiry_prop_type', 'Any property type')
                    )
                    rejected = set(customer.get("rejected_listings", []))
                    listings = [l for l in listings if l.get("url") not in rejected]
                    print(f"[HANDLER] Scraped {len(listings)} properties for re-engagement of {customer_id} (after filtering rejected)")
                    print(f"[HANDLER] Sample scraped property for {customer_id}: {listings[0] if listings else 'No properties found'}")

                    customers_table.update_item(
                        Key={'customer_id': customer_id},
                        UpdateExpression='SET #sl = :sl',
                        ExpressionAttributeNames={'#sl': 'scraped_listings'},
                        ExpressionAttributeValues={':sl': json.dumps(listings)}
                    )

                    # All details are extracted from their enquiry. Send them a first message.
                    message = f"Hi {contact_name}, just checking in — how are you doing? Still on the lookout for a property?"
                    send_sms(customer_id, message)
                    save_message(customer_id, 'assistant', message)

                    # Send them the list of similar properties we found
                    listing_message = format_scraped_properties_into_listings_message(listings, 3)
                    print("[HANDLER] Re-engagement listing message:", listing_message)
                    send_sms(customer_id, listing_message)
                    save_message(customer_id, 'assistant', listing_message)
                    print(f"[HANDLER] Re-engaged {customer_id}")

                else:
                    skipped.append(customer_id)
                    print(f"[HANDLER] {customer_id} is still active, skipping")

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

            if not customer:
                print(f"[HANDLER] New customer flow")
                save_message(phone, 'user', message)
                enquiry = {
                    'created_at': datetime.now().isoformat(),
                    'updated_at': datetime.now().isoformat(),
                    'status': 'active'
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