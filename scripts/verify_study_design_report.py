"""Verify executed-source/output hashes and compare development replays."""
import argparse,hashlib,json,math
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
FILES={'development-replay.json','development-sensitivity.csv','precision-scenarios.json'}
SOURCES={'experiments/study_design/decision.py','experiments/study_design/precision.py','scripts/build_study_design_report.py',
         'scripts/verify_study_design_report.py','tests/test_study_design.py','scripts/verify_paired_archive.py',
         'paper/followup/query-rendering-load/protocol/main_decision_components_v1.json',
         'paper/followup/query-rendering-load/protocol/application_reservation_v1.json'}


def verify(directory):
    value=json.loads((directory/'provenance.json').read_text())
    if set(value['source_sha256'])!=SOURCES:raise ValueError('executed source set differs')
    if len(value['output_files'])!=len(FILES) or {r['path'] for r in value['output_files']}!=FILES:raise ValueError('report file set differs')
    for item in value['output_files']:
        data=(directory/item['path']).read_bytes()
        if len(data)!=item['bytes'] or hashlib.sha256(data).hexdigest()!=item['sha256']:raise ValueError('output hash differs')
    for name,digest in value['source_sha256'].items():
        path=Path(name)
        if path.is_absolute() or '..' in path.parts or hashlib.sha256((ROOT/path).read_bytes()).hexdigest()!=digest:raise ValueError('executed source differs')
    return value


def compare(a,b,path='$',roundoff=None):
    roundoff=[] if roundoff is None else roundoff
    if type(a) is not type(b):raise ValueError('type differs: '+path)
    if isinstance(a,dict):
        if a.keys()!=b.keys():raise ValueError('keys differ: '+path)
        for key in a:compare(a[key],b[key],path+'.'+key,roundoff)
    elif isinstance(a,list):
        if len(a)!=len(b):raise ValueError('length differs: '+path)
        for i,(x,y) in enumerate(zip(a,b)):compare(x,y,f'{path}[{i}]',roundoff)
    elif type(a) is float:
        if not math.isfinite(a) or not math.isfinite(b) or not math.isclose(a,b,rel_tol=1e-12,abs_tol=1e-12):raise ValueError('float differs: '+path)
        if a!=b:roundoff.append(path)
    elif a!=b:raise ValueError('value differs: '+path)
    return roundoff


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('directory',type=Path);p.add_argument('--expected',type=Path,required=True);a=p.parse_args()
    actual=verify(a.directory);expected=verify(a.expected)
    if actual['source_sha256']!=expected['source_sha256']:raise ValueError('different replay source')
    differences=[];byte_identical={}
    for name in sorted(FILES):
        left=(a.expected/name).read_bytes();right=(a.directory/name).read_bytes();byte_identical[name]=left==right
        if name.endswith('.csv'):
            if left!=right:raise ValueError('raw CSV counts differ')
        else:compare(json.loads(left),json.loads(right),name,differences)
    print(json.dumps({'status':'passed','output_byte_identical':byte_identical,'floating_roundoff_paths':differences,
        'scope':'All structural, integer, Boolean, identity and classification values exact; floats permit 1e-12 absolute/relative roundoff'},indent=2))


if __name__=='__main__':main()
