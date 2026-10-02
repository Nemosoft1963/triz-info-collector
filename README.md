# triz-info-collector

公開ソース(エンジニアリングブログ・GitHub・Qiita・Stack Exchange等)から、TRIZ(発明的問題解決理論)的な「課題定義」のネタを自動収集し、人手レビューを経て取り込むための収集エージェントです。社内の業務自動化基盤「セバス」のサポートエージェントの1つとして運用しています。

## 何をするアプリか

1. 定期的に公開ソースから、設計上のトレードオフ・障害対応・アーキテクチャ上の制約などが書かれた記事を取得する。
2. ローカルLLM(Ollama)を使って、各記事を「目標・改善したい点・悪化する点・理想・物理的矛盾・制約・資源・根拠」というTRIZの定型フォーマットの下書き(課題定義)に変換する。
3. 人間がWeb画面(`/items`)で下書きを1件ずつ、またはまとめてレビューし、承認・却下する。
   - 承認の条件は機械的に検証される: 各項目が2500文字以内であること、必須項目が空でないこと等。
4. 承認済みの課題定義だけを、ボタン操作で外部システム(セバス側の実行例・学習APIを経由した課題登録)へ送信できる。

**自動生成された内容を人の確認なしに「確認済みの課題」として確定させることはありません。** 承認は常に人の操作が必要です。

## 主な機能

- 複数の公開ソース(RSS、GitHub Issues/RFC、Qiita API、Stack Exchange等)からのスケジュール収集(1日1回)とrobots.txt遵守、レート制限
- 下書きの自動生成(Ollama、`DRAFT_PROVIDER=none`なら生成せず手動入力のみ)
- 一覧画面でのフィルタ(収集領域・課題分野・状態・取得日)
- 個別承認・却下・下書き戻し
- 複数選択してのまとめて承認(`/items/bulk-approve`)
- 承認済み課題定義の外部システムへの送信(`/items/push-to-sebas`、送信先は環境変数で設定。未設定時は送信しない)

## 動作環境

- Python 3.12 / Flask
- SQLite(ファイルベース、`DATABASE_PATH`で指定)
- Ollama(任意。下書き自動生成を使う場合のみ)

## セットアップ

```bash
pip install -r requirements.txt
python -m app.web
```

Dockerで動かす場合:

```bash
docker build -t triz-info-collector .
docker run -p 8080:8080 -v $(pwd)/data:/data triz-info-collector
```

## 環境変数

| 変数名 | 既定値 | 説明 |
|---|---|---|
| `DATABASE_PATH` | `/data/collector.db` | SQLiteファイルの場所 |
| `BIND_HOST` | `127.0.0.1` | 待受ホスト |
| `PORT` | `8080` | 待受ポート |
| `CONTACT_EMAIL` | (空) | 収集時のUser-Agentに含める連絡先 |
| `GITHUB_TOKEN` | (空) | GitHub系ソースの取得にレート制限緩和のため使用(任意) |
| `DRAFT_PROVIDER` | `none` | 下書き生成方式(`none`または`ollama`) |
| `OLLAMA_HOST` | `http://host.docker.internal:11434` | Ollamaの接続先 |
| `OLLAMA_MODEL` | (空) | 使用するOllamaモデル名 |
| `SEBAS_BASE_URL` | (空) | 承認済み課題の送信先(外部システムのベースURL)。未設定なら送信機能は無効 |
| `SEBAS_PROJECT_ID` | `default` | 送信先プロジェクトID |
| `TZ` | `UTC` | スケジュール収集の基準タイムゾーン |

## 収集ソース例

- GitHub Issues / RFC(Rust、Kubernetes、PyTorch、TensorFlow、vLLM 等)
- エンジニアリングブログ(Kubernetes、Cloudflare、GitHub、Meta、NVIDIA 等)
- Qiita API(トラブルシューティング、パフォーマンスチューニング、アーキテクチャ設計、リファクタリング)
- Stack Exchange(Software Engineering、Quant、XBRL/会計関連)

## 安全設計

- 収集元のrobots.txtを尊重し、公開APIのレート制限を守る間隔制御を行う。
- 本文の切り詰め・改ざんを行わない。検証に必要な最低限の文字数制限のみ設ける。
- 承認は常に人間の操作を要求する(自動承認の機能は存在しない)。
- 送信先(`SEBAS_BASE_URL`)が未設定の環境では、外部への送信は一切行われない。

## ライセンス

Apache License 2.0。詳細は[LICENSE](LICENSE)を参照してください。
