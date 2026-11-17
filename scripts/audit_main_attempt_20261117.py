"""Independently retain/replay the first actual main date, including its failed app.

No acquisition, fitting, source repair, replacement date or weight change occurs.
The analytics setup failure is unavailable; its underlying ValueError message was
not retained by the gateway and is not inferred here.
"""
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

RUN = '44152930722'
REPLAY = '44161306818'
DAY = '2026-11-17'
SOURCE = 'bbb0dfdf1209eb77201c4823ec4edbce5240ecf4'
MODEL_SOURCE = 'a5970603b779f136b2037b9a4967325b02331607'
RELEASE = '02e15de76ae6c4b62b10693d7e615a4d2b46fd80c7710671e346fe6dccb2666c'
CLAIM = '1ce0441c745b82d62be05c9348feba7086f35175'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def load(path):
    return json.loads(path.read_bytes())


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def audit(directory, output):
    output.mkdir(exist_ok=False)
    assets = 0
    for folder, tag, count in [('raw', 'reserved-main-v2', 8), ('execution', 'execution-' + RUN, 2),
                               ('replay', 'replay-' + RUN, 5), ('replay-execution', 'replay-execution-' + REPLAY, 2)]:
        provider = load(directory / folder / 'provider-release.json')
        require(provider['tag_name'] == tag and provider['target_commitish'] == SOURCE, 'source-bound provider differs')
        selected = [r for r in provider['assets'] if folder != 'raw' or ('-' + RUN + '-1.') in r['name']]
        require(len(selected) == len({r['name'] for r in selected}) == count, 'complete actual attempt assets required')
        for row in selected:
            require(Path(row['name']).name == row['name'], 'confined provider asset required')
            raw = (directory / folder / row['name']).read_bytes()
            require(len(raw) == row['size'] and row['digest'] == 'sha256:' + sha(raw), 'provider digest differs')
            assets += 1
    metadata = load(directory / 'execution' / ('public-execution-' + RUN + '-1-metadata.json'))
    actual = load(directory / 'actual-run.json')
    require(str(metadata['databaseId']) == str(actual['id']) == RUN and metadata['headSha'] == actual['head_sha'] == SOURCE
            and metadata['status'] == actual['status'] == 'completed' and metadata['conclusion'] == actual['conclusion'] == 'failure'
            and metadata['event'] == actual['event'] == 'schedule' and actual['run_attempt'] == 1 and actual['head_branch'] == 'main',
            'actual single scheduled main attempt differs')
    replay_metadata = load(directory / 'replay-execution' / ('public-application-replay-' + REPLAY + '-1-metadata.json'))
    replay_actual = load(directory / 'actual-replay-run.json')
    require(str(replay_metadata['databaseId']) == str(replay_actual['id']) == REPLAY and replay_actual['head_sha'] == SOURCE
            and replay_actual['status'] == 'completed' and replay_actual['conclusion'] == 'failure'
            and replay_actual['run_attempt'] == 1 and replay_actual['event'] == 'workflow_run', 'actual automatic replay differs')
    jobs = {r['name']: r for r in metadata['jobs'] if r['name'].startswith('collection (')}
    require(len(jobs) == 4, 'four actual collection roles required')
    for task in ('inventory', 'analytics'):
        for role in ('target', 'observer'):
            job = jobs[f'collection ({task}, {role})']
            require(job['conclusion'] == ('success' if task == 'inventory' else 'failure')
                    and next(s['conclusion'] for s in job['steps'] if s['name'].startswith('Remove only this job')) == 'success',
                    'actual collection or own-node cleanup step differs')
    log = (directory / 'execution' / ('public-execution-' + RUN + '-1-all-jobs.log')).read_text()
    require(all(f'collection ({t}, {r})' in log for t in ('inventory', 'analytics') for r in ('target', 'observer'))
            and 'urllib.error.HTTPError: HTTP Error 400: Bad Request' in log and 'RuntimeError: observer idle timeout' in log
            and 'GH_TOKEN: ***' in log, 'complete masked original failure logs required')
    message = {'collection_date_utc': DAY, 'mode': 'main', 'run_id': RUN, 'run_attempt': '1',
               'source_commit': SOURCE, 'release_sha256': RELEASE}
    claim = load(directory / 'claim-tag.json')
    require(claim['sha'] == CLAIM and claim['object']['type'] == 'commit' and claim['object']['sha'] == SOURCE
            and json.loads(claim['message']) == message, 'immutable actual main claim differs')
    refs = {r['ref']: r['object']['sha'] for r in load(directory / 'main-claim-refs.json') if r['object']['type'] == 'tag'}
    require(refs.get('refs/tags/reserved-main-v2-' + DAY) == CLAIM, 'original main claim missing')
    gates = {r['name']: r['value'] for r in load(directory / 'gate-readback.json')}
    require(gates == {'QRL_FROZEN_EXECUTION_SHA': SOURCE, 'QRL_RELEASE_SHA256': RELEASE,
                      'QRL_MAIN_ENABLED': 'true', 'QRL_QUALIFICATION_ENABLED': 'false'}, 'frozen acquisition gates differ')
    summary = load(directory / 'replay' / ('attempt-summary-' + RUN + '.json'))
    require(summary['parent_run_id'] == RUN and summary['analysis_workflow_run_id'] == REPLAY
            and summary['source_commit'] == SOURCE and summary['release_sha256'] == RELEASE and summary['claim_sha'] == CLAIM
            and summary['day'] == DAY and summary['mode'] == 'main' and summary['qualification_included_in_main'] is False
            and summary['all_required_application_audits_passed'] is False, 'actual partial main summary differs')
    roles = []
    exact = False
    with tempfile.TemporaryDirectory(prefix='qrl-first-main-source-') as temporary:
        source = Path(temporary) / 'source'
        subprocess.run(['git', 'clone', '-q', str((directory / 'public-source.bundle').resolve()), str(source)], check=True, capture_output=True)
        def git(*args):
            return subprocess.run(['git', *args], cwd=source, check=True, capture_output=True).stdout
        require(git('rev-list', '--max-parents=0', SOURCE).decode().splitlines() == ['b2c396b2b769b29308bc16b80cbc26de18b57d65'],
                'isolated public source history required')
        raw = git('show', SOURCE + ':release/release.json')
        require(sha(raw) == RELEASE, 'actual release differs')
        envelope = json.loads(raw)
        require(envelope['model_source_commit'] == MODEL_SOURCE and len(envelope['source_files']) == 155, 'measured source identity differs')
        for name, expected in envelope['source_files'].items():
            require(sha(git('show', SOURCE + ':' + name)) == sha(git('show', MODEL_SOURCE + ':' + name)) == expected, 'measured source changed')
            if name.startswith(('experiments/', 'scripts/audit_main_evaluation.py')) and (ROOT / name).exists():
                require(sha((ROOT / name).read_bytes()) == expected, 'local independent dependency differs')
        artifacts = {}
        for name, row in envelope['artifacts'].items():
            raw = git('show', SOURCE + ':' + row['path'])
            require(len(raw) == row['bytes'] and sha(raw) == row['sha256'], 'seven-artifact binding differs')
            artifacts[name] = json.loads(raw)
        sample = artifacts['sample_plan']
        require(sample['day_count'] == 20 and sample['collection_dates_utc'][0] == '2026-11-15'
                and sample['collection_dates_utc'][-1] == '2026-12-04' and sample['qualification_attempt_limit'] == 3,
                'original fixed calendar or consumed qualification cap differs')
        permission = SimpleNamespace(artifacts=artifacts, _source_sha=envelope['source_files'], expected_sha256=RELEASE)
        retained_component = None
        for task in ('inventory', 'analytics'):
            print(json.dumps({'task': task, 'stage': 'verify_original_raw_and_replay'}), flush=True)
            with tempfile.TemporaryDirectory(prefix='qrl-first-main-pair-') as pair_temp:
                pair = Path(pair_temp)
                paths = {}
                for role in ('target', 'observer'):
                    stem = f'{DAY}-{task}-{role}-{RUN}-1'
                    path = pair / role
                    manifest = extract(directory / 'raw' / (stem + '.tar.gz'), directory / 'raw' / (stem + '.json'), path)
                    paths[role] = path / ('main-' + task)
                    report = load(paths[role] / 'report.json')
                    identity = load(path / 'public-role-identity.json')
                    registration = load(path / 'network-registration.json')
                    cleanup = load(path / 'network-cleanup.json')
                    receipt = load(path / 'registration-auth-key.json')
                    outcome = 'success' if task == 'inventory' else 'failure'
                    require(identity == {'task': task, 'role': role, 'day': DAY, 'mode': 'main', 'source_commit': SOURCE,
                                         'run_id': RUN, 'run_attempt': '1', 'claim_sha': CLAIM, 'release_sha256': RELEASE,
                                         'collection_step_outcome': outcome}, 'actual role identity differs')
                    require(report['task'] == task and report['collection_date_utc'] == DAY and report['smoke'] is False
                            and report['confirmatory_data'] is True and report['status'] == ('passed' if task == 'inventory' else 'failed')
                            and len(report['cases']) == 66 and {r['rows'] for r in report['cases']} == {1000, 100000}
                            and len(report['cells']) == (66 if task == 'inventory' else 0)
                            and report['source_sha256'] == envelope['source_files'], 'actual full or unavailable panel differs')
                    require(report['authorization']['verified_calendar_claim'] == message
                            and report['authorization']['calendar_claim_sha'] == CLAIM
                            and report['authorization']['additional_spending_limit'] == 0, 'actual claim authorization differs')
                    name = f'qrl-{RUN}{0 if task == "inventory" else 1}-1-{role}'
                    verify_key_receipt(receipt, name)
                    require(registration['hostname'] == name and registration['tag'] == TAG and registration['verified_standard_node'] is True
                            and registration['provider_is_ephemeral'] is not True and registration['registration_auth_key_id'] == receipt['key_id']
                            and registration['client_version'] == VERSION and registration['client_tarball_sha256'] == BINARY_SHA,
                            'ordinary pinned own-node registration differs')
                    require(cleanup == {'status': 'removed', 'hostname': name, 'node_id': registration['node_id'], 'tag': TAG,
                                        'verified_absent': True}, 'exact own-node API absence differs')
                    require(report['local']['overlay_name'] == name and report['local']['github_run_id'] == RUN
                            and report['local']['source_commit'] == SOURCE, 'actual host identity differs')
                    roles.append({'task': task, 'role': role, 'node_id': registration['node_id'],
                                  'kernel_boot_sha256': report['local']['kernel_boot_sha256'], 'cells': len(report['cells']),
                                  'raw_members_verified': len(manifest['files']), 'own_node_absence_verified': True})
                    if task == 'analytics':
                        require(not any('policy-commitment' in r['path'] or '/l-' in r['path'] for r in manifest['files']),
                                'failed analytics cannot have a fabricated pre-load policy or loaded cell')
                        expected_error = ('HTTPError', 'HTTP Error 400: Bad Request') if role == 'observer' else ('RuntimeError', 'observer idle timeout')
                        require((report['error_type'], report['error']) == expected_error, 'recorded analytics error differs')
                        if role == 'target':
                            require(report['gateway_errors'] == [{'path': '/qrl/begin', 'error_type': 'ValueError'}], 'recorded target setup error differs')
                            connections = load(paths[role] / 'connection-observations.json')
                            require([(r['path'], r['status']) for r in connections] == [('/manifest', 200), ('/qrl/hello', 200), ('/qrl/begin', 400)],
                                    'actual setup-response prefix differs')
                replay_path = pair / 'automatic'
                stem = 'application-replay-' + RUN + '-' + task
                extract(directory / 'replay' / (stem + '.tar.gz'), directory / 'replay' / (stem + '.json'), replay_path)
                automatic = load(replay_path / 'application-replay.json')
                require(automatic == next(r['replay'] for r in summary['applications'] if r['task'] == task)
                        and automatic['status'] == ('passed' if task == 'inventory' else 'unavailable'), 'retained automatic replay differs')
                if task == 'analytics':
                    require(not (replay_path / 'component.json').exists() and automatic['error'] == 'raw role identity or collection outcome differs',
                            'unavailable analytics must retain its missing component')
                    continue
                choices = load(paths['observer'] / 'daily-choices.json')
                profiles = load(paths['observer'] / 'daily-profiles.json')
                commitment = load(paths['target'] / 'policy-commitment.json')
                offer = load(paths['observer'] / 'policy-commitment-offer.json')
                require(commitment == load(paths['observer'] / 'policy-commitment.json')
                        and commitment['choices_sha256'] == digest(choices) == offer['choices_sha256']
                        and commitment['profiles_sha256'] == digest(profiles) == offer['profiles_sha256']
                        and commitment['completed_cases'] == 18 and commitment['loaded_cases_started'] == offer['loaded_cases_started'] == 0
                        and commitment['release_sha256'] == RELEASE and commitment['collection_date_utc'] == DAY
                        and choices['loaded_evaluation_outcomes_used'] is profiles['loaded_evaluation_outcomes_used'] is False
                        and 0 < int(commitment['target_monotonic_ns']) < int(load(paths['target'] / 'l-000/windows.json')[0]['start_monotonic_ns'])
                        and 0 < int(offer['observer_monotonic_ns']) < int(load(paths['observer'] / 'l-000/loaded/loaded-start.json')['measurement_start_monotonic_ns']),
                        'original inventory policy commitment must precede load')
                retained = load(replay_path / 'paired-audit.json')
                require(automatic['paired_auditor_sha256'] == sha((ROOT / 'scripts/audit_main_evaluation.py').read_bytes()), 'original paired auditor differs')
                save(output / 'inventory-automatic-paired-audit.json', retained)
                retained_component = load(replay_path / 'component.json')
                require(component(retained, artifacts, CLAIM) == retained_component, 'exact finite inventory component differs')
                save(output / 'inventory-component-reconstructed.json', retained_component)
                from scripts.audit_main_evaluation import audit as paired_audit
                try:
                    replayed = paired_audit(paths['target'], paths['observer'], permission)
                    require(replayed == retained, 'independent full paired replay differs')
                    save(output / 'inventory-independent-paired-audit.json', replayed)
                    exact = True
                except ValueError as error:
                    if str(error) != 'policy choice replay differs':
                        raise
                    trace = error.__traceback__
                    while trace and not (trace.tb_frame.f_code.co_name == 'audit' and Path(trace.tb_frame.f_code.co_filename).resolve() == (ROOT / 'scripts/audit_main_evaluation.py').resolve()):
                        trace = trace.tb_next
                    require(trace is not None and len(trace.tb_frame.f_locals['summaries']) == 66, 'unexpected exact replay failure')
                    current = trace.tb_frame.f_locals
                    differences = []
                    def compare(a, b, path):
                        if isinstance(a, dict) and isinstance(b, dict) and a.keys() == b.keys():
                            for key in a:
                                compare(a[key], b[key], path + '/' + str(key))
                        elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
                            for i, (left, right) in enumerate(zip(a, b)):
                                compare(left, right, path + '/' + str(i))
                        elif a != b:
                            differences.append({'path': path, 'retained': a, 'replayed': b})
                    compare(choices, current['choices'], 'choices')
                    compare(profiles, current['profiles'], 'profiles')
                    save(output / 'inventory-exact-replay-differences.json', {'error': str(error), 'all_semantic_cells_traversed': 66,
                         'exact_profiles_equal': profiles == current['profiles'], 'selected_actions_equal': choices['choices'] == current['choices']['choices'],
                         'differences': differences})
        require(len({r['node_id'] for r in roles}) == len({r['kernel_boot_sha256'] for r in roles}) == 4, 'four distinct original roles required')
        require(day_summary(artifacts, [retained_component], DAY, SOURCE, RELEASE) == summary['main_estimate'], 'original-weight partial day bounds differ')
    result = {'version': 'independent-first-main-attempt-v1', 'status': 'passed' if exact else 'exact_linux_recheck_required',
              'run_id': RUN, 'analysis_workflow_run_id': REPLAY, 'source_commit': SOURCE, 'release_sha256': RELEASE, 'claim_sha': CLAIM,
              'day_utc': DAY, 'provider_assets_verified': assets, 'raw_member_digests_verified': sum(r['raw_members_verified'] for r in roles),
              'roles': roles, 'own_nodes_verified_removed': 4, 'inventory_exact_paired_replay': exact,
              'inventory_original_preload_policy_verified': True, 'analytics_component': 'unavailable', 'analytics_completed_cells': 0,
              'analytics_underlying_setup_ValueError_message_available': False, 'planned_calendar_count': 20,
              'missing_application_weight': {'numerator': 1, 'denominator': 40}, 'elapsed_unavailable_application_components': 5,
              'elapsed_identification_half_width': {'numerator': 1, 'denominator': 8, 'value': 0.125},
              'qualification_included_in_main': False, 'replacement_acquisition': False, 'refit_performed': False,
              'successful_date_renormalization': False, 'auditor_sha256': sha(Path(__file__).read_bytes())}
    save(output / 'independent-main-attempt-audit.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(audit(args.directory, args.output)))


if __name__ == '__main__':
    main()
