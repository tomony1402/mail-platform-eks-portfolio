# mail-platform-eks

[![CI](https://github.com/tomony1402/mail-platform-eks/actions/workflows/ci.yml/badge.svg)](https://github.com/tomony1402/mail-platform-eks/actions/workflows/ci.yml)

オンプレミス（AWS EC2）138台のメール配信基盤を EKS + Karpenter に移行したプロジェクト。

## 目次

1. [プロジェクト概要](#プロジェクト概要)
2. [インフラ構成](#インフラ構成)
3. [アーキテクチャ図](#アーキテクチャ図)
4. [各ファイルの役割](#各ファイルの役割)
5. [コスト効果](#コスト効果)

---

## プロジェクト概要

| 項目 | 内容 |
|------|------|
| 配信量 | 1時間最大60万件 |
| 宛先 | docomo |
| 移行状況 | 28/28クライアント移行完了 |
| 旧構成 | オンプレ EC2 138台 |
| 新構成 | EKS + Karpenter（Spot中心）|
| 月額コスト削減 | $1,646 → $922（**44%削減 / 年間約¥130万削減**）|

---

## インフラ構成

| コンポーネント | 内容 |
|---------------|------|
| AWS リージョン | us-east-1 / 4 AZ |
| EKS バージョン | 1.33 |
| Karpenter バージョン | 1.10.0 |
| Gateway | HAProxy DaemonSet × 2台（t3.medium On-Demand）|
| 配信 Pod | postfix-deployment-a / b 各35台（昼間合計70台）|
| NodePool 優先度 | small-spot → medium-spot → od（3段階）|
| expireAfter | a=40分 / b=50分（一斉expire回避）|
| Spot率 | ほぼ100% |

### 夜間スケールスケジュール（JST）

| 時刻 | 台数（a+b 合計）|
|------|----------------|
| 08:00 | 70台（昼間フル稼働、各35台）|
| 22:00 | 16台（各8台）|
| 02:00 | 8台（各4台）|
| 06:00 | 16台（各8台）|

---

## アーキテクチャ図

```
┌──────────────────────────────┐
│  オンプレミス  x.x.x.x/24    │
└──────────────┬───────────────┘
               │ SMTP :25
               ▼
┌──────────────────────────────────┐
│  Gateway Node Group              │
│  t3.medium × 2  On-Demand        │
│  ┌────────────────────────────┐  │
│  │  HAProxy DaemonSet         │  │
│  │  hostNetwork: true  :25    │  │
│  └────────────────────────────┘  │
└──────────────┬───────────────────┘
               │ ClusterIP x.x.x.x:25
               ▼
      ┌─────────────────────┐
      │    Postfix Service  │
      │  x.x.x.x 固定 │
      └─────────┬───────────┘
           ┌────┴────┐
           ▼         ▼
  ┌─────────────┐ ┌─────────────┐
  │  deploy-a   │ │  deploy-b   │
  │  35 pods    │ │  35 pods    │
  │  expire 40m │ │  expire 50m │
  └─────────────┘ └─────────────┘
   small/med-spot   small/med-spot
   On-Demand        On-Demand
```

### Postfix 設定のポイント

- 中継専用モード（ローカル配信なし）
- tmpfs化: `/var/spool/postfix` をメモリ上に配置（I/O高速化）
- `recipient_canonical_maps`: deferredキューをほぼゼロに抑制
- HELOローテーション: 5分ごとにランダムローテーション
- `header_checks` で `Received:` ヘッダーを除去
- キュー保持時間: 最大6時間 / 接続タイムアウト: 2秒 / 最大同時接続: 50
- S3メール救済システム: キュー1000件超でS3退避 → ノード削除
- preStop フック: Pod終了時にキューをS3に退避

---

## 各ファイルの役割

### Terraform

```
envs/prod/
├── main.tf              # モジュール呼び出し（network / eks / ecr / vpc-endpoints / karpenter）
├── providers.tf         # AWS プロバイダー設定（リージョン: us-east-1）
├── variables.tf         # 入力変数の定義
├── terraform.tfvars     # 本番環境の変数値（.gitignore 対象）
├── terraform.tfstate    # 現在のインフラ状態（.gitignore 対象）
└── .terraform.lock.hcl  # プロバイダーバージョンのロック

modules/
├── network/             # VPC・サブネット作成（terraform-aws-modules/vpc 使用）
│   ├── main.tf          # VPC 本体、Karpenter 用ディスカバリータグ付与
│   ├── outputs.tf       # vpc_id / public_subnets / route_table_ids を出力
│   └── variables.tf
├── eks/                 # EKS クラスター・Gateway ノードグループ作成
│   ├── main.tf          # クラスター、Gateway 用 SG（25/tcp from x.x.x.x/24）
│   │                    # ノードグループ（t3.medium、Taint: role=gateway:NoSchedule）
│   ├── outputs.tf       # cluster_name / oidc_provider_arn / node_security_group_id など
│   └── variables.tf
├── ecr/                 # ECR リポジトリ（postfix イメージ）
│   ├── main.tf          # スキャン有効・Mutable タグ・force_delete
│   ├── outputs.tf       # repository_url を出力
│   └── variables.tf
├── iam/                 # IRSA 用 IAM ロール・ポリシー
│   ├── main.tf          # nightmode-controller / Postfix Pod 用ロール（S3アクセス）
│   ├── outputs.tf
│   └── variables.tf
├── karpenter/           # Karpenter インストール（IAM + Helm）
│   ├── main.tf          # コントローラー IAM ロール・ポリシー、Helm リリース
│   └── variables.tf
└── vpc-endpoints/       # S3 用 VPC エンドポイント（プライベート通信）
    ├── main.tf          # S3 (Gateway)
    └── variables.tf
```

### Kubernetes マニフェスト

```
manifests/
├── gateway/
│   ├── gateway.yaml                  # HAProxy DaemonSet（hostNetwork: true、port 25）
│   └── configmap.yaml                # HAProxy 設定（backend: x.x.x.x:25）
├── postfix/
│   ├── deployment-a.yaml             # Postfix Deployment A（35 replicas、workload: postfix-a）
│   ├── deployment-b.yaml             # Postfix Deployment B（35 replicas、workload: postfix-b）
│   ├── service.yaml                  # ClusterIP Service（固定 IP: x.x.x.x、port 25）
│   ├── configmap-helo.yaml                 # HELO ローテーションスクリプト（helo.sh）
│   ├── configmap-recipient-canonical.yaml  # recipient_canonical_maps 設定
│   └── configmap-sender-canonical.yaml     # sender_canonical_maps 設定
├── karpenter/
│   ├── ec2nodeclass.yaml             # EC2NodeClass（IAM ロール・AMI・サブネット・SG の自動検出）
│   ├── nodepool-a-small-spot.yaml    # NodePool for postfix-a（small Spot）
│   ├── nodepool-a-medium-spot.yaml   # NodePool for postfix-a（medium Spot）
│   ├── nodepool-a-od.yaml            # NodePool for postfix-a（On-Demand フォールバック）
│   ├── nodepool-b-small-spot.yaml    # NodePool for postfix-b（small Spot）
│   ├── nodepool-b-medium-spot.yaml   # NodePool for postfix-b（medium Spot）
│   └── nodepool-b-od.yaml            # NodePool for postfix-b（On-Demand フォールバック）
├── cronjob-nightmode.yaml            # 夜間スケール・キュー救済 CronJob
├── cronjob-karpenter-watch.yaml      # Karpenter 死活監視 CronJob（5分ごと、ChatWork通知）
└── cronjob-s3-recovery.yaml          # S3 退避メール再送 CronJob（JST 9〜19時・2時間おき）
```

### コンテナイメージ

```
services/postfix/
├── Dockerfile       # ベース: ubi9-minimal / postfix + cronie + expect インストール
├── entrypoint.sh    # 起動スクリプト（crond 開始 → postfix フォアグラウンド実行）
├── header_checks    # Received ヘッダーを除去するフィルタールール
└── postfix-cron     # 5 分ごとに helo.sh を実行するクーロン定義
```

### 管理ツール

```
tools/
├── admin-server.py       # Web 管理 UI（port 8080）
│                         # - コストダッシュボード（USD / JPY）
│                         # - HELO ConfigMap 編集・適用
│                         # - Sender-canonical ConfigMap 編集・適用
│                         # - キュースナップショット（0・3・6時 JST）+ S3 リカバリ統計
│                         # - ノードスナップショット（23・3・7時 JST）
│                         # - クラスターメトリクス（ノード/Pod 状態・CronJob 監視）
├── cost-viewer.py        # AWS Cost Explorer でコスト取得 → report.html 生成
├── report.html           # コストレポート（Chart.js グラフ付き、自動生成）
├── queue-monitor.html    # キュー監視ダッシュボード（自動生成）
├── node-monitor.html     # ノード監視ダッシュボード（自動生成）
├── queue-snapshots.json  # キュースナップショット永続化データ（自動生成）
├── node-snapshots.json   # ノードスナップショット永続化データ（自動生成）
└── recovery-stats.json   # S3 リカバリ統計データ（自動生成）
```

---

## セキュリティ設計（IAM / IRSA）

### NodeのIAMにS3ポリシーを付与しない理由

EKSのNodeはEC2メタデータエンドポイント（169.254.169.254）経由でIAM資格情報を取得できる。  
`hostNetwork: true` のPostfix / HAProxy Podなど、同一Node上のPodがNodeのIAMを利用できてしまうリスクがあるため、  
NodeのIAMには最低限の権限のみ付与し、S3アクセスはIRSAでPod単位に絞っている。

### IRSAで最小権限を実現

| コンポーネント | IAM (IRSA) | 用途 |
|---|---|---|
| nightmode CronJob | ❌ | 夜間スケールダウン（k8s APIのみ）|
| Postfix Pod（preStop） | ✅ S3 Put | Pod終了時キュー退避 |
| queue-monitor CronJob | ✅ S3 Put | キュー救済・S3退避（1000件超）|
| s3-recovery CronJob | ✅ S3 Get/Delete | S3からの復元配信 |

S3アクセスが必要なPodのみServiceAccountにIAM Roleを紐付け（IRSA）。  
他のPodがNodeのIAMを経由してもS3には触れない設計。

---

## コスト効果

| 項目 | 旧（EC2 138台） | 新（EKS+Karpenter） | 削減率 |
|------|----------------|---------------------|--------|
| 月額 | $1,646 | $922 | **44%** |
| 年間削減額 | - | - | 約¥130万 |

- Spot インスタンス率ほぼ100% により大幅なコスト削減を実現
- 夜間スケールダウンにより深夜帯のコストをさらに圧縮
