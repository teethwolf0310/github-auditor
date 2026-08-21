# github-auditor

判断一个 GitHub 账号**是不是真人** + **活跃度** + **工程能力**。每个维度都是百分制，评分规则全部可读、禁止黑盒。

## 一句话使用

```bash
cd github-auditor
cp config.example.yaml config.local.yaml
# 编辑 config.local.yaml 填入 github_token / proxy

python -m app.main eval torvalds
python -m app.main eval torvalds --report     # 输出 markdown 详细报告
python -m app.main batch ./handles.txt        # 一行一个 handle 批量评分
python -m app.main list                       # 列出本地已评估
```

## 评分维度和权重

| 维度 | 权重 | 主要信号 |
|---|---|---|
| **真实性 authenticity** | 35 | 头像 / bio / 提交时间健康 / 跨 owner 被互动 / 反 burst |
| **活跃度 activity** | 20 | 近 6-12 月事件数、类型多样性 |
| **代码能力 capability** | 20 | 原创 repo 数、星、语言、协作规模 |
| **广度 breadth** | 10 | 涉及到的 owner / 组织 / 语言数 |
| **历史 longevity** | 8 | 账号年龄对数曲线 |
| **身份锚定 identity** | 7 | 主页可解析 / GPG / 公开 email / 组织成员 |

总分 = Σ dim_score × weight。

## 数据 / 隐私

- PAT 默认从 `config.local.yaml` 读，**不会被提交**（`.gitignore`）
- SQLite 文件放在 `data_dir`（指到 `/home/zhoupeng/dev/...`，绕开 CIFS 锁问题）
- 也只缓存必要字段，不存私有 / 邮箱明文

## 文件结构

```
github-auditor/
  README.md
  requirements.txt
  config.example.yaml         # ← 复制为 config.local.yaml
  app/
    config.py                 # 配置加载
    models.py                 # SQLAlchemy 表定义
    db.py                     # SessionLocal
    github.py                 # GitHub API 客户端（带限流退避）
    collector.py              # 一个用户的完整评估流水线
    scorer.py                 # 六维打分规则
    signals.py                # 从 raw JSON 抽取信号
    report.py                 # markdown 报告
    main.py                   # CLI + --serve
  scripts/
    smoke_eval.sh
  tests/
    fixtures/*.json
    test_*.py
```

## 服务模式（可选）

```bash
python -m app.main --serve
# 默认 127.0.0.1:8010
curl localhost:8010/health
curl localhost:8010/users/torvalds
curl localhost:8010/users/torvalds/report.md
```
