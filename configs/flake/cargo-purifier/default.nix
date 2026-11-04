# SPDX-License-Identifier: MIT
{ closure-lib, ... }:
{
  config,
  inputs,
  lib,
  ...
}:
{

  imports = [
    inputs.flake-parts.flakeModules.partitions
    closure-lib.caisson.modules.flake."purifier/purifier-packages"
  ];

  purifier.scope = "cargo-purifier";

  debug = false;
  caisson = {
    libOverlays.exported = libOverlays: {
      inherit (libOverlays) default;
    };

    # The scope is the `default` entry of this flake's package overlay
    # registry (pkg-overlays/): the package set applies it by default, and
    # the flake exports it as `pkgOverlays` and as the plain
    # `overlays.default`. The `purifier-packages` module publishes the
    # derivations of the scope as `packages`.
  };

  # `nix run github:nix-purifiers/cargo-purifier -- <command>` runs the
  # `cargo-purifier` command: lock, prefetch or publish.
  perSystem =
    { config, ... }:
    {
      packages.default = config.packages.cargo-purifier;
    };

  partitionedAttrs.checks = "checks";
  partitionedAttrs.formatter = "formatter";

  partitions.formatter = {
    extraInputs = (lib.caisson.pins.flake-compat ../../../tests/dependencies).sources;
    module =
      { inputs, ... }:
      {
        imports = [ inputs.treefmt-nix.flakeModule ];
        perSystem.treefmt.programs.nixfmt.enable = true;
      };
  };

  partitions.checks = {
    extraInputs = (lib.caisson.pins.flake-compat ../../../tests/dependencies).sources;
    module =
      { inputs, self, ... }:
      {
        imports = [ inputs.treefmt-nix.flakeModule ];
        perSystem =
          { pkgs, system, ... }:
          {
            checks = import ../../../tests {
              inherit pkgs;
              inherit ((pkgs.extend self.overlays.default).cargo-purifier) buildCargoProject;
              inherit (self.lib.cargo-purifier) readLock;
              inherit (self.packages.${system}) cargo-purifier;
              inherit (inputs) ripgrep;
              historicalRustSource = inputs.historical-rust;
            };
            treefmt.programs.nixfmt.enable = true;
          };
      };
  };

}
