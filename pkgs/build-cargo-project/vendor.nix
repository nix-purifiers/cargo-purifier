# SPDX-License-Identifier: MIT
#
# The vendored sources of a workspace: every crate its Cargo.lock names,
# fetched as the entries of its lock in memory (read-lock.nix) say, in the
# layout of `cargo vendor`. The `.cargo/config.toml` of the result replaces
# each registry and git source with the directory `cargo-vendor-dir`, which
# nixpkgs' cargoSetupHook and the cargo wrapper point at the store path.
#
# nixpkgs importCargoLock vendors the crates from crates.io and from git,
# given a Cargo.lock that names only those, and the hash of each git
# checkout from its entry. The purifier vendors the crates from every other
# registry from their file entries, whose URLs follow the registry's `dl`
# template, since importCargoLock knows only templates without markers. The
# result holds the workspace's own Cargo.lock, which cargoSetupHook compares
# with the one in the build's source.
#
# A git checkout is fetched from the URL its entry records, which is the
# HTTPS form of an ssh:// dependency's URL, and the source replacement still
# names the source as Cargo.lock does.
{
  lib,
  rustPlatform,
  fetchurl,
  fetchgit,
  runCommand,
  # The workspace's Cargo.lock.
  cargoLock,
  # The entries of the workspace's lock in memory.
  entries,
  provided ? [ ],
}:
let
  inherit (builtins) toJSON;

  parsed = builtins.fromTOML (builtins.readFile cargoLock);

  cratesIo = "registry+https://github.com/rust-lang/crates.io-index";
  isRegistry = source: lib.hasPrefix "registry+" source || lib.hasPrefix "sparse+" source;
  isGit = source: lib.hasPrefix "git+" source;

  supplied =
    package: lib.any (entry: entry.name == package.name && entry.version == package.version) provided;
  sourced = lib.filter (package: package ? source && !(supplied package)) parsed.package;
  importedPackages = lib.filter (package: package.source == cratesIo || isGit package.source) sourced;

  # A git source in Cargo.lock ends with `#<commit>`.
  revOf = package: lib.last (lib.splitString "#" package.source);
  # The URL in a git source: git+<url>[?…]#<commit>.
  sourceUrlOf = package: lib.head (builtins.match "git\\+([^?#]+).*" package.source);

  gitEntryOf =
    package: lib.findFirst (entry: entry ? git && entry.git.rev == revOf package) null entries;

  gitPackages = lib.filter (package: isGit package.source) importedPackages;

  outputHashes = lib.listToAttrs (
    map (
      package: lib.nameValuePair "${package.name}-${package.version}" (gitEntryOf package).hash
    ) gitPackages
  );

  # Git sources whose checkout comes from another URL, as
  # { original, fetched } pairs.
  rewrites = lib.unique (
    lib.concatMap (
      package:
      let
        original = sourceUrlOf package;
        fetched = (gitEntryOf package).git.url;
      in
      lib.optional (original != fetched) { inherit original fetched; }
    ) gitPackages
  );

  # A package's source as importCargoLock sees it: with the URL a git
  # checkout is fetched from.
  importedSource =
    package:
    if isGit package.source then
      "git+" + (gitEntryOf package).git.url + lib.removePrefix "git+${sourceUrlOf package}" package.source
    else
      package.source;

  checksumOf =
    package:
    package.checksum
      or parsed.metadata."checksum ${package.name} ${package.version} (${package.source})";

  # A Cargo.lock with the fields importCargoLock reads, for the packages
  # it vendors. JSON strings are valid TOML basic strings. importCargoLock
  # needs the `package` key even when it vendors nothing.
  importedLock = lib.concatStrings (
    lib.optional (parsed ? version) "version = ${toString parsed.version}\n"
    ++ lib.optional (importedPackages == [ ]) "package = []\n"
    ++ map (
      package:
      ''

        [[package]]
        name = ${toJSON package.name}
        version = ${toJSON package.version}
        source = ${toJSON (importedSource package)}
      ''
      + lib.optionalString (package.source == cratesIo) ''
        checksum = ${toJSON (checksumOf package)}
      ''
    ) importedPackages
  );

  # The fetchers a consumer passes, such as a fetchurl for a mirror or a
  # credentialed fetchgit, reach crates.io and git downloads too.
  imported = (rustPlatform.importCargoLock.override { inherit fetchurl fetchgit; }) {
    lockFileContents = importedLock;
    inherit outputHashes;
  };

  registryEntries = lib.filter (
    entry: entry ? url && isRegistry entry.cargo.source && entry.cargo.source != cratesIo
  ) entries;

  registryCrate =
    entry:
    let
      tarball = fetchurl {
        name = "crate-${entry.cargo.name}-${entry.cargo.version}.tar.gz";
        urls = [ entry.url ] ++ entry.mirrors;
        inherit (entry) hash;
      };
      checksum = builtins.convertHash {
        inherit (entry) hash;
        toHashFormat = "base16";
      };
    in
    runCommand "${entry.cargo.name}-${entry.cargo.version}" { } ''
      mkdir $out
      tar xf ${tarball} -C $out --strip-components=1
      printf '{"files":{},"package":"%s"}' ${checksum} >$out/.cargo-checksum.json
    '';

  registrySources = lib.unique (map (entry: entry.cargo.source) registryEntries);

  # The `registry` value of a source replacement: the index URL, which
  # keeps the `sparse+` prefix and drops `registry+`.
  indexOf = lib.removePrefix "registry+";
in
runCommand "cargo-vendor-dir" { } ''
  mkdir -p $out/.cargo
  for entry in ${imported}/*; do
    ln -s "$entry" "$out/$(basename "$entry")"
  done
  rm $out/Cargo.lock
  ln -s ${cargoLock} $out/Cargo.lock
  cp ${imported}/.cargo/config.toml $out/.cargo/config.toml
  chmod u+w $out/.cargo/config.toml

  ${lib.concatMapStrings (rewrite: ''
    substituteInPlace $out/.cargo/config.toml \
      --replace-fail ${lib.escapeShellArg "\"git+${rewrite.fetched}"} ${lib.escapeShellArg "\"git+${rewrite.original}"} \
      --replace-fail ${lib.escapeShellArg "git = \"${rewrite.fetched}\""} ${lib.escapeShellArg "git = \"${rewrite.original}\""}
  '') rewrites}

  ${lib.concatMapStrings (entry: ''
    ln -s ${registryCrate entry} $out/${entry.path}
  '') registryEntries}

  ${lib.concatMapStrings (source: ''
    cat >>$out/.cargo/config.toml <<'EOF'

    [source.${toJSON source}]
    registry = ${toJSON (indexOf source)}
    replace-with = "vendored-sources"
    EOF
  '') registrySources}
''
