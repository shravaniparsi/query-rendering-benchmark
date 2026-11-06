from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from experiments.study_design.public_artifacts import extract,download
from experiments.study_design.public_github import GitHub
from scripts.bundle_public_output import bundle
from scripts.inspect_public_attempt import inspect


class PublicReplayContracts(unittest.TestCase):
    def test_normalized_roundtrip_rejects_changed_archive_or_member_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'source';source.mkdir();(source/'nested').mkdir();(source/'nested/evidence.json').write_text('{"synthetic":true}\n')
            archive=root/'evidence.tar.gz';manifest=root/'evidence.json';value=bundle(source,archive,manifest)
            extract(archive,manifest,root/'checked');self.assertEqual((root/'checked/nested/evidence.json').read_bytes(),(source/'nested/evidence.json').read_bytes())
            changed=deepcopy(value);changed['files'][0]['sha256']='0'*64;manifest.write_text(json.dumps(changed))
            with self.assertRaises(ValueError):extract(archive,manifest,root/'bad-member')
            manifest.write_text(json.dumps(value));archive.write_bytes(archive.read_bytes()+b'x')
            with self.assertRaises(ValueError):extract(archive,manifest,root/'bad-archive')

    def test_rehashed_traversal_symlink_and_duplicate_members_are_rejected(self):
        for name,kind in (('../escaped',tarfile.REGTYPE),('/absolute',tarfile.REGTYPE),('a/./alias',tarfile.REGTYPE),('linked',tarfile.SYMTYPE),('duplicate',tarfile.REGTYPE)):
            with self.subTest(name=name),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);archive=root/'evidence.tar.gz';manifest=root/'evidence.json';raw=b'x'
                with tarfile.open(archive,'w:gz') as stream:
                    for _ in range(2 if name=='duplicate' else 1):
                        row=tarfile.TarInfo(name);row.mode=0o644;row.type=kind;row.size=1 if kind==tarfile.REGTYPE else 0;row.linkname='../escaped' if kind==tarfile.SYMTYPE else ''
                        stream.addfile(row,io.BytesIO(raw) if row.size else None)
                manifest.write_text(json.dumps({'format':'qrl-public-evidence-bundle-v1','archive':archive.name,'bytes':archive.stat().st_size,'sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
                    'files':[{'path':name,'bytes':1,'sha256':hashlib.sha256(raw).hexdigest()}]}))
                with self.assertRaises(ValueError):extract(archive,manifest,root/'output')
                self.assertFalse((root/'escaped').exists())

    def test_asset_download_never_requests_an_unbound_url_or_accepts_wrong_digest(self):
        api=GitHub('owner/repo','synthetic-token');asset={'name':'evidence.json','size':1,'digest':'sha256:'+'0'*64,'browser_download_url':'https://github.com/owner/repo/releases/download/day/evidence.json'}
        with tempfile.TemporaryDirectory() as temp:
            destination=Path(temp)/'evidence.json'
            with patch('experiments.study_design.public_artifacts.urlopen',return_value=io.BytesIO(b'x')) as opened:
                with self.assertRaises(ValueError):download(api,{'tag_name':'day'},asset,destination)
                self.assertIsInstance(opened.call_args.args[0],str)  # No token-bearing Request object.
            with patch('experiments.study_design.public_artifacts.urlopen') as opened:
                with self.assertRaises(ValueError):download(api,{'tag_name':'other-day'},asset,Path(temp)/'other.json')
                opened.assert_not_called()

    def test_claim_resolution_handles_queue_crossing_midnight_and_rejects_source_or_rerun(self):
        source='1'*40;claim='2'*40;release='3'*64;runid='123';api=GitHub('owner/repo','synthetic-token')
        permission=SimpleNamespace(expected_sha256=release,artifacts={'sample_plan':{'execution_repository':api.repository,'campaign_id':'campaign','collection_dates_utc':['2027-01-02','2027-01-03'],'qualification_dates_utc':['2027-01-01']}})
        run={'id':123,'status':'completed','head_sha':source,'run_attempt':1,'head_branch':'main','head_repository':{'full_name':api.repository},
            'path':'.github/workflows/public-main-evaluation.yml','event':'workflow_dispatch','created_at':'2027-01-01T23:58:00Z','updated_at':'2027-01-02T01:00:00Z'}
        message={'collection_date_utc':'2027-01-02','mode':'main','run_id':runid,'run_attempt':'1','source_commit':source,'release_sha256':release}
        tag={'tag':'campaign-2027-01-02','message':json.dumps(message),'object':{'type':'commit','sha':source,'url':api.base+'/git/commits/'+source}}
        reference={'object':{'type':'tag','sha':claim}}
        def call(method,path,**kwargs):
            if path=='/actions/runs/'+runid:return run
            if path=='/git/tags/'+claim:return tag
            if path=='/git/ref/tags/campaign-2027-01-02':return reference
            raise AssertionError(path)
        def optional(path):return reference if path.endswith('campaign-2027-01-02') else None
        with patch.object(api,'require_public'),patch.object(api,'call',side_effect=call),patch.object(api,'optional',side_effect=optional):
            value=inspect(api,permission,runid,source);self.assertEqual(value['claim']['day'],'2027-01-02')
            tag['message']=json.dumps({**message,'run_id':'999'});self.assertIsNone(inspect(api,permission,runid,source)['claim']);tag['message']=json.dumps(message)
            for key,value in (('head_sha','9'*40),('run_attempt',2),('head_branch','other'),('event','pull_request')):
                before=run[key];run[key]=value
                with self.subTest(key=key),self.assertRaises(ValueError):inspect(api,permission,runid,source)
                run[key]=before


if __name__=='__main__':unittest.main()
