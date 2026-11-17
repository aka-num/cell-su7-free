## v4.2.1 — OCR/PDF 导出稳定性修复

- 修复 `convert()` 中分支内导入 `tempfile` 造成的 `UnboundLocalError`。
- 新增本地 OCR 的 `line` 模式转 PPTX + HTML 报告回归测试。
- 新增额外 PDF 和直接 PDF 的导出回归测试。
- 保留 v4.2 原有图形矢量化、OCR、可编辑文本及 GUI，不触发云端 API。
- 与真实 Helicobacter pylori 图片端到端测试：584 个形状、26 个 PowerPoint 文本框。

# Changelog

## 4.2.0 — 结构感知对象重建

- 新增 `structure_aware.py`：高 IoU 原生几何提升、跨瓦片组件识别、重复实例分组和带状结构上/下轨迹。
- 新增 `--structure-mode objects`，命令行及 GUI 推荐此模式；引擎公共 API 的默认值维持 `hybrid` 以保证旧调用兼容。
- SVG 导出支持 `<rect>`、`<ellipse>`、`<line>`；PPTX 中矩形、椭圆使用原生预设图形，其他图形保留可编辑自由形状。
- 可视化画布增加 `group` 过滤器；重复组移动支持撤销/重做、JSON 编辑记录和真正导出。
- 场景图 schema 升至 2，加入 `native_primitive_count`、`region_hints`、重复实例坐标。
- 新增结构、导出、分组移动测试及同条件 LibreOffice 质量对照脚本。
- 无需联网、不上传图片、不依赖付费图像矢量化服务。

## 4.1.0 · 离线颜色与结构候选（2026-11-17）

- `perceptual_color.py`：Lab 聚类，颜色优先、结构优先和融合调色板；中位色校准；多段背景渐变检测；明暗权重调整。
- `precision_trace.py`：候选连接区域/独立颜色块、严格几何条件下椭圆拟合、重复大小/颜色组索引；始终保留精确路径回退。
- `engine.py` / `app.py`：新选项、安全独立的 `scene_graph_out`，支持 SVG 中结构标记和 PowerPoint 原生渐变。
- `studio_ui.py`：高级选项改为可滚动的分行布局，新增配色、结构策略选择，自动导出结构候选 JSON。
- `quality_report.py`：ΔE76 平均值、P95 和色差热力图。
- 新增颜色与几何回归测试；98 项测试在虚拟图形环境下全部通过；不启用 GUI 测试时 90 项通过、8 项跳过。


## v4.0 — 2026-11-17
- Added `precision` pixel-cell edge vector tracer, selected by `auto` offline mode.
- Preserved 1px details and compound holes via clockwise outer paths and counterclockwise holes.
- Added tile-based native editable compound paths and optional large connected-component separation.
- Added explicit command and object complexity safeguards; no raster fallback.
- Integrated CLI, GUI, PPTX/SVG output and visual editor edit-signature checks.
- Added reproducible same-source quality benchmark and 7 focused regression tests.
- Measured on a 1080×589 research figure: contour SVG 0.5767 SSIM, precision SVG 0.9199 SSIM (16 colors).
- Measured actual LibreOffice-rendered PPTX 0.9012 SSIM (16 colors); no claim of superiority to commercial vectorization.

## v3.5
- Visual editing canvas and safe retention of valid OCR partial results.
