# 架构与数据契约

## 核心原则

```text
source Markdown ── parse/hash ── chunk ── provider ── append-only log
       │                                        │             │
       │                                        └─ {id, zh} ──┘
       └──────── immutable English ── assemble ── verify ── publish
```

英文原文只从本地源文件进入最终成品。模型不能返回或修改英文侧，因此一次生成故障不会静默污染原文。

## 模块

| 模块 | 责任 |
|---|---|
| `document.py` | 解析 Markdown，识别块类型、章节与稳定内容 ID |
| `chunker.py` | 按语义边界和输出上限分块，提供只读上下文 |
| `glossary.py` | 合并三层义项，冻结不可变快照 |
| `client.py` | 提供商协议、鉴权、重试与严格 JSON 回包解析 |
| `state.py` | 构建身份、单实例锁、原子状态与追加式译文日志 |
| `workflow.py` | 并发执行、恢复、校验和装配编排 |
| `verify.py` | 段落级数字、标记、术语、回抄与结构检查 |
| `assemble.py` | 从不可变原文和译文映射生成双语 Markdown |
| `artifact_verify.py` | 对最终 Markdown 再检查顺序、配对、标题与字符卫生 |
| `visuals.py` | 合并视觉 sidecar，在原图后追加可搜索旁注 |
| `review.py` | 把可追踪审校补丁追加到构建日志 |
| `publish.py` | 在所有门禁通过后进行拒绝覆盖的原子发布 |

## 构建身份

状态文件绑定以下字段：

- 原始 Markdown SHA-256；
- 冻结术语快照版本；
- 文风卡版本；
- 提供商协议与模型 ID；
- chunk 计划；
- 翻译提示词版本。

任一字段改变都必须使用新的 build root 或显式迁移，不能自动混用旧译文。

## 追加式日志

`translations.jsonl` 保存 API 结果和审校修订。最新记录覆盖读取视图，但历史记录保留，支持追踪 reviewer、来源、usage 和 attempts。不要原地编辑或截断该文件。

## 视觉旁注

图片路径是 sidecar 的稳定键。原图 Markdown 和二进制资产保持不变；旁注只追加在匹配原图之后，并明确标识“人工视觉转写”、置信度与可确认标签。无法可靠辨认时应跳过并记录原因，不猜补。

## 故障模型

- 鉴权、模型名与请求格式错误：永久错误，不重试。
- 限流、超时、5xx、空响应或不完整 JSON：瞬态错误，指数退避。
- 返回 ID 缺失或多余：chunk 不完整，不得标记 done。
- 本地中断：追加日志与原子状态保证可恢复。
- 校验失败：保留构建证据，但不发布。
