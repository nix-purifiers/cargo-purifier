# SPDX-License-Identifier: MIT
#
# cargo with the purifier. In a workspace with a cargo-purifier.lock.json,
# the commands that read the dependencies' sources (build, check, run,
# test and the like) read them from the vendored sources Nix builds from
# the two lockfiles, offline. The vendored sources stay alive through a GC
# root at target/cargo-purifier/vendor, which `cargo clean` removes;
# `cargo-purifier prefetch` keeps them alive in target/cargo-purifier/prefetch
# too, with the tools.
#
# CARGO_PURIFIER_LOCKFILE names the purifier lockfile when it lives outside
# the workspace, and CARGO_PURIFIER_TOOL_LOCKFILE names a Cargo.lock kept
# outside the workspace, which the wrapper copies to the workspace root.
# Every other command runs plain cargo, and so does every
# command when the workspace has no purifier lockfile or CARGO_PURIFIER=0 is
# set. A command that would need a download the lockfiles do not record
# stops, and says which command fixes it: the lockfiles disagree, a manifest
# needs a dependency Cargo.lock lacks, or the vendored sources cannot be
# built offline.
{
  lib,
  path,
  stdenv,
  writeShellApplication,
  coreutils,
  diffutils,
  gnused,
  gnugrep,
  # nixpkgs' cargo and rustc, taken from `pkgs`: in this package scope
  # `cargo` names this wrapper.
  pkgs,
  # The `cargo-purifier` command.
  command,
}:
let
  inherit (pkgs) cargo rustc;
in
writeShellApplication {
  name = "cargo";
  runtimeInputs = [
    coreutils
    diffutils
    gnused
    gnugrep
  ];
  text = ''
    real=${lib.getExe cargo}
    # `cargo purifier` runs the cargo-purifier command, and cargo needs
    # rustc. rustc links with `cc`, and the package set's C compiler comes
    # last on the PATH, so a `cc` the user already has comes first.
    export PATH="${
      lib.makeBinPath [
        command
        rustc
      ]
    }:$PATH:${lib.makeBinPath [ stdenv.cc ]}"

    # The subcommand is the first argument that is neither an option nor
    # an option's value.
    sub=
    args=("$@")
    i=0
    while [ "$i" -lt "''${#args[@]}" ]; do
      case "''${args[$i]}" in
        --config | --color | -C | -Z | --explain) i=$((i + 2)) ;;
        -* | +*) i=$((i + 1)) ;;
        *)
          sub=''${args[$i]}
          break
          ;;
      esac
    done

    if [ "''${CARGO_PURIFIER:-1}" = 0 ]; then
      exec "$real" "$@"
    fi

    manifest=$(${lib.getExe command} _workspace-root 2>/dev/null) || {
      echo "cargo-purifier: warning: no cargo-purifier.lock.json found; running cargo impurely. Run \`cargo-purifier lock\` to use Nix-resolved dependencies." >&2
      exec "$real" "$@"
    }
    root=''${manifest%/Cargo.toml}
    lock=''${CARGO_PURIFIER_LOCKFILE:-$root/cargo-purifier.lock.json}
    if [ ! -e "$lock" ]; then
      echo "cargo-purifier: warning: no cargo-purifier.lock.json found; running cargo impurely. Run \`cargo-purifier lock\` to use Nix-resolved dependencies." >&2
      exec "$real" "$@"
    fi
    # cargo reads Cargo.lock only from the workspace root, so a Cargo.lock
    # kept outside the tree is copied there.
    tool_lock=''${CARGO_PURIFIER_TOOL_LOCKFILE:-}
    if [ -n "$tool_lock" ] && [ -f "$tool_lock" ] && ! cmp -s "$tool_lock" "$root/Cargo.lock"; then
      cp "$tool_lock" "$root/Cargo.lock"
      chmod u+w "$root/Cargo.lock"
    fi
    if [ ! -f "$root/Cargo.lock" ]; then
      echo "cargo-purifier: Cargo.lock is missing beside an existing purifier lock; run cargo-purifier lock" >&2
      exit 101
    fi
    if grep -q '"projectInputs"' "$lock"; then
      exec ${lib.getExe command} _cargo --root "$root" \
        --native-cargo "$real" --subcommand "$sub" -- "$@"
    fi
    case "$sub" in
      build | b | check | c | run | r | test | t | bench | doc | d | clippy | rustc | rustdoc | tree | metadata | package | fix) ;;
      *) exec "$real" "$@" ;;
    esac

    lock=$(realpath "$lock")

    # stop <message>: the purifier refuses the command.
    stop() {
      echo "cargo-purifier: $1" >&2
      echo "cargo-purifier: CARGO_PURIFIER=0 runs plain cargo, which downloads what it needs." >&2
      exit 101
    }

    state=$root/target/cargo-purifier
    mkdir -p "$state"
    stamp=$(cat "$lock" "$root/Cargo.lock" | sha256sum)
    if [ -e "$state/vendor" ] && [ "$(cat "$state/stamp" 2>/dev/null)" = "$stamp" ]; then
      vendor=$(readlink -f "$state/vendor")
    elif vendor=$(nix-build ${../../nix/lib}/tool-vendor.nix \
      --arg nixpkgs ${path} \
      --argstr system ${stdenv.hostPlatform.system} \
      --arg src "/. + \"$root\"" \
      --arg lockFile "/. + \"$lock\"" \
      --out-link "$state/vendor" 2>"$state/nix-build.log"); then
      printf '%s\n' "$stamp" >"$state/stamp"
    else
      # Nix prints the error that ends evaluation or the build last.
      reason=$(grep 'error: ' "$state/nix-build.log" | tail -n 1 | sed 's/^ *//') || true
      stop "the locked sources did not build: ''${reason:-see $state/nix-build.log}. Run \`cargo-purifier lock\` if Cargo.lock changed, or \`cargo-purifier prefetch\` while online before working offline."
    fi

    sed "s|^directory = \"cargo-vendor-dir\"\$|directory = \"$vendor\"|" \
      "$vendor/.cargo/config.toml" >"$state/config.toml"

    # A manifest that needs a dependency Cargo.lock lacks cannot resolve
    # from the vendored sources: cargo finds no such package there, or wants
    # to update Cargo.lock.
    if ! "$real" --config "$state/config.toml" metadata --offline --locked --format-version 1 \
      >/dev/null 2>"$state/metadata.log" &&
      grep -q -e 'needs to be updated' -e 'no matching package named' "$state/metadata.log"; then
      stop "Cargo.toml needs dependencies that Cargo.lock lacks. Run \`cargo-purifier lock\` to lock them."
    fi

    exec "$real" --config "$state/config.toml" --offline "$@"
  '';
}
