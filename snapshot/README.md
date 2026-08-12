# envsnap — 状態を引き継ぐ実行環境（warm-env）

既存の `ephemeral-env` は毎回まっさらな GitHub-hosted runner を借りるため、
パッケージのインストールや環境構築を起動のたびにやり直す必要があります。
`warm-env` はその双子で、**シェルが動く世界を overlayfs の上に載せ、その差分
（upper レイヤ）だけをスナップショットとして保存・復元**します。

```
                       ┌──────────────────────────────┐
  あなたが触る世界  →  │  /mnt/envsnap/merged (chroot) │
                       └───────────┬──────────────────┘
                      overlayfs    │
         ┌─────────────────────────┴─────────────────────────┐
         │                                                   │
  upper = /mnt/envsnap/upper                    lower = / (runner の実体)
  ＝ 差分。これがスナップショットの中身              ＝ 毎回配られる素の Ubuntu
```

- **差分だけを保存する。** ランナーイメージ本体（20GB 超）は毎回 GitHub 側が
  用意してくれるので、運ぶ必要があるのは自分が加えた変更だけです。
- **保存先は非公開リポジトリの GitHub Release アセット。** Actions ↔ Release 間は
  同じ GitHub 内なので実測 50MiB/s 前後出ます。
- **既存の `ephemeral-env.yml` には一切手を入れていません。** まっさらな環境が
  欲しいときは今まで通り `platform="linux"` を使ってください。

## 使い方

broker 経由:

```
create_env(platform="linux-warm")                  # スナップショット "default" を復元
create_env(platform="linux-warm", snapshot="ml")   # "ml" という別系統を復元
snapshot_env(env_id)                               # 途中でチェックポイント
destroy_env(env_id)                                # 終了時に自動保存
```

スナップショット名が違えば互いの変更は一切見えません。用途ごとに分けられます。

初回は空の overlay で起動し、終了時にそこまでの差分が保存されます。2 回目以降は
その続きから始まります。`exec` も `sudo_exec` も、ブラウザ操作も、`linux` と同じ
ように使えます。

### ホスト側に出たいとき

コマンドの先頭に `#!host` を付けると overlay の外（ランナー本体）で実行されます。
dockerd や tailscaled、Actions のランナーエージェントに触りたいときに使います。

```
exec(env_id, "#!host systemctl status docker")
```

## 仕組み

### ログインシェルの差し替え

Tailscale SSH はセッションごとにアカウントのログインシェルを起動します。そこを
`envsnap-shell` に差し替えるだけで、broker には何の変更もなく `exec` が overlay の
中に着地します。`sudo_exec` も同じで、chroot が先に効くので `sudo` は overlay の
中の root になります（ホストの root にはなりません）。

ただしログインシェルの差し替えだけでは足りません。Tailscale SSH は **接続を確立した
時点で** `--login-shell` を解決して保持し、broker は SSH 接続を使い回すので、`chsh`
より前に張られた接続はその後もずっと `/bin/bash` を起動し続けます。実際、中から
`ps` を取ると `tailscaled be-child ssh --login-shell=/bin/bash ... --cmd=bash -lc …`
が見えます。warm-env.yml は tailscaled を起動する **前に** mount と activate を
済ませるので本番ではこの順序問題は起きませんが、正しさを順序に依存させないために
`/etc/profile.d/envsnap.sh` にも同じ処理を置いています。broker が送るのは
`bash -lc '<command>'` で、その内側のログインシェルは必ず `/etc/profile.d/*.sh` を
読むため、接続がいつ張られていても overlay に入れます。

このフックは条件を全部満たしたときだけ発動します（`/run/envsnap/active` がある・
`envsnap-enter` が実行可能・`merged` がマウント済み・まだ中に入っていない）。
どれか欠ければ普通のシェルとして振る舞うので、設定を間違えても SSH できなく
なることはありません。

### レイヤと世代

保存は**差分レイヤの積み重ね**です。前回保存時のファイル一覧（パス・種別・サイズ・
mtime）を `index.tsv` として持っておき、次回はそれとの差分だけを tar に固めます。
削除されたパスは `<layer>.del` として別に記録し、復元時に適用します。

レイヤが `ENVSNAP_MAX_LAYERS`（既定 8）に達すると、次の保存で自動的に full に
切り替わり 1 枚に圧縮されます。復元にかかる時間が無限に伸びるのを防ぐためです。

### 2GiB の壁とチャンク分割

GitHub Release の制約は「1 アセット 2GiB 未満・1 リリースあたり 1000 アセット・
**合計サイズと帯域は無制限**」です。そこで各レイヤを `ENVSNAP_CHUNK_MB`（既定
256MiB）で分割し、`p000`, `p001`, … という名前で並列アップロードします。
計算上は 1 スナップショットあたり最大約 250GB まで載る勘定で、実用上は上限なしと
考えて差し支えありません。

分割は速度のためでもあります（実測）:

| 処理 | 単一ストリーム | 4 並列 × 256MiB |
|---|---|---|
| Release へのアップロード | 16 MiB/s | **52 MiB/s** |
| Release からのダウンロード | 30–134 MiB/s | **101 MiB/s** |

参考値: zstd -6 圧縮 176MiB/s（圧縮率 4.05x）、展開 897MiB/s、AES-256-CTR 暗号化
576MiB/s、復号 568MiB/s。いずれも 4 コアの ubuntu-latest 上での実測です。

### マニフェストは上書きしない

世代番号付きで `manifest-000001.json`, `manifest-000002.json` … と**新しい名前で
追加**し、復元側は名前順の最後を読みます。`gh release upload --clobber` は「消して
から上げ直す」挙動なので、途中で失敗するとスナップショットごと壊れます。それを
避けるための設計です。古いマニフェストと参照されなくなったチャンクは、新しい
マニフェストのアップロードが**成功したあとで**掃除します。

> 保存先リポジトリの immutable releases は無効のままにしてください。有効だと
> 掃除もチャンク追加もできなくなります。

### 暗号化

`ENVSNAP_PASSPHRASE` があれば AES-256-CTR（PBKDF2 20 万回）で暗号化します。
zstd 圧縮後にかけるので圧縮率は落ちず、速度も 576MiB/s とアップロード帯域
（52MiB/s）の 10 倍以上なので、実質ノーコストです。既定で有効にしてください。

### 何を保存しないか

`exclude.txt` で除外しています。大きく 3 種類です。

1. **ホスト固有の身元** — `/etc/hostname`, `/etc/machine-id`, `/etc/resolv.conf`,
   `/var/lib/tailscale`, SSH ホスト鍵。別のマシンに持ち込むと壊れます。
2. **認証情報** — `~/.git-credentials`, `~/.config/gh`, `~/.ssh`, `~/.aws`。
   これらはワークフローが毎回作り直すので、アーティファクトに残す理由がありません。
3. **無駄なもの** — `/proc`, `/sys`, `/dev`, `/tmp`, `/var/log`, apt の .deb
   キャッシュ、docker のストレージ（overlay の外に住んでいるので保存しても無意味）。

逆に `/var/lib/apt/lists` や pip / npm のキャッシュは**あえて残しています**。
毎回 `apt-get update` からやり直さないためです。

## コマンド

```
envsnap mount        スナップショットを復元して overlay をマウント
envsnap umount       アンマウント（何も削除しません）
envsnap activate     ログインシェルを overlay の中に向ける
envsnap deactivate   元に戻す
envsnap save         差分を保存して push
envsnap save --full  1 枚の full レイヤとして保存し直す
envsnap status       設定と保存済み世代の一覧
```

## 設定

| 変数 | 既定値 | 意味 |
|---|---|---|
| `ENVSNAP_NAME` | `default` | スナップショット名。Release のタグは `snap-<name>` |
| `ENVSNAP_STORE` | `ghrelease` | `ghrelease` または `rclone` |
| `ENVSNAP_GH_REPO` | `<owner>/gha-env-snapshots` | 保存先の非公開リポジトリ |
| `ENVSNAP_PASSPHRASE` | （なし） | 設定すると暗号化を有効化 |
| `ENVSNAP_CHUNK_MB` | `256` | チャンクサイズ |
| `ENVSNAP_JOBS` | `4` | 並列転送数 |
| `ENVSNAP_MAX_LAYERS` | `8` | この枚数に達したら full へ自動圧縮 |
| `ENVSNAP_ZSTD_LEVEL` | `6` | 圧縮レベル |
| `ENVSNAP_FORCE` | `0` | ランナーイメージ不一致でも復元を強行する |

必要なシークレット: `GH_PAT`（`repo` スコープ。保存先リポジトリへの読み書きに使用）、
`ENVSNAP_PASSPHRASE`。保存先リポジトリ名を変えたい場合は変数 `ENVSNAP_GH_REPO`。

## 制限と注意

- **ランナーイメージが更新されると復元しません。** 別のベース OS の上に他所の差分を
  貼ると壊れるためで、警告を出して空の overlay で起動します（`ENVSNAP_FORCE=1` で
  強行可能）。GitHub のイメージ更新は月 1 回程度なので、そのときだけ環境を作り直す
  ことになります。
- **`/tmp` はホストと共有です。** broker の停止指示（`/tmp/stop.txt`）が overlay の
  中から書かれても届くようにするためで、意図的な設計です。`/tmp` はスナップショット
  対象外です。
- **docker は overlay の外で動いています。** コンテナイメージは保存されません。
- Playwright はホスト側（＝ lower 層）に入れています。毎回同じものが手に入るので、
  差分に含める意味がないためです。
- Windows / macOS は対象外です。overlayfs が前提なので Linux 専用です。

## 開発時にやらかしたこと

`umount` に失敗しているのに気づかず `rm -rf` でクリーンアップして、**ホストの
`/dev` と `/run` を消し飛ばしました**。bind マウントが shared 伝播のままだったため、
`rm` が overlay を突き抜けて実体を消していったのが原因です。

そのため現在の実装では:

- `mount --bind` の直後に必ず `--make-rprivate` する
- overlay の中から `/mnt/envsnap` 自体が見えないよう tmpfs で蓋をする
- 各 `umount` の前に `mountpoint -q` で確認し、失敗を握り潰さない
- **`envsnap` はいかなる場合もディレクトリを `rm -rf` しない**

という 4 点を守っています。触るときも守ってください。
