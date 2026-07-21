# tailscale-vnc-runner (macOS 版)

GitHub Actions の **macOS ランナー**に **Tailscale 経由で画面共有 (VNC) 接続**し、無料の macOS デスクトップをリモート操作するためのリポジトリです。公開ポートを一切開けず、自分の tailnet 内だけで完結するので安全です。

Windows 版 (`rdp.yml` / `setup.ps1`) と同じリポジトリに共存できます。macOS 用は `vnc.yml` / `setup.sh` です。

## 必要なもの

- GitHub アカウント（リポジトリを **Public** にすると Actions が無料枠で潤沢）
- Tailscale アカウント（無料枠でOK）
- 手元の VNC クライアント
  - Mac: 標準の **画面共有 (Screen Sharing)** アプリ → `vnc://100.x.y.z` を開くだけ
  - Windows: **RealVNC Viewer**（macOS 標準の画面共有と互換）

## Windows 版との対応

| 項目 | Windows 版 | macOS 版 |
| --- | --- | --- |
| ランナー | `windows-latest` | `macos-latest` |
| リモート方式 | RDP (3389) | 画面共有 / VNC (5900) |
| ログインユーザー | `runneradmin` | `runner` |
| プロビジョニング | `setup.ps1` | `setup.sh` |
| ワークフロー | `.github/workflows/rdp.yml` | `.github/workflows/vnc.yml` |

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
| `RDP_PASSWORD` | `runner` アカウントのログインパスワード（強固なものを！） |
| `TS_API_KEY` | （任意）古いノードの自動削除に使う Tailscale API キー |
| `VNC_PASSWORD` | （任意）レガシー VNC 用パスワード。**先頭 8 文字のみ**有効 |

> Windows 版と Secret 名を揃えているので `RDP_PASSWORD` / `TS_AUTHKEY` / `TS_API_KEY` はそのまま共用できます。

### 3. ワークフローを実行

1. **Actions** タブ → `macOS Screen Sharing via Tailscale` → **Run workflow**
2. `session_minutes` に維持したい分数を入力
3. `Connect Tailscale` ステップのログに出る **Tailscale IP** を確認

### 4. VNC / 画面共有で接続

- 接続先: ログに出た Tailscale IP（例: `100.x.y.z`）
- ユーザー名: `runner`
- パスワード: `RDP_PASSWORD` に設定した値
- Mac からは Finder で **移動 → サーバへ接続** に `vnc://100.x.y.z` と入力（または `open vnc://100.x.y.z`）
- ※ 手元の端末も同じ tailnet に参加（Tailscale にログイン）しておくこと
- ※ TightVNC など「システム認証に非対応」なクライアントを使う場合のみ `VNC_PASSWORD`（8 文字以内）を設定してください

## 終了方法

- `session_minutes` が経過すると自動終了
- 早く終わらせたい場合は、リモートデスクトップ上で `/Users/runner/Desktop/stop.txt` を作成すると 30 秒以内に終了

## Chrome 環境（任意）

- `setup.sh` を同梱すると、セッションごとに Chrome を導入し、永続プロファイル用の起動ショートカット（デスクトップの `Chrome (persistent)`）を作成します。
- ブックマーク・拡張機能・設定・履歴は `~/chrome-profile` に保存されます。**セッションをまたいで残すにはこのフォルダをクラウド同期**してください。
- 保存パスワードや Cookie はコピーでは復元できません（macOS キーチェーンで暗号化されているため）。Chrome Sync を使ってください。

## 注意

- ジョブは最大 **6 時間**で強制終了します。作業結果は外部（クラウド / 自前サーバー）へ退避を。
- 正規のデバッグ・検証用途で利用してください（常用デスクトップやマイニング等は GitHub 規約違反）。
- `RDP_PASSWORD` は必ず強固なものを設定してください。
- macOS ランナーは Apple Silicon (arm64) です。`brew` で入る Tailscale / Chrome はいずれも arm64 対応です。
