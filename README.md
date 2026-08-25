# Bilingua Workflow

[![CI](https://github.com/lingjiee/bilingua-workflow/actions/workflows/ci.yml/badge.svg)](https://github.com/lingjiee/bilingua-workflow/actions/workflows/ci.yml)

把合法取得的英文 Markdown 构建为段落级中英双语对照稿。Bilingua 把一次性的“让模型翻一本书”，封装为可复现、可中断恢复、可人工审校、通过质量门禁后才发布的工程工作流。

## 为什么使用它

- 原文不经过模型重写：模型只返回段落 ID 与中文译文。
- 内容 hash 作为段落身份：源文局部变化只使相关缓存失效。
- 术语先审核、后冻结：一次构建期间不会发生术语漂移。
- 每个 chunk 追加落盘：中断后只重跑未完成部分。
- 审校补丁可追踪：人工、Codex 或其他审校者的修改不会覆盖历史记录。
- 最终 Markdown 复检：数字、链接、脚注、段落配对、标题和异常字符均有门禁。
- 图片保持原样；可用独立 YAML sidecar 添加带置信度的双语视觉旁注。
- 发布默认拒绝覆盖已有目录，避免损坏 Obsidian 笔记。

## 安全与版权边界

仓库只包含通用代码、测试和合成示例。以下内容默认被 Git 忽略：API 密钥、原书、图片、模型译文、构建缓存、审校补丁、术语证据和书籍专项脚本。请只处理你有权使用的材料。

## 快速开始

需要 Python 3.11+；推荐安装 [uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/lingjiee/bilingua-workflow.git
cd bilingua-workflow
uv sync --locked --extra dev
uv run pytest
```

初始化一个与代码仓库分离的本地翻译项目：

```bash
uv run bilingua init ../my-bilingual-project
cd ../my-bilingual-project
cp .env.example .env
```

编辑 `.env`，再执行零网络诊断：

```bash
uv run --project ../bilingua-workflow bilingua doctor --project-root .
```

`doctor` 只检查本地文件和配置，不访问网络，也不会产生 API 费用。

## 标准工作流

### 1. 预扫描，不调用 API

```bash
uv run --project ../bilingua-workflow bilingua inspect \
  source/book.md \
  --book-slug sample
```

检查章节、段落、chunk 大小和标题层级；结构异常时可重复传入 `--chapter-level 1 --chapter-level 2`。

### 2. 审核并冻结术语

编辑 `glossary/global.yaml`、`glossary/domains/<domain>.yaml` 与 `glossary/books/<book>.yaml`。只有 `status: approved` 的义项进入快照：

```bash
uv run --project ../bilingua-workflow bilingua freeze-glossary \
  --root glossary \
  --domain general \
  --book sample \
  --output glossary/snapshots/sample-v1.lock
```

### 3. 先跑代表章节验收

```bash
uv run --project ../bilingua-workflow bilingua build source/book.md \
  --book-slug sample \
  --snapshot glossary/snapshots/sample-v1.lock \
  --style-card style-card.md \
  --chapter chapter-1 \
  --build-root build-acceptance \
  --concurrency 1
```

人工核对术语、数字、脚注、作者立场与文风。验收不通过时先修规则或术语，不要启动全书。

### 4. 可恢复的全量构建

```bash
uv run --project ../bilingua-workflow bilingua build source/book.md \
  --book-slug sample \
  --snapshot glossary/snapshots/sample-v1.lock \
  --style-card style-card.md \
  --build-root build-main \
  --concurrency 2
```

中断后原命令重跑即可。源文件、模型、术语快照、文风卡或分块计划变化时，构建身份检查会阻止新旧缓存混用。

### 5. 导入审校补丁并离线重建

```bash
uv run --project ../bilingua-workflow bilingua apply-review \
  --build-dir build-main/sample \
  --input reviews/review.json
```

再次运行相同 `build` 命令，只会重新校验和装配已经完成的 chunk，不会再次调用 API。

### 6. 校验通过后发布

```bash
uv run --project ../bilingua-workflow bilingua build source/book.md \
  --book-slug sample \
  --snapshot glossary/snapshots/sample-v1.lock \
  --style-card style-card.md \
  --build-root build-main \
  --publish-to /path/to/Obsidian/双语精读 \
  --folder-name sample-双语对照 \
  --book-title "Sample Book"
```

## 支持的提供商形状

调用层通过环境变量配置，不绑定特定中转站：

```env
TRANSLATE_BASE_URL=https://api.example.com
TRANSLATE_API_KEY=secret
TRANSLATE_MODEL=model-id
TRANSLATE_PROTOCOL=anthropic  # anthropic | openai
TRANSLATE_AUTH=bearer         # bearer | x-api-key
TRANSLATE_PATH_PREFIX=/v1
```

不同网关的真实模型身份、价格、上下文长度和稳定性必须自行验证；不要只根据模型名作结论。

## 文档

- [完整工作流](docs/WORKFLOW.zh-CN.md)
- [架构与数据契约](docs/ARCHITECTURE.zh-CN.md)
- [仓库边界与隐私](docs/REPOSITORY_BOUNDARIES.zh-CN.md)
- [贡献与迭代规范](CONTRIBUTING.md)
- [安全政策](SECURITY.md)
- [版本记录](CHANGELOG.md)

## 当前成熟度

`0.2.0` 为首个 Git 仓库化版本。核心流水线已经在多本长篇图书上完成实际验证，但仍属于 alpha：建议所有新书先跑代表章节验收，并保留独立人工抽查。
