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

broker が送ってくるのは `bash -lc '<command>'` なので、マーカーを消したあとに内側の
ログインシェルが `/etc/profile.d/envsnap.sh` を読み直す。逃げたことを `ENVSNAP_HOST=1`
として環境変数で渡しているのはそのためで、これが無いと hook が親切に overlay へ入れ直し、
`#!host` が無言で効かなくなる。

`sudo` は環境変数を捨てるので、`#!host` の中では `sudo -n envsnap save` のようにプログラムを
直接呼ぶこと。`sudo -n bash -lc ...` にすると overlay に戻ってしまう。

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

2GiB 超えは実測しました。2600MiB の非圧縮データ（乱数）を含む upper を保存すると
256MiB × 11 チャンク＋マニフェストの計 12 アセット・合計 2.54GiB になり、最大アセットは
256MiB です。空の upper へ復元して md5 まで一致します。

境界の扱いは 2 方向に用意してあります。

- `ENVSNAP_CHUNK_MB` を 2GiB 以上にすると、圧縮し終わってからアップロードで落ちます。
  `ASSET_MAX_MB`（1900MiB）で頭を打たせ、警告を出して続行します。設定ミスで
  スナップショットを失うより、少し細かく分けるほうがましです。
- 逆に小さすぎると 1000 アセットに収まりません。**分割前に**レイヤサイズから必要な
  チャンク数を計算し、999 を超えるならチャンクサイズを自動で引き上げます（2600MiB を
  1MiB 指定で保存 → 3MiB に引き上げて 867 チャンク）。`split -a 3` は 1000 個目で
  `output file suffixes exhausted` と言って死ぬだけで何を直せばよいか分からないので、
  そこへ到達させません。1900MiB でも足りない（≒1.8TB 超）ときだけエラーにします。

分割は速度のためでもあります（実測）:

| 処理 | 単一ストリーム | 4 並列 × 256MiB |
|---|---|---|
| Release へのアップロード | 16 MiB/s | **52 MiB/s** |
| Release からのダウンロード | 30–134 MiB/s | **101 MiB/s** |

ただし小さくしすぎると 1 アセットごとの往復が支配的になります。同じ 2600MiB を
16MiB × 163 チャンクで保存すると 3 分 49 秒かかり、256MiB × 11 チャンク（80 秒）の
約 3 倍です。既定の 256MiB は往復回数と 2GiB の壁の間を取った値です。

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
- broker の `exec_start` / `exec_poll` はジョブを `/tmp/gha-job-<id>` に置くので、
  overlay の中から起動しても、ポーリングがどこから来ても同じファイルを見ます。
  ついでにスナップショットにも入りません。
- **docker は overlay の外で動いています。** コンテナイメージは保存されません。
- Playwright はホスト側（＝ lower 層）に入れています。毎回同じものが手に入るので、
  差分に含める意味がないためです。
- Windows / macOS は対象外です。overlayfs が前提なので Linux 専用です。

## 実測値

GitHub Actions の ubuntu-24.04 ランナ（4 コア / 15 GiB / 145 GB ディスク）で、
42 MiB（69 エントリ、うち 20 MB は乱数ファイル）のレイヤで測った値です。

| 操作 | 時間 |
| --- | --- |
| full 保存（圧縮・暗号化・アップロード込み） | 9.4 s |
| 差分保存 | 7.0 s |
| 復元（ダウンロード・展開込み） | 4.4 s |
| **別ホストでの復元** | **4.8 s** |
| 自動 full 化（レイヤ 2 枚 → 1 枚） | 14 s |

2.6 GiB（2600 MiB の乱数ファイルを含む upper）でも測りました。

| 操作 | 256MiB × 11 チャンク | 16MiB × 163 チャンク |
| --- | --- | --- |
| full 保存 | **80 s** | 3 m 49 s |
| 空の upper への復元（md5 一致） | **29 s** | 1 m 39 s |
| リリースのアセット数 | 12 | 164 |

チャンクを小さくしても往復回数が増えるだけで速くなりません。既定の 256MiB のままで、
2.6 GiB のスナップショットが 80 秒で保存でき、29 秒で別の環境に展開できます。

内訳は暗号化 576 MiB/s・復号 568 MiB/s、zstd -6 が 176 MiB/s（圧縮率 4.05 倍）、
展開が 897 MiB/s。Release へのアップロードは単発 16 MiB/s に対し 256 MiB × 4 並列で
52 MiB/s、ダウンロードは並列で 101 MiB/s。律速はネットワークでなく zstd です。

「別ホストでの復元」は、ホスト A で `apt-get install sl` した結果と 20 MB の
ファイルと `/root/marker2.txt` がホスト B にそのまま現れ、`/root/.ssh/id_test` は
除外され、削除したファイルは削除されたままだったことまで確認しています。

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

もうひとつ。`envsnap-enter` が「overlay が mount 済み」だけでなく「`active`（ログイン
シェルのフックが有効）」まで条件にしていたため、`envsnap deactivate` の後は
`envsnap-enter` が**黄って素の `exec` に化け**、中に書いたつもりのファイルがホストの
`/` に落ちていました。コンパクションの検証中に、保存したはずのファイルが別ホストの
復元結果に無いという形で発覚しました。`active` はログインシェルを曲げるかどうかの
フラグであって、明示的な `envsnap-enter` の条件にしてはいけない、が教訓です。今は
mount の有無だけで判断し、入れないときは stderr に警告を出します。
**「安全のための no-op」は、黄ってやると安全ではありません。**
- `#!host` を作ったのに 2 つの hook が互いを打ち消していた。envsnap-shell がマーカーを削って
  ホストで実行し、その内側の `bash -lc` が profile.d を読んで overlay に入り直していた。
  broker 越しに `#!host findmnt -no FSTYPE,SOURCE /` が `ext4 /dev/root` ではなく
  `overlay envsnap` と答えて気づいた。`ENVSNAP_HOST=1` を渡して解決。
- warm 環境の起動が `packages.microsoft.com` の 403 Forbidden で死んだ。`apt-get update` が
  exit 100 で終わると envsnap を入れる前に job ごと落ちるので、要らない apt list を消して
  index 更新の失敗は致命扱いしないようにした。
