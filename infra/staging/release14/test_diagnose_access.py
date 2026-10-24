"""Local config fixtures and command doubles; never contact the server/GitHub."""
import importlib.util
import json
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.request

import pytest

PATH = Path(__file__).with_name('diagnose_access.py')
spec = importlib.util.spec_from_file_location('diagnose_access', PATH)
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)
ORIGIN = 'private-alias:ais0930271103-glitch/vecher.git'
SSH_OUTPUT = ('hostname github.com\nuser git\nport 22\nstricthostkeychecking ask\n'
              'identityfile /private/key-name\nproxycommand none\nproxyjump none\n')


class Run:
    def __init__(self, origins=None, effective=None, ssh_output=SSH_OUTPUT, variant=None, core=None):
        self.origins = [ORIGIN] if origins is None else origins
        self.effective = self.origins if effective is None else effective
        self.ssh_output, self.variant, self.core = ssh_output, variant, core
        self.calls = []

    def __call__(self, argv, env, **kwargs):
        self.calls.append(argv)
        assert env['GIT_SSH_COMMAND'] == 'ssh -o BatchMode=yes'
        assert env['GIT_OPTIONAL_LOCKS'] == '0'
        if argv[0] == 'ssh':
            assert argv[:4] == ['ssh', '-G', '-o', 'BatchMode=yes']
            return 0, self.ssh_output
        assert argv[:3] == ['git', '-C', str(d.REPO)]
        if argv[3:] == ['remote', 'get-url', '--all', 'origin']:
            return 0, '\n'.join(self.effective) + '\n'
        assert argv[3:6] == ['config', '--null', '--get-all']
        values = {'remote.origin.url':self.origins, 'ssh.variant':self.variant, 'core.sshCommand':self.core}[argv[6]]
        return (1, '') if values is None else (0, '\0'.join(values) + '\0')


def facts(tmp_path, run=None, env=None):
    return d.access_facts(run=run or Run(), environ=env or {}, home=tmp_path/'home', system=tmp_path/'system')


def user_config(tmp_path, text):
    folder = tmp_path/'home/.ssh'
    folder.mkdir(parents=True, exist_ok=True)
    (folder/'config').write_text(text)
    return folder


def test_environment_matches_fixed_ssh_but_never_writes_trace_files():
    original = {'GIT_SSH_COMMAND':'private command', 'GIT_CONFIG_COUNT':'1', 'GIT_TRACE':'/private/log', 'SSLKEYLOGFILE':'/private/keys', 'HOME':'/home/example'}
    result = d.runner_environment(original)
    assert result['GIT_SSH_COMMAND'] == 'ssh -o BatchMode=yes'
    assert result['GIT_CONFIG_COUNT'] == '1' and result['HOME'] == '/home/example'
    assert 'GIT_TRACE' not in result and 'SSLKEYLOGFILE' not in result and original['GIT_TRACE'] == '/private/log'


def test_alias_user_and_port_are_resolved_without_forcing_them(tmp_path):
    user_config(tmp_path, 'Host private-alias\n HostName github.com\n User git\n IdentityFile /private/key-name\n')
    run = Run(core=['ssh -F /private/config -i /private/key'])
    git, ssh = facts(tmp_path, run)
    assert git['origin_url_count'] == git['effective_url_count'] == 1
    assert git['core_ssh_command_present'] is True and git['fetch_url_rewritten'] is False
    assert ssh['resolution_performed'] and ssh['alias_resolved']
    assert ssh['resolved_host_is_github'] and ssh['resolved_user_is_git'] and ssh['resolved_port_allowed']
    assert ssh['resolved_host_category']=='github' and ssh['resolved_port_category']=='22'
    assert ssh['identities_configured'] is True
    argv = run.calls[-1]
    assert argv == ['ssh', '-G', '-o', 'BatchMode=yes', '--', 'private-alias']
    assert not any(secret in json.dumps((git,ssh)) for secret in ('private-alias','/private/key','/private/config'))


def test_git_rewrite_is_checked_before_ssh_resolution(tmp_path):
    run = Run(origins=['https://github.com/'+d.REPO_PATH+'.git'], effective=[ORIGIN])
    git, ssh = facts(tmp_path, run)
    assert git['origin_category'] == 'https' and git['effective_category'] == 'ssh_scp'
    assert git['fetch_url_rewritten'] and ssh['resolution_performed']
    assert run.calls[-2][3:] == ['remote','get-url','--all','origin']


def test_url_user_and_port_are_preserved_and_nonstandard_port_is_reported(tmp_path):
    run = Run(origins=['ssh://git@private-alias:443/'+d.REPO_PATH], ssh_output=SSH_OUTPUT.replace('port 22','port 443'))
    _, ssh = facts(tmp_path, run)
    assert run.calls[-1] == ['ssh','-G','-o','BatchMode=yes','-l','git','-p','443','--','private-alias']
    assert ssh['resolved_user_is_git'] is True and ssh['resolved_port_allowed'] is False


def test_alternate_github_endpoint_is_classified_without_changing_permission_flags(tmp_path):
    output=SSH_OUTPUT.replace('github.com','ssh.github.com').replace('port 22','port 443')
    _,ssh=facts(tmp_path,Run(ssh_output=output))
    assert ssh['resolved_host_category']=='github_ssh_endpoint' and ssh['resolved_port_category']=='443'
    assert ssh['resolved_host_is_github'] is False and ssh['resolved_port_allowed'] is False


def test_foreign_resolved_host_or_default_root_user_never_looks_like_github(tmp_path):
    run = Run(ssh_output=SSH_OUTPUT.replace('github.com','elsewhere.invalid').replace('user git','user root'))
    _, ssh = facts(tmp_path, run)
    assert ssh['resolution_performed'] and not ssh['resolved_host_is_github'] and not ssh['resolved_user_is_git']
    assert 'elsewhere.invalid' not in json.dumps(ssh) and 'root' not in json.dumps(ssh)


@pytest.mark.parametrize('origin',[
    '-option:'+d.REPO_PATH, 'alias;COMMAND:'+d.REPO_PATH, 'alias:'+d.REPO_PATH+'/extra',
    'alias:other-owner/vecher.git', 'alias:'+d.REPO_PATH+'\nOTHER',
    'ssh://git:SECRET@private-alias/'+d.REPO_PATH, 'ssh://git@private-alias/'+d.REPO_PATH+'?secret=VALUE',
    'ssh://-option@private-alias/'+d.REPO_PATH, 'ext::command',
])
def test_unsafe_effective_url_never_reaches_ssh(tmp_path,origin):
    run = Run(effective=[origin])
    _, ssh = facts(tmp_path, run)
    assert not ssh['resolution_performed'] and all(argv[0]=='git' for argv in run.calls)


@pytest.mark.parametrize('origins',[[ORIGIN,ORIGIN],[ORIGIN,''],[ORIGIN,'https://SECRET@other.invalid/private']])
def test_multiple_origins_do_not_select_one_for_resolution(tmp_path,origins):
    run = Run(origins=origins)
    git, ssh = facts(tmp_path, run)
    assert git['origin_url_count']==2 and ssh['reason']=='origin_not_unique'
    assert all(argv[0]=='git' for argv in run.calls) and 'SECRET' not in json.dumps((git,ssh))


@pytest.mark.parametrize('env,variant',[({'GIT_SSH_VARIANT':'plink'},None),({},['simple'])])
def test_unsupported_ssh_variant_does_not_resolve(tmp_path,env,variant):
    run = Run(variant=variant)
    git, ssh = facts(tmp_path, run, env)
    assert not git['ssh_variant_supported'] and ssh['reason']=='ssh_variant_unsupported'
    assert all(argv[0]=='git' for argv in run.calls)


@pytest.mark.parametrize('line', ['Match exec "touch /private/marker"\n', 'Match !exec "private-command"\n', 'Match exec="private-command"\n'])
def test_match_exec_is_detected_before_any_ssh_process(tmp_path,line):
    folder = user_config(tmp_path,'Include config.d/*.conf\n')
    (folder/'config.d').mkdir();(folder/'config.d/dynamic.conf').write_text(line)
    run = Run(); _, ssh = facts(tmp_path,run)
    assert ssh['match_exec_present'] is True and ssh['reason']=='ssh_match_exec_present'
    assert not ssh['resolution_performed'] and all(argv[0]=='git' for argv in run.calls)


def test_static_includes_and_comments_can_be_resolved(tmp_path):
    folder = user_config(tmp_path,'# Match exec "not a command"\nInclude=config.d/*.conf\n')
    (folder/'config.d').mkdir();(folder/'config.d/alias.conf').write_text('Host private-alias\n HostName github.com\n')
    _, ssh = facts(tmp_path)
    assert ssh['resolution_performed'] and ssh['match_exec_present'] is False


@pytest.mark.parametrize('include',['id_ed25519','*','~/.ssh/*.conf','${HOME}/.ssh/*.conf','/outside/private.conf','config.d/**.conf'])
def test_unsupported_include_is_not_read_or_resolved(tmp_path,include):
    folder = user_config(tmp_path,'Include '+include+'\n')
    (folder/'id_ed25519').write_text('PRIVATE KEY MUST NOT BE READ')
    run=Run(); _, ssh=facts(tmp_path,run)
    assert ssh['reason']=='unsupported_ssh_include' and not ssh['resolution_performed']
    assert all(argv[0]=='git' for argv in run.calls)


def test_symlink_include_and_oversize_config_fail_closed(tmp_path):
    folder=user_config(tmp_path,'Include link.conf\n')
    secret=tmp_path/'private-key';secret.write_text('DO NOT READ')
    (folder/'link.conf').symlink_to(secret)
    _,ssh=facts(tmp_path)
    assert ssh['reason']=='unsupported_ssh_config_file' and not ssh['resolution_performed']
    (folder/'config').write_text('#'+('x'*65536))
    _,ssh=facts(tmp_path)
    assert ssh['reason']=='ssh_config_limit' and not ssh['resolution_performed']


def test_proxy_and_command_flags_do_not_disclose_commands_or_identity_paths(tmp_path):
    output=SSH_OUTPUT.replace('proxycommand none','proxycommand custom --secret PRIVATE_PROXY')
    output=output.replace('proxyjump none','proxyjump PRIVATE_JUMP').replace('stricthostkeychecking ask','stricthostkeychecking false')
    output += 'knownhostscommand PRIVATE_KNOWN\npermitlocalcommand yes\nlocalcommand PRIVATE_LOCAL\nhostkeyalias PRIVATE_ALIAS\n'
    _,ssh=facts(tmp_path,Run(ssh_output=output))
    assert all(ssh[key] for key in ('proxy_command_present','proxy_jump_present','host_key_verification_disabled',
                                  'known_hosts_command_present','local_command_enabled','host_key_alias_present'))
    assert 'PRIVATE' not in json.dumps(ssh) and '/private/' not in json.dumps(ssh)


def probe(monkeypatch,capsys,body=None,error=None,url='http://127.0.0.1:8092/health',kind='health'):
    handlers=[]
    class Response:
        status=200
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,limit):return json.dumps(body).encode()[:limit]
    class Opener:
        def open(self,request,timeout):
            assert timeout==8
            if error:raise error
            return Response()
    def build(*args):handlers.extend(args);return Opener()
    monkeypatch.setattr(urllib.request,'build_opener',build)
    monkeypatch.setattr(sys,'argv',['probe',url,kind])
    exec(d.HTTP_PROBE,{'__name__':'__main__'})
    return json.loads(capsys.readouterr().out),handlers


def test_local_health_ignores_proxies_and_filters_response(monkeypatch,capsys):
    result,handlers=probe(monkeypatch,capsys,{'ok':True,'env':'staging','version':'0.8.0','SECRET':'PRIVATE'})
    assert result=={'http_status':200,'error_category':None,'health_ok':True,'env_staging':True,'api_version':'0.8.0'}
    assert any(isinstance(h,urllib.request.ProxyHandler) and h.proxies=={} for h in handlers)
    redirect=next(h for h in handlers if isinstance(h,urllib.request.HTTPRedirectHandler))
    assert redirect.redirect_request(None,None,302,'PRIVATE',{},'https://other.invalid') is None
    context=next(h._context for h in handlers if isinstance(h,urllib.request.HTTPSHandler))
    assert context.check_hostname and context.verify_mode==ssl.CERT_REQUIRED


def test_apk_output_is_limited_to_version_and_release13_comparison(monkeypatch,capsys):
    result,_=probe(monkeypatch,capsys,{'version_code':13,'version_name':'0.8.0','bytes':36591005,
        'sha256':'a87d14f11e874f81509e4c6964f1c7355103a61e24d4e279ff61e454102bc1bc','notes':'PRIVATE','url':'PRIVATE'},kind='apk')
    assert result['apk13_hash_matches'] and result['apk13_bytes_matches'] and result['apk_version_code']==13
    assert 'PRIVATE' not in json.dumps(result) and 'sha256' not in result


def test_malformed_response_is_not_reported_as_a_success(monkeypatch,capsys):
    result,_=probe(monkeypatch,capsys,{'version':'PRIVATE_SECRET'})
    assert result['error_category']=='invalid_response' and 'PRIVATE_SECRET' not in json.dumps(result)


@pytest.mark.parametrize('error,category',[
    (urllib.error.HTTPError('https://PRIVATE',302,'SECRET',{},None),'redirect_blocked'),
    (urllib.error.URLError(ssl.SSLCertVerificationError('PRIVATE')),'tls_verification_failed'),
    (urllib.error.URLError(socket.gaierror('PRIVATE')),'dns_error'),
    (urllib.error.URLError(ConnectionRefusedError('PRIVATE')),'connection_refused'),
    (urllib.error.URLError(TimeoutError('PRIVATE')),'timeout'),
])
def test_http_errors_are_redacted(monkeypatch,capsys,error,category):
    result,_=probe(monkeypatch,capsys,error=error)
    assert result['error_category']==category and 'PRIVATE' not in json.dumps(result) and 'SECRET' not in json.dumps(result)


def test_http_child_has_hard_deadline_and_filtered_timeout():
    calls=[]
    def run(argv,env,**kwargs):
        calls.append((argv,kwargs));raise d.DiagnosticError('timeout')
    result=d.endpoint_facts(*d.ENDPOINTS['server_public_https'],run=run,environ={})
    assert result=={'http_status':None,'error_category':'timeout'}
    assert calls[0][0][1:3]==['-I','-'] and calls[0][1]['timeout']==10
    assert calls[0][1]['data']==d.HTTP_PROBE.encode()


def test_stdin_cli_emits_exactly_safe_json_and_complete_marker():
    # Execute the real entry point from stdin. All child commands are replaced
    # before loading it, so this test cannot read server config or use network.
    launcher='''import subprocess
def fake(argv,**kwargs):
 if argv[0]=='git':
  output=b'https://github.com/ais0930271103-glitch/vecher.git\\0' if argv[-1]=='remote.origin.url' else b''
  code=0 if output else 1
 else: code,output=0,b'{"http_status":200,"error_category":null}'
 return subprocess.CompletedProcess(argv,code,output,b'')
subprocess.run=fake
exec(compile(SOURCE,'<stdin>','exec'),{'__name__':'__main__'})
'''.replace('SOURCE',repr(PATH.read_text()))
    result=subprocess.run([sys.executable,'-'],input=launcher.encode(),capture_output=True,timeout=5,check=True)
    lines=result.stdout.decode().splitlines()
    assert len(lines)==2 and lines[1]==d.MARKER and not result.stderr
    report=json.loads(lines[0]);assert report['format']==1 and report['ssh']['resolution_performed'] is False
    assert set(d.ENDPOINTS)<=report.keys()
