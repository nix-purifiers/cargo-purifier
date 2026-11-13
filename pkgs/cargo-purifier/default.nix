# SPDX-License-Identifier: MIT
#
# The `cargo-purifier` command: `lock` writes cargo-purifier.lock.json,
# `prefetch` builds what offline work needs, and `publish` uploads the
# .crate Nix packages. cargo also runs it as `cargo purifier <command>`. It
# runs the project's installed Cargo; SDKs are chosen by the caller.
{
  lib,
  path,
  stdenv,
  stdenvNoCC,
  makeWrapper,
  python3,
  git,
  nix-prefetch-git,
  cacert,
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
  python = python3.withPackages (packages: [ packages.tomlkit ]);
in
stdenvNoCC.mkDerivation {
  pname = "cargo-purifier-command";
  version = "0.1.0";
  src = ./cargo_purifier.py;
  dontUnpack = true;
  nativeBuildInputs = [ makeWrapper ];
  installPhase = ''
    runHook preInstall
    install -Dm644 $src $out/libexec/cargo-purifier/cargo_purifier.py
    makeWrapper ${lib.getExe python} $out/bin/cargo-purifier \
      --add-flags $out/libexec/cargo-purifier/cargo_purifier.py \
      --set CARGO_PURIFIER_TOOLS_DIR ${../../tools} \
      --set CARGO_PURIFIER_SHARED_SOURCE ${lib.cargo-purifier.sharedLibrarySource} \
      --prefix PYTHONPATH : ${lib.purifier.projectInputsTool} \
      --prefix PATH : ${
        lib.makeBinPath [
          nix-prefetch-git
          git
        ]
      } \
      --set CARGO_PURIFIER_NIX_LIB ${runtimeSource}/pkgs/cargo-purifier \
      --set CARGO_PURIFIER_NIXPKGS ${path} \
      --set CARGO_PURIFIER_SYSTEM ${stdenv.hostPlatform.system} \
      --set-default SSL_CERT_FILE ${cacert}/etc/ssl/certs/ca-bundle.crt
    runHook postInstall
  '';
  meta.mainProgram = "cargo-purifier";
}
