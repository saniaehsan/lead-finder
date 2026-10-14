import math
import requests
from urllib.parse import quote_plus


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

    - "Nearby Competitors": same business type within radius_km,
      nearest first -> "Name (1.2 km) | Name (2.0 km)"
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

        with_site = [(d, o) for d, o in cands if _has_website(o)]
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
            "Address": address,
            "Website": website if website else "NO WEBSITE",
            "Rating": rating,
            "Reviews": reviews_text,
            "Place ID": place_id,
            "Google Maps": google_maps_link,
            "Latitude": loc.get("latitude"),
            "Longitude": loc.get("longitude")
        })

    next_page_token = result.get("nextPageToken")

    return {
        "success": True,
        "leads": leads,
        "total_results_checked": len(places),
        "next_page_token": next_page_token,
        "has_more_pages": bool(next_page_token)
    }