# 設計判断の記録（Architecture Decision Records）

ADR はプロジェクトにおける重要なアーキテクチャ上の意思決定を記録するドキュメントです。
「なぜそうしたか」という背景・選択肢・トレードオフを残すことで、将来の自分やチームが文脈を失わずに判断を再評価できます。
各 ADR は一度承認されたら変更せず、覆した場合は新しい ADR を作成して旧 ADR を「非推奨」に更新します。

## ADR 一覧

| ID | タイトル | ステータス |
|----|----------|------------|
| [ADR 0001](0001-why-karpenter.md) | なぜ Karpenter を選んだか | 草稿 |
| [ADR 0002](0002-why-spot-100-percent.md) | なぜ Spot率100%にしたか | 草稿 |
| [ADR 0003](0003-why-irsa-not-node-iam.md) | なぜ Node IAM ではなく IRSA を選んだか | 草稿 |

## 新しい ADR を書く

[template.md](template.md) をコピーして `NNNN-short-title.md` というファイル名で作成してください。

```bash
cp docs/adr/template.md docs/adr/NNNN-your-title.md
```
