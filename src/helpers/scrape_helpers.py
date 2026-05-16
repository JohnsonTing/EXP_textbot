import os
import json
import re
from typing import Dict, List, Optional
import requests
from bs4 import BeautifulSoup

SCRAPE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.5",
    "Connection": "keep-alive",
}

# ─────────────────────────────────────────────
# Scraper
# ─────────────────────────────────────────────

def extract_postcode(address: str) -> Optional[str]:
    match = re.search(r'\b([A-Z]{1,2}\d{1,2}[A-Z]?\s?\d[A-Z]{2})\b', address.upper())
    return match.group(1) if match else None


def build_search_url(postcode: str, max_price: int, min_beds: int, prop_type: str, radius: float = 4, listing_type: str = 'sale') -> str:
    from urllib.parse import quote_plus
    location = postcode.strip()
    if re.match(r'^[A-Za-z]{1,2}\d', location):
        location = location.upper().replace(' ', '')
    else:
        location = quote_plus(location)
    base = "properties-for-rent" if listing_type == 'rental' else "properties-for-sale"
    url = f"https://exp.uk.com/{base}/results/?action=search&location={location}&search_radii={radius}"
    price_param = "max-rent" if listing_type == 'rental' else "max-price"
    url += f"&{price_param}={max_price}" if max_price > 0 else f"&{price_param}=0"

    prop_type_map = {
        "any property type": "0",
        "semi-detached house": "3",
        "detached house": "4",
        "flat": "6",
        "bungalow": "8",
        "cottage": "10"
    }
    url += f"&property-type={prop_type_map.get(prop_type.lower(), '0')}"
    url += f"&min-bedrooms={min_beds if min_beds < 6 else 0}"
    return url


def scrape_exp(postcode: str, max_price: int = 0, min_beds: int = 0, prop_type: str = "Any property type", radius: float = 4, listing_type: str = 'sale') -> List[Dict]:
    url = build_search_url(postcode, max_price, min_beds, prop_type, radius, listing_type)
    print(f"[SCRAPER] Built URL: {url}")

    try:
        print(f"[SCRAPER] Sending HTTP request...")
        response = requests.get(url, headers=SCRAPE_HEADERS, timeout=15)
        print(f"[SCRAPER] Response status: {response.status_code}")
        response.raise_for_status()
    except requests.exceptions.Timeout:
        print(f"[SCRAPER] ERROR: Request timed out after 15s")
        return []
    except requests.exceptions.HTTPError as e:
        print(f"[SCRAPER] ERROR: HTTP error {e.response.status_code}: {e}")
        return []
    except requests.exceptions.RequestException as e:
        print(f"[SCRAPER] ERROR: Request failed: {e}")
        return []

    try:
        soup = BeautifulSoup(response.text, 'html.parser')
        print(f"[SCRAPER] Page parsed, searching for aProperties...")

        aProperties = []
        for script in soup.find_all('script'):
            if script.string and 'aProperties' in script.string:
                match = re.search(r'const aProperties = (\[.*?\]);', script.string, re.DOTALL)
                if match:
                    aProperties = json.loads(match.group(1))
                    print(f"[SCRAPER] Found aProperties block with {len(aProperties)} items")

        if not aProperties:
            print(f"[SCRAPER] No aProperties found in page scripts")
            return []

        results = []
        for p in aProperties:
            try:
                price_html = p['html_rent'] if listing_type == 'rental' else p['html_price']
                price_text = BeautifulSoup(price_html, 'html.parser').get_text()
                price_match = re.search(r'£[\d,]+', price_text)
                type_match = re.search(r'\d+\s*bedroom[s]?\s+(.+)', p['description'], re.IGNORECASE)
                prop_type_clean = type_match.group(1).strip() if type_match else "Unknown"
                postcode_clean = extract_postcode(p['display_address']) or ''

                status_code = p.get('rental_status') if listing_type == 'rental' else p.get('status_code')
                if listing_type == 'rental':
                    status = "To Let" if status_code == 2 else "Let Agreed"
                else:
                    status = "For Sale" if status_code == 2 else "Sold STC"

                results.append({
                    'address':       p['display_address'],
                    'status':        status,
                    'price':         price_match.group(0) if price_match else 'POA',
                    'postcode':      postcode_clean,
                    'bedrooms':      p['bedrooms'],
                    'bathrooms':     p['bathrooms'],
                    'receptions':    p['receptionrooms'],
                    'property_type': prop_type_clean,
                    'agent_name':    p['agent_name'],
                    'agent_phone':   p['agent_phone'],
                    'agent_email':   p['agent_email'],
                    'url':           "https://exp.uk.com" + p['property_url_part'],
                })
            except Exception as e:
                print(f"[SCRAPER] WARNING: Skipping malformed property entry: {e}")
                continue

        print(f"[SCRAPER] Returning {len(results)} properties")
        return results

    except Exception as e:
        print(f"[SCRAPER] ERROR parsing page content: {e}")
        return []

def scrape_property_details_from_url(property_url: str):
    print(f"[SCRAPER] Scraping property details from URL: {property_url}")
    response = requests.get(property_url, headers=SCRAPE_HEADERS, timeout=30)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    # Grab the property-features section
    property_features_sections = soup.find_all("section", id="property-features")
    property_info_sections = soup.find_all("section", id="property-info")
    property_description_sections = soup.find_all("section", id="property-details")
    
    # Log which sections were found and which were missing
    sections = {
        "property-features": property_features_sections,
        "property-info": property_info_sections,
        "property-details": property_description_sections,
    }
    missing = [name for name, section in sections.items() if not section]
    if missing:
        print(f"[SCRAPER] ERROR: Missing sections: {', '.join(missing)}")

    # Initiate a dictionary to hold all the extracted details
    total_property_details = {}

    # PROPERTY FEATURES. Extract all list items from the overview ul
    property_features = property_features_sections[0].find("ul", class_="overview") if property_features_sections else None
    property_features_string = ""
    if property_features:
        items = property_features.find_all("li")
        print(f"\nProperty Composition ({len(items)} items):")
        for item in items:
            text = item.get_text(strip=True)
            property_features_string += text + "; "
            if text:
                print(f"  - {text}")
        total_property_details["Property Composition"] = property_features_string

    # PROPERTY INFO. Extract all list items from the features ul
    property_info = property_info_sections[0].find("ul", class_="features") if property_info_sections else None
    property_info_string = ""
    if property_info:
        info_items = property_info.find_all("li")
        print(f"\nProperty Info ({len(info_items)} items):")
        for item in info_items:
            text = item.get_text(strip=True)
            property_info_string += text + "; "
            if text:
                print(f"  - {text}")
    total_property_details["Property Info"] = property_info_string

    # PROPERTY DETAILS. Extract all list items from the description section
    property_details = property_description_sections[0] if property_description_sections else None
    property_description_string = ""
    if property_details:
        description_items = property_details.find_all("p")
        print(f"\nProperty Description ({len(description_items)} items):")
        for item in description_items:
            text = item.get_text(strip=True)
            property_description_string += text + "; "
            if text:
                print(f"  - {text}")
    # Clean up the description string by removing duplicates and unnecessary parts                            
    try:
        property_description_string = property_description_string.encode('raw_unicode_escape').decode('utf-8')
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass

    import re

    # Split on semicolons, normalize whitespace, deduplicate
    sentences = [re.sub(r'\s+', ' ', s).strip() for s in property_description_string.split(';') if s.strip()]
    seen = []
    for s in sentences:
        if s not in seen:
            seen.append(s)

    property_description_string = ' '.join(seen).strip()
    # Put the cleaned description back into the total details
    total_property_details["Property Description"] = property_description_string

    print(f"\nTotal extracted details: {total_property_details}")
    return total_property_details

def find_specific_agent_listing_from_loop_postcode(postcode: str, simple_address: str = "",responsible_agent_email: str = "johnson.ting2022@gmail.com", max_price: int = 0, min_beds: int = 0, prop_type: str = "Any property type", radius: float = 4, listing_type: str = "sale") -> Dict:
    url = build_search_url(postcode, max_price, min_beds, prop_type, radius, listing_type)
    print(f"[SCRAPER] Built URL: {url}")

    try:
        print(f"[SCRAPER] Sending HTTP request...")
        response = requests.get(url, headers=SCRAPE_HEADERS, timeout=15)
        print(f"[SCRAPER] Response status: {response.status_code}")
        response.raise_for_status()
    except requests.exceptions.Timeout:
        print(f"[SCRAPER] ERROR: Request timed out after 15s")
        return {}
    except requests.exceptions.HTTPError as e:
        print(f"[SCRAPER] ERROR: HTTP error {e.response.status_code}: {e}")
        return {}
    except requests.exceptions.RequestException as e:
        print(f"[SCRAPER] ERROR: Request failed: {e}")
        return {}

    try:
        soup = BeautifulSoup(response.text, 'html.parser')
        print(f"[SCRAPER] Page parsed, searching for aProperties...")

        aProperties = []
        for script in soup.find_all('script'):
            if script.string and 'aProperties' in script.string:
                match = re.search(r'const aProperties = (\[.*?\]);', script.string, re.DOTALL)
                if match:
                    aProperties = json.loads(match.group(1))
                    print(f"[SCRAPER] Found aProperties block with {len(aProperties)} items")

        if not aProperties:
            print(f"[SCRAPER] No aProperties found in page scripts")
            return {}

        result = {}
        properties_same_postcode_and_agent = []

        for p in aProperties:
            try:
                #if p.get('agent_email') == responsible_agent_email:
                if p.get('agent_email') == responsible_agent_email or p.get('agent_email') == responsible_agent_email.replace("exp.uk","expuk"):
                    print(f"Found the responsible agent's property: {p['display_address']}")
                    properties_same_postcode_and_agent.append(p)
            except Exception as e:
                print(f"[SCRAPER] WARNING: Skipping malformed property entry: {e}")
                continue
        
        for p in properties_same_postcode_and_agent:
            if simple_address in p['display_address']:
                price_html = p['html_rent'] if listing_type == 'rental' else p['html_price']
                price_text = BeautifulSoup(price_html, 'html.parser').get_text()
                price_match = re.search(r'£[\d,]+', price_text)
                type_match = re.search(r'\d+\s*bedroom[s]?\s+(.+)', p['description'], re.IGNORECASE)
                prop_type_clean = type_match.group(1).strip() if type_match else "Unknown"
                postcode_clean = extract_postcode(p['display_address']) or ''

                status_code = p.get('rental_status') if listing_type == 'rental' else p.get('status_code')
                if listing_type == 'rental':
                    status = "To Let" if status_code == 2 else "Let Agreed"
                else:
                    status = "For Sale" if status_code == 2 else "Sold STC"
                result = {
                    'address':       p['display_address'],
                    'status':        status,
                    'price':         price_match.group(0) if price_match else 'POA',
                    'postcode':      postcode_clean,
                    'bedrooms':      p['bedrooms'],
                    'bathrooms':     p['bathrooms'],
                    'receptions':    p['receptionrooms'],
                    'property_type': prop_type_clean,
                    'agent_name':    p['agent_name'],
                    'agent_phone':   p['agent_phone'],
                    'agent_email':   p['agent_email'],
                    'url':           "https://exp.uk.com" + p['property_url_part'],
                }
                print(f"Matched property with same agent, simple address and postcode: {result}")
                break  # Stop after finding the first matching property with the same agent and postcode

        print(f"Results for the responsible agent's properties: {result}")
        return result

    except Exception as e:
        print(f"[SCRAPER] ERROR parsing page content: {e}")
        return {}

def find_responsible_agent_and_listing_from_enquiry_details(postcode: str, listing_type: str, simple_address: str, max_price: int = 0, min_beds: int = 0, prop_type: str = "Any property type",radius: float = 1) -> Dict:
    url = build_search_url(postcode, max_price, min_beds, prop_type, radius, listing_type)
    print(f"[SCRAPER] Built URL: {url}")

    try:
        print(f"[SCRAPER] Sending HTTP request...")
        response = requests.get(url, headers=SCRAPE_HEADERS, timeout=15)
        print(f"[SCRAPER] Response status: {response.status_code}")
        response.raise_for_status()
    except requests.exceptions.Timeout:
        print(f"[SCRAPER] ERROR: Request timed out after 15s")
        return {}
    except requests.exceptions.HTTPError as e:
        print(f"[SCRAPER] ERROR: HTTP error {e.response.status_code}: {e}")
        return {}
    except requests.exceptions.RequestException as e:
        print(f"[SCRAPER] ERROR: Request failed: {e}")
        return {}

    try:
        soup = BeautifulSoup(response.text, 'html.parser')
        print(f"[SCRAPER] Page parsed, searching for aProperties...")

        aProperties = []
        for script in soup.find_all('script'):
            if script.string and 'aProperties' in script.string:
                match = re.search(r'const aProperties = (\[.*?\]);', script.string, re.DOTALL)
                if match:
                    aProperties = json.loads(match.group(1))
                    print(f"[SCRAPER] Found aProperties block with {len(aProperties)} items")

        if not aProperties:
            print(f"[SCRAPER] No aProperties found in page scripts")
            return {}

        result = {}
        for p in aProperties:
            try:
                if simple_address in p.get('display_address'):
                    price_html = p['html_rent'] if listing_type == 'rental' else p['html_price']
                    print(f"Found the responsible agent's property: {p['display_address']} - {price_html}")

                    price_text = BeautifulSoup(price_html, 'html.parser').get_text()
                    price_match = re.search(r'£[\d,]+', price_text)
                    type_match = re.search(r'\d+\s*bedroom[s]?\s+(.+)', p['description'], re.IGNORECASE)
                    prop_type_clean = type_match.group(1).strip() if type_match else "Unknown"
                    postcode_clean = extract_postcode(p['display_address']) or ''

                    status_code = p.get('rental_status') if listing_type == 'rental' else p.get('status_code')
                    if listing_type == 'rental':
                        status = "To Let" if status_code == 2 else "Let Agreed"
                    else:
                        status = "For Sale" if status_code == 2 else "Sold STC"

                    result = {
                        'address':       p['display_address'],
                        'status':        status,
                        'price':         price_match.group(0) if price_match else 'POA',
                        'postcode':      postcode_clean,
                        'bedrooms':      p['bedrooms'],
                        'bathrooms':     p['bathrooms'],
                        'receptions':    p['receptionrooms'],
                        'property_type': prop_type_clean,
                        'agent_name':    p['agent_name'],
                        'agent_phone':   p['agent_phone'],
                        'agent_email':   p['agent_email'],
                        'url':           "https://exp.uk.com" + p['property_url_part'],
                    }

            except Exception as e:
                print(f"[SCRAPER] WARNING: Skipping malformed property entry: {e}")
                continue

        print(f"Results for the responsible agent's properties: {result}")
        return result

    except Exception as e:
        print(f"[SCRAPER] ERROR parsing page content: {e}")
        return {}
