# 別AWSアカウントへの展開手順

既存ディレクトリをコピーして別AWSアカウントに展開する場合の手順。

---

## Step 1: stateファイルの削除

コピー元のstateはそのまま使えないため削除する。

```bash
rm envs/prod/terraform.tfstate
rm envs/prod/terraform.tfstate.backup
```

---

## Step 2: providers.tf の修正

```hcl
provider "aws" {
  region  = "ap-northeast-1"   # 対象リージョンに変更
  profile = "aws215"           # 対象アカウントのprofileに変更
}
```

helm provider の `args` にも `--region` と `--profile` を追加する。

```hcl
args = ["eks", "get-token", "--cluster-name", var.cluster_name, "--region", "ap-northeast-1", "--profile", "aws215"]
```

---

## Step 3: リージョン・アカウントIDの一括置換

```bash
find envs/prod modules manifests -type f | xargs sed -i 's/us-east-1/ap-northeast-1/g'
find envs/prod modules manifests -type f | xargs sed -i 's/<旧アカウントID>/<新アカウントID>/g'

# 漏れ確認
grep -rn "us-east-1\|<旧アカウントID>" envs/prod/ modules/ manifests/
```

---

## Step 4: terraform.tfvars の修正

```bash
# 利用可能なAZを事前確認
aws ec2 describe-availability-zones --region ap-northeast-1 --profile aws215 \
  --query 'AvailabilityZones[*].ZoneName'

# VPCの上限確認（デフォルト5つ）
aws ec2 describe-vpcs --region ap-northeast-1 --profile aws215 \
  --query 'Vpcs[*].VpcId'
```

- `cluster_name` を変更（S3バケット名の重複を避けるため）
- AZを利用可能なものに変更
- subnetをAZ数に合わせて調整

---

## 注意点

| 項目 | 内容 |
|------|------|
| S3バケット名 | グローバルで一意のため、別アカウントでも同じ名前は使えない |
| VPC上限 | リージョンごとにデフォルト5つ。超える場合は不要なVPCを削除するか上限緩和申請 |
| AZ | アカウントによって利用可能なものが異なるため事前確認必須 |

---

準備完了後は [SETUP.md](SETUP.md) の Step 1 から進める。
