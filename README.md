# tailscale-rdp-runner

GitHub Actions の Windows ランナーに **Tailscale 経由で RDP 接続**し、無料の Windows デスクトップをリモート操作するためのリポジトリです。公開ポートを一切開けず、自分の tailnet 内だけで完結するので安全です。

## 必要なもの

- GitHub アカウント（リポジトリを **Public** にすると Actions が無料枠で潤沢）
- Tailscale アカウント（無料枠でOK）
- 手元の RDP クライアント（Windows: `mstsc` / Mac: Windows App）

## セットアップ

### 1. Tailscale の Auth Key を発行

1. Tailscale 管理画面 → **Settings → Keys → Generate auth key**
2. **Reusable** と **Ephemeral** を ON（Ephemeral だとジョブ終了後にノードが自動削除される）
3. 必要なら Tag を付与（例: `tag:ci`）
4. 生成された `tskey-auth-xxxx` を控える

### 2. GitHub Secrets を登録

リポジトリ → **Settings → Secrets and variables → Actions**

| Secret 名 | 内容 |
| --- | --- |
| `TS_AUTHKEY` | 上で発行した Tailscale の Auth Key |
| `RDP_PASSWORD` | RDP ログイン用パスワード（強固なものを！） |

### 3. ワークフローを実行

1. **Actions** タブ → `Windows RDP via Tailscale` → **Run workflow**
2. `session_minutes` に維持したい分数を入力
3. `Connect Tailscale` ステップのログに出る **Tailscale IP** を確認

### 4. RDP で接続

- ユーザー名: `runneradmin`
- パスワード: `RDP_PASSWORD` に設定した値
- 接続先: ログに出た Tailscale IP（例: `100.x.y.z`）
- ※ 手元の端末も同じ tailnet に参加（Tailscale にログイン）しておくこと

## 終了方法

- `session_minutes` が経過すると自動終了
- 早く終わらせたい場合は、リモートデスクトップ上で `C:\Users\runneradmin\Desktop\stop.txt` を作成すると 30 秒以内に終了

## 注意

- ジョブは最大 **6 時間**で強制終了します。作業結果は外部（クラウド / 自前サーバー）へ退避を。
- 正規のデバッグ・検証用途で利用してください（常用デスクトップやマイニング等は GitHub 規約違反）。
- `RDP_PASSWORD` は必ず強固なものを設定してください。
- NLA で繋がらない場合は `rdp.yml` 内のコメントアウトされた `UserAuthentication` を `0` にする行を有効化してください。
