import html
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import requests
from urllib.parse import parse_qs, quote_plus, unquote, urlparse


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
# EMAIL + OWNER FINDER (reads the business's own public website)
# ============================================================
# Google Places does NOT provide emails or owner details, so they
# are looked up on the business website: home page first, then the
# usual contact / about / team pages. Only information written
# publicly on the site is collected.

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

SITE_PATHS = [
    "", "/contact", "/contact-us", "/about", "/about-us",
    "/our-team", "/team", "/meet-the-team", "/staff"
]

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}

# ---- owner detection ----------------------------------------

# Best role first (used to pick between several people found)
OWNER_ROLES = [
    ("owner", "Owner"),
    ("co-owner", "Co-Owner"),
    ("co owner", "Co-Owner"),
    ("proprietor", "Proprietor"),
    ("founder", "Founder"),
    ("co-founder", "Co-Founder"),
    ("co founder", "Co-Founder"),
    ("president", "President"),
    ("ceo", "CEO"),
    ("chief executive officer", "CEO"),
    ("managing partner", "Managing Partner"),
    ("principal", "Principal"),
]

ROLE_LABEL = dict(OWNER_ROLES)
ROLE_PRIORITY = {
    label: i for i, (_, label) in reversed(list(enumerate(OWNER_ROLES)))
}

_ROLE_ALT = "|".join(
    re.escape(k) for k in sorted(ROLE_LABEL, key=len, reverse=True)
)
ROLE = rf"(?i:{_ROLE_ALT})"

NAME = r"[A-Z][a-z'\-]+(?:\s+[A-Z]\.?)?\s+[A-Z][A-Za-z'\-]+"

BAD_NAME_WORDS = set("""
plumbing services service heating cooling air company inc llc family
team our the meet contact about home repair repairs roofing electric
electrical group corp co staff owner founder president manager office
united states street road avenue drive suite phone email call free
estimate licensed insured master plumber best quality customer support
privacy policy terms all rights reserved new york north south east west
county city dental dentist salon gym hotel restaurant auto real estate
agents marketing agency lawyers law firm attorney attorneys senior chief
executive officer and of for with from your view read more learn
""".split())

SEP = r"\s*[,\-–—|:(]\s*(?i:the\s+)?"

PAT_NAME_ROLE = re.compile(rf"({NAME}){SEP}({ROLE})\b")
PAT_ROLE_NAME = re.compile(rf"\b({ROLE})\s*[:\-–—|]\s*({NAME})")
PAT_NAME_IS = re.compile(
    rf"({NAME})\s+(?i:is|serves as)\s+(?i:the\s+)?(?:\w+\s+){{0,2}}?"
    rf"(?i:and\s+)?({ROLE})\b"
)
PAT_BY = re.compile(
    rf"(?i:owned|founded|operated|started|run)\s+(?i:by)\s+({NAME})"
)
NAME_ONLY = re.compile(
    r"[A-Z][a-z'\-]+(?:\s+[A-Z][a-z'\-]*\.?)?\s+[A-Z][A-Za-z'\-]+"
)
ROLE_LINE = re.compile(rf"^({ROLE})\b[^.]{{0,40}}$")


def _valid_name(name):
    words = re.split(r"\s+", name.strip())

    if not 2 <= len(words) <= 3:
        return False

    return not any(
        w.strip(".,").lower() in BAD_NAME_WORDS for w in words
    )


def html_to_lines(page_html):
    """Very small HTML -> list of text lines (no extra libraries)."""
    text = re.sub(
        r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", page_html
    )
    text = re.sub(r"(?s)<!--.*?-->", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|td|section)>",
                  "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)

    lines = []
    for line in text.split("\n"):
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)
    return lines


def extract_owners(page_html):
    """[(priority, name, role_label), ...] found in one page."""
    lines = html_to_lines(page_html)
    found = []

    def add(name, role_text):
        label = ROLE_LABEL.get(role_text.lower().strip())
        if label and _valid_name(name):
            found.append((ROLE_PRIORITY[label], name.strip(), label))

    for line in lines:
        for name, role in PAT_NAME_ROLE.findall(line):
            add(name, role)
        for role, name in PAT_ROLE_NAME.findall(line):
            add(name, role)
        for name, role in PAT_NAME_IS.findall(line):
            add(name, role)
        for name in PAT_BY.findall(line):
            if _valid_name(name):
                found.append((ROLE_PRIORITY["Owner"], name.strip(), "Owner"))

    # Team cards: a name on one line and the role on the next/previous
    for i, line in enumerate(lines):
        m = ROLE_LINE.match(line)
        if not m:
            continue
        # Previous line first (usual card layout); next line only if
        # the previous one is not a name.
        if i > 0 and NAME_ONLY.fullmatch(lines[i - 1]):
            add(lines[i - 1], m.group(1))
        elif i + 1 < len(lines) and NAME_ONLY.fullmatch(lines[i + 1]):
            add(lines[i + 1], m.group(1))

    return found


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


def scrape_contact_info(website, timeout=6):
    """{"email", "owner_name", "owner_role"} found on a website.

    Any value that was not found is "".
    """
    result = {"email": "", "owner_name": "", "owner_role": ""}

    if not website or not str(website).lower().startswith(
        ("http://", "https://")
    ):
        return result

    parsed = urlparse(website)
    root = f"{parsed.scheme}://{parsed.netloc}"
    site_domain = parsed.netloc.lower()

    if site_domain.startswith("www."):
        site_domain = site_domain[4:]

    emails = []
    owners = []

    for i, path in enumerate(SITE_PATHS):

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
                break
            continue

        content_type = response.headers.get("Content-Type", "")

        if response.status_code != 200 or "text" not in content_type:
            continue

        page = response.text[:500000]

        for email in extract_emails(page):
            if email not in emails:
                emails.append(email)

        owners.extend(extract_owners(page))

        own_email = [
            e for e in emails
            if e.split("@")[-1].endswith(site_domain)
        ]

        # Stop early once we have a good email AND an owner
        if own_email and owners:
            break

    own_email = [
        e for e in emails if e.split("@")[-1].endswith(site_domain)
    ]

    if own_email:
        result["email"] = own_email[0]
    elif emails:
        result["email"] = emails[0]

    if owners:
        owners.sort(key=lambda x: x[0])
        result["owner_name"] = owners[0][1]
        result["owner_role"] = owners[0][2]

    return result


def find_email(website, timeout=6):
    """Kept for compatibility: just the email."""
    return scrape_contact_info(website, timeout)["email"]


# ============================================================
# OWNER FROM GOOGLE REVIEWS
# ============================================================
# Customers often write things like "Mike, the owner, came out"
# or "owner John was great". Google's reviews (already returned by
# the Places API, so no extra cost) are checked for such mentions.

_NM = r"([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})?)"

REVIEW_P1 = re.compile(
    _NM + r"\s*[,(\-–]?\s*(?:[Tt]he|[Oo]ur|[Tt]heir)?\s*"
    r"(?:[Bb]usiness\s+|[Cc]ompany\s+)?\(?[Oo]wner\b"
)
REVIEW_P2 = re.compile(
    r"\b[Oo]wners?\s*(?:,|:|-|–|named|called|is|was)?\s*(?:named\s+)?" + _NM
)
REVIEW_P3 = re.compile(
    _NM + r"\s+(?:is|was)\s+(?:the|an?)\s+(?:\w+\s+)?[Oo]wner\b"
)

REVIEW_STOP = set("""
the this that and but very great nice good excellent amazing friendly
professional honest helpful awesome wonderful best super our their his
her its home house business company shop small local family previous new
current original called asked spoke talked met thanks thank contact
contacted texted emailed hired using used plumber plumbers plumbing
technician tech manager boss guy man mr mrs ms dr highly definitely
absolutely also just then when after before while once today yesterday
he she they we you not will would could should had has was were are
service services owner owners
""".split())

REVIEW_CONTEXT_BAD = ("home", "house", "property", "building", "landlord")


def _clean_review_name(raw):
    words = raw.split()

    while words and words[0].lower() in REVIEW_STOP:
        words.pop(0)

    if not words or any(w.lower() in REVIEW_STOP for w in words):
        return ""

    if len(words[0]) < 3:
        return ""

    return " ".join(words)


def extract_review_owner_names(text):
    names = []

    for m in REVIEW_P1.finditer(text):
        names.append(m.group(1))

    for m in REVIEW_P2.finditer(text):
        before = text[max(0, m.start() - 12):m.start()].lower()
        if any(b in before for b in REVIEW_CONTEXT_BAD):
            continue
        names.append(m.group(1))

    for m in REVIEW_P3.finditer(text):
        names.append(m.group(1))

    cleaned = [_clean_review_name(n) for n in names]
    return [n for n in cleaned if n]


def find_owner_in_reviews(reviews):
    """Most-mentioned owner name in the reviews, or ""."""
    counts = {}

    for review in reviews or []:
        text = (
            (review.get("text") or {}).get("text")
            or (review.get("originalText") or {}).get("text")
            or ""
        )
        for name in extract_review_owner_names(text):
            counts[name] = counts.get(name, 0) + 1

    if not counts:
        return ""

    # "Mike" + "Mike Smith" -> count both for the longer name
    for short in [n for n in counts if " " not in n]:
        longer = [n for n in counts if n != short and n.startswith(short + " ")]
        if longer:
            counts[longer[0]] += counts.pop(short)

    return sorted(
        counts.items(),
        key=lambda kv: (-kv[1], -len(kv[0].split()))
    )[0][0]


# ============================================================
# OWNER FROM ONLINE PLUMBER DIRECTORIES (BBB, Yelp, Angi, ...)
# ============================================================
# For businesses without a website: search the web for the business
# name, then read the directory profile pages (BBB, Angi, Yelp,
# Facebook, ...) that belong to that business.

DIRECTORY_SITES = {
    "bbb.org": "BBB",
    "yelp.com": "Yelp",
    "angi.com": "Angi",
    "thumbtack.com": "Thumbtack",
    "homeadvisor.com": "HomeAdvisor",
    "facebook.com": "Facebook",
    "yellowpages.com": "Yellow Pages",
    "manta.com": "Manta",
    "mapquest.com": "MapQuest",
    "chamberofcommerce.com": "Chamber of Commerce",
    "porch.com": "Porch",
    "buildzoom.com": "BuildZoom",
    "superpages.com": "Superpages",
    "alignable.com": "Alignable",
    "linkedin.com": "LinkedIn",
}

_SEARCH_LOCK = threading.Semaphore(2)


def _alnum(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _name_key(business_name):
    """Short comparable key of the business name (first 2 words)."""
    words = re.findall(r"[A-Za-z0-9]+", business_name or "")
    key = _alnum("".join(words[:2]))
    return key if len(key) >= 6 else _alnum(business_name)


def _directory_label(url):
    host = urlparse(url).netloc.lower()
    for domain, label in DIRECTORY_SITES.items():
        if host == domain or host.endswith("." + domain):
            return label
    return ""


def web_search(query, timeout=10):
    """[(url, title, snippet), ...] from DuckDuckGo's HTML page."""
    with _SEARCH_LOCK:
        time.sleep(0.8)
        try:
            response = requests.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers=REQUEST_HEADERS,
                timeout=timeout
            )
        except requests.RequestException:
            return []

    if response.status_code != 200:
        return []

    page = response.text

    link_tags = re.findall(
        r'<a\b[^>]*class="[^"]*result__a[^"]*"[^>]*>(.*?)</a>',
        page, re.S
    )
    link_attrs = re.findall(
        r'<a\b([^>]*class="[^"]*result__a[^"]*"[^>]*)>', page
    )
    snippets = re.findall(
        r'<a\b[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>',
        page, re.S
    )

    results = []

    for i, attrs in enumerate(link_attrs):
        href_m = re.search(r'href="([^"]+)"', attrs)
        if not href_m:
            continue

        href = html.unescape(href_m.group(1))

        if href.startswith("//"):
            href = "https:" + href

        if "uddg=" in href:
            qs = parse_qs(urlparse(href).query)
            href = unquote(qs.get("uddg", [""])[0])

        if not href.startswith("http"):
            continue

        strip = lambda t: html.unescape(re.sub(r"<[^>]+>", " ", t)).strip()

        title = strip(link_tags[i]) if i < len(link_tags) else ""
        snippet = strip(snippets[i]) if i < len(snippets) else ""
        results.append((href, title, snippet))

    return results


def _location_hint(address):
    parts = [p.strip() for p in (address or "").split(",")]

    if len(parts) >= 3:
        state = parts[-2].split()[0] if parts[-2].split() else ""
        return f"{parts[-3]} {state}".strip()

    return address or ""


def find_owner_online(business_name, address, timeout=8, max_pages=3):
    """(owner_name, role, source) from directory pages, or ("","","")."""
    if not business_name:
        return "", "", ""

    query = f'"{business_name}" {_location_hint(address)} owner'
    key = _name_key(business_name)

    if not key:
        return "", "", ""

    candidates = []   # (priority, name, role, source)
    pages_read = 0

    for url, title, snippet in web_search(query):

        label = _directory_label(url)

        # 1) the search snippet itself ("Owner: John Smith ...")
        if key in _alnum(title + snippet):
            for pr, nm, rl in extract_owners(f"<p>{title}</p><p>{snippet}</p>"):
                candidates.append((pr, nm, rl, label or urlparse(url).netloc))

        # 2) the business's own profile page on a directory site
        if label and key in _alnum(url) and pages_read < max_pages:
            pages_read += 1
            try:
                resp = requests.get(
                    url,
                    headers=REQUEST_HEADERS,
                    timeout=timeout,
                    allow_redirects=True
                )
            except requests.RequestException:
                continue

            if resp.status_code == 200 and "text" in resp.headers.get(
                "Content-Type", ""
            ):
                for pr, nm, rl in extract_owners(resp.text[:500000]):
                    candidates.append((pr, nm, rl, label))

    if not candidates:
        return "", "", ""

    candidates.sort(key=lambda c: c[0])
    _, name, role, source = candidates[0]
    return name, role, source


def _enrich_lead(lead):
    """Fills Email / Owner Name / Owner Role / Owner Source."""
    reviews = lead.pop("_reviews", [])
    site = lead["Website"] if lead["Website"] != "NO WEBSITE" else ""

    email = owner = role = source = ""

    try:
        if site:
            info = scrape_contact_info(site)
            email = info["email"]
            if info["owner_name"]:
                owner, role, source = (
                    info["owner_name"], info["owner_role"], "Website"
                )

        if not owner:
            from_reviews = find_owner_in_reviews(reviews)
            if from_reviews:
                owner, role, source = from_reviews, "Owner", "Google Reviews"

        if not owner:
            owner, role, source = find_owner_online(
                lead.get("Business Name", ""),
                lead.get("Address", "")
            )
    except Exception:
        pass

    lead["Email"] = email or ("NOT FOUND" if site else "NO WEBSITE")
    lead["Owner Name"] = owner or "NOT FOUND"
    lead["Owner Role"] = role or "NOT FOUND"
    lead["Owner Source"] = source or "NOT FOUND"


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
            "Longitude": loc.get("longitude"),
            "_reviews": place.get("reviews", [])
        })

    # Email + owner lookup (website, Google reviews, online
    # directories) - in parallel for speed
    if leads:
        with ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(_enrich_lead, leads))

    next_page_token = result.get("nextPageToken")

    return {
        "success": True,
        "leads": leads,
        "total_results_checked": len(places),
        "next_page_token": next_page_token,
        "has_more_pages": bool(next_page_token)
    }