# SPDX-License-Identifier: MIT
#
# refuses <name> { src, lockFile, expected }: a check that the lock in memory
# of a workspace has a problem that contains `expected`, or that reading it
# fails with that message. The check runs nix-instantiate in the sandbox, on
# a dummy store, because a parse error is one that builtins.tryEval cannot
# observe, and because the message is what a developer reads.
{ pkgs }:
name:
{
  # The workspace root, which holds its Cargo.lock.
  src,
  lockFile,
  expected,
}:
pkgs.runCommand name { nativeBuildInputs = [ pkgs.nix ]; } ''
  export HOME=$PWD NIX_STATE_DIR=$PWD/state NIX_LOG_DIR=$PWD/log NIX_CONF_DIR=$PWD
  expression='
    let
      lib = import ${pkgs.path}/lib;
      lock = import ${../lib-overlays/default}/read-lock.nix { inherit lib; } {
        src = ${src};
        lockFile = ${lockFile};
      };
    in
    if lock.problems == [ ] then true else throw (lib.concatStringsSep "\n" lock.problems)'
  if nix-instantiate --eval --strict --readonly-mode --store dummy:// --expr "$expression" 2>stderr; then
    echo "${name}: the lock has no problem" >&2
    exit 1
  fi
  if ! grep -qF -- ${pkgs.lib.escapeShellArg expected} stderr; then
    echo "${name}: the error does not say: "${pkgs.lib.escapeShellArg expected} >&2
    cat stderr >&2
    exit 1
  fi
  touch $out
''
