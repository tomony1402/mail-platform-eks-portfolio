# mail-platform-eks

[![CI](https://github.com/tomony1402/mail-platform-eks/actions/workflows/ci.yml/badge.svg)](https://github.com/tomony1402/mail-platform-eks/actions/workflows/ci.yml)

オンプレミス（AWS EC2）138台のメール配信基盤を EKS + Karpenter に移行したプロジェクト。

## 概要

| 項目 | 内容 |
|------|------|
| 配信量 | 1時間最大60万件 |
| 旧構成 | オンプレ EC2 138台 |
| 新構成 | EKS + Karpenter（Spot中心）|
| Spot率 | ほぼ100% |
| コスト削減 | $1,646 → $922（**44%削減 / 年間約¥130万削減**）|

## アーキテクチャ

```
オンプレミス (x.x.x.x/24)
       │ SMTP :25
       ▼
Gateway Node Group (t3.medium × 2, On-Demand)
  └─ HAProxy DaemonSet (hostNetwork: true)
       │ ClusterIP x.x.x.x:25
       ▼
  Postfix Service
  ├─ deployment-a (35 pods, expire 40m, small/med Spot)
  └─ deployment-b (35 pods, expire 50m, small/med Spot)
```

## 主な技術

- **EKS 1.33** / **Karpenter 1.10.0**
- Spot 100% + 3段階 NodePool フォールバック（small-spot → medium-spot → On-Demand）
- Postfix tmpfs化による I/O 高速化
- 夜間スケールダウン（70台 → 16台 or 8台）
- S3 キュー救済システム（キュー1000件超でS3退避 → 2時間おきに再送）
- preStop フックによる Pod 終了時キュー退避
- **GitHub Actions CI**（ruff / terraform-lint / yamllint / tflint / tfsec / kubeconform）

## ドキュメント

- [詳細 README](docs/README.md)
- [セットアップ手順](docs/SETUP.md)
- [ADR（アーキテクチャ決定記録）](docs/adr/)
- [TODO](docs/TODO.md)
- [トラブルシューティング](docs/TROUBLESHOOTING.md)
