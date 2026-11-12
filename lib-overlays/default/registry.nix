# SPDX-License-Identifier: MIT
#
# Download URLs of crates from a cargo registry, as cargo computes them
# from the `dl` field of the registry's index configuration
# (https://doc.rust-lang.org/cargo/reference/registry-index.html#index-configuration).
{ lib }:
let
  markers = [
    "{crate}"
    "{version}"
    "{prefix}"
    "{lowerprefix}"
    "{sha256-checksum}"
  ];

  # The directory of a crate in an index: `1`, `2`, `3/a` or `ab/cd`.
  prefixOf =
    name:
    let
      length = lib.stringLength name;
    in
    if length == 1 then
      "1"
    else if length == 2 then
      "2"
    else if length == 3 then
      "3/${lib.substring 0 1 name}"
    else
      "${lib.substring 0 2 name}/${lib.substring 2 2 name}";
in
{
  inherit prefixOf;

  # The URL of a crate's .crate file. A `dl` without markers gets
  # `/{crate}/{version}/download` appended, as cargo does.
  downloadUrl =
    {
      dl,
      name,
      version,
      checksum,
    }:
    let
      template =
        if lib.any (marker: lib.hasInfix marker dl) markers then dl else "${dl}/{crate}/{version}/download";
      prefix = prefixOf name;
    in
    lib.replaceStrings markers [
      name
      version
      prefix
      (lib.toLower prefix)
      checksum
    ] template;
}
