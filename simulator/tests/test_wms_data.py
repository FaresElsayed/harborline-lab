"""Pure checks of the curated reference data and identifier formats (no database)."""

from __future__ import annotations

import re

import numpy as np
import pytest

from harborline.sim import carrier, geo, wms


def test_cities_are_coherent() -> None:
    c = geo.cities()
    assert 300 <= len(c) <= 450
    assert set(c["country"]) == {"US", "CA", "MX"}
    for row in c.itertuples():
        assert -60 < row.lat < 72 and -170 < row.lon < -50, row.city
        for p in row.postal_list:
            if row.country == "US":
                assert re.fullmatch(r"\d{5}", p), (row.city, p)
            elif row.country == "CA":
                assert re.fullmatch(r"[A-Z]\d[A-Z]", p), (row.city, p)
            else:
                assert re.fullmatch(r"\d{3}", p), (row.city, p)
    assert not c.duplicated(["city", "state", "country"]).any()


def test_postal_codes_match_the_city() -> None:
    rng = np.random.default_rng(1)
    idx = np.arange(len(geo.cities()))
    codes = geo.postal_codes(rng, idx)
    c = geo.cities()
    for i, code in zip(idx, codes):
        row = c.iloc[i]
        if row["country"] == "CA":
            assert re.fullmatch(r"[A-Z]\d[A-Z] \d[A-Z]\d", code) and code[:3] in row["postal_list"]
        elif row["country"] == "MX":
            assert re.fullmatch(r"\d{5}", code) and code[:3] in row["postal_list"]
        else:
            assert code in row["postal_list"]


def test_warehouse_cities_exist() -> None:
    from harborline.settings import get_settings
    from harborline.sim.engine import load_yaml

    rules = load_yaml(get_settings().rules_path)
    for w in rules["warehouses"]:
        a = w["address"]
        assert geo.city_index(a["city"], a["state"], a["country"]) >= 0, w["code"]


def _upc_ok(code: str) -> bool:
    digits = [int(ch) for ch in code]
    if len(code) == 12:
        s = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(digits[:-1]))
    else:
        s = sum(d * (1 if i % 2 == 0 else 3) for i, d in enumerate(digits[:-1]))
    return (10 - s % 10) % 10 == digits[-1]


@pytest.mark.parametrize("country,cat", [("US", "apparel"), ("MX", "salsas_es"), ("CA", "books")])
def test_upc_check_digits(country: str, cat: str) -> None:
    codes = wms._upcs({"code": "TESTCO", "cat": cat, "country": country}, np.arange(1, 400))
    assert len(set(codes)) == len(codes)
    for code in codes:
        assert code.isdigit() and len(code) in (12, 13) and _upc_ok(code), code
    if country == "MX":
        assert all(c.startswith("750") for c in codes)
    if cat == "books":
        assert all(c.startswith("978") for c in codes)


FORMATS = {
    "ups": r"1Z[0-9A-Z]{6}\d{2}\d{8}", "fedex": r"\d{12}", "usps": r"(9400|9205)\d{18}", "dhl": r"GM\d{16}",
    "vrpx": r"VR\d{12}", "canadapost": r"\d{16}", "purolator": r"\d{12}", "estafeta": r"[0-9A-Z]{22}", "pro": r"\d{9,11}",
}


@pytest.mark.parametrize("fmt", sorted(FORMATS))
def test_tracking_number_formats(fmt: str) -> None:
    n = 500
    out = carrier.tracking_numbers(np.full(n, fmt, dtype=object), np.full(n, "GND", dtype=object),
                                   np.full(n, "ATL1", dtype=object), np.arange(50_000, 50_000 + n))
    assert len(set(out)) == n
    for t in out:
        assert re.fullmatch(FORMATS[fmt], t), (fmt, t)


def test_parcel_zone_and_dim_weight() -> None:
    assert list(geo.parcel_zone(np.array([10, 200, 500, 900, 1300, 1700, 2500]))) == [2, 3, 4, 5, 6, 7, 8]
    billed = carrier.billed_weight_lb(np.array([2.0, 30.0]), np.array([[20.0, 16.0, 12.0], [10.0, 10.0, 10.0]]), np.array([False, False]))
    assert list(billed) == [28.0, 30.0]


def test_time_zone_offsets() -> None:
    oct_2026, jan_2026 = 1_790_987_400, 1_767_225_600
    u = np.array([oct_2026, jan_2026])
    assert list(geo.utc_offset_s("America/New_York", u) // 3600) == [-4, -5]
    assert list(geo.utc_offset_s("America/Phoenix", u) // 3600) == [-7, -7]
    assert list(geo.utc_offset_s("America/Monterrey", u) // 3600) == [-6, -6]
    assert list(geo.utc_offset_s("America/St_Johns", u)) == [-9000, -12600]


def test_time_zone_round_trip() -> None:
    utc = np.array([1_773_500_000, 1_793_000_000, 1_800_000_000])
    for tz in ("America/New_York", "America/Monterrey", "America/Toronto", "America/Los_Angeles"):
        assert (geo.local_to_utc(tz, geo.utc_to_local(tz, utc)) == utc).all()


def test_iso_offsets() -> None:
    out = geo.iso_with_offset(np.array([0, 3600]), np.array([-18000, 19800]))
    assert list(out) == ["1970-01-01T00:00:00-05:00", "1970-01-01T01:00:00+05:30"]
