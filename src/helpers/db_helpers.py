import os
import json
import re
import uuid
from datetime import datetime
from typing import Dict, List, Optional
import boto3

from config import dynamodb, customers_table, conversations_table

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
            'max_price':  enquiry.get('max_price', 0),
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
