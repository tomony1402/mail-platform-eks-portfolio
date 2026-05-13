# ADR 0001: なぜ Karpenter を選んだか

## ステータス
承認済み — 2026-01 採用決定 / 2026-04 全クライアント移行完了

## コンテキスト

オンプレミス EC2 138台のメール配信基盤を EKS に移行するにあたり、ワーカーノードのオートスケール方式を決める必要があった。

メール配信ワークロードには以下の特性がある：

- 配信量が時間帯で大きく変動する（昼間60台、深夜8台）
- 配信 Pod の起動が遅れると既存 Pod にキューが集中し、負荷の雪だるま式悪化に繋がる
- Spot インスタンスを最大限活用してコストを下げたい
- 配信 Pod と Gateway Pod で要求インスタンスタイプが異なる

つまり「**起動速度**」「**Spot 活用**」「**インスタンスタイプの柔軟性**」の3つが同時に要求された。

## 選択肢

### 選択肢 A: EKS Managed Node Group（ASG ベース）
- メリット:
  - EKS のマネージド機能として標準的、情報も多い
  - AWS コンソールから管理しやすい
- デメリット:
  - ノード起動が遅い（ASG 経由のため）
  - インスタンスタイプを ASG ごとに固定する必要がある
  - Spot と On-Demand を混在させる場合の制御が粗い

### 選択肢 B: Karpenter
- メリット:
  - Pod-first 方式でノード起動が速い（EC2 API を直接叩く）
  - NodePool ごとにインスタンスタイプ・容量タイプを柔軟に指定できる
  - AWS 公式が推奨しており、今後の発展が期待できる
  - Spot インスタンスの扱いがシンプル
- デメリット:
  - AWS 側（EC2制御）と K8s 側（Node/Pod制御）の両方の権限設計が必要で、初見では複雑
  - 比較的新しいため、v0 時代のブログ記事は参考にならない

### 選択肢 C: 固定台数の Node Group（スケーリングなし）
- メリット:
  - 構成がシンプル
- デメリット:
  - 昼間のピークに合わせると深夜の無駄が大きい
  - 今回のコスト削減要求を満たせない

## 決定

**Karpenter v1.10.0** を採用する。

## 理由

メール配信ワークロードの特性と合致していたため：

- **起動速度**：Pod の起動遅延がキュー詰まりを誘発する構造上、スケール速度が致命的に重要。Karpenter の Pod-first 方式は Node Group より明確に速い。
- **Spot 活用**：Spot 優先で安価にスケールしつつ、容量が取れない時だけ On-Demand にフォールバックする構成が NodePool で自然に表現できる。
- **インスタンスタイプの柔軟性**：配信 Pod（small〜medium）と Gateway Pod（固定 t3.medium）で要求が違うため、NodePool 単位で指定できるのが合っていた。
- **AWS 公式の推奨**：長期運用を見据えたとき、公式サポートが厚い方が安心。

## トレードオフ（諦めたこと）

- **情報量**：Node Group の方が枯れていて情報が多い。Karpenter はまだ新しく、v0 時代の記事は API が違うため参考にならない。
- **権限設計の複雑さ**：Karpenter は AWS 側の EC2 リソースと K8s 側の Node リソースの**両方**を操作する。そのため：
  - **IRSA（AWS 権限）**：EC2 起動・タグ付け・終了などの操作権限を ServiceAccount に紐付ける
  - **RBAC（K8s 権限）**：Node 作成・Pod 情報取得などの操作権限をクラスタ内で付与する
  
  この2軸の権限設計が必要になるのは Karpenter 特有の学習コスト。ただし一度組んでしまえば定型。

## 結果

- 移行後4ヶ月運用して、スケール速度起因の配信断はゼロ。
- Spot 率100%を維持しつつ、昼夜でのノード台数変動にスムーズに追従できている。
- 3段階 NodePool（small-spot → medium-spot → on-demand）の優先度制御は詳細を [ADR 0004](./0004-nodepool-weight-design.md) に分離して記録する。
- 導入初期に IRSA/RBAC の二重権限設計でつまずいたが、現在は定型化してコード化済み（`modules/karpenter/`、`modules/iam/`）。

## 参考

- Karpenter 公式: https://karpenter.sh/
- 関連 ADR:
  - [ADR 0002](./0002-why-spot-100-percent.md): なぜ Spot率100%にしたか
  - [ADR 0003](./0003-why-irsa-not-node-iam.md): なぜ Node IAM ではなく IRSA を選んだか
  - [ADR 0004](./0004-nodepool-weight-design.md): NodePool の優先度設計（weight による Spot フォールバック）
  - [ADR 0005](./0005-pod-termination-queue-rescue.md): Pod 終了時のキュー消失を3層防御で処理する
  - [ADR 0006](./0006-karpenter-sqs-interruption-queue.md): Karpenter SQS interruption queue の導入
