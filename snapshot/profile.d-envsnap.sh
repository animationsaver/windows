# envsnap -> /etc/profile.d/envsnap.sh
# shellcheck shell=bash
#
# ログインシェル差し替え (envsnap-shell) が効かない経路のための第 2 のフック。
#
# Tailscale SSH は接続を確立した時点で --login-shell を解決して保持する。
# broker は SSH 接続を使い回すので、chsh より前に張られた接続はその後も
# ずっと /bin/bash を起動し続ける (実測して確認済み)。
# 一方で broker が送るのは `bash -lc '<command>'` なので、その内側の
# ログインシェルは必ず /etc/profile.d/*.sh を読む。ここで入れば確実。
#
# 万一壊れても SSH できなくならないよう、条件を全部満たしたときだけ入る。

[ -n "${BASH_VERSION:-}" ] || return 0          # dash に読まれても壊れないように
[ -z "${ENVSNAP_INSIDE:-}" ] || return 0        # すでに中
[ -f /run/envsnap/active ] || return 0          # activate されていない
[ -x /usr/local/sbin/envsnap-enter ] || return 0
mountpoint -q "${ENVSNAP_ROOT:-/mnt/envsnap}/merged" 2>/dev/null || return 0

__envsnap_enter() {
  if [ "$(id -u)" = 0 ]; then
    # root のとき sudo は介さない。envsnap-enter は SUDO_* で入る先の uid を決める。
    exec env SUDO_UID=0 SUDO_GID=0 SUDO_USER=root /usr/local/sbin/envsnap-enter "$@"
  fi
  exec sudo -n /usr/local/sbin/envsnap-enter "$@"
}

case "${BASH_EXECUTION_STRING:-}" in
  *'#!host'*)
    # 逃げ道: コマンドのどこかに '#!host' を入れると overlay に入らずホストで実行する。
    # broker は bash -lc '<command>' の形で送ってくるので前方一致では拾えないし、
    # 残したままにすると bash が以降をコメント扱いして無言で何もしないので取り除く。
    exec /bin/bash -c "${BASH_EXECUTION_STRING/'#!host'/}"
    ;;
  ?*)
    __envsnap_enter /bin/bash -lc "$BASH_EXECUTION_STRING"
    ;;
  *)
    # 対話ログインのときだけ。非対話でコマンドも無い場合は何もしない。
    if [ -t 0 ]; then
      __envsnap_enter /bin/bash -l
    fi
    ;;
esac
