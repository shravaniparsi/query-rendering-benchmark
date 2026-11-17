"""Read-only exact Linux replay of the already claimed first main date."""
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
from scripts.audit_main_attempt_20261117 import audit, RUN, REPLAY, DAY, SOURCE, RELEASE, CLAIM

REPO = 'shravaniparsi/query-rendering-benchmark'
BRANCH = 'offline-main-20261117-recheck-v1'


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
            and os.environ['QRL_MAIN_ENABLED'] == 'true' and os.environ['QRL_QUALIFICATION_ENABLED'] == 'false',
            'unchanged frozen main gates required')
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
        require(provider['target_commitish'] == SOURCE, 'original frozen provider source differs')
        selected = [r for r in provider['assets'] if folder != 'raw' or ('-' + RUN + '-1.') in r['name']]
        require(len(selected) == count, 'complete original attempt assets required')
        save(target / 'provider-release.json', provider)
        for asset in selected:
            download(api, provider, asset, target / asset['name'])
    save(inputs / 'claim-tag.json', api.call('GET', '/git/tags/' + CLAIM))
    save(inputs / 'main-claim-refs.json', api.call('GET', '/git/matching-refs/tags/reserved-main-v2-'))
    save(inputs / 'actual-run.json', api.call('GET', '/actions/runs/' + RUN))
    save(inputs / 'actual-replay-run.json', api.call('GET', '/actions/runs/' + REPLAY))
    save(inputs / 'gate-readback.json', [{'name': name, 'value': os.environ[name]} for name in
         ('QRL_FROZEN_EXECUTION_SHA', 'QRL_RELEASE_SHA256', 'QRL_MAIN_ENABLED', 'QRL_QUALIFICATION_ENABLED')])
    subprocess.run(['git', 'bundle', 'create', str((inputs / 'public-source.bundle').resolve()),
                    'refs/tags/reserved-main-v3-amended-frozen-source'], cwd=ROOT, check=True, capture_output=True)
    result = audit(inputs, output / 'independent-replay')
    require(result['status'] == 'passed' and result['inventory_exact_paired_replay'] is True
            and result['own_nodes_verified_removed'] == 4 and result['analytics_component'] == 'unavailable',
            'exact full inventory replay and original unavailable analytics required')
    report = {'version': 'actual-first-main-linux-recheck-v1', 'status': 'passed',
              'recheck_run_id': os.environ['GITHUB_RUN_ID'], 'recheck_source_commit': current,
              'parent_main_run_id': RUN, 'automatic_replay_run_id': REPLAY, 'collection_date_utc': DAY,
              'frozen_execution_source': SOURCE, 'release_sha256': RELEASE, 'claim_sha': CLAIM,
              'provider_assets_verified': 17, 'raw_member_digests_verified': 2445,
              'inventory_exact_full_cells': 66, 'analytics_component': 'unavailable',
              'own_node_removals_verified': 4, 'auditor_sha256': sha(ROOT / 'scripts/audit_main_attempt_20261117.py'),
              'driver_sha256': sha(Path(__file__)),
              'independent_audit_sha256': sha(output / 'independent-replay/independent-main-attempt-audit.json'),
              'inventory_paired_audit_sha256': sha(output / 'independent-replay/inventory-independent-paired-audit.json'),
              'inventory_component_sha256': sha(output / 'independent-replay/inventory-component-reconstructed.json'),
              'model_refit': False, 'new_measurement_acquisition': False, 'new_qualification_attempt': False,
              'acquisition_gates_modified': False, 'public_artifact_or_release_upload': False,
              'permissions': 'contents:read; no artifact or release uploads', 'python': sys.version, 'platform': platform.platform()}
    save(output / 'linux-recheck-report.json', report)
    print('QRL_MAIN_LINUX_RECHECK ' + json.dumps(report, sort_keys=True), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    run(args.output)


if __name__ == '__main__':
    main()
