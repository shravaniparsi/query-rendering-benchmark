"""Enter a pre-created browser cpuset, drop setup privileges, then exec Node."""
import argparse
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cpu',required=True,type=int)
    parser.add_argument('--uid',required=True,type=int)
    parser.add_argument('--gid',required=True,type=int)
    parser.add_argument('command',nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command and args.command[0]=='--' else args.command
    if os.geteuid()!=0 or args.uid<=0 or args.gid<=0 or args.cpu<0 or not command:
        parser.error('root cpuset setup followed by a non-root command required')
    root = Path('/sys/fs/cgroup')
    groups = [p for p in (root/'qrl_browser_target',root/'qrl_browser_observer')
              if p.is_dir() and (p/'cpuset.cpus.effective').read_text().strip()==str(args.cpu)]
    if len(groups)!=1:
        raise RuntimeError('exactly one pre-created browser cpuset must match the assigned CPU')
    (groups[0]/'cgroup.procs').write_text(str(os.getpid())+'\n')
    os.sched_setaffinity(0,{args.cpu})
    os.setgroups([])
    os.setgid(args.gid)
    os.setuid(args.uid)
    os.execvp(command[0],command)


if __name__=='__main__':
    main()
