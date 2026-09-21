import json
import os

import streamlit as st
import pandas as pd
from io import BytesIO

import scraper


# ============================================================
# PAGE SETTINGS
# ============================================================

st.set_page_config(
    page_title="Google Maps Lead Finder",
    layout="wide"
)


# ============================================================
# SESSION STATE
# ============================================================

if "leads" not in st.session_state:
    st.session_state["leads"] = pd.DataFrame()

if "searches" not in st.session_state:
    st.session_state["searches"] = []

if "search_progress" not in st.session_state:
    st.session_state["search_progress"] = {}


# ============================================================
# STATIC US GEO DATA (State -> County -> Cities / Landmarks)
# ============================================================
# This replaces the old live Census / Nominatim API lookups.
# All data lives in us_geo_data.json, shipped alongside this file,
# so State -> County -> City -> Location works instantly and
# reliably without depending on outside APIs at runtime.

GEO_DATA_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "us_geo_data.json"
)


@st.cache_data(show_spinner=False)
def load_geo_data():

    with open(GEO_DATA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


GEO_DATA = load_geo_data()


# ============================================================
# TITLE
# ============================================================

st.title("Google Maps Lead Finder")

st.write(
    "Select Country → State → County → City → Location, "
    "then search each Google Maps page one by one."
)


# ============================================================
# SIDEBAR
# ============================================================

with st.sidebar:

    st.header("Google Maps API")

    api_key = st.text_input(
        "Enter Google Maps API Key",
        type="password"
    )


# ============================================================
# COUNTRY
# ============================================================

country = st.selectbox(
    "Country",
    ["United States"]
)


# ============================================================
# STATE
# ============================================================

state_options = sorted(GEO_DATA.keys())

state = st.selectbox(
    "State",
    ["Select State"] + state_options
)


# ============================================================
# COUNTY
# ============================================================

if state == "Select State":

    county_options = []

else:

    county_options = sorted(
        GEO_DATA[state].keys()
    )


county = st.selectbox(
    "County",
    ["Select County"] + county_options
)


# ============================================================
# CITY
# ============================================================

if (
    state == "Select State"
    or
    county == "Select County"
):

    city_options = []

else:

    # County seat comes first (as stored in the data file),
    # followed by the county's other major cities/towns.
    city_options = GEO_DATA[state][county]["cities"]


city = st.selectbox(
    "City",
    ["Select City"] + city_options
)


# ============================================================
# LOCATION (landmarks + other areas/towns in the same county)
# ============================================================

if (
    state == "Select State"
    or
    county == "Select County"
    or
    city == "Select City"
):

    location_options = []

else:

    county_info = GEO_DATA[state][county]

    landmarks = county_info["landmarks"]

    other_areas = [
        c for c in county_info["cities"]
        if c != city
    ]

    # Landmarks first, then any other towns/areas in the
    # same county, with duplicates removed.
    combined = landmarks + other_areas

    seen = set()
    location_options = []

    for item in combined:
        if item not in seen:
            seen.add(item)
            location_options.append(item)


location_name = st.selectbox(
    "Location",
    ["Select Location"] + location_options
)


# ============================================================
# BUSINESS TYPE
# ============================================================

business_options = [
    "Restaurants",
    "Dentists",
    "Real Estate Agents",
    "Plumbers",
    "Electricians",
    "Roofing Companies",
    "Salons",
    "Gyms",
    "Hotels",
    "Auto Repair Shops",
    "Marketing Agencies",
    "Lawyers"
]

business_type = st.selectbox(
    "Business Type",
    business_options
)


# ============================================================
# RESULT FILTERS (applied to the saved results table below)
# ============================================================

col1, col2 = st.columns(2)

with col1:

    website_filter = st.selectbox(
        "Website Filter",
        [
            "All",
            "Only No Website",
            "Only With Website"
        ],
        index=0
    )

with col2:

    phone_filter = st.checkbox(
        "Only businesses with phone numbers",
        value=False
    )


# ============================================================
# BUILD SEARCH LOCATION
# ============================================================

if location_name != "Select Location":

    location = (
        f"{location_name}, "
        f"{city}, "
        f"{county}, "
        f"{state}, "
        f"{country}"
    )

else:

    location = ""


# ============================================================
# SEARCH KEY
# ============================================================

search_key = (
    f"{business_type}|"
    f"{location_name}|"
    f"{city}|"
    f"{county}|"
    f"{state}|"
    f"{country}"
)


# ============================================================
# SEARCH BUTTON
# ============================================================

if st.button(
    "SEARCH LEADS",
    use_container_width=True
):

    if not api_key:

        st.error(
            "Please enter your Google Maps API Key."
        )

    elif state == "Select State":

        st.error(
            "Please select a State."
        )

    elif county == "Select County":

        st.error(
            "Please select a County."
        )

    elif city == "Select City":

        st.error(
            "Please select a City."
        )

    elif location_name == "Select Location":

        st.error(
            "Please select a Location."
        )

    else:

        progress = (
            st.session_state["search_progress"]
            .get(search_key, {})
        )

        if progress.get("completed", False):

            st.success(
                f"SUCCESS! All available results for "
                f"{location_name}, {city} have already "
                f"been searched."
            )

            st.info(
                "Please select another Location."
            )

        else:

            page_token = progress.get(
                "page_token",
                None
            )

            page_number = progress.get(
                "page_number",
                1
            )

            st.info(
                f"Searching {business_type} in "
                f"{location_name}, {city}, {county}, "
                f"{state}..."
            )

            st.caption(
                f"Page {page_number}"
            )

            with st.spinner(
                f"Searching Page {page_number}..."
            ):

                result = scraper.search_google_maps(
                    api_key=api_key,
                    business_type=business_type,
                    location=location,
                    only_no_website=False,
                    page_token=page_token
                )

            if not result.get("success", False):

                st.error(
                    "Google Maps API Error"
                )

                st.code(
                    result.get(
                        "error",
                        "Unknown error"
                    )
                )

            else:

                leads = result.get(
                    "leads",
                    []
                )

                if leads:

                    new_df = pd.DataFrame(
                        leads
                    )

                    new_df[
                        "Search Country"
                    ] = country

                    new_df[
                        "Search State"
                    ] = state

                    new_df[
                        "Search County"
                    ] = county

                    new_df[
                        "Search City"
                    ] = city

                    new_df[
                        "Location"
                    ] = location_name

                    new_df[
                        "Business Type"
                    ] = business_type

                    if (
                        st.session_state[
                            "leads"
                        ].empty
                    ):

                        combined_df = (
                            new_df.copy()
                        )

                    else:

                        combined_df = pd.concat(
                            [
                                st.session_state[
                                    "leads"
                                ],
                                new_df
                            ],
                            ignore_index=True
                        )

                    if (
                        "Place ID"
                        in combined_df.columns
                    ):

                        combined_df = (
                            combined_df
                            .drop_duplicates(
                                subset=["Place ID"],
                                keep="first"
                            )
                            .reset_index(
                                drop=True
                            )
                        )

                    st.session_state[
                        "leads"
                    ] = combined_df

                    st.success(
                        f"Page {page_number}: "
                        f"{len(leads)} new lead(s) found. "
                        f"Total saved leads: "
                        f"{len(combined_df)}"
                    )

                else:

                    st.warning(
                        f"Page {page_number}: "
                        f"No matching leads found "
                        f"on this page."
                    )

                search_text = (
                    f"{business_type} | "
                    f"{location_name}, {city}, "
                    f"{county}, {state}"
                )

                if (
                    search_text
                    not in st.session_state[
                        "searches"
                    ]
                ):

                    st.session_state[
                        "searches"
                    ].append(
                        search_text
                    )

                next_page_token = result.get(
                    "next_page_token"
                )

                if next_page_token:

                    st.session_state[
                        "search_progress"
                    ][search_key] = {

                        "page_token":
                            next_page_token,

                        "page_number":
                            page_number + 1,

                        "completed":
                            False
                    }

                    st.info(
                        f"More Google Maps results are "
                        f"available. Click SEARCH LEADS "
                        f"again for Page "
                        f"{page_number + 1}."
                    )

                else:

                    st.session_state[
                        "search_progress"
                    ][search_key] = {

                        "page_token":
                            None,

                        "page_number":
                            page_number,

                        "completed":
                            True
                    }

                    st.success(
                        f"SUCCESS! All available Google "
                        f"Maps pages for {location_name}, "
                        f"{city} have been searched."
                    )

                    st.info(
                        "Please select another Location "
                        "to continue finding leads."
                    )


# ============================================================
# SHOW SAVED RESULTS
# ============================================================

if not st.session_state["leads"].empty:

    df = (
        st.session_state["leads"]
        .copy()
    )

    st.divider()

    st.subheader(
        f"Saved Results "
        f"({len(df)} Total Leads)"
    )

    filtered_df = df.copy()

    if website_filter == "Only No Website":

        filtered_df = filtered_df[
            filtered_df["Website"]
            .fillna("")
            .astype(str)
            .str.upper()
            == "NO WEBSITE"
        ]

    elif website_filter == "Only With Website":

        filtered_df = filtered_df[
            filtered_df["Website"]
            .fillna("")
            .astype(str)
            .str.upper()
            != "NO WEBSITE"
        ]

    if phone_filter:

        filtered_df = filtered_df[
            filtered_df["Phone Number"]
            .fillna("")
            .astype(str)
            .str.strip()
            != ""
        ]

    st.info(
        f"{len(filtered_df)} leads "
        f"currently shown."
    )

    # Show every row on this same page (no internal scroll
    # cutting results short). Height grows with the number of
    # rows, capped so extremely large tables still scroll the
    # normal page instead of becoming unusably tall.
    table_height = min(
        (len(filtered_df) + 1) * 35 + 3,
        2000
    )

    # Build a clickable HTML version of the table (Website and
    # Google Maps become real <a> links that open in a new tab).
    # st.dataframe's LinkColumn depends on the Streamlit version
    # installed, so this HTML table is used instead — it works
    # the same way everywhere.

    import html as html_lib

    def make_link(url, link_text):

        if (
            not url
            or str(url).strip().upper() == "NO WEBSITE"
        ):
            return "NO WEBSITE"

        safe_url = html_lib.escape(
            str(url),
            quote=True
        )

        safe_text = html_lib.escape(
            str(link_text)
        )

        return (
            f'<a href="{safe_url}" target="_blank" '
            f'rel="noopener noreferrer">{safe_text}</a>'
        )

    html_df = filtered_df.copy()

    if "Website" in html_df.columns:

        html_df["Website"] = html_df["Website"].apply(
            lambda w: make_link(w, w)
        )

    if "Google Maps" in html_df.columns:

        html_df["Google Maps"] = html_df["Google Maps"].apply(
            lambda g: make_link(g, "Open in Maps")
        )

    for col in html_df.columns:

        if col not in ("Website", "Google Maps"):

            html_df[col] = (
                html_df[col]
                .fillna("")
                .astype(str)
                .apply(html_lib.escape)
            )

    table_html = html_df.to_html(
        escape=False,
        index=False,
        classes="leads-table",
        border=0
    )

    # Show every row on this same page (no internal scroll
    # cutting results short). Height grows with the number of
    # rows, capped so extremely large tables still scroll the
    # normal page instead of becoming unusably tall.
    table_height = min(
        (len(filtered_df) + 1) * 40 + 40,
        2000
    )

    # NOTE: every line below starts at column 0 (no leading
    # spaces) on purpose. If this HTML/CSS block is indented
    # (e.g. because it lives inside nested if-blocks), Streamlit's
    # markdown parser treats any line starting with 4+ spaces as
    # an indented CODE BLOCK, not HTML. That was the original bug:
    # the <style> and wrapper <div>/<table> tags were showing up
    # as literal escaped text instead of being rendered, while the
    # unindented {table_html} content (from pandas to_html) still
    # rendered as raw HTML but with zero styling/columns.

    leads_table_css = """
<style>
.leads-table-wrap {
    max-height: TABLE_HEIGHTpx;
    overflow: auto;
    border: 1px solid rgba(250, 250, 250, 0.2);
    border-radius: 6px;
}
table.leads-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 14px;
    white-space: nowrap;
}
table.leads-table thead th {
    position: sticky;
    top: 0;
    background: #0e1117;
    color: #fafafa;
    text-align: left;
    padding: 8px 12px;
    border-bottom: 1px solid rgba(250, 250, 250, 0.3);
    z-index: 1;
}
table.leads-table tbody td {
    padding: 8px 12px;
    border-bottom: 1px solid rgba(250, 250, 250, 0.1);
    border-right: 1px solid rgba(250, 250, 250, 0.08);
    color: #fafafa;
}
table.leads-table tbody tr:hover {
    background: rgba(250, 250, 250, 0.05);
}
table.leads-table a {
    color: #4dabf7;
    text-decoration: underline;
}
</style>
""".replace("TABLE_HEIGHT", str(table_height))

    # Also strip any leading whitespace from every line of the
    # generated table HTML itself (pandas' to_html indents nested
    # tags), so nothing inside it can re-trigger the same
    # indented-code-block markdown rule.
    flat_table_html = "\n".join(
        line.lstrip() for line in table_html.split("\n")
    )

    full_html = (
        leads_table_css
        + '<div class="leads-table-wrap">\n'
        + flat_table_html
        + "\n</div>"
    )

    st.markdown(
        full_html,
        unsafe_allow_html=True
    )

    output = BytesIO()

    with pd.ExcelWriter(
        output,
        engine="openpyxl"
    ) as writer:

        filtered_df.to_excel(
            writer,
            index=False,
            sheet_name="Leads"
        )

    st.download_button(
        label="EXPORT LEADS TO EXCEL",
        data=output.getvalue(),
        file_name=(
            "Google_Maps_All_Saved_Leads.xlsx"
        ),
        mime=(
            "application/vnd.openxmlformats-"
            "officedocument.spreadsheetml.sheet"
        ),
        use_container_width=True
    )

    if st.button(
        "CLEAR ALL SAVED RESULTS",
        use_container_width=True
    ):

        st.session_state[
            "leads"
        ] = pd.DataFrame()

        st.session_state[
            "searches"
        ] = []

        st.session_state[
            "search_progress"
        ] = {}

        st.rerun()
