import resend
import os

resend.api_key = os.environ.get("RESEND_API_KEY", "")

# Must be an address on a domain verified in your Resend account
BOT_FROM = "EXP Bot <bot@yourdomain.com>"
ADMIN_EMAIL = "johnson.ting2022@gmail.com"


def send_viewing_request(
    contact_name: str,
    customer_phone: str,
    property_address: str,
    property_url: str,
    property_price: str,
    agent_name: str,
    agent_phone: str,
    agent_email: str,
    responsible_agent_email: str,
    availability: str,
    last_five_messages: str,
):
    to_email = responsible_agent_email or agent_email or ADMIN_EMAIL
    html = f"""
    <h2>Viewing Request</h2>
    <p><strong>Customer:</strong> {contact_name} ({customer_phone})</p>
    <p><strong>Property:</strong> <a href="{property_url}">{property_address}</a></p>
    <p><strong>Price:</strong> {property_price}</p>
    <p><strong>Availability:</strong> {availability or 'Not specified'}</p>
    <p><strong>Listing agent:</strong> {agent_name} — {agent_phone} — {agent_email}</p>
    <hr>
    <h3>Last 5 messages</h3>
    <pre>{last_five_messages}</pre>
    """
    resend.Emails.send({
        "from": BOT_FROM,
        "to": [to_email],
        "subject": f"Viewing Request — {contact_name} — {property_address}",
        "html": html,
    })
    print(f"[EMAIL] Viewing request sent to {to_email}")


def send_inactive_notification(
    contact_name: str,
    customer_phone: str,
    reason: str,
    responsible_agent_email: str,
    last_five_messages: str,
):
    to_email = responsible_agent_email or ADMIN_EMAIL
    html = f"""
    <h2>Customer No Longer Active</h2>
    <p><strong>Customer:</strong> {contact_name} ({customer_phone})</p>
    <p><strong>Reason:</strong> {reason}</p>
    <hr>
    <h3>Last 5 messages</h3>
    <pre>{last_five_messages}</pre>
    """
    resend.Emails.send({
        "from": BOT_FROM,
        "to": [to_email],
        "subject": f"Customer Inactive — {contact_name} — {reason}",
        "html": html,
    })
    print(f"[EMAIL] Inactive notification sent to {to_email}")


def send_error_alert(customer_name: str, customer_phone: str, error_message: str):
    html = f"""
    <h2>Bot Error Alert</h2>
    <p><strong>Customer:</strong> {customer_name}</p>
    <p><strong>Phone:</strong> {customer_phone}</p>
    <p><strong>Error:</strong> {error_message}</p>
    """
    resend.Emails.send({
        "from": BOT_FROM,
        "to": [ADMIN_EMAIL],
        "subject": f"Bot Error — {error_message[:60]}",
        "html": html,
    })
    print(f"[EMAIL] Error alert sent to {ADMIN_EMAIL}")
