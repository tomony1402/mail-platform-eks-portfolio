# ADR 0004: NodePool の優先度設計（weight による Spot フォールバック）

## ステータス

採択済み — 2026-05-13

## コンテキスト

Spot インスタンスは在庫枯渇により起動できない場合がある。
単一インスタンスタイプのみを対象にすると、その Spot 在庫が枯渇した際に新規 Pod が Pending 状態になり配信が停止するリスクがある。

また、コスト最適化のため On-Demand への依存はできるだけ避けたい。

### 以前の構成の問題

以前は Spot・On-Demand を1つの NodePool にまとめ、`capacityType` に両方を許可していた。

```yaml
requirements:
  - key: karpenter.sh/capacity-type
    operator: In
    values: ["spot", "on-demand"]  # 両方許可
```

この構成では Karpenter がノード起動を最優先に判断するため、
**Spot が取れる状況でも On-Demand を選択することがあった**。
結果として意図せずコストが増加する問題が発生した。

## 決定

Karpenter の `weight` を使って3段階のフォールバック構成を採用する。

### weight 設計

| weight | NodePool | capacityType | インスタンス |
|:---:|:---|:---|:---|
| 100 | small-spot | Spot | t3.small / t3a.small / t2.small |
| 50 | medium-spot | Spot | t3.medium / t3a.medium / t2.medium |
| 1 | on-demand | On-Demand | t3.small / t3a.small / t2.small |

Karpenter は weight が高い NodePool を優先してスケジューリングする。
small-spot → medium-spot → On-Demand の順にフォールバックする。

### deployment-a / b で expireAfter をずらす設計

| deployment | expireAfter |
|:---|:---:|
| deployment-a | 40m |
| deployment-b | 45m |

同じ expireAfter にすると全 Node が同時に入れ替わり、瞬間的に配信能力が落ちる。
5分ずらすことで入れ替えのタイミングを分散させている。

### インスタンスタイプを複数指定する理由

- `t3.small` / `t3a.small` / `t2.small` の3種類を指定
- 1種類が在庫枯渇していても他で代替できる
- medium-spot も同様に3種類指定

## 理由

- **weight 100（small-spot）を最優先**：最安のインスタンスで Spot 100% を維持する
- **weight 50（medium-spot）をフォールバック**：small の在庫枯渇時でも Spot で動かし続ける
- **weight 1（on-demand）を最終手段**：Spot が完全に枯渇した場合のみ On-Demand を使う

## トレードオフ

- NodePool が deployment-a/b × 3段階 = **6本**になり管理ファイルが増える
- weight の調整やインスタンスタイプの追加・削除が必要になった場合、6ファイルを更新する必要がある

## 関連 ADR

- [ADR 0001](./0001-why-karpenter.md): なぜ Karpenter を選んだか
- [ADR 0002](./0002-why-spot-100-percent.md): なぜ Spot 率100% にしたか
- [ADR 0003](./0003-why-irsa-not-node-iam.md): なぜ Node IAM ではなく IRSA を選んだか
- [ADR 0005](./0005-pod-termination-queue-rescue.md): Pod 終了時のキュー消失を3層防御で処理する
- [ADR 0006](./0006-karpenter-sqs-interruption-queue.md): Karpenter SQS interruption queue の導入
