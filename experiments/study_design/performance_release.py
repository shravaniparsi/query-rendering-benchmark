"""Actual-byte/date/source permission for reserved performance acquisition."""
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
from .artifact_release import verify_release
from .decision import require

TASK_APPLICATIONS={'inventory':'inventory-reservation','analytics':'time-window-analytics'}
PERFORMANCE_SOURCES={
    'experiments/study_design/main_selection.py',
    'experiments/study_design/performance_release.py',
    'experiments/heldout_tasks/workload.py',
    'experiments/heldout_tasks/main_design.py',
    'experiments/heldout_tasks/performance_evidence.py',
    'experiments/heldout_tasks/transport.py',
    'experiments/heldout_tasks/gateway.py',
    'experiments/heldout_tasks/dom.cjs',
    'experiments/heldout_tasks/visible.py',
    'experiments/heldout_tasks/performance_service.py',
    'experiments/heldout_tasks/performance_runtime.py',
    'experiments/heldout_tasks/performance_browser.cjs',
    'experiments/heldout_tasks/performance_target.py',
    'experiments/heldout_tasks/performance_observer.py',
    'scripts/offhost_main_evaluation.py',
    'scripts/check_reserved_performance_permission.py',
    'scripts/audit_main_evaluation.py',
}


class PerformancePermission:
    def __init__(self,root,release_path,expected_sha256):
        self.root=Path(root).resolve();self.release_path=release_path;self.expected_sha256=expected_sha256
        self.checked=verify_release(self.root,release_path,expected_release_sha256=expected_sha256)
        release=json.loads((self.root/release_path).read_bytes())
        require(PERFORMANCE_SOURCES<=set(release['source_files']),'complete frozen performance collector/measurement sources required')
        self.artifacts=self.checked['artifacts'];self._source_sha=release['source_files']
        # This imported gate must be the code whose bytes were actually checked.
        own='experiments/study_design/performance_release.py'
        require(hashlib.sha256(Path(__file__).read_bytes()).hexdigest()==self._source_sha[own],'executing performance gate differs from frozen source')

    def permit(self,task,phase,rows,candidate,strategy,collection_date_utc,*,context_id=None):
        require(task in TASK_APPLICATIONS and phase in ('qualification','serial','loaded'),'unknown reserved task/phase')
        today=datetime.now(timezone.utc).date().isoformat();require(today==collection_date_utc,'actual UTC date differs from declared acquisition day')
        sample=self.artifacts['sample_plan'];permitted=sample['collection_dates_utc']
        if phase=='qualification':permitted=sorted(set(permitted)|set(sample.get('qualification_dates_utc',[])))
        else:require(collection_date_utc in sample['collection_dates_utc'] or sample.get('qualification_date_mode')=='complete_profile_and_loaded_smoke','qualification date does not permit serial/loaded acquisition')
        if collection_date_utc not in sample['collection_dates_utc']:
            require(collection_date_utc in sample.get('qualification_dates_utc',[]) and sample.get('qualification_date_mode')=='complete_profile_and_loaded_smoke','actual date is outside the fixed acquisition/qualification plan')
        require(collection_date_utc in permitted or collection_date_utc in sample.get('qualification_dates_utc',[]),'unplanned real UTC date')
        application=TASK_APPLICATIONS[task];contexts=self.artifacts['main_contexts'];catalog=contexts['catalogs'][application]
        require(candidate+'/'+strategy in catalog,'undeclared query/rendering performance configuration')
        selected=[row for row in contexts['contexts'] if row['application']==application]
        require(rows in {row['rows'] for row in selected},'undeclared reserved fixture scale')
        if phase=='loaded':require(context_id in {row['id'] for row in selected if row['rows']==rows},'undeclared loaded workload context')
        if phase=='qualification':require(rows==self.artifacts['profile_protocol']['overhead']['rows'],'undeclared overhead-qualification scale')
        return {'status':'passed','application':application,'task':task,'phase':phase,'rows':rows,'configuration':candidate+'/'+strategy,
            'context_id':context_id,'collection_date_utc':today,'release_sha256':self.expected_sha256,'model_source_commit':self.checked['model_source_commit']}
