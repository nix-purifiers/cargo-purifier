# SPDX-License-Identifier: MIT
"""Keep registry identities separate from same-name Git source replacements."""
import hashlib,json,os,shutil,sys,tomllib
from pathlib import Path
import tomlkit


def merge(inputs, output):
    output=Path(output);output.mkdir();base=Path(inputs[0]['vendor'])
    for child in base.iterdir():
        if child.name != '.cargo':(output/child.name).symlink_to(child)
    (output/'.cargo').mkdir()
    config=tomlkit.parse((base/'.cargo/config.toml').read_text())
    for table in config.get('source', {}).values():
        value=table.get('directory')
        if value=='cargo-vendor-dir':table['directory']='@vendor@'
        elif isinstance(value,str) and value.startswith('cargo-vendor-dir/'):
            table['directory']='@vendor@/'+value.removeprefix('cargo-vendor-dir/')
    registries={}
    for item in inputs:
        vendor=Path(item['vendor']);candidates={}
        for crate in vendor.iterdir():
            checksum=crate/'.cargo-checksum.json'
            if not checksum.is_file():continue
            digest=json.loads(checksum.read_text()).get('package')
            if not digest:continue  # Git bytes are never registry artifacts.
            package=tomllib.loads((crate/'Cargo.toml').read_text())['package']
            candidates[(package['name'],package['version'],digest)]=crate
        for package in tomllib.loads(Path(item['lock']).read_text()).get('package',[]):
            source=package.get('source','')
            if not source.startswith(('registry+','sparse+')):continue
            identity=(package['name'],package['version'],package.get('checksum'))
            if identity not in candidates:raise ValueError(f'no authentic registry artifact for {source} {identity}')
            directory=registries.setdefault(source,{})
            key=identity[:2]
            if key in directory and directory[key][0]!=identity[2]:
                raise ValueError(f'conflicting registry checksums for {source} {key}')
            directory[key]=(identity[2],candidates[identity])
    # An uncaptured registry stays empty even when the caller has a warm cache.
    registries.setdefault('registry+https://github.com/rust-lang/crates.io-index', {})
    for source,crates in registries.items():
        suffix=hashlib.sha256(source.encode()).hexdigest()
        relative='.package-registries/'+suffix;directory=output/relative;directory.mkdir(parents=True)
        for (name,version),(_,crate) in crates.items():(directory/(name+'-'+version)).symlink_to(crate)
        native='crates-io' if source=='registry+https://github.com/rust-lang/crates.io-index' else source
        replacement='package-registry-'+suffix
        config.setdefault('source',{}).setdefault(native,{})['replace-with']=replacement
        if native!='crates-io':config['source'][native]['registry']=source.removeprefix('registry+')
        config['source'][replacement]={'directory':'@vendor@/'+relative}
    (output/'.cargo/config.toml').write_text(tomlkit.dumps(config))


def source_directory(config, source, output):
    output=Path(output);visited=set();sources=config.get('source', {})
    while source not in visited:
        visited.add(source);table=sources.get(source, {})
        if 'directory' in table:
            value=table['directory']
            if value=='cargo-vendor-dir':target=output
            elif value.startswith('cargo-vendor-dir/'):target=output/value.removeprefix('cargo-vendor-dir/')
            else:target=Path(value.replace('@vendor@', str(output)))
            if not target.resolve().is_relative_to(output.resolve()):raise ValueError('vendor namespace escapes output')
            return target
        source=table.get('replace-with')
        if not source:break
    raise ValueError('no directory replacement for registry')


def merge_stdlib(project, stdlib, output):
    project,stdlib,output=map(Path,(project,stdlib,output))
    shutil.copytree(project,output,symlinks=True);output.chmod(0o755)
    config=tomlkit.parse((output/'.cargo/config.toml').read_text())
    target=source_directory(config,'crates-io',output);target.mkdir(parents=True,exist_ok=True);target.chmod(0o755)
    for crate in stdlib.iterdir():
        checksum=crate/'.cargo-checksum.json'
        if not checksum.is_file():continue
        new=json.loads(checksum.read_text()).get('package')
        if not new:raise ValueError('SDK registry artifact has no checksum: '+crate.name)
        destination=target/crate.name
        if destination.exists():
            old=json.loads((destination/'.cargo-checksum.json').read_text()).get('package')
            if old!=new:raise ValueError('conflicting project/SDK crate: '+crate.name)
        else:destination.symlink_to(crate)


if __name__=='__main__':
    if sys.argv[1]=='--stdlib':merge_stdlib(*sys.argv[2:])
    else:merge(json.loads(Path(sys.argv[1]).read_text()),sys.argv[2])
