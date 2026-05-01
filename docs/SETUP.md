# セットアップ・運用手順

## 目次

1. [新規構築手順](#新規構築手順)
2. [夜間スケール設定](#夜間スケール設定)
3. [運用手順](#運用手順)

---

## 新規構築手順

### 前提条件

- AWS CLI 設定済み（リージョン: `us-east-1`）
- Terraform >= 1.6.0
- kubectl / helm インストール済み
- Docker インストール済み
- ECR へのイメージプッシュ権限

---

> 別AWSアカウントへの展開は [NEW-ACCOUNT-SETUP.md](NEW-ACCOUNT-SETUP.md) を参照。

---

### Step 1: Terraform でインフラ構築

```bash
# 初回のみ
terraform -chdir=envs/prod/ init

# 適用
terraform -chdir=envs/prod/ apply
```

---

### Step 2: kubeconfig 更新

```bash
aws eks update-kubeconfig \
  --region us-east-1 \
  --name mail-platform-eks
```

---

### Step 3: ECR 認証 & イメージ push

```bash
aws ecr get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin \
    <YOUR_AWS_ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com

docker push <YOUR_AWS_ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/postfix:latest
```

---

### Step 4: Gateway Pod の準備

```bash
kubectl apply -f manifests/gateway/configmap.yaml
kubectl apply -f manifests/gateway/gateway.yaml
kubectl get pods -o wide
```

---

### Step 5: ec2nodeclass.yaml の role 書き換え

新規構築時は EC2NodeClass の IAM ロール名をクラスターに合わせて書き換える必要がある。

```bash
# ノードグループのロール ARN を確認
aws eks list-nodegroups --cluster-name mail-platform-eks --region us-east-1

aws eks describe-nodegroup \
  --cluster-name mail-platform-eks \
  --nodegroup-name $(aws eks list-nodegroups \
    --cluster-name mail-platform-eks \
    --region us-east-1 \
    --query "nodegroups[0]" \
    --output text) \
  --region us-east-1 \
  --query "nodegroup.nodeRole" \
  --output text

# 確認した ARN のロール名を ec2nodeclass.yaml に反映
vi manifests/karpenter/ec2nodeclass.yaml
```

---

### Step 6: 配信 Pod 用ノード作成

```bash
kubectl apply -f manifests/karpenter/ec2nodeclass.yaml
kubectl apply -f manifests/karpenter/nodepool-a-small-spot.yaml
kubectl apply -f manifests/karpenter/nodepool-a-medium-spot.yaml
kubectl apply -f manifests/karpenter/nodepool-a-od.yaml
kubectl apply -f manifests/karpenter/nodepool-b-small-spot.yaml
kubectl apply -f manifests/karpenter/nodepool-b-medium-spot.yaml
kubectl apply -f manifests/karpenter/nodepool-b-od.yaml
```

---

### Step 7: 配信 Pod の準備

```bash
kubectl apply -f manifests/postfix/configmap-helo.yaml
kubectl apply -f manifests/postfix/configmap-recipient-canonical.yaml
kubectl apply -f manifests/postfix/configmap-sender-canonical.yaml
kubectl apply -f manifests/postfix/service.yaml
kubectl apply -f manifests/postfix/deployment-a.yaml

# deployment-b は a が安定してから適用（約10分待機）
sleep 600
kubectl apply -f manifests/postfix/deployment-b.yaml
```

> **注意**: `service.yaml` で ClusterIP `x.x.x.x` を固定指定しているため、
> 同じ IP が既に使用中の場合はエラーになる。`kubectl get svc -A` で事前確認すること。

---

### Step 8: 夜間スケール設定

```bash
kubectl apply -f manifests/cronjob-nightmode.yaml
```

---

### Step 9: S3 退避メール再送設定

```bash
kubectl apply -f manifests/cronjob-s3-recovery.yaml
```

---

### Step 10a: Karpenter 死活監視設定

```bash
kubectl create secret generic chatwork-secret \
  --from-literal=token=YOUR_CHATWORK_TOKEN \
  --from-literal=room_id=YOUR_CHATWORK_ROOM_ID \
  -n kube-system

kubectl apply -f manifests/cronjob-karpenter-watch.yaml
```

---

### Step 10b: metrics-server のインストール

```bash
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
```

---

### Step 11: 動作確認

```bash
# Karpenter 確認
kubectl get pods -n karpenter

# 異常 Pod の確認（Running / Completed 以外）
kubectl get pods -A | grep -v Running | grep -v Completed

# CronJob 確認
kubectl get cronjob -n kube-system

# ノード確認
kubectl get nodes -L workload --sort-by=.metadata.creationTimestamp

# Karpenter ログ確認
kubectl logs -n karpenter -l app.kubernetes.io/name=karpenter --tail=50
```

---

## 夜間スケール設定

スケジュールは `manifests/cronjob-nightmode.yaml` で管理。UTC で記載する。

| JST | UTC | replicas (a+b) |
|-----|-----|----------------|
| 08:00 | 23:00 (前日) | 35+35 = 70台 |
| 22:00 | 13:00 | 8+8 = 16台 |
| 02:00 | 17:00 | 4+4 = 8台 |
| 06:00 | 21:00 | 8+8 = 16台 |

スケジュールや台数を変更した場合は再 apply する。

```bash
kubectl apply -f manifests/cronjob-nightmode.yaml
```

---

## 運用手順

### HELO 名の変更

HELO 名は `manifests/postfix/configmap-helo.yaml` 内の `helo.sh` で制御。
admin-server.py の Web UI から変更・反映できる。

**直接変更する場合**

```bash
kubectl edit configmap postfix-helo-script

# 反映のため Pod を再起動
kubectl rollout restart deployment/postfix-deployment-a
kubectl rollout restart deployment/postfix-deployment-b
```

---

### 手動スケール

```bash
kubectl scale deployment postfix-deployment-a --replicas=<台数>
kubectl scale deployment postfix-deployment-b --replicas=<台数>
```

---

### S3 メール救済システム

キュー件数が1000件を超えた Pod が検知されると、メールを S3 バケット
`mail-platform-recovery-mail-platform-eks` に退避してノードを削除する。
退避されたメールは `cronjob-s3-recovery.yaml` により JST 9〜19時・2時間おきに自動再送される。

---

### IP ローテーション確認（Karpenter expireAfter）

| NodePool | expireAfter | 用途 |
|----------|-------------|------|
| nodepool-a-* | 40分 | postfix-deployment-a |
| nodepool-b-* | 50分 | postfix-deployment-b |

```bash
# ノードの Age を確認
kubectl get nodes -L workload --sort-by=.metadata.creationTimestamp

# Karpenter がノードを削除/作成するログ
kubectl logs -n karpenter -l app.kubernetes.io/name=karpenter --tail=100 \
  | grep -E "disrupting|launched"
```

expireAfter を変更する場合はマニフェストを編集して再 apply する。

```bash
kubectl apply -f manifests/karpenter/nodepool-a-small-spot.yaml
# 他の nodepool も同様に apply
```

---

### コスト確認

```bash
cd tools
python3 cost-viewer.py
# → tools/report.html が生成される
```

または admin-server.py の Web UI（`http://localhost:8080/`）からも確認可能。

> 為替レートは `cost-viewer.py` 内に `1 USD = 150 JPY` でハードコードされている。
