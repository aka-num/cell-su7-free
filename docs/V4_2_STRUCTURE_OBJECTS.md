# v4.2 实现与验证说明

## 设计策略

路线 B：保留 v4.1 的精确像素边界描摹与 Lab 感知配色。新增的规则对象提升采用 **pixel-mask intersection-over-union（IoU）门控**，而非对图形语义作无依据的猜测。当前默认严格门槛 0.985，少数已确认箭头样式候选容忍 0.980；同时检测镂空以避免误填充。

`structure_aware.py` 的 `fit_primitive()` 在原图量化后的同色连通区域内尝试：

1. 单色实心细长矩形 → 画布坐标下的线形对象（具有真实描边）；
2. 无镂空实心区域 → 原生矩形；
3. 无镂空近轴椭圆 → 原生椭圆；
4. 七个顶点并有内凹肩部的多边形 → 填充箭头**候选**；
5. 不匹配 → 保留 v4.1 精确路径。

`precision_trace.py` 的 `objects` 模式先在原始全图的量化 id 矩阵中检索候选，因此跨 192 像素瓦片的同一个矩形/球状单元不容易被拆碎。每个被接受的对象会从尚未追踪的 id 掩码中移除，避免重复绘制；剩余对象由旧引擎继续重建。颜色原样沿用 v4.1 的量化调色板，未改颜色控制逻辑。

**不要将这些对象称为已识别的病原体、细胞膜或炎症小体。** 跨颜色复杂结构尚未完成完整语义恢复。`region_hints` 用 HSV/空间分布生成较长蓝色带状结构和上/下轨迹，用于后续人工结构标注，不替换任何原始颜色或路径。

## GUI / 组编辑

`studio_ui.py` 高级设置可选 `objects`。`visual_editor.py` 的过滤器选择 `group` 后点击成员所在的外接框即可选中几何重复组，再拖动整组。保存时仍输出每个独立对象的位移，并通过已有 `apply_edit_plan` 离线重放；不会把 SVG / PPTX 变成扁平位图，也不会自动修改原图。组是**几何相似分组**，成员间可能相距很远，拖动前应确认。

## 测试

命令行：

```bash
python -m pip install -r requirements.txt
CELL_SU7_GUI_TESTS=1 xvfb-run -a pytest -q
python app.py examples/demo_scientific.png -o benchmarks/v4_2/pathogen_objects.pptx \
 --svg-out benchmarks/v4_2/pathogen_objects.svg --report benchmarks/v4_2/pathogen_objects_quality.html \
 --scene-graph-out benchmarks/v4_2/pathogen_objects.scene.json --colors 32 --color-mode perceptual \
 --structure-mode objects
```

2026-11-17 本地结果：104 项自动化/GUI 测试通过。样例生成 470 个原生图形，其中 41 个高置信度规则几何对象。SVG 平均 ΔE76 4.758 / SSIM 0.9048。用相同 LibreOffice → PDF → pdftoppm 流程比较新旧 PPTX，v4.1 SSIM 0.9206、平均 ΔE76 4.188；v4.2 SSIM 0.9207、平均 ΔE76 4.190；质量基本持平，不能声称有确定的颜色改进。

## 已知局限

- 色彩/纹理依赖调色板量化，绝非逐像素无损。
- 复杂图形通常包含多个色块，尚不能自动作为一个完整可编辑物体拖动。
- 局部 SVG/PPTX 的抗锯齿不同，原生线条的显示与原始像素格可能有轻微差异。
- `scene_graph` 是候选索引，不是验证过的生物学语义标注。
- PowerPoint 文件经过 LibreOffice 打开测试，尚未在用户的具体 Windows/Office 环境测试。
