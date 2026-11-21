"""Independent full-panel replay of the actual November 21 main attempt."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from experiments.study_design.decision import require
from experiments.study_design.public_artifacts import extract
from experiments.study_design.main_analysis import component, day_summary
from experiments.heldout_tasks.performance_evidence import digest
from scripts.research_network import verify_key_receipt, TAG, VERSION, BINARY_SHA

RUN = '44927977706'
REPLAY = '44932212931'
DAY = '2026-11-21'
SOURCE = 'bbb0dfdf1209eb77201c4823ec4edbce5240ecf4'
MODEL_SOURCE = 'a5970603b779f136b2037b9a4967325b02331607'
RELEASE = '02e15de76ae6c4b62b10693d7e615a4d2b46fd80c7710671e346fe6dccb2666c'
CLAIM = '13a8871d5cd60263fc71092e1f2fe8a423fcd56c'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def load(path):
    return json.loads(path.read_bytes())


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def audit(directory, output):
    output.mkdir(exist_ok=False)
    asset_count = 0
    for folder, tag, count in [('raw', 'reserved-main-v2', 8), ('execution', 'execution-' + RUN, 2),
                               ('replay', 'replay-' + RUN, 5), ('replay-execution', 'replay-execution-' + REPLAY, 2)]:
        provider = load(directory / folder / 'provider-release.json')
        rows = [r for r in provider['assets'] if folder != 'raw' or ('-' + RUN + '-1.') in r['name']]
        require(provider['tag_name'] == tag and provider['target_commitish'] == SOURCE
                and len(rows) == len({r['name'] for r in rows}) == count, 'complete source-bound exact-run provider inventory required')
        for row in rows:
            require(Path(row['name']).name == row['name'], 'confined provider asset required')
            raw = (directory / folder / row['name']).read_bytes()
            require(len(raw) == row['size'] and row['digest'] == 'sha256:' + sha(raw), 'provider digest differs')
            asset_count += 1
    metadata = load(directory / 'execution' / ('public-execution-' + RUN + '-1-metadata.json'))
    actual = load(directory / 'actual-run.json')
    require(str(metadata['databaseId']) == str(actual['id']) == RUN and metadata['headSha'] == actual['head_sha'] == SOURCE
            and metadata['status'] == actual['status'] == 'completed' and metadata['conclusion'] == actual['conclusion'] == 'success'
            and metadata['event'] == actual['event'] == 'schedule' and actual['run_attempt'] == 1 and actual['head_branch'] == 'main',
            'actual single completed scheduled main attempt required')
    replay_actual = load(directory / 'actual-replay-run.json')
    require(str(replay_actual['id']) == REPLAY and replay_actual['head_sha'] == SOURCE and replay_actual['event'] == 'workflow_run'
            and replay_actual['run_attempt'] == 1 and replay_actual['status'] == 'completed' and replay_actual['conclusion'] == 'success',
            'actual automatic replay differs')
    jobs = {r['name']: r for r in metadata['jobs'] if r['name'].startswith('collection (')}
    require(len(jobs) == 4, 'four actual collection roles required')
    for job in jobs.values():
        require(job['conclusion'] == 'success' and next(s['conclusion'] for s in job['steps'] if s['name'].startswith('Remove only this job')) == 'success',
                'collection or cleanup step failed')
    message = {'collection_date_utc': DAY, 'mode': 'main', 'run_id': RUN, 'run_attempt': '1', 'source_commit': SOURCE, 'release_sha256': RELEASE}
    claim = load(directory / 'claim-tag.json')
    require(claim['sha'] == CLAIM and claim['object']['type'] == 'commit' and claim['object']['sha'] == SOURCE
            and json.loads(claim['message']) == message, 'exact immutable main claim differs')
    refs = {r['ref']: r['object']['sha'] for r in load(directory / 'main-claim-refs.json') if r['object']['type'] == 'tag'}
    require(refs.get('refs/tags/reserved-main-v2-' + DAY) == CLAIM, 'actual main-date claim missing')
    summary = load(directory / 'replay' / ('attempt-summary-' + RUN + '.json'))
    require(summary['parent_run_id'] == RUN and summary['analysis_workflow_run_id'] == REPLAY and summary['source_commit'] == SOURCE
            and summary['release_sha256'] == RELEASE and summary['claim_sha'] == CLAIM and summary['mode'] == 'main' and summary['day'] == DAY
            and summary['qualification_included_in_main'] is False
            and summary['all_required_application_audits_passed'] is summary['four_distinct_research_hosts_verified'] is True, 'automatic full-main replay differs')
    roles, applications, components = [], [], []
    with tempfile.TemporaryDirectory(prefix='qrl-full-main-source-') as temporary:
        source = Path(temporary) / 'source'
        subprocess.run(['git', 'clone', '-q', str((directory / 'public-source.bundle').resolve()), str(source)], check=True, capture_output=True)
        def git(*args):
            return subprocess.run(['git', *args], cwd=source, check=True, capture_output=True).stdout
        require(git('rev-list', '--max-parents=0', SOURCE).decode().splitlines() == ['b2c396b2b769b29308bc16b80cbc26de18b57d65'], 'isolated public history required')
        raw = git('show', SOURCE + ':release/release.json')
        require(sha(raw) == RELEASE, 'frozen actual release differs')
        envelope = json.loads(raw)
        require(envelope['model_source_commit'] == MODEL_SOURCE and len(envelope['source_files']) == 155, 'actual prepared source identity differs')
        for name, expected in envelope['source_files'].items():
            require(sha(git('show', SOURCE + ':' + name)) == sha(git('show', MODEL_SOURCE + ':' + name)) == expected, 'measured source changed')
            if name.startswith(('experiments/', 'scripts/audit_main_evaluation.py')) and (ROOT / name).exists():
                require(sha((ROOT / name).read_bytes()) == expected, 'independent replay dependency differs')
        artifacts = {}
        for name, row in envelope['artifacts'].items():
            raw = git('show', SOURCE + ':' + row['path'])
            require(len(raw) == row['bytes'] and sha(raw) == row['sha256'], 'actual seven-artifact descriptor differs')
            artifacts[name] = json.loads(raw)
        sample = artifacts['sample_plan']
        require(sample['qualification_attempt_limit'] == 3 and sample['qualification_dates_utc'] == ['2026-11-13'] and sample['day_count'] == 20
                and sample['collection_dates_utc'][0] == '2026-11-15' and sample['collection_dates_utc'][-1] == '2026-12-04' and DAY in sample['collection_dates_utc'],
                'approved original protocol differs')
        permission = SimpleNamespace(artifacts=artifacts, _source_sha=envelope['source_files'], expected_sha256=RELEASE)
        for task in ('inventory', 'analytics'):
            print(json.dumps({'task': task, 'stage': 'extract_and_independent_full_replay'}), flush=True)
            with tempfile.TemporaryDirectory(prefix='qrl-full-main-pair-') as pair_temp:
                pair, paths = Path(pair_temp), {}
                for role in ('target', 'observer'):
                    stem = f'{DAY}-{task}-{role}-{RUN}-1'
                    path = pair / role
                    manifest = extract(directory / 'raw' / (stem + '.tar.gz'), directory / 'raw' / (stem + '.json'), path)
                    paths[role] = path / ('main-' + task)
                    identity = load(path / 'public-role-identity.json')
                    report = load(paths[role] / 'report.json')
                    registration, cleanup, receipt = (load(path / n) for n in ('network-registration.json', 'network-cleanup.json', 'registration-auth-key.json'))
                    require(identity == {'task': task, 'role': role, 'day': DAY, 'mode': 'main', 'source_commit': SOURCE, 'run_id': RUN, 'run_attempt': '1',
                                         'claim_sha': CLAIM, 'release_sha256': RELEASE, 'collection_step_outcome': 'success'}, 'actual full main role identity differs')
                    require(report['status'] == 'passed' and report['smoke'] is False and report['confirmatory_data'] is True
                            and len(report['cases']) == len(report['cells']) == 66 and {r['rows'] for r in report['cases']} == {1000, 100000}
                            and report['source_sha256'] == envelope['source_files'], 'full 66-cell two-scale main panel differs')
                    require(report['authorization']['verified_calendar_claim'] == message and report['authorization']['calendar_claim_sha'] == CLAIM
                            and report['authorization']['additional_spending_limit'] == 0, 'actual panel authorization differs')
                    name = f'qrl-{RUN}{0 if task == "inventory" else 1}-1-{role}'
                    verify_key_receipt(receipt, name)
                    require(registration['hostname'] == name and registration['tag'] == TAG and registration['verified_standard_node'] is True
                            and registration['provider_is_ephemeral'] is not True and registration['registration_auth_key_id'] == receipt['key_id']
                            and registration['client_version'] == VERSION and registration['client_tarball_sha256'] == BINARY_SHA, 'ordinary pinned registration differs')
                    require(cleanup == {'status': 'removed', 'hostname': name, 'node_id': registration['node_id'], 'tag': TAG, 'verified_absent': True}, 'exact own-node absence receipt differs')
                    require(report['local']['overlay_name'] == name and report['local']['github_run_id'] == RUN and report['local']['source_commit'] == SOURCE, 'actual runner source identity differs')
                    roles.append({'task': task, 'role': role, 'node_id': registration['node_id'], 'kernel_boot_sha256': report['local']['kernel_boot_sha256'],
                                  'full_cells': 66, 'members_verified': len(manifest['files']), 'exact_own_node_removed': True})
                commitment, offer = load(paths['target'] / 'policy-commitment.json'), load(paths['observer'] / 'policy-commitment-offer.json')
                choices, profiles = load(paths['observer'] / 'daily-choices.json'), load(paths['observer'] / 'daily-profiles.json')
                require(commitment == load(paths['observer'] / 'policy-commitment.json') and commitment['choices_sha256'] == digest(choices) == offer['choices_sha256']
                        and commitment['profiles_sha256'] == digest(profiles) == offer['profiles_sha256'] and commitment['release_sha256'] == RELEASE
                        and commitment['collection_date_utc'] == DAY and commitment['completed_cases'] == 18
                        and commitment['loaded_cases_started'] == offer['loaded_cases_started'] == 0
                        and choices['loaded_evaluation_outcomes_used'] is profiles['loaded_evaluation_outcomes_used'] is False, 'original preloaded policy commitment differs')
                require(0 < int(commitment['target_monotonic_ns']) < int(load(paths['target'] / 'l-000/windows.json')[0]['start_monotonic_ns'])
                        and 0 < int(offer['observer_monotonic_ns']) < int(load(paths['observer'] / 'l-000/loaded/loaded-start.json')['measurement_start_monotonic_ns']), 'policy commitment postdates load')
                replay_path = pair / 'automatic'
                stem = 'application-replay-' + RUN + '-' + task
                extract(directory / 'replay' / (stem + '.tar.gz'), directory / 'replay' / (stem + '.json'), replay_path)
                automatic, retained = load(replay_path / 'application-replay.json'), load(replay_path / 'paired-audit.json')
                require(automatic == next(r['replay'] for r in summary['applications'] if r['task'] == task) and automatic['status'] == 'passed'
                        and automatic['paired_auditor_sha256'] == sha((ROOT / 'scripts/audit_main_evaluation.py').read_bytes()), 'automatic paired auditor differs')
                from scripts.audit_main_evaluation import audit as paired_audit
                replayed = paired_audit(paths['target'], paths['observer'], permission)
                require(replayed == retained, 'fresh full paired replay differs from original automatic Linux report')
                save(output / (task + '-independent-paired-audit.json'), replayed)
                finite = component(replayed, artifacts, CLAIM)
                require(finite == load(replay_path / 'component.json'), 'exact main finite component differs')
                save(output / (task + '-component-reconstructed.json'), finite)
                components.append(finite)
                applications.append({'task': task, 'status': 'passed', 'cells': 66, 'policy_commitment_verified': True, 'exact_paired_replay': True})
        require(day_summary(artifacts, components, DAY, SOURCE, RELEASE) == summary['main_estimate'], 'exact original-weight day summary differs')
    require(len({r['node_id'] for r in roles}) == len({r['kernel_boot_sha256'] for r in roles}) == 4, 'four distinct nodes and kernels required')
    result = {'version': 'independent-full-main-20261121-v1', 'status': 'passed', 'run_id': RUN, 'day_utc': DAY, 'source_commit': SOURCE,
              'release_sha256': RELEASE, 'claim_sha': CLAIM, 'provider_assets_verified': asset_count, 'applications': applications, 'roles': roles,
              'raw_member_digests_verified': sum(r['members_verified'] for r in roles), 'own_nodes_verified_removed': 4,
              'main_components_verified': 2, 'qualification_included_in_main': False, 'refit_performed': False, 'acquisition_started': False,
              'auditor_sha256': sha(Path(__file__).read_bytes())}
    save(output / 'independent-full-main-audit.json', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(audit(args.directory, args.output)))
