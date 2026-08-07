"""Self-evaluation corpus: manifest, synthetic generators, and operations.

The package re-exports the corpus surface previously provided by
``scripts/corpus.py``: manifest records and loading, the deterministic
synthetic-shape generators, and the generate/download/verify/list
operations (plus their ``main`` entry point).
"""

from legolization.corpus.generators import (
    GENERATORS,
    cantilever,
    letter_h,
    letter_h_bicolour,
    letter_t,
    mushroom,
    press_tower,
    sparse_pillars,
    staircase_overhang,
    thin_shell,
    topple_arm,
    torsion_bridge,
    two_towers_bridge,
    wide_arch,
)
from legolization.corpus.manifest import (
    MANIFEST,
    CorpusModel,
    load_manifest,
    select_models,
)
from legolization.corpus.ops import (
    download,
    generate,
    list_models,
    main,
    verify,
)

__all__ = [
    "GENERATORS",
    "MANIFEST",
    "CorpusModel",
    "cantilever",
    "download",
    "generate",
    "letter_h",
    "letter_h_bicolour",
    "letter_t",
    "list_models",
    "load_manifest",
    "main",
    "mushroom",
    "press_tower",
    "select_models",
    "sparse_pillars",
    "staircase_overhang",
    "thin_shell",
    "topple_arm",
    "torsion_bridge",
    "two_towers_bridge",
    "verify",
    "wide_arch",
]
