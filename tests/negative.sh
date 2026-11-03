#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Downloads whose hash the lockfiles get wrong. Each case is a copy of the
# workspace fixture whose lockfiles are consistent with each other, with
# one recorded hash changed, so only fetching the download shows the
# mismatch: a fixed-output hash mismatch, which the flake's checks cannot
# observe. The flake's conformance checks cover the lockfiles evaluation
# refuses.
#
# Run it from the repository root: tests/negative.sh
set -euo pipefail
# The checkout stays as it was, without Python bytecode.
export PYTHONDONTWRITEBYTECODE=1

repo=$PWD
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

failures=0

# expect_failure <case> <text the error must contain>
expect_failure() {
  local case=$1 expected=$2
  local expression="
    let
      flake = builtins.getFlake \"git+file://$repo\";
      pkgs = import flake.inputs.nixpkgs {
        system = builtins.currentSystem;
        overlays = [ flake.overlays.default ];
      };
    in
    (pkgs.cargo-purifier.buildCargoProject { src = $work/$case; }).packages.hello"
  if nix build --impure --no-link --expr "$expression" 2>"$work/$case.log"; then
    echo "FAIL $case: the build succeeded" >&2
    failures=$((failures + 1))
  elif ! grep -qF -- "$expected" "$work/$case.log"; then
    echo "FAIL $case: the error does not say: $expected" >&2
    cat "$work/$case.log" >&2
    failures=$((failures + 1))
  else
    echo "ok   $case: $(grep -F -- "$expected" "$work/$case.log" | head -n 1 | sed 's/^ *//')"
  fi
}

# new_case <case>: a fresh copy of the workspace fixture.
new_case() {
  cp -r "$repo/tests/fixtures/workspace" "$work/$1"
  chmod -R u+w "$work/$1"
}

# relock <case> <python statement on `lock`>: changes the purifier lockfile,
# and writes the hashes `cargo-purifier lock` would write for the result.
relock() {
  python3 - "$work/$1" "$2" <<'EOF'
import json, sys
from pathlib import Path
sys.path.insert(0, "pkgs/cargo-purifier")
import cargo_purifier
root = Path(sys.argv[1])
path = root / "cargo-purifier.lock.json"
lock = json.loads(path.read_text())
exec(sys.argv[2])
lock["cargoLock"] = cargo_purifier.sri_sha256((root / "Cargo.lock").read_bytes())
lock["contentHash"] = cargo_purifier.content_hash(lock)
path.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")
EOF
}

new_case tampered-git-hash
relock tampered-git-hash 'lock["git"][0]["hash"] = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="'
expect_failure tampered-git-hash 'hash mismatch'

new_case tampered-checksum
# Through a copy: macOS's sed -i takes a backup suffix.
sed 's/^checksum = "8f/checksum = "00/' "$work/tampered-checksum/Cargo.lock" >"$work/Cargo.lock"
mv "$work/Cargo.lock" "$work/tampered-checksum/Cargo.lock"
relock tampered-checksum 'pass'
expect_failure tampered-checksum 'hash mismatch'

if [ "$failures" -ne 0 ]; then
  echo "$failures case(s) failed" >&2
  exit 1
fi
