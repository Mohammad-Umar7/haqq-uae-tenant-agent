from datetime import date

import pytest

from agent.dates import long_date, parse_date, subtract_months
from agent.tools.notice import notice_deadline_check


@pytest.mark.parametrize(
    "text, expected",
    [
        ("2026-01-15", date(2026, 1, 15)),
        ("15/01/2026", date(2026, 1, 15)),   # day first, UAE style
        ("05/01/2026", date(2026, 1, 5)),
        ("15.01.2026", date(2026, 1, 15)),
        ("15 January 2026", date(2026, 1, 15)),
        ("15 Jan 2026", date(2026, 1, 15)),
        ("15th January, 2026", date(2026, 1, 15)),
        ("January 15, 2026", date(2026, 1, 15)),
        ("31/02/2026", None),                # not a real date
        ("next Tuesday", None),
        ("", None),
    ],
)
def test_parse_date(text, expected):
    assert parse_date(text) == expected


def test_long_date():
    assert long_date("2027-01-14") == "14 January 2027"
    assert long_date("whenever") == "whenever"


def test_subtract_months_clamps_to_month_end():
    assert subtract_months(date(2027, 1, 14), 12) == date(2026, 1, 14)
    assert subtract_months(date(2026, 3, 31), 1) == date(2026, 2, 28)
    assert subtract_months(date(2029, 2, 28), 12) == date(2028, 2, 28)
    assert subtract_months(date(2028, 2, 29), 12) == date(2027, 2, 28)


def test_rent_increase_deadline_is_90_days_before_expiry():
    r = notice_deadline_check("2027-01-14", "rent_increase", today="2026-09-01")
    assert r["last_valid_notice_date"].startswith("2026-10-16")
    assert r["days_until_notice_deadline"] == 45
    assert r["rule_ids"] == ["notice_change_terms_90_days"]


def test_notice_exactly_90_days_before_is_on_time():
    r = notice_deadline_check("2027-01-14", "rent_increase", notice_date="2026-10-16", today="2026-11-08")
    assert r["notice_days_before_expiry"] == 90
    assert r["notice_on_time"] is True


def test_late_notice_sample_scenario():
    r = notice_deadline_check("2027-01-14", "rent_increase", notice_date="2026-11-01", today="2026-11-08")
    assert r["notice_on_time"] is False
    assert r["notice_days_before_expiry"] == 74
    assert "LATE by 16 days" in r["verdict"]


def test_deadline_already_passed_without_notice():
    r = notice_deadline_check("2027-01-14", "change_terms", today="2026-11-08")
    assert r["days_until_notice_deadline"] < 0
    assert "deadline passed" in r["verdict"]


def test_eviction_needs_12_months_and_valid_delivery():
    r = notice_deadline_check(
        "2027-01-14", "eviction", notice_date="2026-06-01", delivery_method="whatsapp_or_sms", today="2026-11-08"
    )
    assert r["last_valid_notice_date"].startswith("2026-01-14")
    assert r["notice_on_time"] is False
    assert r["delivery_method_ok"] is False
    assert r["rule_ids"] == ["eviction_12_months_notice"]


def test_eviction_on_time_by_notary():
    r = notice_deadline_check(
        "2027-01-14", "eviction", notice_date="2026-01-10", delivery_method="notary_public", today="2026-11-08"
    )
    assert r["notice_on_time"] is True
    assert r["delivery_method_ok"] is True


def test_eviction_unknown_delivery_is_flagged_for_checking():
    r = notice_deadline_check("2027-01-14", "eviction", notice_date="2026-01-10", today="2026-11-08")
    assert r["delivery_method_ok"] is None
    assert "notary" in r["delivery_note"]


def test_accepts_day_first_dates():
    r = notice_deadline_check("14/01/2027", "rent_increase", notice_date="01/11/2026", today="08/11/2026")
    assert r["notice_on_time"] is False


@pytest.mark.parametrize(
    "kwargs",
    [
        {"contract_end_date": "2027-01-14", "notice_type": "holiday"},
        {"contract_end_date": "not a date", "notice_type": "rent_increase"},
        {"contract_end_date": "2027-01-14", "notice_type": "eviction", "delivery_method": "pigeon"},
        {"contract_end_date": "2027-01-14", "notice_type": "rent_increase", "notice_date": "soon"},
    ],
)
def test_invalid_inputs_raise(kwargs):
    with pytest.raises(ValueError):
        notice_deadline_check(**kwargs)
