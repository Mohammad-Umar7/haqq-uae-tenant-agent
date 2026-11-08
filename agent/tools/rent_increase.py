"""Tool: rera_rent_increase_check - the Dubai Decree No. 43 of 2013 calculator.

The rule: the further your current rent is BELOW the RERA Rental Index
average for similar units, the bigger the increase a landlord may ask for.

    % below average      max increase
    up to 10%            0%
    11-20%               5%
    21-30%               10%
    31-40%               15%
    more than 40%        20%

The decree's bands are written in whole percentages (11-20 and so on). For
values between bands, such as 10.4% below, we use "more than the lower
bound": >10 to 20 -> 5%, >20 to 30 -> 10%, and so on. The official RERA
calculator is the source of truth; this is a helper, not a ruling.
"""

from __future__ import annotations

from typing import Any

from agent.knowledge import citation_for

# (upper bound of "% below average", max increase %, label). Order matters.
BANDS: list[tuple[float, int, str]] = [
    (10.0, 0, "up to 10% below average"),
    (20.0, 5, "11-20% below average"),
    (30.0, 10, "21-30% below average"),
    (40.0, 15, "31-40% below average"),
    (float("inf"), 20, "more than 40% below average"),
]


def max_increase_percent(percent_below_average: float) -> tuple[int, str]:
    """Return (max increase %, band label) for a given '% below average'.

    A negative value means the rent is ABOVE the average: no increase.
    """
    if percent_below_average <= 0:
        return 0, "at or above the average"
    for upper, cap, label in BANDS:
        if percent_below_average <= upper:
            return cap, label
    raise AssertionError("unreachable: the last band is infinite")


def rera_rent_increase_check(
    current_annual_rent: float,
    rera_average_rent: float,
    proposed_new_rent: float | None = None,
) -> dict[str, Any]:
    """Work out the legal maximum increase, and judge a proposed rent if given."""
    if current_annual_rent <= 0 or rera_average_rent <= 0:
        raise ValueError("current_annual_rent and rera_average_rent must be positive numbers (AED per year).")
    if proposed_new_rent is not None and proposed_new_rent <= 0:
        raise ValueError("proposed_new_rent must be a positive number (AED per year).")

    # Round to 2 decimals before choosing a band, so 19.999999 from float
    # arithmetic doesn't land in a different band than 20.00 would.
    pct_below = round((rera_average_rent - current_annual_rent) / rera_average_rent * 100, 2)
    cap, band = max_increase_percent(pct_below)
    max_new_rent = round(current_annual_rent * (1 + cap / 100), 2)

    result: dict[str, Any] = {
        "current_annual_rent": current_annual_rent,
        "rera_average_rent": rera_average_rent,
        "percent_below_average": pct_below,
        "band": band,
        "max_increase_percent": cap,
        "max_increase_amount": round(max_new_rent - current_annual_rent, 2),
        "max_allowed_new_rent": max_new_rent,
    }

    if proposed_new_rent is not None:
        increase = round(proposed_new_rent - current_annual_rent, 2)
        increase_pct = round(increase / current_annual_rent * 100, 2)
        within = proposed_new_rent <= max_new_rent
        result.update(
            proposed_new_rent=proposed_new_rent,
            proposed_increase_amount=increase,
            proposed_increase_percent=increase_pct,
            is_within_cap=within,
            excess_over_cap=0.0 if within else round(proposed_new_rent - max_new_rent, 2),
        )
        if increase <= 0:
            verdict = "The proposed rent is not an increase, so the cap does not limit it."
        elif within:
            verdict = (
                f"The proposed increase of {increase_pct}% is within the {cap}% cap "
                f"(max AED {max_new_rent:,.0f})."
            )
        else:
            verdict = (
                f"The proposed increase of {increase_pct}% exceeds the {cap}% cap. "
                f"The most the landlord may ask for is AED {max_new_rent:,.0f}, "
                f"which is AED {proposed_new_rent - max_new_rent:,.0f} less than proposed."
            )
    elif cap == 0:
        verdict = "No increase is allowed: the rent is no more than 10% below the RERA average."
    else:
        verdict = f"The landlord may increase the rent by at most {cap}% (to AED {max_new_rent:,.0f})."

    result["verdict"] = verdict
    result["rule_ids"] = ["rent_increase_caps", "rental_index_source", "notice_change_terms_90_days"]
    result["citations"] = [citation_for(r) for r in result["rule_ids"]]
    result["caveats"] = [
        "The official DLD/RERA Rental Index calculator is the source of truth. Dubai updated its "
        "rental index in 2025, so use a current figure for your unit type and area.",
        "Any increase also needs written notice at least 90 days before the contract expires (Art. 14).",
    ]
    return result
