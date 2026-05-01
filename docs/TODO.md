# TODO

---

## 改善アイデア

### 1. HAProxy 配信統計の収集

現状の HAProxy DaemonSet は単純なプロキシとして動作しており、配信数・エラー率などの統計情報がない。

- HAProxy の stats ソケットまたは Prometheus エクスポーターを有効化
- admin-server.py のダッシュボードに統計を統合

### 2. HCP Terraform 移行

現状は tfstate をローカルファイルで管理しており、複数人での運用・ロック管理ができていない。

```hcl
# providers.tf に追加するイメージ
terraform {
  cloud {
    organization = "<org-name>"
    workspaces {
      name = "mail-platform-prod"
    }
  }
}
```

### 3. 送信元 IP 別配信数の可視化

各 Postfix Pod は異なる Spot ノード（異なるパブリック IP）から配信しているが、IP 別の配信数を把握する手段がない。

- Fluent Bit で各 Pod の maillog を CloudWatch Logs / OpenSearch に集約
- 送信元ノード IP ごとの配信数を集計・グラフ化
