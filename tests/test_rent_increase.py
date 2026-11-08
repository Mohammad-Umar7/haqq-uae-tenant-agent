import pytest

from agent.tools.rent_increase import max_increase_percent, rera_rent_increase_check


@pytest.mark.parametrize(
    "pct_below, expected_cap",
    [
        (-5, 0),      # above average
        (0, 0),
        (5, 0),
        (10, 0),      # "up to 10%" includes 10
        (10.01, 5),
        (15, 5),
        (20, 5),
        (20.01, 10),
        (30, 10),
        (30.5, 15),
        (40, 15),
        (40.01, 20),
        (75, 20),
    ],
)
def test_bands(pct_below, expected_cap):
    cap, _label = max_increase_percent(pct_below)
    assert cap == expected_cap


def test_sample_scenario_exceeds_cap():
    # AED 85k vs 100k average = 15% below -> max 5% -> max AED 89,250.
    r = rera_rent_increase_check(85_000, 100_000, 93_500)
    assert r["percent_below_average"] == 15.0
    assert r["max_increase_percent"] == 5
    assert r["max_allowed_new_rent"] == 89_250
    assert r["is_within_cap"] is False
    assert r["excess_over_cap"] == 4_250
    assert r["proposed_increase_percent"] == 10.0
    assert "exceeds" in r["verdict"]
    assert "rent_increase_caps" in r["rule_ids"]


def test_within_cap():
    r = rera_rent_increase_check(85_000, 100_000, 89_000)
    assert r["is_within_cap"] is True
    assert r["excess_over_cap"] == 0


def test_exactly_at_cap_is_allowed():
    r = rera_rent_increase_check(85_000, 100_000, 89_250)
    assert r["is_within_cap"] is True


def test_no_increase_when_close_to_average():
    r = rera_rent_increase_check(95_000, 100_000, 96_000)
    assert r["max_increase_percent"] == 0
    assert r["max_allowed_new_rent"] == 95_000
    assert r["is_within_cap"] is False


def test_rent_above_average_means_no_increase():
    r = rera_rent_increase_check(110_000, 100_000)
    assert r["percent_below_average"] < 0
    assert r["max_increase_percent"] == 0
    assert "No increase" in r["verdict"]


def test_far_below_average_caps_at_20_percent():
    r = rera_rent_increase_check(50_000, 100_000)
    assert r["max_increase_percent"] == 20
    assert r["max_allowed_new_rent"] == 60_000


def test_decrease_is_never_blocked():
    r = rera_rent_increase_check(85_000, 100_000, 80_000)
    assert r["is_within_cap"] is True
    assert "not an increase" in r["verdict"]


def test_caveat_mentions_official_calculator_and_2025_update():
    r = rera_rent_increase_check(85_000, 100_000)
    text = " ".join(r["caveats"])
    assert "official" in text and "2025" in text


@pytest.mark.parametrize("args", [(0, 100_000), (85_000, 0), (-1, 100_000), (85_000, 100_000, -5)])
def test_invalid_inputs_raise(args):
    with pytest.raises(ValueError):
        rera_rent_increase_check(*args)
