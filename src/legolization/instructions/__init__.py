"""Step-by-step building instructions: sequencing, BOM, images, booklets."""

from legolization.instructions.bom import (
    BillOfMaterials,
    BomEntry,
    bill_of_materials,
)
from legolization.instructions.booklet import (
    Booklet,
    BookletConfig,
    ModelStats,
    StepEntry,
    booklet_html,
    build_booklet,
    write_booklet,
    write_booklet_pdf,
)
from legolization.instructions.metrics import (
    PlanQuality,
    SequenceSimilarity,
    plan_quality,
    sequence_similarity,
)
from legolization.instructions.render import (
    RenderConfig,
    Renderer,
    StepImages,
    detect_ldraw_dir,
    detect_renderer,
    render_step_images,
)
from legolization.instructions.sequencer import (
    BuildStep,
    InstructionPlan,
    InstructionsConfig,
    InstructionsError,
    RotStep,
    Subassembly,
    plan_instructions,
    verify_plan,
)

__all__ = [
    "BillOfMaterials",
    "BomEntry",
    "Booklet",
    "BookletConfig",
    "BuildStep",
    "InstructionPlan",
    "InstructionsConfig",
    "InstructionsError",
    "ModelStats",
    "PlanQuality",
    "RenderConfig",
    "Renderer",
    "RotStep",
    "SequenceSimilarity",
    "StepEntry",
    "StepImages",
    "Subassembly",
    "bill_of_materials",
    "booklet_html",
    "build_booklet",
    "detect_ldraw_dir",
    "detect_renderer",
    "plan_instructions",
    "plan_quality",
    "render_step_images",
    "sequence_similarity",
    "verify_plan",
    "write_booklet",
    "write_booklet_pdf",
]
