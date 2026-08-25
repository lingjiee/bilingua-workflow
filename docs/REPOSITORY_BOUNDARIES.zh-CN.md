# 仓库边界、隐私与版权

## 可以提交

- `pipeline/` 通用代码；
- `tests/` 全离线测试；
- `docs/`、`README.md`、贡献与安全规范；
- `examples/` 中完全合成的小样本；
- `.env.example` 中的占位配置；
- `uv.lock` 与 CI 配置。

## 默认不得提交

| 路径/内容 | 原因 |
|---|---|
| `.env*`（除 `.env.example`） | API 密钥和私有端点 |
| `source/`、`*-images/` | 原书与图片版权 |
| `build*/` | 模型译文、费用和构建状态 |
| `reviews/`、`reports/` | 大段派生译文、审校证据与个人记录 |
| `glossary/books/`、`domains/`、`snapshots/` | 书籍专属定义、引文证据和项目知识 |
| `scripts/` | 当前项目中包含本机路径和书籍专项发布逻辑 |
| Obsidian Vault | 私人笔记与双链关系 |

`.gitignore` 是第一道保护，不是唯一保护。首次提交和每次发布前都必须检查：

```bash
git status --short
git diff --cached --stat
git diff --cached --name-only
```

并搜索常见密钥形状、真实端点与本机绝对路径。若密钥曾进入 Git 历史，仅删除文件不够，必须立刻撤销并轮换密钥，再清理历史。

## GitHub 可见性

首次同步默认使用 private 仓库。改为 public 前应完成单独的版权审计、许可证选择和历史扫描；私有并不等于可以上传未经授权的原书。
