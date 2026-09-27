# Phase 0 計測コントラクト

Phase 0 は、既存の NERVE プロトコルを実験フレームワークへ置き換えず、観測・判断・将来結果・執行現実を追記する計測サイドカーを追加する。

## ID の境界

`Impulse.id` は従来どおり `chain:pool` の SHA-256 から作られる永続的な pool / execution identity である。`ExecutorNode` の `client_id = nerve-{impulse.id}` も変更しない。このため、同じ pool を再観測しても既存 intent の exactly-once と reconcile の意味は変わらない。

`snapshot_id` は別の identity である。SENTINEL 処理直後の正規化済み payload、`Impulse.id`、`observed_at`、`captured_at`、stage、`input_hash` を canonical JSON にし、SHA-256 の先頭 24 桁を採用する。同じ pool の別観測には別の `snapshot_id` が与えられ、同一 snapshot を複数 arm が共有する。

payload は canonical JSON 文字列として frozen model に保持される。利用側へ返す dict は毎回復元されるため、後続の `Impulse` や呼び出し側による変更で保存済み入力は変化しない。

## capture point と安全性

capture point は SENTINEL の `process()` 完了後、ANALYST 前の post-SENTINEL reflex より前である。SENTINEL 自身または直後の reflex が拒否しても、SENTINEL facts が存在する観測は保存対象になる。

計測は verdict、Risk、Reflex、SENTINEL、signer、live opt-in を変更しない。Phase 0 の arm は実行命令を返さず、期限切れ decision は `expired` として監査履歴に残るだけである。計測書き込みの失敗も本番 verdict や送信経路を変更しない。

## SQLite

既存の `impulses`、`transitions`、`intents`、`positions` の意味は変更しない。以下を追加する。

- `observation_snapshots`: frozen post-SENTINEL input
- `experiment_decisions`: arm、model、question version、raw answer、decision clock
- `forward_outcomes`: 60 / 300 / 900 / 1800 秒の market outcome event
- `execution_observations`: quote、取得可能数量、fee、impact、execution result

欠損値は `NULL` のまま保持する。quote や価格が取れない場合に 0 を書かない。未解決 horizon は `pending` または `unavailable` であり、架空の return や label を持たない。

計測テーブルへの書き込みは追記であり、既存の計測行を上書きしない。forward outcome の解決も `pending` 行を更新せず、同じ snapshot / horizon に `resolved` event を追加する。query API は各 horizon の最新 event を返す。

## レポート

`nerve report` は従来の operational report のままである。`nerve lab-report` は JSON で snapshot 数、arm と decision、期限切れ、latency、horizon 別 return、model/question/horizon 別 calibration、arm 比較、execution reality を返す。較正は純粋な Python で Brier score、binary log loss、reliability bins、ECE を計算する。

## 既知の制限

- 信頼できる live price resolver はまだ接続していない。新規 snapshot には明示的な `pending` outcome を作る。
- CONTROL は abstain baseline であり、既存 ANALYST の paid call を shadow で重複実行しない。
- EVM の署名前 re-quote は既存実装を維持する。Phase 0 では executor から取得できない quote / fee / fill の値を推測せず `NULL` とする。
- 計測書き込み失敗は安全上 core flow を停止させない。永続的な failure telemetry と live resolver は次段階の課題である。
- JEV、MEMORY、MEME_STATE、Observatory UI、戦略自動昇格、real-money experiment は対象外である。
