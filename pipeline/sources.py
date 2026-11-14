"""Every external data source, in one place. URLs checked 2026-10-25 unless marked."""

COUNTY_FIPS = ["17031"]          # Cook County. Add collar counties here if cases go beyond Cook.
STATE_FIPS = "17"
ACS_YEAR = "2023"                # ACS 5-year vintage; bump when a newer 5-year release is out

SOURCES = {
    "places_tract": {
        "url": "https://data.cdc.gov/resource/yjkw-uj5s.csv?$where=stateabbr=%27IL%27&$limit=50000",
        "file": "places_tract_il.csv",
        "what": "CDC PLACES census tract data (GIS-friendly), 2025 release, Illinois only",
        "page": "https://data.cdc.gov/d/yjkw-uj5s",
        "verified": True,
    },
    "fara_2019": {
        "url": "https://www.ers.usda.gov/media/5627/2019-large-retailer-access-map-lram-formerly-known-as-the-food-access-research-atlas-fara-data.zip?v=69176",
        "file": "fara_2019.zip",
        "what": "USDA Food Access Research Atlas 2019 (now 'LRAM'), on 2010 census tracts",
        "page": "https://www.ers.usda.gov/data-products/food-access-research-atlas/download-the-data",
        "verified": True,
    },
    "acs_vehicles": {
        "url": ("https://api.census.gov/data/{year}/acs/acs5?get=NAME,B08201_001E,B08201_002E"
                "&for=tract:*&in=state:{state}%20county:{county}"),
        "file": "acs_b08201_{county}.json",
        "what": "ACS 5-year B08201: households (001E) and households with no vehicle (002E), by 2020 tract",
        "page": "https://data.census.gov/",
        "verified": True,
        "per_county": True,
    },
    "tract_rel_2020_2010": {
        "url": "https://www2.census.gov/geo/docs/maps-data/data/rel2020/tract/tab20_tract20_tract10_natl.txt",
        "file": "tab20_tract20_tract10_natl.txt",
        "what": "Census 2020-to-2010 tract relationship file (national, pipe-delimited)",
        "page": "https://www.census.gov/geographies/reference-files/time-series/geo/relationship-files.2020.html",
        "verified": False,   # path follows the Census naming scheme; if 404, get the link from the page above
    },
    "gazetteer_tracts": {
        "url": "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2020_Gazetteer/2020_gaz_tracts_{state}.txt",
        "file": "2020_gaz_tracts_{state}.txt",
        "what": "2020 Census Gazetteer: tract internal points (lat/lon)",
        "page": "https://www.census.gov/geographies/reference-files/time-series/geo/gazetteer-files.html",
        "verified": False,   # same: follows the Census naming scheme
    },
    "gtfs_cta": {
        "url": "https://www.transitchicago.com/downloads/sch_data/google_transit.zip",
        "file": "gtfs_cta.zip",
        "what": "CTA GTFS schedule",
        "page": "https://www.transitchicago.com/developers/gtfs/",
        "verified": True,
    },
    "gtfs_pace": {
        "url": "https://www.pacebus.com/sites/default/files/2026-08/GTFS.zip",
        "file": "gtfs_pace.zip",
        "what": "Pace GTFS schedule (the URL changes with each feed release)",
        "page": "https://www.pacebus.com/route-timetable-data-services",
        "verified": True,
    },
    # ---- community resources (open data; checked 2026-10-25). Pantries with hours aren't openly
    # available: the Food Depository locator blocks automated access and 211's data platform needs
    # authorization from 211 Metro Chicago, so those stay hand-curated or permission-gated.
    "chi_warming": {
        "url": "https://data.cityofchicago.org/resource/h243-v2q5.json?$limit=5000",
        "file": "chi_warming_centers.json",
        "what": "City of Chicago warming centers (libraries, parks, community centers) with hours",
        "page": "https://data.cityofchicago.org/d/h243-v2q5",
        "verified": True,
    },
    "chi_cooling": {
        "url": "https://data.cityofchicago.org/resource/msrk-w9ih.json?$limit=5000",
        "file": "chi_cooling_centers.json",
        "what": "City of Chicago cooling centers with hours",
        "page": "https://data.cityofchicago.org/d/msrk-w9ih",
        "verified": True,
    },
    "chi_libraries": {
        "url": "https://data.cityofchicago.org/resource/x8fc-8rcq.json?$limit=500",
        "file": "chi_libraries.json",
        "what": "Chicago Public Library branches: hours, phone (free computers and wifi)",
        "page": "https://data.cityofchicago.org/d/x8fc-8rcq",
        "verified": True,
    },
    "hrsa_sites": {
        "url": "https://data.hrsa.gov/DataDownload/DD_Files/Health_Center_Service_Delivery_and_LookAlike_Sites.csv",
        "file": "hrsa_health_center_sites.csv",
        "what": "HRSA health center (FQHC + look-alike) service delivery sites, national",
        "page": "https://data.hrsa.gov/data/download",
        "verified": True,
    },
    "snap_retailers": {
        "url": "https://www.fns.usda.gov/sites/default/files/resource-files/snap-retailer-locator-data2005-2025.zip",
        "file": "snap_retailers_2005_2025.zip",
        "what": "USDA FNS SNAP retailer locator data 2005-2025 (current = no end date)",
        "page": "https://www.fns.usda.gov/snap/retailer/historical-data",
        "verified": True,
    },
}

GEOCODER_ONELINE = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"
GEOCODER_COORDS = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"
GEOCODER_PARAMS = {"benchmark": "Public_AR_Current", "vintage": "Current_Current", "format": "json"}
