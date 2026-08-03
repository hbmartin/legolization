# pyLDCad

pyLDCad provides a typed, extensible connectivity layer for resolved LDraw
assemblies. It consumes `pyldraw3` model inspection results and combines
geometry primitives, curated metadata, LDCad/Studio metadata, and user registry
extensions without requiring a model to fit a voxel grid.

```python
from pyldcad import ConnectivityConfig, analyze_connectivity

analysis = analyze_connectivity(model_analysis, ConnectivityConfig())
print(analysis.confirmed_component_count)
```

