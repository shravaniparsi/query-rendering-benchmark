from copy import deepcopy
import hashlib
import base64
import io
import json
from pathlib import Path
import tempfile
import tarfile
import unittest
from unittest.mock import patch

from experiments.study_design.public_github import GitHub,APIError,verify_claim
from scripts.research_network import verify_own,verify_standard,verify_key_receipt,cleanup,register,NetworkAPIError,TAG
from scripts.public_network_check import context as network_context,REPOSITORY,WORKFLOW
from scripts.recover_public_network import role_files,FAILED_RUN,FAILED_SOURCE
from scripts.claim_public_date import qualification_block
from experiments.study_design.qualification_budget import AMENDMENT,PRIOR_CLAIMS


class PublicExecutionContracts(unittest.TestCase):
    def amended_history(self,third=False,extra=False,missing=False):
        sample={'qualification_attempt_limit':3,'qualification_budget_dates_utc':['2026-11-07','2026-11-08','2026-11-13'],
            'qualification_dates_utc':['2026-11-13'],'qualification_amendment':deepcopy(AMENDMENT),'campaign_id':'reserved-main-v2',
            'day_count':20,'qualification_date_mode':'complete_profile_and_loaded_full_panel'}
        rows=deepcopy(PRIOR_CLAIMS)
        if third:rows.append({'day':'2026-11-13','claim_sha':'3'*40,'run_id':'synthetic-third','source_commit':'4'*40,'release_sha256':'5'*64})
        if extra:rows.append({'day':'2026-11-12','claim_sha':'6'*40,'run_id':'synthetic-hidden','source_commit':'7'*40,'release_sha256':'8'*64})
        if missing:rows=rows[1:]
        refs=[{'ref':'refs/tags/qualification-reserved-main-v2-'+r['day'],'object':{'type':'tag','sha':r['claim_sha']}} for r in rows]
        def optional(path):return next((r for r in refs if path.endswith(r['ref'].removeprefix('refs/tags/'))),None)
        def call(method,path,**kwargs):
            self.assertEqual(method,'GET')
            if path.startswith('/git/matching-refs/'):return refs
            row=next(r for r in rows if path.endswith(r['claim_sha']))
            return {'message':json.dumps({'collection_date_utc':row['day'],'mode':'qualification','run_attempt':'1',**{k:row[k] for k in ('run_id','source_commit','release_sha256')}})}
        return sample,optional,call

    def test_approved_third_attempt_counts_both_original_claims_and_stops_after_three(self):
        api=GitHub('owner/repo','synthetic-token')
        for third in (False,True):
            sample,optional,call=self.amended_history(third=third)
            with patch.object(api,'optional',side_effect=optional),patch.object(api,'call',side_effect=call),patch('scripts.claim_public_date.verify_claim'),patch('scripts.claim_public_date.source_correction_block',return_value=None):
                result=qualification_block(api,sample,'2026-11-13','9'*40,{})
                if third:self.assertIn('budget exhausted',result)
                else:self.assertIsNone(result)

    def test_amended_calendar_cannot_hide_a_consumed_date_or_drop_an_original_failure(self):
        api=GitHub('owner/repo','synthetic-token')
        for flags in ({'extra':True},{'missing':True}):
            sample,optional,call=self.amended_history(**flags)
            with patch.object(api,'optional',side_effect=optional),patch.object(api,'call',side_effect=call),patch('scripts.claim_public_date.verify_claim'):
                with self.subTest(flags=flags),self.assertRaises(ValueError):qualification_block(api,sample,'2026-11-13','9'*40,{})

    def test_qualification_cannot_repeat_same_source_or_exceed_two_claims(self):
        sample={'qualification_attempt_limit':2,'qualification_budget_dates_utc':['2027-01-01','2027-01-02'],'campaign_id':'synthetic'}
        api=GitHub('owner/repo','synthetic-token');message={'run_id':'123','source_commit':'1'*40,'release_sha256':'2'*64}
        with patch.object(api,'optional',return_value={'object':{'type':'tag','sha':'3'*40}}),patch.object(api,'call',return_value={'message':json.dumps(message)}),patch('scripts.claim_public_date.verify_claim'):
            self.assertIn('already attempted',qualification_block(api,sample,'2027-01-01','1'*40,{}))
            self.assertIn('budget exhausted',qualification_block(api,sample,'2027-01-02','9'*40,{}))

    def test_qualification_artifact_only_retry_is_blocked_but_code_correction_is_permitted(self):
        sample={'qualification_attempt_limit':2,'qualification_budget_dates_utc':['2027-01-01','2027-01-02'],'campaign_id':'synthetic'}
        api=GitHub('owner/repo','synthetic-token');raw=json.dumps({'source_files':{'collector.py':'old'}}).encode();message={'run_id':'123','source_commit':'1'*40,'release_sha256':hashlib.sha256(raw).hexdigest()}
        def optional(path):return {'object':{'type':'tag','sha':'3'*40}} if path.endswith('2027-01-01') else None
        def call(method,path,**kwargs):return {'message':json.dumps(message)} if path.startswith('/git/tags/') else {'encoding':'base64','content':base64.b64encode(raw).decode()}
        with patch.object(api,'optional',side_effect=optional),patch.object(api,'call',side_effect=call),patch('scripts.claim_public_date.verify_claim'):
            self.assertIn('source correction',qualification_block(api,sample,'2027-01-02','9'*40,{'collector.py':'old'}))
            self.assertIsNone(qualification_block(api,sample,'2027-01-02','9'*40,{'collector.py':'corrected'}))

    def test_ordinary_registration_requires_explicit_short_single_use_key_acknowledgement(self):
        name='qrl-1230-1-target';receipt={'description':name,'key_id':'k123CNTRL','created':'2026-11-06T00:00:00Z','expires':'2026-11-06T00:10:00Z',
            'capabilities':{'devices':{'create':{'reusable':False,'ephemeral':False,'preauthorized':True,'tags':[TAG]}}}}
        self.assertTrue(verify_key_receipt(receipt,name))
        device={'nodeId':'n123CNTRL','hostname':name,'tags':[TAG],'isExternal':False}
        self.assertTrue(verify_standard(device,device['nodeId'],name,receipt))
        for key,value in (('ephemeral',True),('reusable',True),('preauthorized',False),('tags',['tag:production'])):
            bad=deepcopy(receipt);bad['capabilities']['devices']['create'][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):verify_standard(device,device['nodeId'],name,bad)
        missing=deepcopy(receipt);del missing['capabilities']['devices']['create']['ephemeral']
        with self.assertRaises(ValueError):verify_key_receipt(missing,name)
        with self.assertRaises(ValueError):verify_key_receipt({**receipt,'expires':'2026-11-07T00:00:00Z'},name)
        with self.assertRaises(ValueError):verify_key_receipt({**receipt,'description':'unrelated-host'},name)
        with self.assertRaises(ValueError):verify_standard({**device,'isEphemeral':True},device['nodeId'],name,receipt)

    def test_recovery_rejects_changed_receipts_or_another_original_role(self):
        identity={'run_id':FAILED_RUN,'source_commit':FAILED_SOURCE,'run_attempt':'1','task':'inventory','role':'target','benchmark_measurements':False}
        raw=json.dumps(identity).encode();buffer=io.BytesIO()
        with tarfile.open(fileobj=buffer,mode='w:gz') as archive:
            member=tarfile.TarInfo('public-network-identity.json');member.size=len(raw);archive.addfile(member,io.BytesIO(raw))
        data=buffer.getvalue();manifest={'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data),'files':[{'path':'public-network-identity.json','bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}]}
        self.assertIn('public-network-identity.json',role_files(data,manifest,'inventory','target'))
        for value,role in (({**manifest,'sha256':'0'*64},'target'),(manifest,'observer')):
            with self.assertRaises(ValueError):role_files(data,value,'inventory',role)
        bad=deepcopy(manifest);bad['files'][0]['sha256']='0'*64
        with self.assertRaises(ValueError):role_files(data,bad,'inventory','target')

    def test_network_check_rejects_wrong_source_event_repository_or_workflow(self):
        api=GitHub(REPOSITORY,'synthetic-token');env={'GITHUB_REPOSITORY':REPOSITORY,'GITHUB_SHA':'1'*40,'QRL_NETWORK_EXECUTION_SHA':'1'*40,
            'GITHUB_RUN_ID':'123','GITHUB_RUN_ATTEMPT':'1','GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_REF':'refs/heads/main',
            'GITHUB_WORKFLOW_REF':WORKFLOW,'QRL_STANDARD_TAGGED_NETWORK_APPROVED':'true'}
        with patch.object(api,'require_public',return_value={'id':1447601875}):
            self.assertFalse(network_context(api,env)['benchmark_measurements'])
            for key,value in (('GITHUB_SHA','2'*40),('GITHUB_RUN_ATTEMPT','2'),('GITHUB_EVENT_NAME','schedule'),('GITHUB_REF','refs/heads/other'),
                ('GITHUB_WORKFLOW_REF',WORKFLOW.replace('public-main-evaluation','other')),('GITHUB_REPOSITORY','other/repo'),('QRL_STANDARD_TAGGED_NETWORK_APPROVED','false')):
                with self.subTest(key=key),self.assertRaises(ValueError):network_context(api,{**env,key:value})
        with patch.object(api,'require_public',return_value={'id':1}),self.assertRaises(ValueError):network_context(api,env)

    def test_claim_must_bind_the_current_ref_and_exact_source_attempt_and_envelope(self):
        source='1'*40;claim='2'*40;release='3'*64;api=GitHub('owner/repo','synthetic-token')
        message={'collection_date_utc':'2027-01-01','mode':'main','run_id':'123','run_attempt':'1','source_commit':source,'release_sha256':release}
        tag={'object':{'type':'commit','sha':source,'url':api.base+'/git/commits/'+source},'tag':'campaign-2027-01-01','message':json.dumps(message)}
        with patch.object(api,'call',side_effect=[tag,{'object':{'sha':claim,'type':'tag'}}]):self.assertEqual(verify_claim(api,claim,day='2027-01-01',mode='main',run_id='123',source=source,release_sha=release),message)
        for changed in ('wrong-ref','wrong-attempt'):
            candidate=deepcopy(tag)
            if changed=='wrong-attempt':candidate['message']=json.dumps({**message,'run_attempt':'2'})
            with patch.object(api,'call',side_effect=[candidate,{'object':{'sha':'9'*40,'type':'tag'}}]):
                with self.assertRaises(ValueError):verify_claim(api,claim,day='2027-01-01',mode='main',run_id='123',source=source,release_sha=release)

    def test_concurrent_release_creation_can_use_only_the_same_frozen_release(self):
        api=GitHub('owner/repo','synthetic-token');value={'tag_name':'campaign-v1','target_commitish':'1'*40,'body':'3'*64}
        with patch.object(api,'optional',return_value=None),patch.object(api,'call',side_effect=[APIError(422),value]):self.assertEqual(api.ensure_release('campaign-v1','1'*40,'3'*64),value)
        with patch.object(api,'optional',return_value={**value,'target_commitish':'9'*40}):
            with self.assertRaises(ValueError):api.ensure_release('campaign-v1','1'*40,'3'*64)

    def test_existing_assets_cannot_be_clobbered_with_different_bytes(self):
        api=GitHub('owner/repo','synthetic-token')
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'evidence.json';path.write_bytes(b'synthetic evidence');asset={'name':path.name,'size':path.stat().st_size,'digest':'sha256:'+hashlib.sha256(path.read_bytes()).hexdigest()}
            with patch.object(api,'call',return_value=[asset]) as called:self.assertEqual(api.upload({'id':1},path),asset);self.assertEqual(called.call_count,1)
            with patch.object(api,'call',return_value=[{**asset,'digest':'sha256:'+'0'*64}]) as called:
                with self.assertRaises(ValueError):api.upload({'id':1},path)
                self.assertEqual(called.call_args.args[0],'GET')

    def test_cleanup_deletes_only_the_exact_standard_research_node(self):
        good={'nodeId':'n123CNTRL','hostname':'qrl-1230-1-target','tags':[TAG],'isEphemeral':False,'isExternal':False}
        self.assertTrue(verify_own(good,good['nodeId'],good['hostname']))
        for key,value in (('hostname','unrelated-host'),('tags',['tag:production']),('nodeId','other')):
            with self.assertRaises(ValueError):verify_own({**good,key:value},good['nodeId'],good['hostname'])
        for flag in (True,None):
            device={k:v for k,v in good.items() if k!='isEphemeral'}
            if flag is True:device['isEphemeral']=True
            self.assertTrue(verify_own(device,good['nodeId'],good['hostname']))
            with self.assertRaises(ValueError):verify_standard(device,good['nodeId'],good['hostname'])
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)/'network-registration.json';output.write_text(json.dumps({'hostname':good['hostname'],'node_id':good['nodeId'],'tag':TAG}))
            with patch('scripts.research_network.access_token',return_value='synthetic-token'),patch('scripts.research_network.api',side_effect=[good,{},NetworkAPIError(404)]) as api,patch('scripts.research_network.subprocess.run'):
                cleanup(output,good['hostname']);self.assertEqual(api.call_args_list[1].args[0],'device/n123CNTRL');self.assertEqual(api.call_args_list[1].kwargs['method'],'DELETE')
                self.assertTrue(json.loads(output.with_name('network-cleanup.json').read_bytes())['verified_absent'])
            with patch('scripts.research_network.access_token',return_value='synthetic-token'),patch('scripts.research_network.api',return_value={**good,'tags':['tag:production']}) as api:
                with self.assertRaises(ValueError):cleanup(output,good['hostname'])
                self.assertEqual(api.call_count,1)
            no_mode={k:v for k,v in good.items() if k!='isEphemeral'}
            with patch('scripts.research_network.access_token',return_value='synthetic-token'),patch('scripts.research_network.api',side_effect=[no_mode,{},NetworkAPIError(404)]) as api,patch('scripts.research_network.subprocess.run',side_effect=FileNotFoundError):
                cleanup(output,good['hostname']);self.assertEqual(api.call_args_list[1].kwargs['method'],'DELETE')

    def test_unrelated_or_previous_nodes_stop_before_client_install_or_registration(self):
        sibling={'hostname':'qrl-1231-1-observer','tags':[TAG],'isEphemeral':False,'isExternal':False}
        invalid=[{**sibling,'hostname':'qrl-old-run-target'},{**sibling,'hostname':'qrl-12345-1-observer'},
            {**sibling,'hostname':'personal-laptop','tags':[]},{**sibling,'tags':['tag:qrl-ci']},
            {**sibling,'tags':[TAG,'tag:production']},{**sibling,'isExternal':True},
            {**sibling,'isEphemeral':True},{**sibling,'hostname':'qrl-1230-1-target'}]
        for device in invalid:
            with self.subTest(device=device),patch.dict('os.environ',{'GITHUB_RUN_ID':'123'}),patch('scripts.research_network.access_token',return_value='synthetic-token'),patch('scripts.research_network.api',return_value={'devices':[device]}),patch('scripts.research_network.install_client') as install:
                with self.assertRaises(ValueError):register(Path('/unused'),'qrl-1230-1-target')
                install.assert_not_called()


if __name__=='__main__':unittest.main()
