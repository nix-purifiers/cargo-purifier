# SPDX-License-Identifier: MIT
"""Attest exceptional native publication metadata against captured archives."""
import hashlib, json, tarfile, tomllib
from pathlib import Path, PurePosixPath

def digest(data):return hashlib.sha256(data).hexdigest()

def prepare(manifests, captures):
    adjustments={}
    for capture in captures:
        archive=Path(capture['archive']);data=archive.read_bytes()
        if digest(data)!=capture['checksum']:raise ValueError('corrupt publication source archive')
        with tarfile.open(archive) as tar:
            manifests_in_archive=[m for m in tar if m.isfile() and m.name.count('/')==1 and m.name.endswith('/Cargo.toml')]
            if len(manifests_in_archive)!=1:raise ValueError('publication archive must have one native manifest')
            native_manifest=tar.extractfile(manifests_in_archive[0]).read()
            native=tomllib.loads(native_manifest.decode());package=native['package'];prefix=package['name']+'-'+package['version']
            readme=package.get('readme')
            if not isinstance(readme,str):raise ValueError('publication exception requires an explicit native readme')
            logical=PurePosixPath(readme)
            if logical.is_absolute() or '..' in logical.parts:raise ValueError('publication readme is outside archive')
            members={}
            for member in tar.getmembers():
                parts=PurePosixPath(member.name).parts
                if not parts or parts[0]!=prefix or '..' in parts or member.name.startswith('/'):
                    raise ValueError('invalid publication archive member')
                if member.isfile():members[PurePosixPath(*parts[1:]).as_posix()]=tar.extractfile(member).read()
                elif not member.isdir():raise ValueError('unsupported publication archive link')
            if logical.as_posix() in members:raise ValueError('original publication contains readme; preserve its bytes')
            matches=[]
            for path in manifests:
                document=tomllib.loads(path.read_text())
                selected=document.get('package',{})
                if (selected.get('name'),selected.get('version'))==(package['name'],package['version']):matches.append(path)
            if len(matches)!=1:raise ValueError('publication source has no unique captured manifest')
            manifest=matches[0];root=manifest.parent
            checksum_path=root/'.cargo-checksum.json'
            if not checksum_path.is_file() or json.loads(checksum_path.read_text()).get('package')!=capture['checksum']:
                raise ValueError('publication archive differs from captured registry checksum')
            for name,original in members.items():
                selected=root/name
                if not selected.is_file() or selected.read_bytes()!=original:
                    raise ValueError('captured publication source differs from original archive: '+name)
            extra={str(path.relative_to(root)) for path in root.rglob('*') if path.is_file()}-set(members)-{'.cargo-checksum.json','.cargo-ok'}
            if extra:raise ValueError('captured publication source added files: '+', '.join(sorted(extra)))
            if (root/logical).exists():raise ValueError('captured source added readme absent from original archive')
            if manifest in adjustments:raise ValueError('duplicate publication source exception')
            adjustments[manifest]={'field':'package.readme','omitted':readme,'reason':'declared optional readme absent in captured original publication archive','sourceArchive':str(archive),'sourceArchiveSha256':capture['checksum'],'sourceManifestSha256':digest(native_manifest),'sourceFileHashes':{name:digest(value) for name,value in sorted(members.items())}}
    return adjustments

def omit(document, manifest, adjustments):
    adjustment=adjustments.get(manifest)
    if adjustment is None:return
    if document.get('package',{}).get('readme')!=adjustment['omitted']:
        raise ValueError('publication readme differs from attested source')
    before=document.as_string().encode()
    del document['package']['readme']
    adjustment['exportManifestBeforeSha256']=digest(before)
    adjustment['exportManifestAfterSha256']=digest(document.as_string().encode())
