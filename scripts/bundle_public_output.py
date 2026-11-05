"""Normalize synthetic public-run evidence and hash every member before release upload."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile


def bundle(source,destination,manifest_path):
    source=source.resolve();destination=destination.resolve()
    if not source.is_dir() or destination.is_relative_to(source) or destination.exists() or manifest_path.exists():raise ValueError('new external bundle/manifest paths and existing evidence directory required')
    records=[]
    with destination.open('xb') as stream,gzip.GzipFile(fileobj=stream,mode='wb',mtime=0,filename='') as compressed,tarfile.open(fileobj=compressed,mode='w') as archive:
        for path in sorted(source.rglob('*')):
            if path.is_symlink():raise ValueError('evidence symlink forbidden')
            if not path.is_file():continue
            raw=path.read_bytes();name=str(path.relative_to(source));entry=tarfile.TarInfo(name);entry.size=len(raw);entry.mtime=entry.uid=entry.gid=0;entry.mode=0o644;entry.uname=entry.gname=''
            archive.addfile(entry,io.BytesIO(raw));records.append({'path':name,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()})
    if not records or destination.stat().st_size>=2*1024**3:raise ValueError('empty or oversized release asset')
    value={'format':'qrl-public-evidence-bundle-v1','archive':destination.name,'bytes':destination.stat().st_size,'sha256':hashlib.sha256(destination.read_bytes()).hexdigest(),'files':records}
    manifest_path.write_text(json.dumps(value,indent=2)+'\n');return value


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for field in ('source','bundle','manifest'):parser.add_argument('--'+field,type=Path,required=True)
    args=parser.parse_args();value=bundle(args.source,args.bundle,args.manifest);print(json.dumps({'members':len(value['files']),'bytes':value['bytes'],'sha256':value['sha256']}))


if __name__=='__main__':main()
