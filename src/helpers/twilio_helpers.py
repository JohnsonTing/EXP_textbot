import os
import json
import re
from typing import Dict, List, Optional
import boto3
from twilio.rest import Client as TwilioClient


twilio_client = TwilioClient(os.environ['TWILIO_ACCOUNT_SID'], os.environ['TWILIO_AUTH_TOKEN'])
TWILIO_FROM = os.environ['TWILIO_FROM_NUMBER']

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


def parse_send_message_body(event):
    raw = event.get('body', '')
    if event.get('isBase64Encoded'):
        import base64
        raw = base64.b64decode(raw).decode('utf-8')
    else:
        print("Not Base64 Encoded")
        if 'body' in event:
            print(f"[PARSE] Using body directly")
            print(f"[PARSE] Raw body: {raw}")
            result = event.get('body')
            if isinstance(result, str):
                result = json.loads(result)
            result = {
                'body': result.get('message', ''),
                'from': result.get('phone', '')
            }    
            return(result)
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