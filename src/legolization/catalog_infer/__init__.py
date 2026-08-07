"""Catalog-extension inference: geometry, estimates, sources, validation.

The ``catalog infer`` and ``catalog validate`` commands live on top of
this package. Inference reads one LDraw part from the detected library,
voxelizes it onto the stud/plate grid, drafts a mass estimate with
labeled provenance, cites every consulted source, and writes a portable
``<key>-legolization-support`` bundle that :func:`legolization.catalog.
resolve_catalog` activates only after all five validation gates pass.
"""

from legolization.catalog_infer.bundle import SupportBundle, write_support_bundle
from legolization.catalog_infer.estimates import DraftEstimate, draft_mass_estimate
from legolization.catalog_infer.geometry import InferredGeometry, infer_geometry
from legolization.catalog_infer.sources import (
    SourceLookup,
    SourceReport,
    lookup_sources,
)
from legolization.catalog_infer.validate import GateResult, validate_extension

__all__ = [
    "DraftEstimate",
    "GateResult",
    "InferredGeometry",
    "SourceLookup",
    "SourceReport",
    "SupportBundle",
    "draft_mass_estimate",
    "infer_geometry",
    "lookup_sources",
    "validate_extension",
    "write_support_bundle",
]
