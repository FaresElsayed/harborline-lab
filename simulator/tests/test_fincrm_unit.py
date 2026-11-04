"""Pure-function tests for the finance-crm modules (no database)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import numpy as np

from harborline.sim.crm import add_months, dispute_share, first_business_day, hq_address, mojibake, sf_dt
from harborline.sim.edi import interchange, isa, render, transaction
from harborline.sim.files import (
    client_profile,
    fuel_pct,
    gri_factor,
    ltl_charge,
    parcel_rate,
    rate_effective,
    tariff_year,
)


def test_isa_is_fixed_width() -> None:
    ts = datetime(2026, 1, 5, 14, 3)
    seg = isa("ZZ", "APEXAUTOPARTS", "ZZ", "HARBORLINE3PL", ts, 4521)
    assert len(seg) + 1 == 106
    assert seg.split("*")[13] == "000004521" and seg.endswith("*>")


def test_envelope_counts() -> None:
    sets = [transaction("940", 1, ["W05*N*SO-1*4410023", "LX*1", "W01*1*EA**VN*X"]), transaction("940", 2, ["W05*N*SO-2*4410024"])]
    segs = interchange(("ZZ", "A"), ("ZZ", "B"), ("A", "B"), datetime(2026, 1, 5, 9), "OW", 7, 3, sets)
    assert segs[2] == "ST*940*0001" and sets[0][-1] == "SE*5*0001" and sets[1][-1] == "SE*3*0002"
    assert segs[-2] == "GE*2*3" and segs[-1] == "IEA*1*000000007"
    assert render(segs, True).count(b"~\r\n") == len(segs)


def test_tariff_monotonic_and_gri() -> None:
    w = np.array([1, 5, 10, 50, 150])
    a = parcel_rate("UPSN", "GND", "US", 2026, np.full(5, 5), w)
    assert np.all(np.diff(a) > 0)
    assert parcel_rate("UPSN", "NDA", "US", 2026, np.array([5]), np.array([5]))[0] > a[1]
    assert gri_factor("UPSN", 2026) > gri_factor("UPSN", 2025) > 1
    assert ltl_charge("MRLF", "US", 2026, np.array([3]), np.array([1]))[0] == ltl_charge("MRLF", "US", 2026, np.array([3]), np.array([20]))[0]
    assert fuel_pct("MRLF", "LTL", 3.85) > fuel_pct("UPSN", "GND", 3.85) > 12


def test_rate_effective_first_monday() -> None:
    assert rate_effective(2026) == date(2026, 1, 5) and rate_effective(2024) == date(2024, 1, 1)
    assert tariff_year(date(2026, 1, 2)) == 2025 and tariff_year(date(2026, 1, 5)) == 2026


def test_helpers() -> None:
    assert sf_dt(datetime(2026, 1, 5, 14, 3, 22, tzinfo=UTC)) == "2026-01-05T14:03:22.000+0000"
    assert first_business_day(2026, 1) == date(2026, 1, 2)
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert mojibake("Lumière") == "LumiÃ¨re"
    a = hq_address(7, "BRUNE", "EU")
    assert a["city"] == "Paris" and a["country"] == "FR"
    shares = [dispute_share(7, f"US-INV{i:07d}", "disputer") for i in range(2000)]
    assert 0.05 < np.mean([s > 0 for s in shares]) < 0.11
    assert dispute_share(7, "US-INV0000001", "on_time") == 0


def test_csv_profiles_are_deterministic_and_varied() -> None:
    profiles = [client_profile(1, f"C{i}", "USD") for i in range(40)]
    assert client_profile(1, "C1", "USD") == profiles[1]
    assert len({p["delimiter"] for p in profiles}) >= 3 and len({p["encoding"] for p in profiles}) >= 2
    eu = client_profile(1, "BRUNE", "EUR")
    assert eu["delimiter"] == ";" and eu["encoding"] == "cp1252" and eu["decimal"] == ","
