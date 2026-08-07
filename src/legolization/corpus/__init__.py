"""Self-evaluation corpus: manifest, generators, storage, and operations.

The manifest ships inside the package and corpus inputs live in
platform user-data storage (:mod:`legolization.corpus.storage`), so the
``legolization corpus`` commands work outside a checkout. This package
re-exports manifest records and loading, the deterministic
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
    select_scope,
)
from legolization.corpus.ops import (
    ModelReport,
    download,
    exit_code,
    generate,
    list_models,
    list_table,
    main,
    verify,
)
from legolization.corpus.storage import corpus_root, migrate_legacy, resolve_input

__all__ = [
    "GENERATORS",
    "MANIFEST",
    "CorpusModel",
    "ModelReport",
    "cantilever",
    "corpus_root",
    "download",
    "exit_code",
    "generate",
    "letter_h",
    "letter_h_bicolour",
    "letter_t",
    "list_models",
    "list_table",
    "load_manifest",
    "main",
    "migrate_legacy",
    "mushroom",
    "press_tower",
    "resolve_input",
    "select_models",
    "select_scope",
    "sparse_pillars",
    "staircase_overhang",
    "thin_shell",
    "topple_arm",
    "torsion_bridge",
    "two_towers_bridge",
    "verify",
    "wide_arch",
]
