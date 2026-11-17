# v3.5 可视化编辑画布与 DeepSeek OCR 截断处理

> 本版在 **v3.4** 上直接开发。用户可用鼠标移动真实矢量路径和文本位置；**不是在 PNG 上画覆盖框**。

## 1. 入口与操作

1. `python app.py` → **① 导入图片**。
2. 如果需要文字识别，在 **② 校对文字** 调用 OCR / 导入已有文字 JSON。
3. 在 **② 校对文字** 点击 `画布拖动文字 / 图形`，或在 **③ 导出结果** 点击 `可视化编辑 / 对比`。
4. 画布左边显示原图，右边显示**由当前场景导出的真实 SVG 渲染**。在右边选中对象并拖动。
5. 点击 **保存画布编辑**，图形位移写入 `源图目录/源文件名_su7_work/visual-edits.json`；文字框直接写回当前校对清单。文字发生改变后必须重新确认审核。
6. 在原来的导出页点击 **开始生成 PPTX**。修改应用在矢量路径和可编辑文字的真实源数据上，不需要再次 OCR。

操作手势：

- **左键拖动右侧**：移动选中的可编辑图形或文字框；上方可过滤为“只选文字”“只选图形”。复杂图片仅按对象包围框近似命中，可能要靠缩放或过滤选择正确对象。
- **鼠标滚轮**：放大缩小；**适应**恢复合适的查看尺寸。
- **中键 / 右键 / Shift + 左键拖动**：平移两侧画面；方向键亦可平移。
- **Ctrl+Z / Ctrl+Y**：撤销、重做本次画布修改。
- **叠加对比**：让右侧原图与重建结果按透明度混合，便于检查结构失真、对齐与漏字。
- **退出**：不保存此次操作；**保存画布编辑**：记录位移，下一步导出时生效。

![真实图形界面（使用模拟文字清单，不是真实 DeepSeek 请求）](../previews/v3.5-visual-editor.png)

**图形位置的正确性依赖重建质量。** 此界面解决人工编辑操作问题，**没有自动修复** 复杂炎症小体图中的蛋白质纹理、图层或图形轮廓失真。右侧难看的区域是真实重建效果，不是界面绘制问题。

## 2. DeepSeek OCR `finish_reason=length` 不再直接丢弃整页

官方文档明确表示该页达到输出长度上限，输出**不完整**，并非所有文字框都无效。新版本只提取 `<｜ref｜>...<｜/ref｜><｜box｜>...<｜/box｜>` 配对完整、坐标合法的条目，忽略被截断的不完整尾部，保留 `partial=true` 和 `partial_tiles` 诊断信息。

- **绝不自动重试或自动付费重扫**。
- OCR 界面会保留已识别的内容，显著提示“**OCR 输出不完整**”，要求人工核对。
- 如果关键区域没识别到，用户可以主动选择 `2x2` 模式，看到费用与 4 次请求确认后再提交。单图模式默认只调用一次。
- 如新图或 API 返回完全没有有效坐标框，不制造文字框；可手动补充文字。
- OCR 诊断文件中保留 `partial`、`partial_tiles`、请求次数与警告，不包含 API Key。

文档：https://api-docs.deepseek.com/zh-cn/guides/ocr/

## 3. 命令行复用已经保存的矢量编辑

```powershell
python app.py source.svg -o output.pptx --visual-edits path\to\visual-edits.json
```

如果图片文字也经过移动，附上最新的审核文字 JSON：

```powershell
python app.py input.png -o corrected.pptx --manifest reviewed.json --visual-edits visual-edits.json --svg-out corrected.svg
```

CLI 和 GUI 都会验证**原图的 SHA-256、矢量化参数、对象数、原始路径包围框**。如果输入被替换、后端/颜色数变化或者局部去字使路径重算，旧的图形编号可能对应另一个对象。此时导出会明确阻止应用旧位移，要求重新开画布编辑，而非无声移动错误对象。

**保存内容：** `visual-edits.json` 只记录原图哈希、模型参数与图形偏移，不记录密钥，不包含原始图像字节。文字改动保存在 OCR 审核 JSON 文件，必须人工确认后才用于正式导出。

## 4. 依赖与已知限制

- GUI 使用 Tkinter；真实 SVG 预览需要 `CairoSVG`（`pip install -r requirements-optional.txt`）。没有该可选依赖，基础 CLI PPTX 转换仍能运行。
- 图形选择基于路径包围框，不是 Adobe Illustrator 那种精确逐像素拾取。大量重叠图形选择时建议先放大或切换只选图形。
- 初次打开复杂位图需本地矢量化，可能需要较多时间；不产生任何 API 费用。
- 仅支持**移动**图形和文字。尚不支持曲线锚点级编辑、对象组合/解除组合、图层重排、AI 原生文件导出。
- 未使用真实 DeepSeek API Key 对 `length` 行为和识别精度进行端到端云端验证；截断测试采用遵循官方响应格式的模拟数据。

## 5. 开发回归命令

```bash
python -m pytest -q
CELL_SU7_GUI_TESTS=1 xvfb-run -a python -m pytest -q tests/test_visual_canvas_gui.py tests/test_studio_gui.py tests/test_scrollable_ui.py
```
