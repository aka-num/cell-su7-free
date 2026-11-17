# DeepSeek 2026-11 API 专项工作流（v3.2）

来源：

- https://api-docs.deepseek.com/zh-cn/guides/ocr/
- https://api-docs.deepseek.com/zh-cn/api/ocr/
- https://api-docs.deepseek.com/zh-cn/guides/vision/
- https://api-docs.deepseek.com/zh-cn/guides/json_mode/
- https://api-docs.deepseek.com/zh-cn/quick_start/pricing/

此实现是对原版 cell_su7 的第三方独立重制，不是 DeepSeek 官方软件。

## 能力边界

| 官方能力 | API 路径/模型 | v3.2 的具体用途 |
| --- | --- | --- |
| OCR | `POST https://api.deepseek.com/v1/tools/ocr` | 小字识别、`<｜ref｜>`/`<｜box｜>` 解码、原图坐标清单 |
| 图像理解 | `deepseek-flash`, `POST /chat/completions` | 对识别出的标签提出文本修正建议，不修改几何框 |
| JSON Output | `response_format:{"type":"json_object"}` | 检验 Flash 校核响应结构，空白或截断立即失败 |
| OpenAI 风格的 `images/edits` | *官方文档未确认此接口* | **不调用**，改用本地 OpenCV 去字 |

DeepSeek OCR **不提供完整的 SVG/PowerPoint 可编辑几何图层**；识别文字也不能自动解决复杂生物图的路径遮挡和高保真问题。Flash 视觉只能提出建议，不能作为绝对可靠的科研事实来源。

## 重要的计费与调用安全

- 官方 OCR 支持 `multipart/form-data` 的 **两个字段**：`file`、`purpose`（`text_recognition` 或 `rich_layout_recognition`），不能额外传入 `model`、`stream` 等字段。
- 官方文档目前列出的价格为 **每 1000 页 1.5 元人民币**，按成功页数计费，OCR 失败/超时不计费；本地 1 张图片一次识别通常按 1 页估算。服务商可调整价格。
- `single`：**1 次** OCR 请求，参考费用约 ¥0.0015；`2x2`：**4 次** OCR 请求，参考费用约 ¥0.006（均仅参考）。`--flash-review` 另有 Flash token 费用。
- OCR 有最多 15 个并发任务、单文件 100 MiB、上传不超过 120 秒、总时长 180 秒的限制。客户端断开**不会取消后台识别或已经发生的计费**；本软件对 DeepSeek OCR **不会自动重试**。
- 无密钥时无法执行真实云端 OCR；密钥只通过环境变量或隐藏输入接收，不写入示例、审核 JSON、日志或测试夹。
- 请将过去粘贴到聊天记录里的密钥**撤销并重新生成**；无需把新密钥发送给 ChatGPT。

## 推荐：识别与重建解耦

安装依赖：

```powershell
python -m pip install -r requirements.txt
python -m pip install -r requirements-ai.txt
```

第一步：**只做 OCR，产生人工审核图，不会擦除原始图片**。

```powershell
python tools/deepseek_workbench.py scan --image examples/demo_scientific.png --out-dir runs/pathogen-deepseek --tiling single
```

会出现 YES 费用确认和隐藏密钥输入。实际处理完成后，生成：

- `ocr-review.png`：文字框编号覆盖图，帮助发现“把蛋白质图形识别成文字”等误报；
- `ocr-review.html`：查看每个文字及坐标框；
- `ocr-manifest.json`：像素级文字清单，**人工修改 `contents`、`bbox`**；
- `ocr-diagnostics.json`：调用数、跳过片段、页面/截断状态、参考费用，不含 API Key。

请修正识别错误、错位框、多行合并以及不应擦除的标签。DeepSeek OCR 返回的坐标是 **0–1000 归一化值**，重制版会转换成图像像素。**不伪造模型置信度**，也不会为不带 `<｜box｜>` 的纯文字猜坐标。

第二步：如 1 次识别漏掉大量小字，可以**自主决定**用四个重叠高清区域再次识别。这不是自动重试，它会额外产生 4 次请求：

```powershell
python tools/deepseek_workbench.py scan --image examples/demo_scientific.png --out-dir runs/pathogen-tiles --tiling 2x2
```

注意：四个 tile 各自归一化，必须映射到原图后去重；贴边标签可能被切断，**并不保证 2x2 一定优于 single**。不建议在同一个任务里默认同时发 single 和 2x2。

可选：追加一次 DeepSeek Flash 校核，输出提议但**不自动覆盖原识别结果**：

```powershell
python tools/deepseek_workbench.py scan --image examples/demo_scientific.png --out-dir runs/pathogen-verified --flash-review
```

生成 `flash-suggestions.json`，包含已存在标签 ID 的文本修正建议；不会创建假想的新坐标、形状或任意学术结论。

第三步：**完全离线**地用已经核对的 `ocr-manifest.json` 重建 PPTX + SVG：

```powershell
python tools/deepseek_workbench.py rebuild --image examples/demo_scientific.png --manifest runs/pathogen-deepseek/ocr-manifest.json --output runs/pathogen.pptx
```

如果已人工核验 `bbox`，可以追加 `--erase-text --approve-review`，对那些框局部做 OpenCV 去字。如果标签覆盖箭头、网格或蛋白质边界，擦除也可能损坏科学图形，建议先做不擦除的预览，确认后再启用。`--erase-text` 不是云端模型去字。超过画布 9% 的异常巨框会被拒绝擦除。

同样可以用原 GUI：**DeepSeek OCR 审核**选项卡先扫图，再在转换工作台选修正后的 JSON 并选择 `off` 离线模式；明确勾选基于清单的去字方框后才会擦除。

## 开发者 API

- `ai_bridge.deepseek_ocr_image(img, config, purpose=...)`：官方单请求/响应契约；发现 HTTP 200 中的 `error`、`finish_reason=length` 时立即报错，不使用部分结果；
- `ai_bridge.parse_deepseek_ocr(data)`：文本-框提取；未带框的文字只计入审核警告；
- `deepseek_workflow.recognize(..., tiling='single'|'2x2')`：区域映射、去重、请求计数、参考成本；
- `deepseek_workflow.write_review_bundle(...)`：不调用网络，只生成审核清单和图形；
- `deepseek_workflow.flash_review(...)`：额外多模态 JSON Output 请求，仅供参考；
- `engine.Settings(manifest_clean=True)`：仅在明确离线重建时使用核验后的框遮罩。

## 验证范围

`python -m pytest -q`：使用本地模拟请求或完全离线测试验证接口格式和文本坐标拼接，不会向 DeepSeek 服务器发出请求。示例审核结果**明确标记为模拟数据**，不代表真实 OCR 的识别质量。未经实际购买/调用，不声明“与原项目效果相同或超过原项目”。
