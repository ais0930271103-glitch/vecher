"""Small stdlib-only resource-limit launcher for the media worker's tools.

Called only with internally built argv, never with request fields. Keeping limits
in this fresh process avoids Python preexec_fn callbacks in a threaded parent.
"""
import os
import resource
import sys


def main():
    timeout, output_limit, executable, *arguments = sys.argv[1:]
    cpu = max(1, min(660, int(timeout) + 5))
    size = max(1024, min(65 * 1024 * 1024, int(output_limit)))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_FSIZE, (size, size))
    resource.setrlimit(resource.RLIMIT_AS, (1024 * 1024 * 1024, 1024 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    os.execve(executable, [executable, *arguments],
              {"PATH": os.defpath, "LANG": "C.UTF-8", "AV_LOG_FORCE_NOCOLOR": "1"})


if __name__ == "__main__":
    main()
