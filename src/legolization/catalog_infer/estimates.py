"""Draft mass estimates for inferred parts, with labeled provenance.

The volumetric model is deliberately simple and conservative: the
occupied-cell volume times the density of ABS (1.05 g/cm^3) — an upper
bound, since real parts are hollow. A source-provided measured mass is
always preferred (method ``catalog-measured``); when both exist and
disagree by more than 25 percent a warning is recorded in the draft.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from legolization.catalog import EstimateProvenance, EstimateRecord
from legolization.ldraw_units import PLATE_LDU, STUD_LDU

if TYPE_CHECKING:
    from legolization.catalog_infer.sources import SourceReport

ABS_DENSITY_G_PER_CM3 = 1.05
LDU_CM = 0.04
CELL_VOLUME_CM3 = (STUD_LDU * LDU_CM) ** 2 * (PLATE_LDU * LDU_CM)
"""One 20x20x8-LDU stud/plate cell in cubic centimetres (0.2048)."""

DISAGREEMENT_LIMIT = 0.25
_VOLUMETRIC_CONFIDENCE = 0.4
_MEASURED_CONFIDENCE = 0.9


@dataclass(frozen=True, slots=True, kw_only=True)
class DraftEstimate:
    """The chosen draft mass with its provenance and any warnings."""

    part_key: str
    mass_g: float
    method: str
    basis: str
    source_url: str | None
    confidence: float
    retrieved_at: str | None
    volumetric_mass_g: float
    warnings: tuple[str, ...] = ()

    def to_record(self) -> EstimateRecord:
        """Return the estimate-sidecar record for this draft."""
        return EstimateRecord(
            part=self.part_key,
            fields={"mass_g": self.mass_g},
            provenance=EstimateProvenance(
                method=self.method,
                basis=self.basis,
                source_url=self.source_url,
                confidence=self.confidence,
                retrieved_at=self.retrieved_at,
            ),
        )

    @property
    def measured(self) -> bool:
        """Whether the chosen mass came from a measured source."""
        return self.method == "catalog-measured"


def volumetric_mass_g(cell_count: int) -> float:
    """Upper-bound mass of ``cell_count`` occupied cells in solid ABS."""
    return round(cell_count * CELL_VOLUME_CM3 * ABS_DENSITY_G_PER_CM3, 4)


def draft_mass_estimate(
    part_key: str,
    cell_count: int,
    sources: SourceReport,
) -> DraftEstimate:
    """Choose the draft mass: measured when a source provides one."""
    volumetric = volumetric_mass_g(cell_count)
    measured = sources.measured
    if measured is None:
        return DraftEstimate(
            part_key=part_key,
            mass_g=volumetric,
            method="volumetric",
            basis=(
                f"{cell_count} occupied cells x {CELL_VOLUME_CM3:g} cm^3 x "
                f"{ABS_DENSITY_G_PER_CM3:g} g/cm^3 solid-ABS upper bound"
            ),
            source_url=None,
            confidence=_VOLUMETRIC_CONFIDENCE,
            retrieved_at=None,
            volumetric_mass_g=volumetric,
        )
    mass = float(cast("float | str", measured.fields["mass_g"]))
    warnings: tuple[str, ...] = ()
    if abs(mass - volumetric) > DISAGREEMENT_LIMIT * mass:
        warnings = (
            (
                f"measured mass {mass:g} g and volumetric estimate "
                f"{volumetric:g} g disagree by more than "
                f"{DISAGREEMENT_LIMIT:.0%}"
            ),
        )
    reference = measured.fields.get("number") or measured.fields.get("part_num")
    detail = f" entry {reference}" if reference else ""
    return DraftEstimate(
        part_key=part_key,
        mass_g=mass,
        method="catalog-measured",
        basis=f"{measured.source}{detail} measured weight",
        source_url=measured.url,
        confidence=_MEASURED_CONFIDENCE,
        retrieved_at=measured.retrieved_at,
        volumetric_mass_g=volumetric,
        warnings=warnings,
    )
