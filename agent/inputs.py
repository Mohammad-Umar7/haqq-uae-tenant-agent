"""Details the tenant types into the form (things the contract can't tell us)."""

from __future__ import annotations

from dataclasses import dataclass

from agent.dates import parse_date


@dataclass(frozen=True)
class TenantInputs:
    rera_average_rent: float | None = None   # from the official RERA calculator
    proposed_new_rent: float | None = None   # what the landlord is asking for
    notice_date: str | None = None           # when the landlord's notice arrived (YYYY-MM-DD)
    notice_type: str | None = None           # rent_increase | change_terms | eviction | None
    delivery_method: str | None = None       # see agent.tools.notice.DELIVERY_METHODS

    @classmethod
    def from_form(
        cls,
        rera_average_rent: float | None,
        proposed_new_rent: float | None,
        notice_date: str | None,
        notice_type: str | None,
        delivery_method: str | None,
    ) -> "TenantInputs":
        """Clean raw form values: blank -> None, 0 -> None, dates -> ISO."""

        def positive(x: float | None) -> float | None:
            return float(x) if x not in (None, "") and float(x) > 0 else None

        parsed = parse_date(notice_date) if notice_date else None
        return cls(
            rera_average_rent=positive(rera_average_rent),
            proposed_new_rent=positive(proposed_new_rent),
            notice_date=parsed.isoformat() if parsed else None,
            notice_type=notice_type if notice_type and notice_type != "none" else None,
            delivery_method=delivery_method if delivery_method and delivery_method != "unknown" else None,
        )

    def as_lines(self) -> list[str]:
        """Human-readable lines, used in the LLM prompt and demo report."""
        lines = []
        if self.rera_average_rent:
            lines.append(f"RERA Rental Index average annual rent for similar units: AED {self.rera_average_rent:,.0f}")
        if self.proposed_new_rent:
            lines.append(f"Landlord's proposed new annual rent: AED {self.proposed_new_rent:,.0f}")
        if self.notice_type:
            lines.append(f"Type of landlord notice: {self.notice_type}")
        if self.notice_date:
            lines.append(f"Date the tenant received the landlord's notice: {self.notice_date}")
        if self.delivery_method:
            lines.append(f"How the notice was delivered: {self.delivery_method}")
        return lines
