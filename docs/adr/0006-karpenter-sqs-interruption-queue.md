# ADR 0006: Karpenter SQS interruption queue の導入

## ステータス

採択済み — 2026-05-13

## コンテキスト

[ADR 0002](./0002-why-spot-100-percent.md) で Spot 100% 構成を採択した際、Karpenter SQS interruption queue が未設定であった。
この状態では AWS が Spot 中断の2分前通知を発行しても Karpenter が受信できず、
通知を活かした graceful drain が行われないリスクがあった。

[ADR 0005](./0005-pod-termination-queue-rescue.md) で preStop hook による Pod 終了時のキュー退避を実装したが、
Spot 中断時に Karpenter が通知を受け取れなければ preStop hook 自体が走らない。

運用5日間の実績では Spot 中断は0回だが、構造的なリスクとして残っていた。

## 決定

Karpenter SQS interruption queue を導入する。

### 構成

```
AWS EC2（Spot中断通知）
    ↓
EventBridge
  - EC2 Spot Instance Interruption Warning
  - EC2 Instance Rebalance Recommendation
  - EC2 Instance State-change Notification
    ↓
SQS キュー（karpenter-interruption / SSE暗号化あり）
    ↓
Karpenter（IRSA経由でSQS読み取り権限付与）
    ↓
Node drain → preStop hook 実行 → キュー流し切り → Pod終了
```

### Terraform で追加したリソース

- `aws_sqs_queue` — メッセージ保持300秒・SSE暗号化
- `aws_sqs_queue_policy` — EventBridge からの SendMessage を許可
- `aws_cloudwatch_event_rule` × 3 — Spot中断・Rebalance・状態変化をSQSに転送
- IAM `KarpenterSQS` Statement — Karpenter に SQS 読み取り権限を付与
- Helm `settings.interruptionQueue` — Karpenter にキュー名を渡す

## 理由

- Spot 中断は現時点で0回だが、発生した際に preStop hook が走らないリスクを放置するのは設計として不完全
- 月額コスト約¥100（SQSのポーリングコスト）で対応できる
- [ADR 0005](./0005-pod-termination-queue-rescue.md) の preStop hook と組み合わせることで Spot 中断時の graceful drain が完成する

## トレードオフ

- SQS キューの追加管理コストが発生する（月額約¥100・運用負荷は軽微）
- EventBridge ルール3本が増えるが、設定は Terraform で管理されており運用負荷は低い

## 残存リスク

- Spot 中断時にキューが数百件ある場合、preStop 25秒では捌ききれない可能性がある（[ADR 0005](./0005-pod-termination-queue-rescue.md) 参照）
- 月10回未満の Spot 中断であれば現状の構成で十分対応可能

## 関連 ADR

- [ADR 0002](./0002-why-spot-100-percent.md): なぜ Spot 100% を選んだか
- [ADR 0005](./0005-pod-termination-queue-rescue.md): Pod 終了時のキュー消失を3層防御で処理する
