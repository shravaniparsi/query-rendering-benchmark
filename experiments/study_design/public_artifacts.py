"""Digest-checked public asset downloads and confined normalized extraction."""
import hashlib
import json
from pathlib import Path,PurePosixPath
import tarfile
from urllib.parse import quote
from urllib.request import urlopen

from .decision import require


def assets(api,release):
    result={}
    for page in range(1,12):
        rows=api.call('GET',f'/releases/{release["id"]}/assets?per_page=100&page={page}')
        for row in rows:
            require(row['name'] not in result,'duplicate release asset');result[row['name']]=row
        if len(rows)<100:return result
    raise ValueError('release asset inventory exceeded the bounded campaign capacity')


def download(api,release,asset,destination):
    name=asset['name'];require(Path(name).name==name and '\\' not in name and not destination.exists(),'new confined asset destination required')
    expected=f'https://github.com/{api.repository}/releases/download/{release["tag_name"]}/'+quote(name,safe='')
    require(asset['browser_download_url']==expected and type(asset['size']) is int and 0<asset['size']<2*1024**3,'bounded exact public release asset required')
    digest=hashlib.sha256();size=0;destination.parent.mkdir(parents=True,exist_ok=True)
    # Public downloads carry no authorization material across provider redirects.
    with urlopen(expected,timeout=60) as response,destination.open('xb') as stream:
        while True:
            chunk=response.read(1024*1024)
            if not chunk:break
            size+=len(chunk);require(size<=asset['size'],'public asset exceeded declared size');digest.update(chunk);stream.write(chunk)
    require(size==asset['size'] and asset['digest']=='sha256:'+digest.hexdigest(),'provider asset digest/bytes differ')
    return {'name':name,'bytes':size,'digest':asset['digest']}


def extract(archive,manifest_path,destination):
    value=json.loads(manifest_path.read_bytes());digest=hashlib.sha256()
    with archive.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    require(value['format']=='qrl-public-evidence-bundle-v1' and value['archive']==archive.name and value['bytes']==archive.stat().st_size
        and value['sha256']==digest.hexdigest(),'normalized archive identity differs')
    expected={r['path']:r for r in value['files']};require(len(expected)==len(value['files']) and expected and not destination.exists(),'new complete extraction required')
    destination.mkdir(parents=True)
    with tarfile.open(archive,'r:gz') as source:
        members=source.getmembers();require(len(members)==len(expected),'archive member count differs')
        for member in members:
            name=PurePosixPath(member.name)
            require(member.isfile() and not name.is_absolute() and '..' not in name.parts and '\\' not in member.name and str(name)==member.name and member.name in expected
                and member.mtime==member.uid==member.gid==0 and member.mode==0o644,'unsafe/non-normalized evidence member')
            row=expected.pop(member.name);require(type(row['bytes']) is int and row['bytes']>=0 and member.size==row['bytes'],'declared member length differs')
            path=destination/member.name;path.parent.mkdir(parents=True,exist_ok=True);digest=hashlib.sha256();size=0
            with source.extractfile(member) as incoming,path.open('xb') as outgoing:
                while True:
                    chunk=incoming.read(1024*1024)
                    if not chunk:break
                    size+=len(chunk);require(size<=row['bytes'],'member exceeded declared length');digest.update(chunk);outgoing.write(chunk)
            require(size==row['bytes'] and digest.hexdigest()==row['sha256'],'member digest differs')
    require(not expected,'unassigned evidence inventory');return value
