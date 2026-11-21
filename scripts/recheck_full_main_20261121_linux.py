"""Read-only Linux reconstruction of the already completed November 21 panels."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from experiments.study_design.decision import require
from experiments.study_design.public_artifacts import download
from experiments.study_design.public_github import GitHub
from scripts.audit_full_main_20261121 import audit, RUN, REPLAY, DAY, SOURCE, RELEASE, CLAIM

REPO = 'shravaniparsi/query-rendering-benchmark'
BRANCH = 'offline-main-20261121-recheck-v1'


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(output):
    require(platform.system() == 'Linux' and platform.python_version() == '3.12.15'
            and os.environ.get('GITHUB_ACTIONS') == 'true' and os.environ.get('RUNNER_OS') == 'Linux'
            and os.environ.get('GITHUB_EVENT_NAME') == 'workflow_dispatch', 'declared read-only Linux platform required')
    require(os.environ['GITHUB_REPOSITORY'] == REPO and os.environ['GITHUB_REF'] == 'refs/heads/' + BRANCH
            and os.environ['GITHUB_WORKFLOW_REF'] == REPO + '/.github/workflows/public-offline-diagnosis.yml@refs/heads/' + BRANCH,
            'isolated existing read-only workflow required')
    current = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    require(current == os.environ['GITHUB_SHA'], 'actual executing source differs')
    require(os.environ['QRL_FROZEN_EXECUTION_SHA'] == SOURCE and os.environ['QRL_RELEASE_SHA256'] == RELEASE
            and os.environ['QRL_MAIN_ENABLED'] == 'true' and os.environ['QRL_QUALIFICATION_ENABLED'] == 'false', 'unchanged frozen gates required')
    api = GitHub(REPO, os.environ['GH_TOKEN'])
    require(api.require_public()['id'] == 1447601875, 'exact public repository required')
    output.mkdir(parents=True, exist_ok=False)
    inputs = output / 'retained-inputs'
    inputs.mkdir()
    for folder, tag, count in [('raw', 'reserved-main-v2', 8), ('execution', 'execution-' + RUN, 2),
                               ('replay', 'replay-' + RUN, 5), ('replay-execution', 'replay-execution-' + REPLAY, 2)]:
        target = inputs / folder
        target.mkdir()
        provider = api.call('GET', '/releases/tags/' + tag)
        require(provider['target_commitish'] == SOURCE, 'original provider source differs')
        rows = [r for r in provider['assets'] if folder != 'raw' or ('-' + RUN + '-1.') in r['name']]
        require(len(rows) == count, 'complete exact-run evidence required')
        save(target / 'provider-release.json', provider)
        for asset in rows:
            download(api, provider, asset, target / asset['name'])
    save(inputs / 'claim-tag.json', api.call('GET', '/git/tags/' + CLAIM))
    save(inputs / 'main-claim-refs.json', api.call('GET', '/git/matching-refs/tags/reserved-main-v2-'))
    save(inputs / 'actual-run.json', api.call('GET', '/actions/runs/' + RUN))
    save(inputs / 'actual-replay-run.json', api.call('GET', '/actions/runs/' + REPLAY))
    subprocess.run(['git', 'bundle', 'create', str((inputs / 'public-source.bundle').resolve()),
                    'refs/tags/reserved-main-v3-amended-frozen-source'], cwd=ROOT, check=True, capture_output=True)
    result = audit(inputs, output / 'independent-replay')
    require(result['status'] == 'passed' and result['main_components_verified'] == 2 and result['own_nodes_verified_removed'] == 4
            and all(r['exact_paired_replay'] is True and r['cells'] == 66 for r in result['applications']), 'both full exact main panels required')
    report = {'version': 'actual-full-main-20261121-linux-v1', 'status': 'passed', 'recheck_run_id': os.environ['GITHUB_RUN_ID'],
              'recheck_source_commit': current, 'parent_main_run_id': RUN, 'automatic_replay_run_id': REPLAY, 'collection_date_utc': DAY,
              'frozen_execution_source': SOURCE, 'release_sha256': RELEASE, 'claim_sha': CLAIM, 'provider_assets_verified': 17,
              'raw_member_digests_verified': result['raw_member_digests_verified'], 'exact_full_panels': ['inventory', 'analytics'],
              'cells_per_application': 66, 'own_node_removals_verified': 4, 'auditor_sha256': sha(ROOT / 'scripts/audit_full_main_20261121.py'),
              'driver_sha256': sha(Path(__file__)), 'independent_audit_sha256': sha(output / 'independent-replay/independent-full-main-audit.json'),
              'paired_audit_sha256': {t: sha(output / 'independent-replay' / (t + '-independent-paired-audit.json')) for t in ('inventory', 'analytics')},
              'component_sha256': {t: sha(output / 'independent-replay' / (t + '-component-reconstructed.json')) for t in ('inventory', 'analytics')},
              'model_refit': False, 'new_measurement_acquisition': False, 'new_qualification_attempt': False,
              'acquisition_gates_modified': False, 'public_artifact_or_release_upload': False,
              'permissions': 'contents:read; no artifact or release uploads', 'python': sys.version, 'platform': platform.platform()}
    save(output / 'linux-recheck-report.json', report)
    print('QRL_FULL_MAIN_INDEPENDENT_AUDIT ' + json.dumps(result, sort_keys=True), flush=True)
    print('QRL_FULL_MAIN_LINUX_RECHECK ' + json.dumps(report, sort_keys=True), flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    run(args.output)
