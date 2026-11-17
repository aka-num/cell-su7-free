# DeepSeek OCR 网络故障排查（v3.2.1）

针对 GUI 提示 `DeepSeek OCR connection failed or timed out; do NOT blindly retry, the OCR may have been billed` 的修复。

## 1. 错误的含义

v3.2 这条错误来自通用异常捕获，不代表密钥失效或 DeepSeek 官方 OCR 不支持图片。请求地址按官方文档使用 `https://api.deepseek.com/v1/tools/ocr`（multipart/form-data 中的 `file` 与 `purpose`）。官方文档说明：总时长上限 180 秒；识别期间会发送换行符保持连接；客户端断开后已接收的文件仍可能成功完成并收费。**程序不会自动重试 POST。**

## 2. 不上传图片的免费诊断

解压新版，在项目目录运行：

```powershell
python -m pip install -r requirements-ai.txt
python tools/diagnose_deepseek.py
```

该命令只检查 DNS 并调用 `GET https://api.deepseek.com/models`，没有上传文件、没有调用 OCR。**没有 Key 时 HTTP 401 是可以接受的网络连通性证据**，但不是认证成功。

如需同时检查认证，输入密钥的安全方式（不要把密钥直接粘到命令行参数中）：

```powershell
python tools/diagnose_deepseek.py --check-key
```

命令会隐藏输入；也可从环境变量 `DEEPSEEK_API_KEY` 读取。不会把 Key 写入日志和工程文件。

在 GUI 的「AI 接口配置」页也可以点 **「诊断 DeepSeek 连接（免费，无图片上传）」**。这里会检查当前 GUI 配置中的 Key。

## 3. 常见结果

| 现象 | 判断 / 建议 |
|---|---|
| DNS 解析失败 | 检查 `api.deepseek.com` 的 DNS，VPN、校园网或代理设置；如果经代理可连，单独 DNS 失败不一定是问题 |
| 代理连接失败 | 检查系统代理以及 `HTTPS_PROXY`、`HTTP_PROXY`、`NO_PROXY` 环境变量；确认代理程序确实运行 |
| HTTPS/TLS 证书错误 | 检查系统时间、可信证书及拦截 HTTPS 的软件，**不要禁用证书验证** |
| 连接超时 | 检查外网、443 端口、防火墙或代理是否能连接 DeepSeek |
| 读取超时 | 请求可能已上传，查询余额/账单后再决定是否单独授权重试 |
| HTTP 401（无密钥诊断） | 网络可达，但尚未验证认证 |
| HTTP 401（带密钥诊断） | 当前密钥可能无效、已撤销或填错 |
| HTTP 402 / 403 | 检查余额或 API 权限 |
| HTTP 200 | 模型列表端点可用，**仍需单独测试 OCR 上传** |

在 Windows PowerShell 还可独立检查端口（不需要 Key）：

```powershell
Resolve-DnsName api.deepseek.com
Test-NetConnection api.deepseek.com -Port 443
```

这些命令不调用任何收费服务；网络路由或本地代理可能使其结果与程序不完全一致。

## 4. 诊断成功后再使用 OCR

1. 确认 AI Base URL 为 `https://api.deepseek.com`（不要粘贴完整 `/v1/tools/ocr` 地址到 Base URL）。
2. 首次验证选 `single`，只请求一张图片。不要先开启 `2x2`，它会发送四个单独的 OCR 请求。
3. 出现 `ReadTimeout`/连接断开时，不要自动重试上传。官方说明上传后断开并不会取消服务端识别。
4. 先核对账号的调用记录或费用，再考虑是否需要重新提交。
5. OCR 识别成功后使用离线审核/重建，不必重复上传同一张图。

官方参考：[OCR 指南](https://api-docs.deepseek.com/zh-cn/guides/ocr/)；[模型列表](https://api-docs.deepseek.com/zh-cn/api/list-models/)。

**安全提醒：** 如果 API Key 曾被发送在聊天或公开网页中，请在 DeepSeek 控制台撤销，并生成新 Key。
