def format_scraped_properties_into_listings_message(listings: list, count: int = 3) -> str:
    top = listings[:count]
    lines = ["Here are some similar properties I found that I thought you might be interested in:"]
    
    for i, p in enumerate(top, start=1):
        lines.append(
            f"{i}. {p['address']}, {p['bedrooms']}bed {p['property_type']} at {p['price']}. {p['url']}"
        )
    
    lines.append("\nIf you are interested or need more information about any of these properties, feel free to ask.")
    return "\n\n".join(lines)

def make_readable_conversation_history(messages):
    formatted = []
    for msg in messages:
        role = msg.get("role", "").capitalize()
        content = msg.get("content", "")
        
        if role == "Assistant":
            label = "🏠 Agent"
        else:
            label = "👤 Customer"
            
        formatted.append(f"{label}: {content}")
    
    return "\n\n".join(formatted)