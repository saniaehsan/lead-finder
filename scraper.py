import html
import math
import re
from concurrent.futures import ThreadPoolExecutor
import requests
from urllib.parse import quote_plus, unquote, urlparse


def build_maps_link(place_id, name="", address=""):
    """Official Google Maps URL that opens this exact place.

    Google's Maps URL API requires the `query` parameter;
    `query_place_id` alone is not enough. Passing the business
    name + address as `query` and the Place ID as
    `query_place_id` opens the correct location page.
    """
    query = quote_plus(f"{name} {address}".strip() or "place")
    link = (
        "https://www.google.com/maps/search/"
        f"?api=1&query={query}"
    )
    if place_id:
        link += f"&query_place_id={quote_plus(str(place_id))}"
    return link


def latest_review_years_ago(reviews):
    """Age of the newest returned review, ONLY if it is in years.

    Google gives text like "a year ago", "3 years ago",
    "2 months ago", "a week ago". Only the "year" ones are kept;
    months / weeks / days / hours are ignored (returns "").
    """
    newest_time = ""
    newest_desc = ""

    for review in reviews or []:
        publish_time = review.get("publishTime", "") or ""
        desc = review.get("relativePublishTimeDescription", "") or ""

        if publish_time >= newest_time:
            newest_time = publish_time
            newest_desc = desc

    if "year" in newest_desc.lower():
        return newest_desc

    return ""


def format_reviews(count, reviews):
    """'133' or '133 (2 years ago)' (years only)."""
    if count is None or count == "":
        return ""

    years_ago = latest_review_years_ago(reviews)

    if years_ago:
        return f"{count} ({years_ago})"

    return str(count)


# ============================================================
# EMAIL FINDER (reads the business's own public website)
# ============================================================
# Google Places does NOT provide email addresses, so the email is
# looked up on the business website: home page first, then the
# usual contact/about pages. Only emails written publicly on the
# site are collected.

EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"
)

BAD_EMAIL_PARTS = (
    "example.", "sentry", "wixpress", "domain.com", "email.com",
    "yourdomain", "your-email", "yourname", "godaddy", "schema.org",
    "w3.org", "noreply", "no-reply", "donotreply"
)

BAD_EMAIL_EXT = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js"
)

CONTACT_PATHS = [
    "", "/contact", "/contact-us", "/contact-us/",
    "/about", "/about-us"
]

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}


def extract_emails(text):
    """All plausible email addresses found in a chunk of HTML/text."""
    text = html.unescape(unquote(text or ""))
    found = []

    for match in EMAIL_RE.findall(text):
        email = match.strip(".").lower()

        if email.endswith(BAD_EMAIL_EXT):
            continue

        if any(bad in email for bad in BAD_EMAIL_PARTS):
            continue

        if email not in found:
            found.append(email)

    return found


def find_email(website, timeout=6):
    """Best email found on a business website, or ''."""
    if not website or not str(website).lower().startswith(
        ("http://", "https://")
    ):
        return ""

    parsed = urlparse(website)
    root = f"{parsed.scheme}://{parsed.netloc}"
    site_domain = parsed.netloc.lower()

    if site_domain.startswith("www."):
        site_domain = site_domain[4:]

    candidates = []

    for i, path in enumerate(CONTACT_PATHS):

        url = website if path == "" else root + path

        try:
            response = requests.get(
                url,
                headers=REQUEST_HEADERS,
                timeout=timeout,
                allow_redirects=True
            )
        except requests.RequestException:
            if i == 0:
                # Home page unreachable -> don't waste time on the rest
                return ""
            continue

        content_type = response.headers.get("Content-Type", "")

        if response.status_code != 200 or "text" not in content_type:
            continue

        for email in extract_emails(response.text[:500000]):
            if email not in candidates:
                candidates.append(email)

        # Prefer an email on the business's own domain
        own = [
            e for e in candidates
            if e.split("@")[-1].endswith(site_domain)
        ]

        if own:
            return own[0]

    return candidates[0] if candidates else ""


# ============================================================
# COMPETITOR COLUMNS
# ============================================================

def _haversine_km(lat1, lng1, lat2, lng2):
    """Distance between two lat/lng points in kilometres."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = (
        math.sin(dp / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(a))


def _coords(rec):
    try:
        lat = float(rec.get("Latitude"))
        lng = float(rec.get("Longitude"))
    except (TypeError, ValueError):
        return None
    if math.isnan(lat) or math.isnan(lng):
        return None
    return lat, lng


def _has_website(rec):
    return str(rec.get("Website", "") or "").lower().startswith(
        ("http://", "https://")
    )


def _rating(rec):
    r = rec.get("Rating")
    try:
        r = float(r)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(r) else r


def add_competitor_columns(
    df,
    radius_km=5,
    max_nearby=5,
    max_with_website=3
):
    """Adds two columns using all saved leads as the pool:

    - "Nearby Competitors": same business type, ONLY ones with a
      website, within radius_km, nearest first -> "Name (1.2 km) | Name (2.0 km)"
    - "Competitors With Well-Designed Websites": nearby competitors
      that HAVE a website -> only the best rated one's link (clickable)
    """
    df = df.copy()
    nearby_col, web_col = [], []

    records = df.to_dict("records")

    for i, rec in enumerate(records):
        me = _coords(rec)

        if me is None:
            nearby_col.append("")
            web_col.append("")
            continue

        cands = []

        for j, other in enumerate(records):
            if j == i or other.get("Place ID") == rec.get("Place ID"):
                continue
            if other.get("Business Type") != rec.get("Business Type"):
                continue
            # Only competitors that actually have a website.
            if not _has_website(other):
                continue
            pt = _coords(other)
            if pt is None:
                continue
            d = _haversine_km(me[0], me[1], pt[0], pt[1])
            if d <= radius_km:
                cands.append((d, other))

        cands.sort(key=lambda x: x[0])

        nearby_col.append(" | ".join(
            f"{o.get('Business Name', '')} ({d:.1f} km)"
            for d, o in cands[:max_nearby]
        ))

        with_site = list(cands)
        with_site.sort(key=lambda x: (-_rating(x[1]), x[0]))

        # Only the website link of the best nearby competitor, so the
        # cell is a single clickable link.
        web_col.append(
            with_site[0][1].get("Website", "") if with_site else ""
        )

    df["Nearby Competitors"] = nearby_col
    df["Competitors With Well-Designed Websites"] = web_col
    return df



# ============================================================
# OWNER FINDER
# ============================================================
# Step 1: look for "<Name>, owner" / "owner <Name>" style phrases
#         in the (up to 5) Google reviews Places API returns.
# Step 2: (optional, needs a SerpAPI key) Google-search
#         "<business>" <city> owner and read the result titles /
#         snippets - these include BBB, Yelp, Facebook, Angi,
#         license directories etc. without scraping those sites.
# Results are best-effort: always verify before outreach.

_ROLES = (
    r"(?i:co-owner|owner|founder|president|general manager|"
    r"manager|operator|master plumber|ceo)"
)

# 1-3 capitalised words, e.g. "Mike", "Mike Smith", "Mike J. Smith"
_NAME = r"([A-Z][a-z]{1,15}(?:\s[A-Z]\.)?(?:\s[A-Z][a-z]{1,20})?)"

_PAT_NAME_THEN_ROLE = re.compile(
    _NAME + r"\s*(?:,|-|\u2013|\()?\s*(?:is\s+)?(?:the\s+|our\s+)?("
    + _ROLES + r")\b"
)
_PAT_ROLE_THEN_NAME = re.compile(
    r"\b(" + _ROLES + r")\s*(?:is|:|,|-|\u2013)?\s*" + _NAME
)

_NOT_NAMES = {
    "The", "Our", "Great", "Very", "Best", "Good", "Highly", "Super",
    "Friendly", "Professional", "Awesome", "Amazing", "Excellent",
    "Family", "Local", "Small", "New", "Real", "Business", "Company",
    "Plumbing", "Service", "Services", "Manager", "Owner", "Customer",
    "Contact", "About", "Meet", "And", "But", "Also", "Thanks", "Thank",
    "Plumber", "Plumbers", "Master", "General", "Called", "Call",
    "Got", "Had", "Was", "Were", "His", "Her", "Their", "This", "That",
    "Facebook", "Yelp", "Angi", "Bbb", "Google", "Reviews", "Review"
}


def _clean_name(name, business_name=""):
    name = (name or "").strip(" .,-")
    parts = name.split()

    if not parts or parts[0] in _NOT_NAMES:
        return ""

    if any(p in _NOT_NAMES for p in parts):
        return ""

    # Skip if it is just part of the business name itself
    biz = (business_name or "").lower()
    if biz and name.lower() in biz:
        return ""

    return name


def owner_from_text(text, business_name=""):
    """(name, role) found in a block of text, or ('', '')."""
    text = html.unescape(text or "")

    for m in _PAT_NAME_THEN_ROLE.finditer(text):
        name = _clean_name(m.group(1), business_name)
        if name:
            return name, m.group(2).title()

    for m in _PAT_ROLE_THEN_NAME.finditer(text):
        name = _clean_name(m.group(2), business_name)
        if name:
            return name, m.group(1).title()

    return "", ""


def owner_from_reviews(reviews, business_name=""):
    for review in reviews or []:
        body = (
            (review.get("text") or {}).get("text", "")
            or (review.get("originalText") or {}).get("text", "")
        )
        name, role = owner_from_text(body, business_name)
        if name:
            return name, role, "Google review"
    return "", "", ""


def owner_from_web(business_name, area, serp_key, timeout=15):
    """Search the web (SerpAPI) and read titles + snippets."""
    if not serp_key:
        return "", "", ""

    try:
        resp = requests.get(
            "https://serpapi.com/search.json",
            params={
                "engine": "google",
                "q": f'"{business_name}" {area} owner',
                "num": 10,
                "api_key": serp_key,
            },
            timeout=timeout,
        )
        if resp.status_code != 200:
            return "", "", ""
        results = resp.json().get("organic_results", [])
    except (requests.RequestException, ValueError):
        return "", "", ""

    for r in results:
        blob = f"{r.get('title', '')}. {r.get('snippet', '')}"
        name, role = owner_from_text(blob, business_name)
        if name:
            domain = urlparse(r.get("link", "")).netloc.replace("www.", "")
            return name, role, f"Web ({domain})"

    return "", "", ""


def find_owner(business_name, reviews, area, serp_key=None):
    name, role, src = owner_from_reviews(reviews, business_name)
    if name:
        return name, role, src
    return owner_from_web(business_name, area, serp_key)

# ============================================================
# GOOGLE PLACES SEARCH
# ============================================================

def search_google_maps(
    api_key,
    business_type,
    location,
    only_no_website=False,
    page_token=None,
    find_owners=True,
    serp_key=None,
    area=""
):

    URL = "https://places.googleapis.com/v1/places:searchText"

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": (
            "places.displayName,"
            "places.formattedAddress,"
            "places.id,"
            "places.websiteUri,"
            "places.internationalPhoneNumber,"
            "places.nationalPhoneNumber,"
            "places.rating,"
            "places.userRatingCount,"
            "places.reviews,"
            "places.location,"
            "nextPageToken"
        )
    }

    data = {
        "textQuery": f"{business_type} in {location}",
        "pageSize": 20
    }

    if page_token:
        data["pageToken"] = page_token

    try:
        response = requests.post(
            URL,
            headers=headers,
            json=data,
            timeout=30
        )

    except requests.RequestException as e:
        return {
            "success": False,
            "error": str(e),
            "leads": [],
            "next_page_token": None
        }

    if response.status_code != 200:
        return {
            "success": False,
            "error": response.text,
            "leads": [],
            "next_page_token": None
        }

    result = response.json()
    places = result.get("places", [])
    leads = []
    raw_reviews = []

    for place in places:

        place_id = place.get("id", "")

        if not place_id:
            continue

        name = place.get(
            "displayName",
            {}
        ).get(
            "text",
            ""
        )

        address = place.get(
            "formattedAddress",
            ""
        )

        website = place.get(
            "websiteUri",
            ""
        )

        # Make sure the website is a full, absolute URL
        # (http:// or https://). Streamlit's LinkColumn only
        # turns a cell into a clickable link when the value
        # starts with a valid scheme, so a bare domain like
        # "example.com" would otherwise show as plain text.
        if website and not website.lower().startswith(
            ("http://", "https://")
        ):
            website = f"https://{website}"

        phone = place.get(
            "internationalPhoneNumber",
            ""
        )

        if not phone:
            phone = place.get(
                "nationalPhoneNumber",
                ""
            )

        rating = place.get("rating", None)

        reviews_text = format_reviews(
            place.get("userRatingCount", None),
            place.get("reviews", [])
        )

        if only_no_website and website:
            continue

        google_maps_link = build_maps_link(
            place_id,
            name,
            address
        )

        loc = place.get("location", {}) or {}

        leads.append({
            "Business Name": name,
            "Phone Number": phone,
            "Email": "",
            "Owner Name": "",
            "Owner Role": "",
            "Owner Source": "",
            "Address": address,
            "Website": website if website else "NO WEBSITE",
            "Rating": rating,
            "Reviews": reviews_text,
            "Place ID": place_id,
            "Google Maps": google_maps_link,
            "Latitude": loc.get("latitude"),
            "Longitude": loc.get("longitude")
        })
        raw_reviews.append(place.get("reviews", []))

    # Look up emails on the business websites (in parallel for speed)
    sites = [
        lead["Website"] if lead["Website"] != "NO WEBSITE" else ""
        for lead in leads
    ]

    if sites:
        with ThreadPoolExecutor(max_workers=10) as pool:
            emails = list(pool.map(find_email, sites))

        for lead, site, email in zip(leads, sites, emails):
            if not site:
                lead["Email"] = "NO WEBSITE"
            else:
                lead["Email"] = email if email else "NOT FOUND"

    if find_owners and leads:
        with ThreadPoolExecutor(max_workers=5) as pool:
            owners = list(pool.map(
                lambda args: find_owner(
                    args[0]["Business Name"], args[1], area, serp_key
                ),
                zip(leads, raw_reviews)
            ))

        for lead, (o_name, o_role, o_src) in zip(leads, owners):
            lead["Owner Name"] = o_name or "NOT FOUND"
            lead["Owner Role"] = o_role
            lead["Owner Source"] = o_src

    next_page_token = result.get("nextPageToken")

    return {
        "success": True,
        "leads": leads,
        "total_results_checked": len(places),
        "next_page_token": next_page_token,
        "has_more_pages": bool(next_page_token)
    }