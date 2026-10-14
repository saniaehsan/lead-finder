import math
import concurrent.futures
import requests
from urllib.parse import quote_plus, urlparse


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
# NEARBY COMPETITORS
# ============================================================

COMPETITOR_RADIUS_M = 10000      # search / keep radius (10 km)
COMPETITORS_FETCHED = 15         # how many nearby places to look at
MAX_COMPETITORS_SHOWN = 5        # max names per cell

# Not a business's own website -> never counted as a "good website"
NOT_A_REAL_WEBSITE = (
    "facebook.com", "instagram.com", "twitter.com", "x.com",
    "linkedin.com", "youtube.com", "tiktok.com", "pinterest.com",
    "yelp.com", "yellowpages.com", "mapquest.com", "bbb.org",
    "angi.com", "angieslist.com", "thumbtack.com", "homeadvisor.com",
    "nextdoor.com", "manta.com", "linktr.ee", "google.com",
    "business.site", "wixsite.com", "weebly.com", "godaddysites.com",
    "tripadvisor.com", "doordash.com", "ubereats.com", "grubhub.com",
)


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = (
        math.sin(dp / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(a))


def is_quality_website(url):
    """Proper own-domain https website (not social/directory/free builder).

    Google's API cannot judge design quality, so this is a practical
    proxy: secure (https) + the business's own domain.
    """
    if not url or not str(url).lower().startswith("https://"):
        return False

    host = (urlparse(str(url)).netloc or "").lower().split(":")[0]

    if host.startswith("www."):
        host = host[4:]

    for bad in NOT_A_REAL_WEBSITE:
        if host == bad or host.endswith("." + bad):
            return False

    return "." in host


def find_competitors(api_key, business_type, lat, lon, own_place_id):
    """Return (nearby_competitors_text, competitors_with_websites_text)."""
    if lat is None or lon is None:
        return "", ""

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": (
            "places.id,"
            "places.displayName,"
            "places.websiteUri,"
            "places.location"
        )
    }

    data = {
        "textQuery": business_type,
        "pageSize": COMPETITORS_FETCHED,
        "locationBias": {
            "circle": {
                "center": {"latitude": lat, "longitude": lon},
                "radius": float(COMPETITOR_RADIUS_M)
            }
        }
    }

    try:
        response = requests.post(
            "https://places.googleapis.com/v1/places:searchText",
            headers=headers,
            json=data,
            timeout=30
        )

        if response.status_code != 200:
            return "", ""

        places = response.json().get("places", [])

    except (requests.RequestException, ValueError):
        return "", ""

    found = []

    for p in places:

        if p.get("id") == own_place_id:
            continue

        loc = p.get("location") or {}
        p_lat = loc.get("latitude")
        p_lon = loc.get("longitude")

        if p_lat is None or p_lon is None:
            continue

        dist = haversine_km(lat, lon, p_lat, p_lon)

        if dist * 1000 > COMPETITOR_RADIUS_M:
            continue

        name = p.get("displayName", {}).get("text", "")

        if not name:
            continue

        found.append((dist, name, p.get("websiteUri", "")))

    found.sort(key=lambda x: x[0])

    nearby = [
        f"{name} ({dist:.1f} km)"
        for dist, name, _ in found[:MAX_COMPETITORS_SHOWN]
    ]

    with_sites = [
        f"{name} - {site}"
        for _, name, site in found
        if is_quality_website(site)
    ][:MAX_COMPETITORS_SHOWN]

    return "; ".join(nearby), "; ".join(with_sites)


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
    coords = []

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

        loc = place.get("location") or {}
        coords.append((
            loc.get("latitude"),
            loc.get("longitude"),
            place_id
        ))

        google_maps_link = build_maps_link(
            place_id,
            name,
            address
        )

        leads.append({
            "Business Name": name,
            "Phone Number": phone,
            "Address": address,
            "Website": website if website else "NO WEBSITE",
            "Rating": rating,
            "Reviews": reviews_text,
            "Nearby Competitors": "",
            "Competitors With Well-Designed Websites": "",
            "Place ID": place_id,
            "Google Maps": google_maps_link
        })

    # Nearby competitors for every lead (parallel, one search each)
    if leads:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=5
        ) as pool:
            outcomes = list(pool.map(
                lambda c: find_competitors(
                    api_key, business_type, c[0], c[1], c[2]
                ),
                coords
            ))

        for lead, (nearby, with_sites) in zip(leads, outcomes):
            lead["Nearby Competitors"] = nearby
            lead["Competitors With Well-Designed Websites"] = with_sites

    next_page_token = result.get("nextPageToken")

    return {
        "success": True,
        "leads": leads,
        "total_results_checked": len(places),
        "next_page_token": next_page_token,
        "has_more_pages": bool(next_page_token)
    }