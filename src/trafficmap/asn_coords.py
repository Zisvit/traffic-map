"""The places of the networks that answer from many cities at once.

The city database knows where an address is *registered*, and that is
not always where the machine is.  A content network puts a copy of its
servers in every city it can (anycast), and the database gives all of
them the place of one office: a request to a network like that is
answered somewhere near the machine that asks, and the map would draw
it on another continent.

This module holds a small table of the networks whose answer is known
to be a point of presence, and not a single machine: the big content
networks, the CDNs and the few messaging networks with a handful of
data centres.  The place of one of their addresses is the point of
presence closest to this machine, which is the one that answers it,
or the best guess when the network is not anycast.

What the table is not
---------------------
It is not a map of the internet: an address of a network that is not
here keeps the place of the database.  It is not an accuracy either:
a point of presence is a city, and the dot is the city.  The table is
small on purpose, because every line of it is a guess that is worth
only as much as the network it describes: a wrong line would move the
traffic of a network to a city it never reaches.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = ["ANYCAST_POPS", "Place", "anycast_place", "nearest_place"]

#: Radius of the Earth, in kilometres.
EARTH_RADIUS = 6371.0


@dataclass(frozen=True, slots=True)
class Place:
    """One city a network answers from.

    Attributes:
        city: Name of the city in English.
        country: ISO 3166-1 alpha-2 code of the country of the city.
        lat: Latitude of the city in degrees.
        lon: Longitude of the city in degrees.
    """

    city: str
    country: str
    lat: float
    lon: float


AMSTERDAM = Place("Amsterdam", "NL", 52.3676, 4.9041)
ASHBURN = Place("Ashburn", "US", 39.0438, -77.4874)
CAMBRIDGE = Place("Cambridge", "US", 42.3736, -71.1097)
DUBLIN = Place("Dublin", "IE", 53.3498, -6.2603)
FRANKFURT = Place("Frankfurt", "DE", 50.1109, 8.6821)
LONDON = Place("London", "GB", 51.5074, -0.1278)
LOS_ANGELES = Place("Los Angeles", "US", 34.0522, -118.2437)
LOS_GATOS = Place("Los Gatos", "US", 37.2350, -121.9624)
MENLO_PARK = Place("Menlo Park", "US", 37.4848, -122.1484)
MIAMI = Place("Miami", "US", 25.7617, -80.1918)
MOSCOW = Place("Moscow", "RU", 55.7558, 37.6173)
MOUNTAIN_VIEW = Place("Mountain View", "US", 37.4220, -122.0850)
NEW_YORK = Place("New York", "US", 40.7128, -74.0060)
PARIS = Place("Paris", "FR", 48.8566, 2.3522)
SAN_FRANCISCO = Place("San Francisco", "US", 37.7749, -122.4194)
SAO_PAULO = Place("São Paulo", "BR", -23.5505, -46.6333)
SINGAPORE = Place("Singapore", "SG", 1.3521, 103.8198)
ST_PETERSBURG = Place("St Petersburg", "RU", 59.9343, 30.3351)
STOCKHOLM = Place("Stockholm", "SE", 59.3293, 18.0686)
SYDNEY = Place("Sydney", "AU", -33.8688, 151.2093)
TOKYO = Place("Tokyo", "JP", 35.6762, 139.6503)
WARSAW = Place("Warsaw", "PL", 52.2297, 21.0122)

#: The points of presence of the networks that answer from many cities,
#: by autonomous system number.  The first place of a network is its
#: main one: it is used when the place of this machine is not known.
ANYCAST_POPS: dict[int, tuple[Place, ...]] = {
    # Cloudflare: the CDN behind a large part of the web.
    13335: (
        AMSTERDAM,
        FRANKFURT,
        LONDON,
        PARIS,
        WARSAW,
        STOCKHOLM,
        MOSCOW,
        ST_PETERSBURG,
        NEW_YORK,
        LOS_ANGELES,
        SINGAPORE,
        TOKYO,
        SAO_PAULO,
        SYDNEY,
    ),
    # Google and YouTube.
    15169: (
        MOUNTAIN_VIEW,
        AMSTERDAM,
        FRANKFURT,
        LONDON,
        PARIS,
        WARSAW,
        STOCKHOLM,
        MOSCOW,
        ST_PETERSBURG,
        NEW_YORK,
        LOS_ANGELES,
        SINGAPORE,
        TOKYO,
        SAO_PAULO,
        SYDNEY,
    ),
    # Akamai, and the second network of the same CDN.
    20940: (
        CAMBRIDGE,
        AMSTERDAM,
        FRANKFURT,
        LONDON,
        PARIS,
        WARSAW,
        STOCKHOLM,
        MOSCOW,
        ST_PETERSBURG,
        NEW_YORK,
        LOS_ANGELES,
        SINGAPORE,
        TOKYO,
        SAO_PAULO,
    ),
    16625: (
        CAMBRIDGE,
        AMSTERDAM,
        FRANKFURT,
        LONDON,
        PARIS,
        WARSAW,
        STOCKHOLM,
        MOSCOW,
        ST_PETERSBURG,
        NEW_YORK,
        LOS_ANGELES,
        SINGAPORE,
        TOKYO,
        SAO_PAULO,
    ),
    # Fastly: the Python package index, among others.
    54113: (
        SAN_FRANCISCO,
        AMSTERDAM,
        FRANKFURT,
        LONDON,
        PARIS,
        STOCKHOLM,
        MOSCOW,
        NEW_YORK,
        LOS_ANGELES,
        SINGAPORE,
        TOKYO,
        SYDNEY,
    ),
    # Meta: Facebook, Instagram, WhatsApp.
    32934: (
        MENLO_PARK,
        AMSTERDAM,
        FRANKFURT,
        LONDON,
        DUBLIN,
        PARIS,
        STOCKHOLM,
        MOSCOW,
        WARSAW,
        NEW_YORK,
        LOS_ANGELES,
        SINGAPORE,
        TOKYO,
        SAO_PAULO,
    ),
    # X.
    13414: (
        SAN_FRANCISCO,
        AMSTERDAM,
        FRANKFURT,
        LONDON,
        DUBLIN,
        ASHBURN,
        MOSCOW,
        SINGAPORE,
        TOKYO,
    ),
    # Netflix: the video is served from the exchanges of every country.
    2906: (
        LOS_GATOS,
        AMSTERDAM,
        FRANKFURT,
        LONDON,
        PARIS,
        STOCKHOLM,
        MOSCOW,
        ASHBURN,
        NEW_YORK,
        LOS_ANGELES,
        SINGAPORE,
        TOKYO,
        SAO_PAULO,
    ),
    # Telegram: a handful of data centres, not anycast.
    62041: (
        AMSTERDAM,
        MIAMI,
        SINGAPORE,
    ),
    # GitHub.
    36459: (
        SAN_FRANCISCO,
        AMSTERDAM,
        FRANKFURT,
        LONDON,
        ASHBURN,
        SINGAPORE,
        TOKYO,
    ),
}


def anycast_place(asn: int | None, home: tuple[float, float] | None) -> Place | None:
    """Return the place of the network an address belongs to.

    Args:
        asn: The autonomous system number of the address, or ``None``
            when it is not known.
        home: The place of this machine, ``(lat, lon)``, or ``None``
            when it is not known.

    Returns:
        The point of presence of the network closest to this machine,
        the main one when the machine has no place, and ``None`` when
        the network is not one of the anycast ones.
    """
    if asn is None:
        return None
    places = ANYCAST_POPS.get(asn)
    if not places:
        return None
    return nearest_place(places, home)


def nearest_place(places: tuple[Place, ...], home: tuple[float, float] | None) -> Place:
    """Return the place of a list that is closest to this machine.

    Args:
        places: The places to choose from.  It must not be empty.
        home: The place of this machine, ``(lat, lon)``, or ``None``
            when it is not known.

    Returns:
        The closest place, or the first one when the place of the
        machine is not known.

    Raises:
        ValueError: If the list of the places is empty.
    """
    if not places:
        raise ValueError("a network has no place")
    if home is None:
        return places[0]
    lat, lon = home
    return min(places, key=lambda place: distance(lat, lon, place.lat, place.lon))


def distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the distance of two places on the Earth, in kilometres.

    Args:
        lat1: Latitude of the first place.
        lon1: Longitude of the first place.
        lat2: Latitude of the second place.
        lon2: Longitude of the second place.

    Returns:
        The great-circle distance, in kilometres.
    """
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    )
    return 2.0 * EARTH_RADIUS * math.asin(min(1.0, math.sqrt(a)))
