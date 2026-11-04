# SPDX-License-Identifier: MIT
{
  inputs.caisson.url = "github:nix-caisson/caisson";
  inputs.flake-parts.url = "github:hercules-ci/flake-parts";
  # Same channel as the main flake, so dev-partition evals agree with the
  # exported pkgSet.
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  inputs.treefmt-nix.url = "github:numtide/treefmt-nix";
  inputs.treefmt-nix.inputs.nixpkgs.follows = "nixpkgs";
  inputs.historical-rust = {
    url = "github:NixOS/nixpkgs/1db42b7fe3878f3f5f7a4f2dc210772fd080e205";
    flake = false;
  };
  # A real workspace the integration checks build: ripgrep 14.1.1.
  inputs.ripgrep.url = "github:BurntSushi/ripgrep/4649aa9700619f94cf9c66876e9549d83420e16c";
  inputs.ripgrep.flake = false;
  outputs = { ... }: { };
}
