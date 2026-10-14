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


# ---- Social media links (found on the same website pages) ------

SOCIAL_SITES = (
    ("Facebook", ("facebook.com", "fb.com")),
    ("Instagram", ("instagram.com",)),
    ("X/Twitter", ("twitter.com", "x.com")),
    ("LinkedIn", ("linkedin.com",)),
    ("YouTube", ("youtube.com", "youtu.be")),
    ("TikTok", ("tiktok.com",)),
    ("Pinterest", ("pinterest.com",)),
    ("Yelp", ("yelp.com",)),
)

SOCIAL_LINK_RE = re.compile(
    r"https?://(?:[a-z0-9\-]+\.)*"
    r"(?:facebook\.com|fb\.com|instagram\.com|twitter\.com|x\.com|"
    r"linkedin\.com|youtube\.com|youtu\.be|tiktok\.com|"
    r"pinterest\.com|yelp\.com)/[^\s\"'<>\\)\]]+",
    re.I
)

# Share buttons, embeds, single posts, login pages etc. are not the
# business's own profile.
BAD_SOCIAL_PARTS = (
    "sharer", "share", "intent/", "/plugins/", "/tr?", "dialog",
    "embed", "watch", "login", "signup", "privacy", "policies",
    "hashtag", "explore", "/p/", "/reel", "/status/", "/posts/",
    "/videos/", "/photo", "/events/", "/groups/"
)


def extract_socials(text):
    """{'Facebook': url, 'Instagram': url, ...} found in HTML/text."""
    text = html.unescape(text or "").replace("\\/", "/")
    found = {}

    for match in SOCIAL_LINK_RE.findall(text):
        url = match.split("?")[0].split("#")[0].rstrip("/.,;")
        low = url.lower()

        if any(bad in low for bad in BAD_SOCIAL_PARTS):
            continue

        host = urlparse(url).netloc.lower()

        for platform, domains in SOCIAL_SITES:
            if any(host == d or host.endswith("." + d) for d in domains):
                found.setdefault(platform, url)
                break

    return found


def format_socials(socials):
    """'Facebook: https://... | Instagram: https://...'"""
    return " | ".join(f"{p}: {u}" for p, u in socials.items())


def scan_website(website, timeout=6):
    """(email, socials_dict) found on a business website."""
    if not website or not str(website).lower().startswith(
        ("http://", "https://")
    ):
        return "", {}

    parsed = urlparse(website)
    root = f"{parsed.scheme}://{parsed.netloc}"
    site_domain = parsed.netloc.lower()

    if site_domain.startswith("www."):
        site_domain = site_domain[4:]

    candidates = []
    socials = {}

    def own_emails():
        return [
            e for e in candidates
            if e.split("@")[-1].endswith(site_domain)
        ]

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
                return "", {}
            continue

        content_type = response.headers.get("Content-Type", "")

        if response.status_code != 200 or "text" not in content_type:
            continue

        page = response.text[:500000]

        for email in extract_emails(page):
            if email not in candidates:
                candidates.append(email)

        for platform, link in extract_socials(page).items():
            socials.setdefault(platform, link)

        # Stop early once both an own-domain email and a social
        # link have been found.
        if own_emails() and socials:
            break

    own = own_emails()
    email = own[0] if own else (candidates[0] if candidates else "")

    return email, socials


# ============================================================
# REVIEW QUALITY / SERVICE ISSUES
# ============================================================
# Google Places returns at most 5 reviews per business, so the
# "issues" part only reflects those recent reviews (low-rated ones).

ISSUE_PATTERNS = (
    ("Late / no-show", re.compile(
        r"\b(?:late|no[- ]?show|never (?:showed|came|arrived)|"
        r"(?:didn'?t|did not|never) (?:show|come|arrive)|"
        r"waited|reschedul\w*|delay\w*)\b", re.I)),
    ("Pricing / overcharging", re.compile(
        r"\b(?:overcharg\w*|over[- ]?priced|rip(?:ped)?[- ]?off|"
        r"too expensive|hidden fees?|extra charges?|scam\w*)\b", re.I)),
    ("Poor communication / rude", re.compile(
        r"\b(?:no response|never (?:called|responded|returned|answered)|"
        r"(?:didn'?t|did not) (?:call|respond|return|answer)|"
        r"unresponsive|ghost\w*|rude|unprofessional|"
        r"disrespectful)\b", re.I)),
    ("Poor work quality", re.compile(
        r"\b(?:poor quality|shoddy|sloppy|botched|"
        r"(?:not|never|wasn'?t) (?:fixed|repaired|resolved)|"
        r"still (?:leak\w*|broken|not working)|"
        r"had to (?:redo|call someone else|hire)|"
        r"damage[d]?|made (?:it|things) worse)\b", re.I)),
)


def _review_text(review):
    return (
        (review.get("text") or {}).get("text", "")
        or (review.get("originalText") or {}).get("text", "")
        or ""
    )


def review_quality(rating, count, reviews):
    """'Good (4.6★, 133 reviews) | Issues: Late / no-show (2 of 5 ...)'"""
    try:
        r = float(rating)
    except (TypeError, ValueError):
        return "NO RATING"

    if r >= 4.5:
        label = "Excellent"
    elif r >= 4.0:
        label = "Good"
    elif r >= 3.5:
        label = "Average"
    else:
        label = "Poor"

    head = f"{label} ({r:.1f}\u2605"
    if count not in (None, ""):
        head += f", {count} reviews"
    head += ")"

    reviews = reviews or []

    if not reviews:
        return head + " | No review text available"

    low = [
        rv for rv in reviews
        if (rv.get("rating") if rv.get("rating") is not None else 5) <= 3
    ]

    if not low:
        return head + " | No issues in recent reviews"

    issues = []
    for rv in low:
        text = _review_text(rv)
        for name, pattern in ISSUE_PATTERNS:
            if name not in issues and pattern.search(text):
                issues.append(name)

    ratio = f"{len(low)} of {len(reviews)} recent reviews \u22643\u2605"

    if issues:
        return f"{head} | Issues: {', '.join(issues)} ({ratio})"

    return f"{head} | Low ratings, reason unclear ({ratio})"


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
# GOOGLE PLACES SEARCH
# ============================================================

def search_google_maps(
    api_key,
    business_type,
    location,
    only_no_website=False,
    page_token=None
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
            "Social Media Presence": "",
            "Address": address,
            "Website": website if website else "NO WEBSITE",
            "Rating": rating,
            "Reviews": reviews_text,
            "Review Quality / Service Issues": review_quality(
                rating,
                place.get("userRatingCount", None),
                place.get("reviews", [])
            ),
            "Place ID": place_id,
            "Google Maps": google_maps_link,
            "Latitude": loc.get("latitude"),
            "Longitude": loc.get("longitude")
        })

    # Look up emails on the business websites (in parallel for speed)
    sites = [
        lead["Website"] if lead["Website"] != "NO WEBSITE" else ""
        for lead in leads
    ]

    if sites:
        with ThreadPoolExecutor(max_workers=10) as pool:
            scans = list(pool.map(scan_website, sites))

        for lead, site, (email, socials) in zip(leads, sites, scans):
            if not site:
                lead["Email"] = "NO WEBSITE"
                lead["Social Media Presence"] = "NO WEBSITE"
            else:
                lead["Email"] = email if email else "NOT FOUND"
                lead["Social Media Presence"] = (
                    format_socials(socials) or "NOT FOUND"
                )

    next_page_token = result.get("nextPageToken")

    return {
        "success": True,
        "leads": leads,
        "total_results_checked": len(places),
        "next_page_token": next_page_token,
        "has_more_pages": bool(next_page_token)
    }