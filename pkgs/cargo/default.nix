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
  # The `cargo-purifier` command.
  command,
}:
let
  runtimeSource = lib.fileset.toSource {
    root = ../..;
    fileset = lib.fileset.unions [
      ../../lib-overlays/default
      ../build-cargo-project
      ../cargo-purifier
    ];
  };
in
writeShellApplication {
  name = "cargo";
  text = ''
    # Retain the caller's plugin search path before adding wrapper utilities.
    export CARGO_PURIFIER_CALLER_PATH="$PATH"
    export PATH="${
      lib.makeBinPath [
        coreutils
        diffutils
        gnused
        gnugrep
      ]
    }:$PATH"
    # cargo-purifier native-cargo wrapper
    # Rustup's leading +toolchain takes precedence over directory overrides.
    # Resolve in that context, including when the default SDK is unavailable.
    case "''${1:-}" in
      +*) export RUSTUP_TOOLCHAIN="''${1#+}" ;;
    esac
    real=$(${lib.getExe command} _native-cargo)
    export CARGO_PURIFIER_REAL_CARGO="$real"
    export PATH="${lib.makeBinPath [ command ]}:$PATH"

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

    # An editor's redirected standard-library graph belongs to the installed
    # SDK. The surrounding project's external lock does not govern it.
    if [ "$sub" = metadata ] && [ -n "''${CARGO_RESOLVER_LOCKFILE_PATH:-}" ] && \
      ${lib.getExe command} _runtime-metadata "$sub" "$@"; then
      exec "$real" "$@"
    fi

    # Resolve the workspace Cargo was asked to operate on, including calls
    # made from a different project. Arguments after -- belong to the program
    # or test harness and must not select the Cargo workspace.
    workspace_args=()
    i=0
    while [ "$i" -lt "''${#args[@]}" ]; do
      case "''${args[$i]}" in
        --) break ;;
        --manifest-path)
          if [ "$((i + 1))" -lt "''${#args[@]}" ]; then
            workspace_args=("''${args[$((i + 1))]}")
          fi
          i=$((i + 2))
          ;;
        --manifest-path=*)
          workspace_args=("''${args[$i]#--manifest-path=}")
          i=$((i + 1))
          ;;
        --config | --color | -C | -Z | --explain) i=$((i + 2)) ;;
        *) i=$((i + 1)) ;;
      esac
    done
    # Bun and other build-std planners query the selected SDK's manifest
    # from the project directory. It shares the prepared dependency union.
    if ${lib.getExe command} _stdlib-metadata "$sub" "$@"; then
      workspace_args=()
    fi
    manifest=$(${lib.getExe command} _workspace-root "''${workspace_args[@]}" 2>/dev/null) || {
      echo "cargo-purifier: warning: no cargo-purifier.lock.json found; running cargo impurely. Run \`cargo-purifier lock\` to use Nix-resolved dependencies." >&2
      exec "$real" "$@"
    }
    root=''${manifest%/Cargo.toml}
    lock=''${CARGO_PURIFIER_LOCKFILE:-$root/cargo-purifier.lock.json}
    if [ ! -e "$lock" ]; then
      echo "cargo-purifier: warning: no cargo-purifier.lock.json found; running cargo impurely. Run \`cargo-purifier lock\` to use Nix-resolved dependencies." >&2
      exec "$real" "$@"
    fi
    if grep -q '"projectInputs"' "$lock"; then
      exec ${lib.getExe command} _cargo --root "$root" \
        --native-cargo "$real" --subcommand "$sub" -- "$@"
    fi
    # Legacy full-name reads retain their existing path. Aliases and external
    # commands need native dispatch classification before any lockfile copy.
    case "$sub" in
      build | check | run | test | bench | doc | clippy | rustc | rustdoc | tree | metadata | package | fix) ;;
      *) exec ${lib.getExe command} _cargo --root "$root" \
        --native-cargo "$real" --subcommand "$sub" -- "$@" ;;
    esac
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
    lock=$(realpath "$lock")

    # stop <message>: the purifier refuses the command.
    stop() {
      echo "cargo-purifier: $1" >&2
      echo "cargo-purifier: CARGO_PURIFIER=0 runs plain cargo, which downloads what it needs." >&2
      exit 101
    }

    state=$root/target/cargo-purifier
    mkdir -p "$state"
    stdlib_args=()
    stdlib_lock=$(${lib.getExe command} _stdlib-lock "$@")
    if [ -n "$stdlib_lock" ]; then
      stdlib_args=(--argstr stdlibLock "$stdlib_lock")
    fi
    stamp=$(${lib.getExe command} _vendor-stamp "$root/Cargo.lock" "$lock" "$@")
    if [ -e "$state/vendor" ] && [ "$(cat "$state/stamp" 2>/dev/null)" = "$stamp" ]; then
      vendor=$(readlink -f "$state/vendor")
    elif vendor=$(nix-build ${runtimeSource}/pkgs/cargo-purifier/tool-vendor.nix \
      --arg nixpkgs ${path} \
      --argstr system ${stdenv.hostPlatform.system} \
      --arg src "/. + \"$root\"" \
      --arg lockFile "/. + \"$lock\"" \
      "''${stdlib_args[@]}" \
      --out-link "$state/vendor" 2>"$state/nix-build.log"); then
      printf '%s\n' "$stamp" >"$state/stamp"
    else
      # Nix prints the error that ends evaluation or the build last.
      reason=$(grep 'error: ' "$state/nix-build.log" | tail -n 1 | sed 's/^ *//') || true
      stop "the locked sources did not build: ''${reason:-see $state/nix-build.log}. Run \`cargo-purifier lock\` if Cargo.lock changed, or \`cargo-purifier prefetch\` while online before working offline."
    fi

    # Some native build scripts generate files beside their own sources
    # (Tauri permissions, for example). Use a writable private materialization;
    # the vendor symlink above remains the immutable Nix store/GC root.
    vendor=$(${lib.getExe command} _writable-vendor "$state" "$vendor")

    # rust-analyzer can run metadata and check concurrently. Never truncate
    # the configuration another Cargo process is reading: an empty config
    # silently disables vendoring and makes its offline resolution fail.
    config=$(mktemp "$state/config.XXXXXX")
    trap 'rm -f "$config"' EXIT
    sed "s|^directory = \"cargo-vendor-dir\"\$|directory = \"$vendor\"|" \
      "$vendor/.cargo/config.toml" >"$config"
    mv -f "$config" "$state/config.toml"
    trap - EXIT

    # A manifest that needs a dependency Cargo.lock lacks cannot resolve
    # from the vendored sources: cargo finds no such package there, or wants
    # to update Cargo.lock.
    if ! "$real" --config "$state/config.toml" metadata --manifest-path "$manifest" --offline --locked --format-version 1 \
      >/dev/null 2>"$state/metadata.log" &&
      grep -q -e 'needs to be updated' -e 'no matching package named' "$state/metadata.log"; then
      stop "Cargo.toml needs dependencies that Cargo.lock lacks. Run \`cargo-purifier lock\` to lock them."
    fi

    # Native Cargo children (for example cbindgen metadata) do not inherit
    # --config. Keep the caller's home unchanged and share the locked sources
    # through a private home with the original settings and cache links.
    export CARGO_HOME
    CARGO_HOME=$(${lib.getExe command} _inherited-cargo-home "$state" "$state/config.toml")
    export CARGO_NET_OFFLINE=true
    exec "$real" --config "$state/config.toml" --offline "$@"
  '';
}
