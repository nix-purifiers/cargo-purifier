# SPDX-License-Identifier: MIT
#
# buildCargoProject, the builder of the purifier contract, as a package for
# callPackage: a cargo workspace, built in the sandbox from its Cargo.lock
# and the cargo-purifier.lock.json that `cargo-purifier lock` wrote. The
# flake's overlays.default adds the scope `pkgs.cargo-purifier`, which holds
# it, so a package takes the scope by name:
#
#   { cargo-purifier }:
#   (cargo-purifier.buildCargoProject { src = ./.; }).packages.<package>
#
# It takes the packages it builds with by name from the scope, so
# `cargo-purifier.overrideScope` replaces what the scope shares, such as
# `rustPlatform` for another Rust toolchain, and
# `buildCargoProject.override` replaces them for the builder alone,
# the purifier library's functions included. It reads the
# lockfiles with readLock (read-lock.nix), fails evaluation through the
# purifier library's checkLock when they cannot be used, and builds the
# downloads that entriesFor gives for the host system. project.nix
# documents the result.
{
  lib,
  stdenv,
  runCommand,
  rustPlatform,
  fetchurl,
  fetchgit,
  # lib.purifier, the purifier library: checkLock and entriesFor.
  purifier,
  python3,
  # lib.cargo-purifier.readLock.
  readLock,
}:
{
  # The source tree.
  src,
  dependencies ? null,
  # The workspace root inside `src`: the directory of the workspace's
  # Cargo.toml and Cargo.lock, when `src` also holds path dependencies
  # outside the workspace.
  workspace ? ".",
  # cargo-purifier.lock.json. A file outside `src` lets a packaging flake
  # build a workspace its author does not change.
  lockFile ? (if workspace == "." then src else src + "/${workspace}") + "/cargo-purifier.lock.json",
  # Cargo.lock: the copy in the workspace, or one kept outside `src`, which
  # the builds use in place of any copy in `src`, for a workspace that does
  # not commit its own.
  cargoLock ? (if workspace == "." then src else src + "/${workspace}") + "/Cargo.lock",
  # Fetchers in place of nixpkgs' own, as the purifier library's fetchEntry
  # takes them: `fetchurl` for registry downloads behind a mirror or
  # credentials, and `fetchgit` for private git repositories.
  fetchers ? { },
  # Runs `cargo test --package <package>` in each package's build too.
  doCheck ? false,
  # Which files of a path `src` the builds see.
  srcFilter ? null,
}:
let
  lock = purifier.checkLock (readLock {
    inherit
      src
      workspace
      lockFile
      cargoLock
      ;
  });
in
import ./project.nix (
  {
    inherit
      lib
      stdenv
      runCommand
      rustPlatform
      purifier
      python3
      src
      workspace
      cargoLock
      lock
      doCheck
      ;
    dependencies =
      if dependencies != null then
        dependencies
      else
        purifier.lockedProjectInputs {
          inputs = lock.projectInputs or [ ];
          system = stdenv.hostPlatform.system;
        };
    fetchurl = fetchers.fetchurl or fetchurl;
    fetchgit = fetchers.fetchgit or fetchgit;
    entries = purifier.entriesFor stdenv.hostPlatform.system lock;
  }
  // lib.optionalAttrs (srcFilter != null) { inherit srcFilter; }
)
