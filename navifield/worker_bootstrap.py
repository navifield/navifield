"""Set POSIX affinity before starting the fixed local CLI worker."""
import os
import sys


def main():
    if len(sys.argv) != 3:
        raise ValueError('invalid affinity bootstrap arguments')
    cpus = {int(value) for value in sys.argv[1].split(',')}
    if not cpus or any(cpu < 0 for cpu in cpus):
        raise ValueError('invalid affinity bootstrap CPUs')
    os.sched_setaffinity(0, cpus)
    if os.sched_getaffinity(0) != cpus:
        raise ValueError('affinity bootstrap readback mismatch')
    os.execv(sys.executable, [sys.executable, '-B', '-m', sys.argv[2]])


if __name__ == '__main__':
    main()
