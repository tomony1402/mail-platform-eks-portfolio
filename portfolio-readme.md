# mail-platform-eks
[![CI](https://github.com/tomony1402/mail-platform-eks-portfolio/actions/workflows/ci.yml/badge.svg)](https://github.com/tomony1402/mail-platform-eks-portfolio/actions/workflows/ci.yml)

オンプレミス（AWS EC2）138台のメール配信基盤を **EKS + Karpenter** に移行・刷新したプロジェクト。

---

## プロジェクト概要

| 項目 | 内容 |
|------|------|
| 配信量 | 1時間最大60万件（1日約660万件）|
| 宛先 | docomo |
| 旧構成 | オンプレ EC2 138台 |
| 新構成 | EKS + Karpenter（Spot中心）|
| 月額コスト削減 | **35%削減**（年間約¥288万削減）|
| 移行期間 | 2026年1月〜2026年4月（4ヶ月） |
| 移行状況 | 全クライアント移行完了 |

---

## インフラ構成

| コンポーネント | 内容 |
|---------------|------|
| AWS リージョン | us-east-1 / 4 AZ |
| EKS バージョン | 1.33 |
| Karpenter バージョン | 1.10.0 |
| Gateway | HAProxy DaemonSet × 2台（t3.medium On-Demand）|
| 配信 Pod | postfix-deployment-a / b 各30台（昼間合計60台）|
| NodePool 優先度 | small-spot → medium-spot → On-Demand（3段階フォールバック）|
| expireAfter | a=40分 / b=50分（一斉expire回避）|
| Spot率 | 100% |

### 夜間スケールスケジュール（JST）

| 時刻 | 台数（a+b 合計）|
|------|----------------|
| 08:00 | 60台（昼間フル稼働）|
| 22:00 | 20台 |
| 02:00 | 8台 |
| 06:00 | 20台 |

---

## アーキテクチャ図

```
┌──────────────────────────────┐
│  オンプレミス  xxx.xxx.xxx.xxx/24  │
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
               │ ClusterIP xxx.xxx.xxx.xxx:25
               ▼
      ┌─────────────────────┐
      │    Postfix Service  │
      │  ClusterIP 固定      │
      └─────────┬───────────┘
           ┌────┴────┐
           ▼         ▼
  ┌─────────────┐ ┌─────────────┐
  │  deploy-a   │ │  deploy-b   │
  │  30 pods    │ │  30 pods    │
  │  expire 40m │ │  expire 50m │
  └─────────────┘ └─────────────┘
   small/med-spot   small/med-spot
   On-Demand        On-Demand
```

---

## 技術的な工夫・こだわりポイント

### 1. Karpenter による動的スケーリング
- Spot インスタンス優先の3段階 NodePool（small-spot → medium-spot → On-Demand）
- `expireAfter` を deployment-a/b で意図的にズラし、一斉ノード入れ替えによる配信断を回避
- `WhenEmpty` 統合ポリシーで不要ノードを自動削除

### 2. 夜間自動スケールダウン（CronJob）
- JST 22:00/02:00/06:00/08:00 に自動スケール
- `nightmode-controller` ServiceAccount + RBAC で最小権限を付与
- 深夜帯のコストをさらに圧縮

### 3. キュー救済システム（S3 Recovery）
キューに1,000件超のメールが滞留したPodを検知し、自動救済する仕組みを実装。

```
queue-monitor（2分毎 CronJob）
    ↓ 1,000件超を検知
S3にメールを退避（20並列 / バウンスメール除外）
    ↓
ノード削除
    ↓
s3-recovery CronJob（JST 9/11/13/15/17/19時）
    ├── 5台のスポットノードを起動（Indexed Job）
    ├── fetcher（initContainer）: S3から20並列ダウンロード
    ├── postfix-delivery: docomoへ直接配信
    └── s3-sync（サイドカー）: 配信済みをS3から削除
```

**設計上のポイント：**
- `hostNetwork: true` + `podAntiAffinity` でport 25衝突を防止（1ノード1Pod強制）
- IRSA（IAM Roles for Service Accounts）でPodレベルのS3アクセスを付与
- 件数に応じた動的台数調整（500件以下は1台のみ処理、残り4台は即終了）
- バウンスメール（MAILER-DAEMON）をS3保存前にフィルタリング
- 15分タイムアウト後は `postsuper -d ALL` で到達不能メールを強制削除

### 4. Postfix 設定の最適化
- tmpfs化: `/var/spool/postfix` をメモリ上に配置（I/O高速化）
- `recipient_canonical_maps`: deferredキューをほぼゼロに抑制
- HELOローテーション: 5分ごとにランダムローテーション
- `header_checks` で `Received:` ヘッダーを除去
- キュー保持時間: 最大6時間 / 接続タイムアウト: 2秒 / 最大同時接続: 50

### 5. Web 管理 UI（admin-server.py）
Python製の管理ダッシュボード（port 8080）を自作。

- AWSコストダッシュボード（USD/JPY、Chart.js グラフ）
- HELO ConfigMap 編集 → kubectl apply → rollout restart をワンクリックで実行
- バウンスドメイン設定（recipient_canonical）のGUI編集
- クラスターメトリクス（ノード/Pod のCPU・メモリ使用率）
- キュー監視（各Podのpostキュー件数 / S3待機件数）
- ノード構成スナップショット（Spot/On-Demand 比率の記録）

---

## ファイル構成

### Terraform

```
envs/prod/
├── main.tf              # モジュール呼び出し（network / eks / ecr / vpc-endpoints / karpenter）
├── providers.tf         # AWS プロバイダー設定（リージョン: us-east-1）
├── variables.tf         # 入力変数の定義
└── .terraform.lock.hcl  # プロバイダーバージョンのロック

modules/
├── network/             # VPC・サブネット作成（terraform-aws-modules/vpc 使用）
├── eks/                 # EKS クラスター・Gateway ノードグループ作成
│                        # Gateway 用 SG（25/tcp from オンプレ CIDR）
├── ecr/                 # ECR リポジトリ（postfix イメージ）
├── karpenter/           # Karpenter インストール（IAM + Helm）
├── iam/                 # IRSA ロール（S3アクセス用）
└── vpc-endpoints/       # S3 用 VPC エンドポイント（プライベート通信）
```

### Kubernetes マニフェスト

```
manifests/
├── gateway/
│   ├── gateway.yaml                  # HAProxy DaemonSet（hostNetwork: true、port 25）
│   └── configmap.yaml                # HAProxy 設定
├── postfix/
│   ├── deployment-a.yaml             # Postfix Deployment A（30 replicas）
│   ├── deployment-b.yaml             # Postfix Deployment B（30 replicas）
│   ├── service.yaml                  # ClusterIP Service（固定IP、port 25）
│   ├── configmap-helo.yaml           # HELO ローテーションスクリプト
│   └── configmap-recipient-canonical.yaml  # recipient_canonical_maps
├── karpenter/
│   ├── ec2nodeclass.yaml             # EC2NodeClass（AMI・サブネット・SG の自動検出）
│   ├── nodepool-a-small-spot.yaml    # NodePool for postfix-a（small Spot）
│   ├── nodepool-a-medium-spot.yaml   # NodePool for postfix-a（medium Spot）
│   ├── nodepool-a-od.yaml            # NodePool for postfix-a（On-Demand フォールバック）
│   ├── nodepool-b-small-spot.yaml    # NodePool for postfix-b（small Spot）
│   ├── nodepool-b-medium-spot.yaml   # NodePool for postfix-b（medium Spot）
│   └── nodepool-b-od.yaml            # NodePool for postfix-b（On-Demand フォールバック）
├── cronjob-nightmode.yaml            # 夜間スケール + キュー救済 CronJob
├── cronjob-recovery.yaml             # S3 Recovery CronJob（Indexed Job × 5台）
└── cronjob-karpenter-watch.yaml      # Karpenter 死活監視 CronJob（5分ごと）
```

### コンテナイメージ

```
services/postfix/
├── Dockerfile       # ベース: ubi9-minimal / postfix + cronie + expect
├── entrypoint.sh    # 起動スクリプト（crond → postfix フォアグラウンド）
├── header_checks    # Received ヘッダー除去フィルター
└── postfix-cron     # 5分ごとに helo.sh を実行
```

### 管理ツール

```
tools/
├── admin-server.py  # Web 管理 UI（port 8080）
├── cost-viewer.py   # AWS Cost Explorer → report.html 生成
├── queue-monitor.html  # キュー監視ダッシュボード
└── report.html      # コストレポート（Chart.js、自動生成）
```

---

## RBAC / IAM 設計

| コンポーネント | RBAC | IAM (IRSA) | 用途 |
|---|---|---|---|
| nightmode CronJob | ✅ deployment scale | ❌ | 夜間スケールダウン |
| queue-monitor CronJob | ✅ pods exec / nodes delete | ✅ S3 Put | キュー救済・S3退避 |
| node-snapshot CronJob | ✅ nodes get | ❌ | ノード構成記録 |
| s3-recovery CronJob | ❌ | ✅ S3 Get/Delete | S3からの復元配信 |

### セキュリティ設計の意図

- **NodeのIAMにS3ポリシーを付与しない**  
  EKSのNodeはEC2メタデータエンドポイント（169.254.169.254）経由でIAM資格情報を取得できる。  
  `hostNetwork: true` のPostfix / HAProxy Podなど、同一Node上のPodがNodeのIAMを利用できてしまうリスクがあるため、  
  NodeのIAMには最低限の権限のみ付与し、S3アクセスはIRSAでPod単位に絞っている。

- **IRSAで最小権限を実現**  
  S3アクセスが必要なPodのみServiceAccountにIAM Roleを紐付け（IRSA）。  
  他のPodがNodeのIAMを経由してもS3には触れない設計。

---

## コスト削減実績

| アカウント | 移行前 | 移行後 | 削減率 |
|------|----------------|---------------------|--------|
| mail-platform-eks | ー | ー | **31%削減** |
| 他アカウントA | ー | ー | **38%削減** |
| 他アカウントB | ー | ー | **31%削減** |
| **3アカウント合計年間削減額** | - | - | **年間約¥288万削減** |

- Spot インスタンス率100% により大幅なコスト削減を実現
- 夜間スケールダウンにより深夜帯のコストをさらに圧縮
- 必要な時だけノードを起動する設計（Recovery CronJob の動的台数調整）

---

## 使用技術

| カテゴリ | 技術 |
|---|---|
| インフラ | AWS EKS / Karpenter / Terraform |
| コンテナ | Docker / Postfix / HAProxy |
| オーケストレーション | Kubernetes（CronJob / Indexed Job / DaemonSet）|
| 認証・認可 | IRSA / RBAC |
| ストレージ | Amazon S3 / tmpfs |
| 監視・管理 | Python（自作 Web UI）/ Chart.js |
| CI/CD | Amazon ECR |
