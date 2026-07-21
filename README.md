# tailscale-mcp/rdp-runner

GitHub Actions のランナー上に、**Tailscale 経由で接続できるリモート環境**（MCP サーバー / RDP デスクトップ）を無料で立ち上げるためのリポジトリです。公開ポートを直接開けず、自分の tailnet 内で完結させます。

現在の主役は **Ubuntu ランナー上に Playwright MCP + SSH MCP + RDP デスクトップ**を立てるワークフローで、Notion AI のカスタム MCP から接続して使うことを想定しています。

> **公開リポジトリなので GitHub Actions の分数課金は発生しません**（同時実行数・1ジョブ最大6時間の上限のみ）。

## ワークフロー一覧

| ファイル | 役割 |
| --- | --- |
| `.github/workflows/ubuntu-playwright-mcp.yml` | **【主】** Ubuntu に Playwright MCP + SSH MCP（Tailscale Funnel で公開 HTTPS）+ 常設 RDP デスクトップ（Tailscale 直結）を起動 |
| `.github/workflows/rdp.yml` | Windows ランナーに RDP 接続（後述） |
| `.github/workflows/vnc.yml` | VNC ベースのデスクトップ（旧構成） |

---

## 【主】Ubuntu Playwright MCP + SSH MCP + RDP

### 構成

`ubuntu-latest` / `timeout-minutes: 360` のジョブで以下を起動します。

- **Playwright MCP**（`@playwright/mcp`, headless Chromium, endpoint `/mcp`）
- **SSH MCP**（`ssh-mcp` を `mcp-proxy` で stdio→SSE 化, endpoint `/sse`）
- **常設デスクトップ**: `Xvfb :0` + XFCE を常駐させ、`x11vnc`（:5900）→ `xrdp`（:3389, libvnc バックエンド）で公開。**再接続しても同じセッション**に戻ります（セッション名 `Standing Desktop`）。
- **Tailscale**: userspace networking で起動し、`--ssh` で Tailscale SSH を有効化。MCP は **Funnel** で公開 HTTPS 化（対応ポートは 443 / 8443 / 10000 のみ）。

> バックグラウンドの常駐プロセス（Xvfb / XFCE / x11vnc / Playwright MCP / mcp-proxy / tailscaled）は `setsid ... < /dev/null > log 2>&1 &` で stdin/stdout を完全に切り離して起動します。これをしないと `mcp-proxy` のステップが完了せずハングし、後続の Funnel 公開ステップまで進みません。

### 必要な Secrets

リポジトリ → **Settings → Secrets and variables → Actions**

| Secret 名 | 必須 | 内容 |
| --- | --- | --- |
| `TS_AUTHKEY` | ✅ | Tailscale の Auth Key（Reusable + Ephemeral 推奨） |
| `VNC_PASSWORD` | 任意 | RDP ログイン用パスワード（未設定だと `changeme`。必ず強固なものを設定推奨） |
| `TS_API_KEY` | 任意 | 古い `gha-ubuntu-mcp` デバイスの自動掃除に使用 |

### Tailscale 側の前提

- **HTTPS 証明書を有効化**（管理画面 → DNS → HTTPS Certificates）。無いと Funnel の証明書が発行できません。
- **ACL の `nodeAttrs` に `funnel` 属性**を付与。無いと Funnel が張れません。
- 自分で SSH する場合は ACL に `ssh` accept ルール（src: member / dst: self / users: `runner`, `autogroup:nonroot`）。

### 実行方法

- **Actions** タブ → `Ubuntu Playwright MCP + SSH MCP + RDP via Tailscale` → **Run workflow**
  - `session_minutes`（既定 `350`）: セッション維持時間
  - `mcp_port`（既定 `8931`）: Playwright MCP のローカルポート
- または `repository_dispatch`（type: `start-playwright-mcp`）でも起動可能。

### 接続情報

ホスト名は `gha-ubuntu-mcp`。以下の `<your-host>.<your-tailnet>.ts.net` は自分の実際のホスト／tailnet に読み替えてください。実行ログの最後にも実際の URL が出力されます。

| 用途 | エンドポイント | 認証 |
| --- | --- | --- |
| Playwright MCP（Notion 追加用・公開） | `https://<your-host>.<your-tailnet>.ts.net/mcp` | なし |
| SSH MCP（SSE・Notion 追加用・公開） | `https://<your-host>.<your-tailnet>.ts.net:8443/sse` | **なし（No Auth）** |
| RDP（自分用・Tailscale 直結、Funnel 不可） | `<your-host>.<your-tailnet>.ts.net:3389` | セッション `Standing Desktop` / パスワード = `VNC_PASSWORD` |
| 自分の SSH（Tailscale SSH） | `ssh runner@<your-host>.<your-tailnet>.ts.net` | Tailscale ACL |

> ⚠️ **セキュリティ警告**: SSH MCP の公開 URL は **認証なし**です。URL を知っている者は誰でもランナー上でシェルを実行できます。**実際のホスト名・tailnet 名・URL を公開リポジトリ（この README 含む）やチャットに絶対に書かない**でください。必要なら SSH MCP に Bearer トークン認証を追加する運用を検討してください。

### 終了方法

- `session_minutes` が経過すると自動終了
- 早期終了したい場合はランナー上で `/tmp/stop.txt` を作成（30 秒以内に終了）
- 終了時に Funnel を off、Tailscale を logout し、`TS_API_KEY` があれば tailnet からデバイスを削除

---

## Windows RDP via Tailscale（`rdp.yml`）

GitHub Actions の Windows ランナーに Tailscale 経由で RDP 接続する構成です。

### 必要な Secrets

| Secret 名 | 内容 |
| --- | --- |
| `TS_AUTHKEY` | Tailscale の Auth Key |
| `RDP_PASSWORD` | RDP ログイン用パスワード（強固なものを！） |

### 実行 & 接続

1. **Actions** タブ → `Windows RDP via Tailscale` → **Run workflow**（`session_minutes` を指定）
2. `Connect Tailscale` ステップのログに出る **Tailscale IP** を確認
3. RDP クライアント（Windows: `mstsc` / Mac: Windows App）で接続
   - ユーザー名: `runneradmin`
   - パスワード: `RDP_PASSWORD`
   - 接続先: ログの Tailscale IP（例 `100.x.y.z`）
   - 手元の端末も同じ tailnet に参加しておくこと
4. 早期終了は `C:\Users\runneradmin\Desktop\stop.txt` を作成

- NLA で繋がらない場合は `rdp.yml` 内のコメントアウトされた `UserAuthentication` を `0` にする行を有効化。

---

## 注意

- ジョブは最大 **6 時間**で強制終了します。作業結果は外部（クラウド / 自前サーバー）へ退避を。
- 正規のデバッグ・検証用途で利用してください（常用デスクトップやマイニング等は GitHub 規約違反）。
- 各種パスワード（`VNC_PASSWORD` / `RDP_PASSWORD`）は必ず強固なものを設定してください。
