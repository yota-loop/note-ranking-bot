# note-ranking

note.comで「フォロワー数からは説明がつかないレベルで伸びた記事」を毎日自動抽出し、
WordPress（good-daily-life.com/kouiunolab/note-ranking/）に更新するボット。

GitHub Actions（`.github/workflows/daily.yml`）で毎日5:00 JSTに自動実行される。
ローカルPCの電源状態に依存しない。

## 必要なRepository Secrets
- `WP_XMLRPC_URL`
- `WP_USERNAME`
- `WP_PASSWORD`
