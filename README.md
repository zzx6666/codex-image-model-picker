# Codex Image Model Picker

一个面向 Codex 的图片生成 Skill：读取当前 API Key 可访问的模型，由你选择生图模型，再通过 OpenAI 兼容的图片接口生成或编辑图片。

Skill 名称保持为 `codex-image2`，以便与已有配置和调用方式兼容。

## 主要能力

- 生图前读取 `/v1/models`，展示当前 Key 可用的图片模型。
- 支持任意完整模型 ID，例如 `gpt-image-2.5-sunburst`，不会写死型号。
- 支持文生图、单图/多图编辑、PNG 蒙版和 JSONL 批量任务。
- 默认直接保存到当前工作目录，并按 `1. 画面重点.png`、`2. 画面重点.png` 递增命名。
- 支持 Base64 和 URL 图片响应，并跟随图片 CDN 跳转。
- 自动识别 PNG、JPEG 和 WebP，拒绝伪装成图片的无效响应。
- 支持网络超时、HTTP 429 和常见 5xx 的有界重试。
- 默认保护已有文件，只有显式传入 `--force` 才会覆盖。
- Windows 使用当前用户 DPAPI 加密 Key；macOS 使用登录钥匙串。
- 仅使用 Python 标准库，无需安装第三方包。

## 环境要求

- Python 3.9 或更高版本。
- Windows 或 macOS。
- 图片服务需兼容以下接口：

```text
GET  /v1/models
POST /v1/images/generations
POST /v1/images/edits
```

模型列表接口可以隐藏或不可用，但正式生图仍要求服务商支持对应的图片接口和模型。

## 安装

将仓库直接克隆到 Codex 的 Skill 目录：

```powershell
git clone https://github.com/zzx6666/codex-image-model-picker.git `
  "$HOME\.codex\skills\codex-image2"
```

macOS：

```bash
git clone https://github.com/zzx6666/codex-image-model-picker.git \
  "$HOME/.codex/skills/codex-image2"
```

也可以在 Codex 中直接说：

```text
请帮我安装这个 Skill：
https://github.com/zzx6666/codex-image-model-picker
```

## 配置

在 Codex 中调用：

```text
使用 $codex-image2 帮我配置图片服务。
```

也可以直接运行：

```powershell
python "$HOME\.codex\skills\codex-image2\codex-image2.py" configure
```

配置页只监听 `127.0.0.1` 的随机端口，使用一次性访问凭据和来源校验。Key 只通过本地页面输入，不进入 Codex 聊天、命令参数或 URL。

Windows 用户也可以双击 `配置图片服务.cmd`，macOS 用户可双击 `配置图片服务.command`。

配置保存位置：

| 系统 | 配置文件 | Key 存储 |
| --- | --- | --- |
| Windows | `%APPDATA%\codex-image2\config.json` | 当前用户 DPAPI |
| macOS | `~/Library/Application Support/codex-image2/config.json` | 登录钥匙串 |

旧环境变量 `CODEX_API_URL` 和 `CODEX_API_KEY` 仍可使用，但必须同时设置。只设置 Key 时程序会拒绝发送，避免把凭据误发到未经指定的服务。

## 使用

先查看当前 Key 的模型：

```powershell
python "<skill-dir>\codex-image2.py" models
```

输出示例：

```json
{
  "image_models": ["gpt-image-2.5-sunburst"],
  "selection_required": true
}
```

生成图片：

```powershell
python "<skill-dir>\codex-image2.py" generate `
  --model "gpt-image-2.5-sunburst" `
  --name "花村樱花鸟居与日式城堡" `
  --prompt "传统日式山城村落，樱花、红色鸟居、石桥与城堡" `
  --size 1536x1024 `
  --quality high
```

默认在当前目录生成 `1. 花村樱花鸟居与日式城堡.png`。目录中已有编号图片时，会从最大编号继续递增。

编辑图片：

```powershell
python "<skill-dir>\codex-image2.py" edit `
  --model "gpt-image-2.5-sunburst" `
  --name "雪山背景人物图" `
  --image "input.png" `
  --prompt "只把背景替换为雪山，人物、服装和姿势保持不变" `
  --quality high
```

只校验请求、不联网也不扣费：

```powershell
python "<skill-dir>\codex-image2.py" generate `
  --model "gpt-image-2.5-sunburst" `
  --name "请求校验" `
  --prompt "测试提示词" `
  --dry-run
```

### 模型选择

显式传入 `--model` 时，程序会原样使用该模型 ID。

只有用户明确要求自动选择时才建议使用：

```powershell
--model auto
```

自动模式优先选择列表中的 `gpt-image-2.5`，然后选择其他 `gpt-image-*`，最后回退到 `gpt-image-2`。

### 批量任务

批量任务使用 JSONL，每行一个对象：

```jsonl
{"prompt":"樱花鸟居","name":"花村樱花鸟居","model":"gpt-image-2.5-sunburst"}
{"prompt":"日式城堡","name":"花村日式城堡","model":"gpt-image-2.5-sunburst","n":2}
```

```powershell
python "<skill-dir>\codex-image2.py" generate-batch `
  --input "jobs.jsonl" `
  --concurrency 2
```

批量任务会在首次请求前校验所有参数、重复输出路径和已有文件，避免因为后面的非法任务产生部分计费。`--fail-fast` 会停止启动后续任务，并在汇总中分别报告 `succeeded`、`failed` 和 `skipped`。

完整格式见 [`references/batch-format.md`](references/batch-format.md)。

## 安全设计

- 不提供默认第三方 API 地址；未配置时不会发送 Key。
- 模型列表输出会过滤控制字符并隐藏疑似回显的 Key。
- 服务商错误信息会截断并脱敏，不输出 Authorization 请求头。
- 图片下载跨域跳转时会移除 Authorization，防止 Key 泄露给 CDN。
- 配置页使用短期随机凭据、Host/Origin 校验、CSP 和请求大小限制。
- 配置保存采用临时文件加原子替换；损坏的本地配置不会静默切换到环境变量。

## 测试

```powershell
python -m unittest discover -s tests -v
python C:\path\to\skill-creator\scripts\quick_validate.py .
```

测试使用本地假服务，不会调用真实计费接口。

## 来源与许可

项目基于 [fengfengzhidao/codex-image2-skill](https://github.com/fengfengzhidao/codex-image2-skill) 的工作继续改造，主要变化包括动态模型选择、Python 标准库实现、安全下载、批量校验和新的文件命名规则。

本项目使用 [MIT License](LICENSE)。
