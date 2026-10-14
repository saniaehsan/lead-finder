import html
import math
import re
from concurrent.futures import ThreadPoolExecutor
import requests
from urllib.parse import quote_plus, unquote, urljoin, urlparse


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


CF_RE = re.compile(r'data-cfemail="([0-9a-fA-F]+)"')
MAILTO_RE = re.compile(r"mailto:([^\"'?\s<>]+)", re.I)
OBFUSCATED_RE = re.compile(
    r"([A-Za-z0-9._%+\-]+)\s*[\[\(\{]\s*at\s*[\]\)\}]\s*"
    r"([A-Za-z0-9.\-]+)\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*([A-Za-z]{2,})",
    re.I
)


def _decode_cloudflare(hexstr):
    """Cloudflare 'email protection' -> real address."""
    try:
        key = int(hexstr[:2], 16)
        return "".join(
            chr(int(hexstr[i:i + 2], 16) ^ key)
            for i in range(2, len(hexstr), 2)
        )
    except ValueError:
        return ""


def extract_emails(text):
    """All plausible email addresses in HTML/text.

    Order = most trustworthy first: mailto: links, Cloudflare-
    protected emails, plain text emails, then "name [at] site [dot] com".
    """
    raw = html.unescape(text or "")
    text = unquote(raw)
    candidates = []

    candidates += [unquote(m) for m in MAILTO_RE.findall(raw)]
    candidates += [_decode_cloudflare(h) for h in CF_RE.findall(raw)]
    candidates += EMAIL_RE.findall(text)
    candidates += [
        f"{a}@{b}.{c}" for a, b, c in OBFUSCATED_RE.findall(text)
    ]

    found = []

    for match in candidates:
        email = match.strip(".").lower()

        if not EMAIL_RE.fullmatch(email):
            continue

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


def format_socials(socials, stats=None):
    """'Facebook: https://... (1.2K likes) | Instagram: https://...'"""
    stats = stats or {}
    return " | ".join(
        f"{p}: {u}" + (f" ({stats[p]})" if stats.get(p) else "")
        for p, u in socials.items()
    )


CONTACT_HINTS = (
    "contact", "about", "quote", "schedule", "request", "team",
    "book", "estimate", "reach"
)
HREF_RE = re.compile(r"href=[\"']([^\"'#]+)", re.I)
MAX_PAGES = 8


def _discover_pages(base_url, page, site_domain):
    """Internal links on a page that look like contact/about pages."""
    out = []
    for href in HREF_RE.findall(page[:300000]):
        if href.lower().startswith(("mailto:", "tel:", "javascript:")):
            continue
        full = urljoin(base_url, href)
        p = urlparse(full)
        host = p.netloc.lower().replace("www.", "", 1)
        if host != site_domain:
            continue
        if any(h in p.path.lower() for h in CONTACT_HINTS):
            clean = f"{p.scheme}://{p.netloc}{p.path}"
            if clean not in out:
                out.append(clean)
    return out[:5]


def scan_website(website, timeout=6):
    """(emails, socials_dict) found on a business website.

    Reads the home page, follows contact/about links found on it,
    then falls back to the usual /contact style URLs. Up to 3 emails
    are returned (business-domain emails first), joined by ' | '.
    """
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

    queue = [website]
    seen = set()
    fetched = 0

    while queue and fetched < MAX_PAGES:
        url = queue.pop(0)

        if url.rstrip("/") in seen:
            continue
        seen.add(url.rstrip("/"))

        try:
            response = requests.get(
                url,
                headers=REQUEST_HEADERS,
                timeout=timeout,
                allow_redirects=True
            )
        except requests.RequestException:
            if fetched == 0:
                # Home page unreachable -> don't waste time on the rest
                return "", {}
            continue

        fetched += 1
        content_type = response.headers.get("Content-Type", "")

        if response.status_code != 200 or "text" not in content_type:
            continue

        page = response.text[:500000]

        for email in extract_emails(page):
            if email not in candidates:
                candidates.append(email)

        for platform, link in extract_socials(page).items():
            socials.setdefault(platform, link)

        if fetched == 1:
            # After the home page: links it points to first, then
            # the standard contact paths.
            queue += _discover_pages(response.url, page, site_domain)
            queue += [root + p for p in CONTACT_PATHS if p]

        if own_emails() and socials and fetched >= 2:
            break

    own = own_emails()
    others = [e for e in candidates if e not in own]
    emails = (own + others)[:3]

    return " | ".join(emails), socials


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
# SOCIAL PRESENCE (score, follower counts, web-search fallback)
# ============================================================

MAIN_PLATFORMS = (
    "Facebook", "Instagram", "X/Twitter", "LinkedIn",
    "YouTube", "TikTok", "Pinterest"
)

NAME_STOP = {
    "plumbing", "plumber", "plumbers", "services", "service",
    "company", "heating", "cooling", "repair", "repairs", "drain",
    "drains", "sewer", "water", "the", "and", "inc", "llc"
}

STATS_RE = re.compile(
    r"(\d[\d.,]*\s?[KkMm]?)\s+(followers|likes|subscribers)", re.I
)


def social_score(socials):
    """'Strong (4 platforms)' / 'Moderate' / 'Weak' / 'None found'."""
    n = sum(1 for p in (socials or {}) if p in MAIN_PLATFORMS)

    if n >= 4:
        return f"Strong ({n} platforms)"
    if n >= 2:
        return f"Moderate ({n} platforms)"
    if n == 1:
        return "Weak (1 platform)"
    return "None found"


def profile_stats(url, timeout=6):
    """Best-effort '1.2K likes' / '350 followers' from a public
    Facebook/Instagram page. Returns '' when the platform hides it
    (login wall) - that is normal, not an error."""
    try:
        r = requests.get(url, headers=REQUEST_HEADERS, timeout=timeout)
    except requests.RequestException:
        return ""

    if r.status_code != 200:
        return ""

    for tag in re.findall(r"<meta[^>]+>", r.text[:300000], re.I):
        low = tag.lower()
        if "og:description" in low or 'name="description"' in low:
            m = re.search(r'content="([^"]*)"', tag, re.I)
            if m:
                s = STATS_RE.search(html.unescape(m.group(1)))
                if s:
                    return f"{s.group(1).strip()} {s.group(2).lower()}"
    return ""


def _name_tokens(name):
    return [
        t for t in re.findall(r"[a-z0-9]+", (name or "").lower())
        if len(t) > 3 and t not in NAME_STOP
    ]


def find_socials_via_search(name, city_hint="", timeout=8):
    """For businesses WITHOUT a website: look for their Facebook /
    Instagram page through a web search. Best-effort - a result is
    kept only if the business name appears in the profile URL."""
    tokens = _name_tokens(name)
    found = {}

    if not tokens:
        return found

    for domain in ("facebook.com", "instagram.com"):
        try:
            r = requests.post(
                "https://html.duckduckgo.com/html/",
                data={"q": f"{name} {city_hint} site:{domain}"},
                headers=REQUEST_HEADERS,
                timeout=timeout
            )
        except requests.RequestException:
            continue

        if r.status_code != 200:
            continue

        for enc in re.findall(r"uddg=([^&\"']+)", r.text):
            url = unquote(enc)
            for platform, link in extract_socials(url).items():
                if any(t in link.lower() for t in tokens):
                    found.setdefault(platform, link)

    return found


# ============================================================
# ENRICHMENT (email + social for a list of leads)
# ============================================================

def _city_hint(location):
    parts = [p.strip() for p in (location or "").split(",")]
    if len(parts) >= 4:
        return f"{parts[1]} {parts[3]}"
    return location or ""


def enrich_leads(leads, deep_social=False, city_hint=""):
    """Fills Email, Social Media Presence, Social Presence Score."""
    if not leads:
        return

    sites = [
        l["Website"] if l["Website"] != "NO WEBSITE" else ""
        for l in leads
    ]

    with ThreadPoolExecutor(max_workers=10) as pool:
        scans = list(pool.map(scan_website, sites))

    found_socials = [dict(s[1]) for s in scans]

    if deep_social:
        # No-website businesses: try to find their social pages.
        idx = [i for i, s in enumerate(sites) if not s]
        with ThreadPoolExecutor(max_workers=3) as pool:
            res = list(pool.map(
                lambda i: find_socials_via_search(
                    leads[i]["Business Name"], city_hint
                ),
                idx
            ))
        for i, r in zip(idx, res):
            found_socials[i].update(r)

    stats = [{} for _ in leads]

    if deep_social:
        jobs = [
            (i, p, u)
            for i, soc in enumerate(found_socials)
            for p, u in soc.items()
            if p in ("Facebook", "Instagram")
        ]
        with ThreadPoolExecutor(max_workers=6) as pool:
            res = list(pool.map(lambda j: profile_stats(j[2]), jobs))
        for (i, p, _), r in zip(jobs, res):
            if r:
                stats[i][p] = r

    for lead, site, (email, _), soc, st in zip(
        leads, sites, scans, found_socials, stats
    ):
        if site:
            lead["Email"] = email or "NOT FOUND"
        else:
            lead["Email"] = "NO WEBSITE"

        if soc:
            lead["Social Media Presence"] = format_socials(soc, st)
        elif site or deep_social:
            lead["Social Media Presence"] = "NOT FOUND"
        else:
            lead["Social Media Presence"] = "NO WEBSITE"

        lead["Social Presence Score"] = social_score(soc)


# ============================================================
# GOOGLE PLACES SEARCH
# ============================================================

GOOGLE_URL = "https://places.googleapis.com/v1/places:searchText"

GOOGLE_FIELDS = (
    "places.displayName,"
    "places.formattedAddress,"
    "places.id,"
    "places.websiteUri,"
    "places.internationalPhoneNumber,"
    "places.nationalPhoneNumber,"
    "places.rating,"
    "places.userRatingCount,"
    "places.reviews,"
    "places.location"
)


def _place_to_lead(place):
    place_id = place.get("id", "")

    if not place_id:
        return None

    name = place.get("displayName", {}).get("text", "")
    address = place.get("formattedAddress", "")
    website = place.get("websiteUri", "")

    # Streamlit's LinkColumn only makes a cell clickable when the
    # value starts with a valid scheme.
    if website and not website.lower().startswith(("http://", "https://")):
        website = f"https://{website}"

    phone = (
        place.get("internationalPhoneNumber", "")
        or place.get("nationalPhoneNumber", "")
    )

    rating = place.get("rating", None)
    count = place.get("userRatingCount", None)
    reviews = place.get("reviews", [])
    loc = place.get("location", {}) or {}

    return {
        "Business Name": name,
        "Phone Number": phone,
        "Email": "",
        "Social Media Presence": "",
        "Social Presence Score": "",
        "Address": address,
        "Website": website if website else "NO WEBSITE",
        "Rating": rating,
        "Reviews": format_reviews(count, reviews),
        "Review Quality / Service Issues": review_quality(
            rating, count, reviews
        ),
        "Listed On": "Google Maps",
        "Yelp URL": "",
        "Place ID": place_id,
        "Google Maps": build_maps_link(place_id, name, address),
        "Latitude": loc.get("latitude"),
        "Longitude": loc.get("longitude")
    }


def _google_post(api_key, body, field_mask):
    return requests.post(
        GOOGLE_URL,
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": field_mask
        },
        json=body,
        timeout=30
    )


def search_google_maps(
    api_key,
    business_type,
    location,
    only_no_website=False,
    page_token=None,
    deep_social=False,
    enrich=True
):
    """Google-only search (kept for backwards compatibility)."""
    body = {"textQuery": f"{business_type} in {location}", "pageSize": 20}

    if page_token:
        body["pageToken"] = page_token

    try:
        response = _google_post(
            api_key, body, GOOGLE_FIELDS + ",nextPageToken"
        )
    except requests.RequestException as e:
        return {
            "success": False, "error": str(e),
            "leads": [], "next_page_token": None
        }

    if response.status_code != 200:
        return {
            "success": False, "error": response.text,
            "leads": [], "next_page_token": None
        }

    result = response.json()
    places = result.get("places", [])
    leads = []

    for place in places:
        lead = _place_to_lead(place)
        if lead is None:
            continue
        if only_no_website and lead["Website"] != "NO WEBSITE":
            continue
        leads.append(lead)

    if enrich:
        enrich_leads(leads, deep_social, _city_hint(location))

    next_page_token = result.get("nextPageToken")

    return {
        "success": True,
        "leads": leads,
        "total_results_checked": len(places),
        "next_page_token": next_page_token,
        "has_more_pages": bool(next_page_token)
    }


# ============================================================
# YELP DIRECTORY (official Yelp Fusion API)
# ============================================================
# Yelp lists many plumbers that Google Maps ranks low or skips.
# The API gives name / phone / address / rating / Yelp page but NOT
# the business website, so Yelp-only businesses are looked up on
# Google Places to fetch their website (then emails + socials come
# from that website like every other lead).

YELP_PAGE_SIZE = 20


def _digits(phone):
    d = re.sub(r"\D", "", str(phone or ""))
    return d[-10:] if len(d) >= 10 else ""


def _norm_name(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def search_yelp(yelp_key, business_type, location, offset=0):
    """({'success','leads','next_offset'})"""
    try:
        r = requests.get(
            "https://api.yelp.com/v3/businesses/search",
            headers={"Authorization": f"Bearer {yelp_key}"},
            params={
                "term": business_type,
                "location": location,
                "limit": YELP_PAGE_SIZE,
                "offset": offset
            },
            timeout=30
        )
    except requests.RequestException as e:
        return {"success": False, "error": str(e), "leads": []}

    if r.status_code != 200:
        return {"success": False, "error": r.text, "leads": []}

    data = r.json()
    leads = []

    for b in data.get("businesses", []):
        name = b.get("name", "")
        address = ", ".join(
            (b.get("location") or {}).get("display_address", [])
        )
        rating = b.get("rating")
        count = b.get("review_count")
        coords = b.get("coordinates") or {}

        leads.append({
            "Business Name": name,
            "Phone Number": b.get("display_phone") or b.get("phone", ""),
            "Email": "",
            "Social Media Presence": "",
            "Social Presence Score": "",
            "Address": address,
            "Website": "NO WEBSITE",
            "Rating": rating,
            "Reviews": "" if count is None else str(count),
            "Review Quality / Service Issues": review_quality(
                rating, count, []
            ),
            "Listed On": "Yelp",
            "Yelp URL": (b.get("url") or "").split("?")[0],
            "Place ID": "yelp:" + str(b.get("id", "")),
            "Google Maps": build_maps_link("", name, address),
            "Latitude": coords.get("latitude"),
            "Longitude": coords.get("longitude")
        })

    total = data.get("total", 0)
    next_offset = offset + YELP_PAGE_SIZE
    # Yelp only serves the first 1000 results of any search.
    has_more = next_offset < min(total, 1000)

    return {
        "success": True,
        "leads": leads,
        "next_offset": next_offset if has_more else None
    }


def _same_business(g, y):
    gp, yp = _digits(g.get("Phone Number")), _digits(y.get("Phone Number"))

    if gp and gp == yp:
        return True

    if _norm_name(g.get("Business Name")) != _norm_name(
        y.get("Business Name")
    ):
        return False

    a, b = _coords(g), _coords(y)

    if a and b:
        return _haversine_km(a[0], a[1], b[0], b[1]) < 0.5

    return True


def _resolve_on_google(api_key, ylead):
    """Find a Yelp-only business on Google Places (for its website)."""
    body = {
        "textQuery": f"{ylead['Business Name']} {ylead['Address']}",
        "pageSize": 3
    }

    try:
        r = _google_post(api_key, body, GOOGLE_FIELDS)
        if r.status_code != 200:
            return None
        places = r.json().get("places", [])
    except (requests.RequestException, ValueError):
        return None

    for place in places:
        cand = _place_to_lead(place)
        if cand and _same_business(cand, ylead):
            cand["Yelp URL"] = ylead["Yelp URL"]
            cand["Listed On"] = "Google Maps, Yelp"
            return cand

    return None


def merge_yelp(api_key, google_leads, yelp_leads):
    """Adds Yelp results to Google results without duplicates."""
    merged = list(google_leads)
    by_id = {l["Place ID"]: l for l in merged}
    pending = []

    for y in yelp_leads:
        match = next((g for g in merged if _same_business(g, y)), None)
        if match:
            match["Yelp URL"] = y["Yelp URL"]
            match["Listed On"] = "Google Maps, Yelp"
        else:
            pending.append(y)

    if pending:
        with ThreadPoolExecutor(max_workers=5) as pool:
            resolved = list(pool.map(
                lambda y: _resolve_on_google(api_key, y), pending
            ))

        for y, res in zip(pending, resolved):
            if res is None:
                merged.append(y)           # Yelp-only, no Google match
            elif res["Place ID"] in by_id:
                by_id[res["Place ID"]]["Yelp URL"] = y["Yelp URL"]
                by_id[res["Place ID"]]["Listed On"] = "Google Maps, Yelp"
            else:
                merged.append(res)
                by_id[res["Place ID"]] = res

    return merged


# ============================================================
# ALL SOURCES AT ONCE
# ============================================================

def search_all_sources(
    api_key,
    business_type,
    location,
    yelp_key="",
    google_token=None,
    google_done=False,
    yelp_offset=0,
    deep_social=False
):
    """One 'page' = next Google page + next Yelp page, merged,
    de-duplicated, then emails/socials looked up for every lead."""
    warnings = []
    g_leads, g_token = [], None
    yelp_leads, y_next = [], None
    g_done = google_done

    if not google_done:
        g = search_google_maps(
            api_key, business_type, location,
            page_token=google_token, enrich=False
        )
        if not g["success"]:
            return {
                "success": False, "error": g["error"],
                "leads": [], "next_page_token": None
            }
        g_leads = g["leads"]
        g_token = g.get("next_page_token")
        g_done = not g_token

    if yelp_key and yelp_offset is not None:
        y = search_yelp(yelp_key, business_type, location, yelp_offset)
        if y["success"]:
            yelp_leads = y["leads"]
            y_next = y["next_offset"]
        else:
            warnings.append(f"Yelp error: {y['error']}")

    # Yelp is "finished" for pagination when it has no next offset.
    leads = merge_yelp(api_key, g_leads, yelp_leads) if yelp_leads else g_leads

    # Emails / socials are looked up once, on the merged list.
    enrich_leads(leads, deep_social, _city_hint(location))

    return {
        "success": True,
        "leads": leads,
        "warnings": warnings,
        "next_page_token": g_token,
        "google_done": g_done,
        "yelp_next_offset": y_next,
        "has_more_pages": bool(g_token) or y_next is not None
    }
