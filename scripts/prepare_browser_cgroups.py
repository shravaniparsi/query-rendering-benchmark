"""Create two browser-only cpuset cgroups on the disposable hosted Linux VM."""
import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target',required=True,type=int)
    parser.add_argument('--observer',required=True,type=int)
    parser.add_argument('--output',required=True,type=Path)
    args = parser.parse_args()
    if os.geteuid()!=0 or args.target==args.observer or not {args.target,args.observer}.issubset(os.sched_getaffinity(0)):
        raise RuntimeError('root setup on disposable VM with distinct allowed CPUs required')
    root = Path('/sys/fs/cgroup')
    if 'cpuset' not in (root/'cgroup.controllers').read_text().split():
        raise RuntimeError('unified cgroup cpuset controller required; no affinity-only fallback')
    if 'cpuset' not in (root/'cgroup.subtree_control').read_text().split():
        (root/'cgroup.subtree_control').write_text('+cpuset\n')
    mems = (root/'cpuset.mems.effective').read_text().strip()
    report = {'role':'browser cpuset containment setup','groups':{}}
    for role,cpu in (('target',args.target),('observer',args.observer)):
        path = root/('qrl_browser_'+role)
        path.mkdir(exist_ok=False)
        (path/'cpuset.mems').write_text(mems+'\n')
        (path/'cpuset.cpus').write_text(str(cpu)+'\n')
        effective = (path/'cpuset.cpus.effective').read_text().strip()
        if effective!=str(cpu):
            raise RuntimeError('browser effective cpuset differs from assignment')
        report['groups'][role] = {'path':str(path),'cpu':cpu,'effective_cpus':effective,'mems':mems}
    args.output.write_text(json.dumps(report,indent=2)+'\n')


if __name__=='__main__':
    main()
