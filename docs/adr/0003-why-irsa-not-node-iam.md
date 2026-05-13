# ADR 0003: なぜ Node IAM ではなく IRSA を選んだか

## ステータス

採択済み — 2026-05-13

## コンテキスト

EKS において Pod が AWS リソース（S3・SQS等）にアクセスするには IAM 権限が必要になる。
権限を付与する方法として主に2つある。

### Node IAM ロール（インスタンスプロファイル）

EC2 Node にIAMロールを付与する方法。
Node 上のプロセスは EC2 のメタデータエンドポイント（`169.254.169.254`）から認証情報を取得できるため、**そのNode上で動く全Podが同じIAMロールを使える**状態になる。

```
Node IAMロール（S3フルアクセス）
  ├─ Postfix Pod    → S3使える（必要）
  ├─ 無関係なPod    → S3使える（不要）
  └─ 侵害されたPod  → S3使える（危険）
```

### IRSA（IAM Roles for Service Accounts）

Kubernetes の ServiceAccount に IAM ロールを紐付ける方法。
OIDC プロバイダーを通じて特定の ServiceAccount だけが IAM ロールを assume できる。

```
IRSAロール（S3フルアクセス）
  └─ Postfix Pod（指定のServiceAccount）だけが使える
     他のPodは使えない
```

## 決定

IRSA を採用する。

## 理由

- Node IAM ロールはNode上の全Podに権限が広がるため、最小権限の原則に反する
- IRSA はPod単位・ServiceAccount単位で権限を絞れる
- 1つのPodが侵害されても他のPodへの横展開リスクを抑えられる

## トレードオフ

- OIDC プロバイダーの設定・IAMロールへの信頼ポリシー追加が必要で、Node IAM より設定が複雑になる
- ServiceAccount と IAM ロールの紐付けを管理する必要がある

## 関連 ADR

- [ADR 0001](./0001-why-karpenter.md): なぜ Karpenter を選んだか
- [ADR 0002](./0002-why-spot-100-percent.md): なぜ Spot 率100% にしたか
- [ADR 0004](./0004-nodepool-weight-design.md): NodePool の優先度設計
- [ADR 0005](./0005-pod-termination-queue-rescue.md): Pod 終了時のキュー消失を3層防御で処理する
- [ADR 0006](./0006-karpenter-sqs-interruption-queue.md): Karpenter SQS interruption queue の導入
