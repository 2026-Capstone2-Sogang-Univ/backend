from __future__ import annotations

from functools import lru_cache
from typing import TypedDict

import h3

from .h3_cells import load_model_h3_cells


class H3Region(TypedDict):
    name: str
    display_name: str
    lat: float
    lng: float


def load_h3_region_map() -> dict[str, H3Region]:
    """Return display labels for supported H3 cells.

    The first version is an approximate Manhattan labeler based on H3 cell
    centers. It is intentionally read-only and independent of the simulation
    loop; exact neighborhood polygon joins can replace this later without
    changing the endpoint contract.
    """
    return dict(_cached_h3_region_map())


@lru_cache(maxsize=1)
def _cached_h3_region_map() -> dict[str, H3Region]:
    regions: dict[str, H3Region] = {}
    for cell in load_model_h3_cells():
        lat, lng = h3.cell_to_latlng(cell)
        name = _manhattan_region_name(lat, lng)
        regions[cell] = {
            "name": name,
            "display_name": f"{name}, Manhattan",
            "lat": lat,
            "lng": lng,
        }
    return regions


def _manhattan_region_name(lat: float, lng: float) -> str:
    if lat < 40.707:
        return "Financial District"
    if lat < 40.716:
        return "Battery Park City" if lng < -74.007 else "Civic Center"
    if lat < 40.724:
        return "Tribeca" if lng < -74.000 else "Lower East Side"
    if lat < 40.732:
        if lng < -74.003:
            return "Hudson Square"
        return "SoHo" if lng < -73.994 else "Lower East Side"
    if lat < 40.742:
        if lng < -74.000:
            return "West Village"
        return "Greenwich Village" if lng < -73.988 else "East Village"
    if lat < 40.753:
        if lng < -74.000:
            return "Chelsea"
        return "Flatiron District" if lng < -73.986 else "Gramercy"
    if lat < 40.766:
        if lng < -73.993:
            return "Hell's Kitchen"
        return "Times Square" if lng < -73.982 else "Midtown East"
    if lat < 40.780:
        if lng < -73.989:
            return "Lincoln Square"
        return "Central Park South" if lng < -73.974 else "Upper East Side"
    if lat < 40.800:
        return "Upper West Side" if lng < -73.967 else "Upper East Side"
    if lat < 40.815:
        return "Manhattan Valley" if lng < -73.955 else "East Harlem"
    if lat < 40.835:
        return "Morningside Heights" if lng < -73.945 else "Harlem"
    if lat < 40.855:
        return "Hamilton Heights" if lng < -73.935 else "East Harlem"
    return "Washington Heights"
