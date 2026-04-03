def format_scraped_properties_into_listings_message(listings: list, count: int = 5) -> str:
    top = listings[:count]
    lines = ["Here are some similar properties I found that I thought you might be interested in:"]
    
    for i, p in enumerate(top, start=1):
        lines.append(
            f"{i}. *{p['address']}*\n"
            f"   - Price: {p['price']}\n"
            f"   - Beds: {p['bedrooms']}\n"
            f"   - Type: {p['property_type']}\n"
            f"   - Status: {p['status']}\n"
            f"   - [View Property]({p['url']})"
        )
    
    lines.append("\nIf you are interested or need more information about any of these properties, feel free to ask!")
    return "\n\n".join(lines)