from app.h3_cells import load_model_h3_cells
from app.h3_regions import load_h3_region_map


def test_h3_region_map_covers_supported_cells():
    supported = set(load_model_h3_cells())

    regions = load_h3_region_map()

    assert set(regions) == supported
    assert len(regions) == 564


def test_h3_region_map_entries_have_display_labels_and_centers():
    regions = load_h3_region_map()

    sample = next(iter(regions.values()))

    assert sample["name"]
    assert sample["display_name"].endswith(", Manhattan")
    assert isinstance(sample["lat"], float)
    assert isinstance(sample["lng"], float)
