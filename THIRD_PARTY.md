# Third-party components and licensing

This repository's independent source is MIT licensed. Concept inspired by the MIT-licensed [yrui-cmd/cell_su7](https://github.com/yrui-cmd/cell_su7). No original paid-service internals, model weights or API keys are included.

| Component | Role | License / note |
|---|---|---|
| Python | Runtime | PSF |
| python-pptx | Editable OOXML / PPTX | MIT |
| OpenCV | Raster processing, local contour/inpaint | Apache-2.0 |
| NumPy | Numerical processing | BSD-3-Clause |
| Pillow | Image IO | HPND |
| defusedxml | SVG XML safe parser | PSF |
| Shapely | Local vector clipping | BSD-3-Clause |
| VTracer (optional) | Local spline tracing | MIT OR Apache-2.0 |
| pytesseract / Tesseract (optional) | Local OCR | Apache-2.0 |
| CairoSVG (optional) | SVG preview / PDF | LGPL-3.0-or-later |
| requests (optional) | AI-provider HTTP transport | Apache-2.0 |
| keyring (optional) | OS credential storage | MIT |

External models via user-specified APIs **are not part of this open-source distribution**. Their fees, weights, terms, data retention and availability are controlled by their provider. The local reconstruction path is free and open-source; turning on external AI may cost money.

Dependencies, model outputs and fonts may have their own licensing terms. If redistributing an environment, independently review licenses of all installed native dependencies and model weights.

## v4 local precision tracer

`precision_trace.py` is original MIT-licensed Python implementation using NumPy, Pillow, and optionally OpenCV for connected-region separation. It contains no paid-service or externally downloaded model code. It is not derived from any proprietary image-to-vector engine.
