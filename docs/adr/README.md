# 設計判断の記録（Architecture Decision Records）

ADR とは、プロジェクトにおける重要なアーキテクチャ上の意思決定を記録したドキュメントです。
「なぜそうしたのか」という背景・選択肢・トレードオフを残すことで、将来のチームメンバーや自分自身が判断の経緯を追えるようにします。
新しい ADR は [template.md](./template.md) をコピーして作成してください。

## ADR 一覧

| No. | タイトル | ステータス | 日付 |
|-----|----------|------------|------|
| [0001](./0001-why-karpenter.md) | なぜ Karpenter を選んだか | 提案中 | - |
| [0002](./0002-why-spot-100-percent.md) | なぜ Spot 率 100% にしたか | 提案中 | - |
| [0003](./0003-why-irsa-not-node-iam.md) | なぜ Node IAM ではなく IRSA を選んだか | 提案中 | - |
| [0005](./0005-pod-termination-queue-rescue.md) | Pod 終了時のキュー消失をどう扱うか | 検討中 | 2026-04-23 |
