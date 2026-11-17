# v4.1｜离线颜色保真与结构候选重建

此版本在 v4.0 精确像素边界矢量化的基础上升级。目标是改善真实科研图的色相、明暗和背景渐变，同时保留可编辑性，不发起任何联网或付费请求。

## 颜色策略

提供四个 `--color-mode`：

- `perceptual`（GUI/命令行默认）：前景分离后使用 Lab 感知空间聚类，可使用 `--luminance-weight` 调整亮度权重。适合颜色丰富的生物机制图，默认 32 色和 1.7 倍亮度权重。
- `balanced`：保留 PIL MEDIANCUT 的像素区域分割，在每个类别内从原图恢复 RGB 中位色，通常保留更多亮度边界，但可能把少量饱和色错误合并，例如绿色和蓝色。比较颜色时需要检查真实视觉效果，而不只是 SSIM。
- `fusion`：组合 MEDIANCUT 和 Lab 的候选调色板，实验性模式。
- `legacy`：原始 MEDIANCUT 配色，便于回归比较。

`--background-gradient` 默认开启：通过多个高度带的低饱和度高亮像素估计背景色，且只对匹配的像素使用渐变。背景会成为 SVG `<linearGradient>` / PPTX DrawingML `<a:gradFill>`，不是整图位图。纯色或证据不足时不会凭空生成渐变。

建议：彩色科研图用 `perceptual 32`，需要更多亮度/纹理结构的图试用 `balanced 16`；在质量报告里比较 MAE、SSIM、平均 ΔE76，并结合实际图片判断。

## 结构模式

- `hybrid`（默认）：连通区域拆分，尝试识别紧凑块、细长连接候选、自由曲线；对满足严格阈值的近似椭圆尝试原生曲线替换，其他区域使用精确像素边界保底。对大小与色彩相近的独立形状生成重复组候选。
- `annotate`：尽可能沿用瓦片路径组织，仅输出可视/颜色分层信息，速度与对象数更友好。
- `off`：不进行结构候选分析；仍然得到原生可编辑路径。

**重要限制**：重复组只代表外观相似，不能推断生物学身份。`scene_graph.json` 是可用于人工分组、后续研究的候选描述，并不意味着 PowerPoint 自动获得真正的复合体对象树或自动识别出细胞膜/蛋白质/箭头的所有语义。

## 可复现命令

```powershell
# 彩色科研图：颜色优先；输出 PPTX、SVG 和候选结构图
py -3 app.py examples/demo_scientific.png -o result-color.pptx --svg-out result-color.svg --report result-color.html --scene-graph-out result-color.structure.json --backend precision --colors 32 --color-mode perceptual --luminance-weight 1.7 --structure-mode hybrid

# 亮度结构优先：在 SSIM 和颜色准确度之间提供另一种选择
py -3 app.py examples/demo_scientific.png -o result-structure.pptx --colors 16 --color-mode balanced --structure-mode hybrid

# 如需完全关闭渐变
py -3 app.py examples/demo_scientific.png -o flat.svg --color-mode balanced --no-background-gradient
```

启动 GUI：`py -3 app.py`。在「导出」步骤展开高级选项，修改配色策略、色数和结构模式。可视化编辑记录在配色策略改变后会拒绝错误复用。

## 实测及其解释

测试文件：`examples/demo_scientific.png`，1080×589。色差指标使用 CIE Lab 的 ΔE76（不是 ΔE2000）。

| 配置 | SVG SSIM | SVG MAE | SVG 平均 ΔE76 | SVG对象数 |
|---|---:|---:|---:|---:|
| v4.0 / 原始 16 色 | 0.9199 | 10.659 | 9.240 | 271 |
| v4.1 / 结构优先 16 色 | 0.9242 | 10.032 | 8.559 | 571 |
| v4.1 / 颜色优先 32 色 | 0.9047 | 8.845 | 4.759 | 735 |

**不能只按 SSIM 排名**。旧方案曾把绿色细菌涂成蓝灰色，SSIM 却更高。颜色优先方案用更准确的调色板换取局部纹理指标的退让。在实际 LibreOffice 导出的 PPTX 渲染中，颜色优先版本 SSIM 约 0.9336，平均 ΔE76 约 3.816；该结果与 SVG 不是相同渲染器和采样链路，不能直接比较数字。

此次 普通无显示模式：90 项通过、8 项跳过；虚拟图形环境开启 GUI 测试时：98 项全部通过；真实 Windows PowerPoint 颜色未在此执行环境验收。

## 仍未完成

- 从位图中准确恢复真正的生物学语义对象和设计图层。
- 原生 Illustrator `.ai` 工作流和 12 光标回放完整兼容。
- 非线性复杂滤镜、阴影、混合模式的矢量完全还原。
- 与原作者付费矢量识别服务在同图、同机器上的对照。

有关真实图像优先使用原始 SVG/AI/PPTX 文件；在仅有 PNG 时，不能保证无损逆向恢复。
