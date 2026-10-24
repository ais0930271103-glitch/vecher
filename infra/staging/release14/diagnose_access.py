#!/usr/bin/env python3
"""Read-only access facts; supports ``python3 -`` and never authorizes deploy.

No key files, remote authentication, fetch, checkout, config writes, or raw
URLs/configuration/exception messages are emitted. The final marker means
that a report was collected, including any partial or unsupported results.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import glob
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import stat
import subprocess
import sys
from urllib.parse import urlsplit

REPO = Path('/opt/vecher-test/repo')
REPO_PATH = 'ais0930271103-glitch/vecher'
SAFE_TOKEN = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,252}\Z')
MARKER = 'VECHER_ACCESS_DIAGNOSTIC_COMPLETE'
ENDPOINTS = {
    'server_api_local': ('http://127.0.0.1:8092/health', 'health'),
    'server_apk_local': ('http://127.0.0.1:8092/app/version', 'apk'),
    'server_public_https': ('https://api-test.xn--b1aga8b7a.online/health', 'health'),
}


class DiagnosticError(Exception):
    pass


def runner_environment(environ):
    result = dict(environ)
    result.update(GIT_TERMINAL_PROMPT='0', GIT_SSH_COMMAND='ssh -o BatchMode=yes', GIT_OPTIONAL_LOCKS='0')
    for key in tuple(result):
        if key.startswith(('COMPOSE_', 'GIT_TRACE')) or key in {'DOCKER_HOST', 'DOCKER_CONTEXT', 'BASH_ENV', 'ENV', 'SSLKEYLOGFILE'}:
            result.pop(key, None)
    return result


def command(argv, env, *, data=None, timeout=8):
    try:
        result = subprocess.run(argv, input=data, stdin=subprocess.DEVNULL if data is None else None,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise DiagnosticError('timeout') from None
    except OSError:
        raise DiagnosticError('command_unavailable') from None
    if len(result.stdout) > 128 * 1024 or len(result.stderr) > 128 * 1024:
        raise DiagnosticError('output_limit')
    try:
        output = result.stdout.decode('utf-8')
    except UnicodeError:
        raise DiagnosticError('invalid_encoding') from None
    return result.returncode, output


def parse_origin(value):
    """Private parsed fields must never be serialized by the caller."""
    result = dict(category='unknown', path_matches=False, host=None, user=None, port=None,
                  safe_ssh=False, credentials_present=False)
    if not value or any(c.isspace() or ord(c) < 32 for c in value):
        return result
    try:
        if '://' in value:
            parsed = urlsplit(value)
            result.update(category={'ssh': 'ssh_url', 'https': 'https'}.get(parsed.scheme, 'other'),
                          host=parsed.hostname, user=parsed.username, port=parsed.port,
                          credentials_present=parsed.username is not None or parsed.password is not None)
            path = parsed.path.removeprefix('/')
            valid = not parsed.query and not parsed.fragment and parsed.password is None
        else:
            matched = re.fullmatch(r'(?:(?P<user>[^/@:]+)@)?(?P<host>[^/:]+):(?P<path>.+)', value)
            if not matched:
                return result
            result.update(category='ssh_scp', host=matched['host'], user=matched['user'],
                          credentials_present=matched['user'] is not None)
            path, valid = matched['path'], True
        result['path_matches'] = path.removesuffix('.git') == REPO_PATH
        result['safe_ssh'] = bool(valid and result['path_matches'] and result['category'] in {'ssh_url', 'ssh_scp'}
            and SAFE_TOKEN.fullmatch(result['host'] or '')
            and (result['user'] is None or SAFE_TOKEN.fullmatch(result['user']))
            and (result['port'] is None or 1 <= result['port'] <= 65535))
    except ValueError:
        pass
    return result


class SSHConfigScan:
    """Conservative local config reader. Unsupported includes stop resolution.

    Only the default config files and *.conf beneath their SSH directories
    are read. Broad globs, key-file names, links, external roots, dynamic
    includes, and incomplete scans never lead to ssh -G.
    """
    def __init__(self, home, system=Path('/etc/ssh')):
        self.user = Path(home) / '.ssh'
        self.system = Path(system)
        self.seen = set()
        self.total = 0
        self.match_exec_present = False

    def scan(self):
        self.read(self.user / 'config', self.user, 0, default=True)
        self.read(self.system / 'ssh_config', self.system, 0, default=True)

    def read(self, path, base, depth, *, default=False):
        if depth > 6 or len(self.seen) >= 32:
            raise DiagnosticError('ssh_config_limit')
        path = Path(os.path.abspath(path))
        if not path.is_relative_to(base) or (not default and path.suffix != '.conf'):
            raise DiagnosticError('unsupported_ssh_include')
        if path in self.seen:
            raise DiagnosticError('ssh_include_cycle')
        try:
            info = path.lstat()
        except FileNotFoundError:
            if default:
                return
            raise DiagnosticError('ssh_config_unreadable') from None
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise DiagnosticError('unsupported_ssh_config_file')
        for parent in path.parents:
            if parent.is_symlink():
                raise DiagnosticError('unsupported_ssh_config_file')
        if info.st_size > 64 * 1024:
            raise DiagnosticError('ssh_config_limit')
        self.seen.add(path)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or (opened.st_dev,opened.st_ino) != (info.st_dev,info.st_ino):
                raise DiagnosticError('ssh_config_changed')
            raw = handle.read(64 * 1024 + 1)
        self.total += len(raw)
        if len(raw) > 64 * 1024 or self.total > 256 * 1024:
            raise DiagnosticError('ssh_config_limit')
        try:
            lines = raw.decode('utf-8').splitlines()
            for line in lines:
                if not line.strip() or line.lstrip().startswith('#'):
                    continue
                matched = re.fullmatch(r'\s*([A-Za-z][A-Za-z0-9]*)\s*(?:=\s*)?(.*?)\s*', line)
                if not matched:
                    raise DiagnosticError('unsupported_ssh_config_syntax')
                key, value = matched[1].lower(), shlex.split(matched[2], comments=True)
                if key == 'match' and any(token.lstrip('!').split('=',1)[0].lower() == 'exec' for token in value):
                    self.match_exec_present = True
                    raise DiagnosticError('ssh_match_exec_present')
                if key == 'match' and any(token.lstrip('!').split('=',1)[0].lower() == 'command' for token in value):
                    raise DiagnosticError('ssh_command_match_unsupported')
                if key != 'include':
                    continue
                for pattern in value:
                    if any(token in pattern for token in ('$', '%', '**', '\\')) or pattern.startswith('~'):
                        raise DiagnosticError('unsupported_ssh_include')
                    candidate = Path(pattern) if Path(pattern).is_absolute() else base / pattern
                    candidate = Path(os.path.abspath(candidate))
                    if not candidate.is_relative_to(base) or candidate.suffix != '.conf':
                        raise DiagnosticError('unsupported_ssh_include')
                    matches = sorted(glob.glob(str(candidate)))
                    if len(matches) > 32:
                        raise DiagnosticError('ssh_config_limit')
                    for included in matches:
                        self.read(Path(included), base, depth + 1)
        except (UnicodeError, ValueError):
            raise DiagnosticError('unsupported_ssh_config_syntax') from None


def access_facts(*, run=command, environ=None, home=None, system=Path('/etc/ssh')):
    original = os.environ if environ is None else environ
    env = runner_environment(original)
    git = dict(origin_url_count=None, origin_category='unknown', origin_repository_path_matches=None,
               effective_url_count=None, effective_category='unknown', effective_repository_path_matches=None,
               fetch_url_rewritten=None, core_ssh_command_present=None, ssh_command_mode='runner_fixed_openssh',
               ssh_env_override_present=any(k in original for k in ('GIT_SSH', 'GIT_SSH_COMMAND')),
               git_config_env_override_present=any(k.startswith('GIT_CONFIG_') for k in original),
               ssh_variant_supported=None, reason=None)
    ssh = dict(resolution_performed=False, reason='not_checked', alias_resolved=None,
               resolved_host_is_github=None, resolved_user_is_git=None, resolved_port_allowed=None,
               resolved_host_category=None, resolved_port_category=None,
               proxy_command_present=None, proxy_jump_present=None, match_exec_present=None,
               host_key_verification_disabled=None, identities_configured=None,
               known_hosts_command_present=None, local_command_enabled=None, host_key_alias_present=None)
    def call(*args):
        return run(['git', '-C', str(REPO), *args], env)
    def config(key):
        code, output = call('config', '--null', '--get-all', key)
        if code == 1:
            return []
        if code != 0 or not output.endswith('\0'):
            raise DiagnosticError('git_config_unavailable')
        return output[:-1].split('\0')
    try:
        origins = config('remote.origin.url')
        git['origin_url_count'] = len(origins)
        if len(origins) == 1:
            parsed = parse_origin(origins[0])
            git.update(origin_category=parsed['category'], origin_repository_path_matches=parsed['path_matches'])
        git['core_ssh_command_present'] = bool(config('core.sshCommand'))
        variant = config('ssh.variant')
        active_variant = original.get('GIT_SSH_VARIANT', variant[-1] if variant else 'auto')
        git['ssh_variant_supported'] = active_variant in {'auto', 'ssh'}
        code, output = call('remote', 'get-url', '--all', 'origin')
        if code != 0:
            raise DiagnosticError('effective_url_unavailable')
        effective = output.splitlines()
        git['effective_url_count'] = len(effective)
        if len(origins) != 1 or len(effective) != 1:
            raise DiagnosticError('origin_not_unique')
        parsed = parse_origin(effective[0])
        git.update(effective_category=parsed['category'], effective_repository_path_matches=parsed['path_matches'],
                   fetch_url_rewritten=origins[0] != effective[0])
        if not parsed['safe_ssh']:
            ssh['reason'] = 'effective_url_not_supported_ssh'
            return git, ssh
        if not git['ssh_variant_supported']:
            ssh['reason'] = 'ssh_variant_unsupported'
            return git, ssh
        scan = SSHConfigScan(home if home is not None else pwd.getpwuid(os.getuid()).pw_dir, system)
        try:
            scan.scan()
        finally:
            ssh['match_exec_present'] = scan.match_exec_present
        argv = ['ssh', '-G', '-o', 'BatchMode=yes']
        if parsed['user'] is not None:
            argv += ['-l', parsed['user']]
        if parsed['port'] is not None:
            argv += ['-p', str(parsed['port'])]
        code, output = run([*argv, '--', parsed['host']], env)
        if code != 0:
            raise DiagnosticError('ssh_configuration_unavailable')
        fields = {}
        for line in output.splitlines():
            key, separator, value = line.partition(' ')
            if separator:
                fields.setdefault(key.lower(), []).append(value.strip())
        if any(len(fields.get(key, [])) != 1 for key in ('hostname', 'user', 'port', 'stricthostkeychecking')):
            raise DiagnosticError('ssh_configuration_ambiguous')
        def one(key):
            return fields.get(key, [''])[0]
        def present(key):
            return any(value and value.lower() != 'none' for value in fields.get(key, []))
        host = one('hostname').lower()
        ssh.update(resolution_performed=True, reason=None, alias_resolved=parsed['host'].lower() != host,
                   resolved_host_is_github=host == 'github.com', resolved_user_is_git=one('user') == 'git',
                   resolved_host_category={'github.com':'github','ssh.github.com':'github_ssh_endpoint'}.get(host,'other'),
                   resolved_port_category=one('port') if one('port') in {'22','443'} else 'other',
                   resolved_port_allowed=one('port') == '22', proxy_command_present=present('proxycommand'),
                   proxy_jump_present=present('proxyjump'),
                   host_key_verification_disabled=one('stricthostkeychecking').lower() in {'no', 'false', 'off'},
                   identities_configured=present('identityfile'), known_hosts_command_present=present('knownhostscommand'),
                   local_command_enabled=one('permitlocalcommand').lower() in {'yes', 'true'} and present('localcommand'),
                   host_key_alias_present=present('hostkeyalias'))
    except DiagnosticError as error:
        if git['effective_url_count'] is None or git['effective_url_count'] != 1:
            git['reason'] = str(error)
        ssh['reason'] = str(error)
    except OSError:
        ssh['reason'] = 'local_configuration_unavailable'
    return git, ssh


# A bounded child process makes the deadline include DNS resolution. Its only
# stdout is filtered JSON, and no redirects or client certificates are used.
HTTP_PROBE = r'''
import json, socket, ssl, sys, urllib.error, urllib.request
url, kind = sys.argv[1:]
result = {'http_status':None,'error_category':None}
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs): return None
def category(error):
 if isinstance(error,ssl.SSLCertVerificationError): return 'tls_verification_failed'
 if isinstance(error,ssl.SSLError): return 'tls_error'
 if isinstance(error,(TimeoutError,socket.timeout)): return 'timeout'
 if isinstance(error,socket.gaierror): return 'dns_error'
 if isinstance(error,ConnectionRefusedError): return 'connection_refused'
 return 'network_error'
try:
 handlers=[NoRedirect(),urllib.request.HTTPSHandler(context=ssl.create_default_context())]
 if url.startswith('http://127.0.0.1:'): handlers.append(urllib.request.ProxyHandler({}))
 opener=urllib.request.build_opener(*handlers)
 with opener.open(urllib.request.Request(url,headers={'Accept':'application/json'}),timeout=8) as response:
  result['http_status']=response.status; raw=response.read(16385)
 if len(raw)>16384: raise ValueError()
 body=json.loads(raw)
 if not isinstance(body,dict): raise ValueError()
 import re
 def version(value):
  return value if isinstance(value,str) and re.fullmatch(r'[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}',value) else None
 if kind=='health':
  result.update(health_ok=body.get('ok') is True,env_staging=body.get('env')=='staging',api_version=version(body.get('version')))
  if type(body.get('ok')) is not bool or not isinstance(body.get('env'),str) or result['api_version'] is None: raise ValueError()
 else:
  code, size = body.get('version_code'),body.get('bytes')
  result.update(apk_version=version(body.get('version_name')),
   apk_version_code=code if type(code) is int and 0<=code<=2147483647 else None,
   apk_bytes=size if type(size) is int and 0<=size<=268435456 else None,
   apk13_hash_matches=body.get('sha256')=='a87d14f11e874f81509e4c6964f1c7355103a61e24d4e279ff61e454102bc1bc',
   apk13_bytes_matches=type(size) is int and size==36591005)
  if any(result[key] is None for key in ('apk_version','apk_version_code','apk_bytes')) or not re.fullmatch(r'[0-9a-f]{64}',str(body.get('sha256',''))): raise ValueError()
except urllib.error.HTTPError as error:
 result.update(http_status=error.code,error_category='redirect_blocked' if 300<=error.code<400 else 'http_error')
except urllib.error.URLError as error: result['error_category']=category(error.reason)
except (ValueError,UnicodeError): result['error_category']='invalid_response'
except Exception as error: result['error_category']=category(error)
print(json.dumps(result,sort_keys=True))
'''


def endpoint_facts(url, kind, *, run=command, environ=None):
    env = runner_environment(os.environ if environ is None else environ)
    try:
        code, output = run([sys.executable, '-I', '-', url, kind], env, data=HTTP_PROBE.encode(), timeout=10)
        if code != 0:
            raise DiagnosticError('probe_failed')
        result = json.loads(output)
        if not isinstance(result, dict):
            raise DiagnosticError('invalid_response')
        return result
    except (DiagnosticError, ValueError) as error:
        return {'http_status':None,'error_category':str(error) if isinstance(error,DiagnosticError) else 'invalid_response'}


def collect():
    with ThreadPoolExecutor(max_workers=3) as pool:
        pending = {name:pool.submit(endpoint_facts,*parameters) for name,parameters in ENDPOINTS.items()}
        git, ssh = access_facts()
        return {'format':1,'git':git,'ssh':ssh,**{name:task.result() for name,task in pending.items()}}


def main():
    try:
        report = collect()
    except Exception:
        report = {'format':1,'error_category':'diagnostic_unavailable'}
    print(json.dumps(report,sort_keys=True))
    print(MARKER)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
