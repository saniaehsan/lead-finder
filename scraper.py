import requests


# ============================================================
# GOOGLE PLACES SEARCH
# ============================================================

def search_google_maps(
    api_key,
    business_type,
    location,
    only_no_website=True,
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

        phone = place.get(
            "internationalPhoneNumber",
            ""
        )

        if not phone:
            phone = place.get(
                "nationalPhoneNumber",
                ""
            )

        if only_no_website and website:
            continue

        google_maps_link = (
            "https://www.google.com/maps/search/"
            f"?api=1&query_place_id={place_id}"
        )

        leads.append({
            "Business Name": name,
            "Phone Number": phone,
            "Address": address,
            "Website": website if website else "NO WEBSITE",
            "Place ID": place_id,
            "Google Maps": google_maps_link
        })

    next_page_token = result.get("nextPageToken")

    return {
        "success": True,
        "leads": leads,
        "total_results_checked": len(places),
        "next_page_token": next_page_token,
        "has_more_pages": bool(next_page_token)
    }