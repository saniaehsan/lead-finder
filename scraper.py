import html
import json
import math
import os
import re
import threading
from datetime import date
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


def _normalize_social(url, platform):
    """Cut a social URL down to the profile root (or None)."""
    parsed = urlparse(url)
    segs = [p for p in parsed.path.split("/") if p]

    if not segs:
        return None

    if platform == "LinkedIn":
        keep = 2
    elif platform == "YouTube":
        keep = 1 if segs[0].startswith("@") else 2
    elif platform == "Yelp":
        keep = 2
    elif platform == "Facebook":
        if segs[0] == "profile.php":
            return None
        keep = 3 if segs[0] == "pages" else 1
    else:
        keep = 1

    if len(segs) < keep:
        return None

    return f"https://{parsed.netloc}/" + "/".join(segs[:keep])


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
                clean = _normalize_social(url, platform)
                if clean:
                    found.setdefault(platform, clean)
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
# DIRECTORY RESEARCH (Yelp, BBB, Angi, Facebook, Yellow Pages ...)
# ============================================================
# Searches the web for "<business> <city, state>" through a search
# API, then:
#   - picks social profiles out of the result links (Facebook,
#     Instagram, ...) when the result clearly belongs to the business
#   - reads emails out of result snippets
#   - opens the matching directory/listing pages and takes emails that
#     appear right next to the business name
# Google's own Custom Search API is closed to new customers, so this
# uses Serper.dev (Google results) or Brave Search instead.

USAGE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "search_usage.json"
)
_usage_lock = threading.Lock()
_quota_hit = {"day": "", "hit": False}

NO_FETCH_HOSTS = (
    "facebook.com", "instagram.com", "twitter.com", "x.com",
    "linkedin.com", "tiktok.com", "youtube.com", "pinterest.com",
    "yelp.com", "google.com", "apple.com"
)

DIRECTORY_EMAIL_DOMAINS = (
    "yelp.com", "bbb.org", "angi.com", "angieslist.com",
    "yellowpages.com", "thumbtack.com", "homeadvisor.com",
    "facebook.com", "manta.com", "mapquest.com", "nextdoor.com"
)

_GENERIC_NAME_WORDS = {
    "the", "and", "llc", "inc", "co", "corp", "company", "services",
    "service", "plumbing", "plumber", "plumbers", "pllc", "ltd", "of"
}


def _take_search_slot(daily_limit):
    """True if one more search may be spent today (and counts it)."""
    today = date.today().isoformat()

    with _usage_lock:
        if _quota_hit["day"] == today and _quota_hit["hit"]:
            return False

        try:
            with open(USAGE_FILE, "r", encoding="utf-8") as f:
                usage = json.load(f)
        except (OSError, ValueError):
            usage = {}

        if usage.get("date") != today:
            usage = {"date": today, "count": 0}

        if usage["count"] >= daily_limit:
            return False

        usage["count"] += 1

        try:
            with open(USAGE_FILE, "w", encoding="utf-8") as f:
                json.dump(usage, f)
        except OSError:
            pass

        return True


def searches_used_today():
    try:
        with open(USAGE_FILE, "r", encoding="utf-8") as f:
            usage = json.load(f)
        if usage.get("date") == date.today().isoformat():
            return int(usage.get("count", 0))
    except (OSError, ValueError):
        pass
    return 0


def _search_web(query, provider, key, timeout=15):
    """[{'title','link','snippet'}, ...] from Serper.dev or Brave."""
    try:
        if provider == "brave":
            resp = requests.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": 10},
                headers={
                    "X-Subscription-Token": key,
                    "Accept": "application/json"
                },
                timeout=timeout,
            )
        else:
            resp = requests.post(
                "https://google.serper.dev/search",
                json={"q": query, "num": 10, "gl": "us"},
                headers={
                    "X-API-KEY": key,
                    "Content-Type": "application/json"
                },
                timeout=timeout,
            )
    except requests.RequestException:
        return []

    if resp.status_code in (401, 402, 403, 429):
        # Bad key / quota used up: stop wasting calls today.
        with _usage_lock:
            _quota_hit["day"] = date.today().isoformat()
            _quota_hit["hit"] = True
        return []

    if resp.status_code != 200:
        return []

    try:
        data = resp.json()
    except ValueError:
        return []

    out = []

    if provider == "brave":
        for r in (data.get("web") or {}).get("results", []):
            out.append({
                "title": r.get("title", ""),
                "link": r.get("url", ""),
                "snippet": r.get("description", "")
            })
    else:
        for r in data.get("organic", []):
            out.append({
                "title": r.get("title", ""),
                "link": r.get("link", ""),
                "snippet": r.get("snippet", "")
            })

    return out


def _host(url):
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _name_tokens(name):
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    return [
        w for w in words
        if len(w) > 2 and w not in _GENERIC_NAME_WORDS
    ]


def _matches_business(blob, tokens):
    """Every distinctive word of the business name is in the text."""
    if not tokens:
        return False
    blob = blob.lower()
    return all(t in blob for t in tokens)


def _emails_near_name(text, name, before=300, after=700):
    """Emails written close to the business name on a page."""
    low = (text or "").lower()
    needle = (name or "").lower().strip()
    found = []

    if not needle:
        return found

    pos = 0
    while len(found) < 3:
        i = low.find(needle, pos)
        if i < 0:
            break
        chunk = text[max(0, i - before): i + after]
        for e in extract_emails(chunk):
            if e not in found:
                found.append(e)
        pos = i + len(needle)

    return found


def web_research(
    business_name, area, provider, key, daily_limit=200, timeout=8
):
    """{'socials': {...}, 'emails': [...], 'sites': [...]}"""
    result = {"socials": {}, "emails": [], "sites": []}

    if not key:
        return result

    tokens = _name_tokens(business_name)
    to_fetch = []

    def handle(items):
        for item in items:
            link = item.get("link", "") or ""
            blob = (
                f"{item.get('title', '')} {item.get('snippet', '')} {link}"
            )

            if not _matches_business(blob, tokens):
                continue

            host = _host(link)

            if host and host not in result["sites"]:
                result["sites"].append(host)

            for platform, url in extract_socials(link).items():
                result["socials"].setdefault(platform, url)

            snippet_text = (
                f"{item.get('title', '')}. {item.get('snippet', '')}"
            )
            for e in extract_emails(snippet_text):
                dom = e.split("@")[-1]
                if any(dom.endswith(d) for d in DIRECTORY_EMAIL_DOMAINS):
                    continue
                if e not in result["emails"]:
                    result["emails"].append(e)

            if (
                host
                and not any(
                    host == d or host.endswith("." + d)
                    for d in NO_FETCH_HOSTS
                )
                and link not in to_fetch
            ):
                to_fetch.append(link)

    def run(query):
        if not _take_search_slot(daily_limit):
            return []
        return _search_web(query, provider, key)

    handle(run(f'"{business_name}" {area}'))

    # Nothing social yet -> one more, social-focused search.
    if not any(p in result["socials"] for p in ("Facebook", "Instagram")):
        handle(run(f'"{business_name}" {area} facebook instagram'))

    # Still no email -> read the matching listing pages.
    if not result["emails"]:
        for link in to_fetch[:3]:
            try:
                resp = requests.get(
                    link,
                    headers=REQUEST_HEADERS,
                    timeout=timeout,
                    allow_redirects=True
                )
            except requests.RequestException:
                continue

            if resp.status_code != 200 or "text" not in resp.headers.get(
                "Content-Type", ""
            ):
                continue

            page_host = _host(link)
            page_root = ".".join(page_host.split(".")[-2:])

            for e in _emails_near_name(resp.text[:500000], business_name):
                dom = e.split("@")[-1]
                if dom.endswith(page_root):
                    continue
                if any(dom.endswith(d) for d in DIRECTORY_EMAIL_DOMAINS):
                    continue
                if e not in result["emails"]:
                    result["emails"].append(e)

            if result["emails"]:
                break

    result["sites"] = result["sites"][:6]
    return result


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
    page_token=None,
    area="",
    search_provider="serper",
    search_key=None,
    daily_limit=200
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
            "Listed On": "",
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

    scans = [("", {})] * len(leads)

    if sites:
        with ThreadPoolExecutor(max_workers=10) as pool:
            scans = list(pool.map(scan_website, sites))

    research_on = bool(search_key)

    # Which leads still miss an email or social profile after the
    # website scan? (Includes businesses without a website.)
    todo = []

    if research_on:
        for idx, (site, (email, socials)) in enumerate(zip(sites, scans)):
            if not site or not email or not socials:
                todo.append(idx)

    researched = {}

    if todo:
        with ThreadPoolExecutor(max_workers=5) as pool:
            outs = list(pool.map(
                lambda i: web_research(
                    leads[i]["Business Name"], area,
                    search_provider, search_key, daily_limit
                ),
                todo
            ))
        researched = dict(zip(todo, outs))

    for idx, (lead, site, (email, socials)) in enumerate(
        zip(leads, sites, scans)
    ):
        found = researched.get(idx, {"socials": {}, "emails": [], "sites": []})

        all_socials = dict(socials)
        for p, u in found["socials"].items():
            all_socials.setdefault(p, u)

        best_email = email or (found["emails"][0] if found["emails"] else "")

        if best_email:
            lead["Email"] = best_email
        elif not site and not research_on:
            lead["Email"] = "NO WEBSITE"
        else:
            lead["Email"] = "NOT FOUND"

        if all_socials:
            lead["Social Media Presence"] = format_socials(all_socials)
        elif not site and not research_on:
            lead["Social Media Presence"] = "NO WEBSITE"
        else:
            lead["Social Media Presence"] = "NOT FOUND"

        lead["Listed On"] = ", ".join(found["sites"])

    next_page_token = result.get("nextPageToken")

    return {
        "success": True,
        "leads": leads,
        "total_results_checked": len(places),
        "next_page_token": next_page_token,
        "has_more_pages": bool(next_page_token)
    }