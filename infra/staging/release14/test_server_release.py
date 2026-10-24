"""Release14 recovery gates: temporary files and command doubles, no Docker."""
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import ssl
import stat
import subprocess
import sys
import tarfile
from types import ModuleType, SimpleNamespace
import urllib.error
import urllib.request

import pytest

spec=importlib.util.spec_from_file_location('server_release14',Path(__file__).with_name('server_release.py'))
r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
IMAGE='sha256:'+'a'*64
OTHER_IMAGE='sha256:'+'b'*64


def private_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    r.durable(path,value)
    return path


@pytest.fixture
def release(tmp_path):
    backup=tmp_path/'backups';backup.mkdir(mode=0o700)
    root=tmp_path/'live';root.mkdir()
    instance=r.Release(root=root,backups=backup)
    instance.begin({'old_api_container':'old-api','db_container':'db','old_api_image_id':IMAGE,
                    'db_image_id':IMAGE,'old_env_sha256':'env','media_helper_sha256':'helper'})
    instance.journal.update(api_image_id=OTHER_IMAGE,worker_image_id=OTHER_IMAGE)
    instance.phase('images_pinned')
    return instance


def test_fixed_source_schema_and_artifact_pins():
    assert r.SOURCE=='40e43d85a2e434d4ee2090884a872a507f60fc93'
    assert r.OLD_SOURCE=='713b6b34819a6df382da03d5213135986d7c0c0c'
    assert (r.OLD_SCHEMA,r.SCHEMA)==('0005_content','0006_public_blogs')
    assert r.APK_SHA=='a87d14f11e874f81509e4c6964f1c7355103a61e24d4e279ff61e454102bc1bc'
    assert r.APK_BYTES==36591005


class NextPreflightGate(Exception):
    """Stop after the repository gate, without contacting Docker."""


class AccessCommands:
    """Actual reviewed parser/scanner, with Git and ssh -G command responses."""
    def __init__(self,origins,*,effective=None,config=None,ssh_fields=None,calls=None):
        self.origins=origins if isinstance(origins,list) else [origins]
        self.effective=self.origins if effective is None else effective
        self.config=config or {};self.fields=ssh_fields or {};self.calls=calls if calls is not None else []

    def __call__(self,argv,env,**kwargs):
        assert env['GIT_SSH_COMMAND']==r.SSH_COMMAND
        if argv[0]=='git':
            args=tuple(argv[3:]);self.calls.append(args)
            if args[:3]==('config','--null','--get-all'):
                values=self.origins if args[-1]=='remote.origin.url' else self.config.get(args[-1],[])
                return (0,'\0'.join(values)+'\0') if values else (1,'')
            assert args==('remote','get-url','--all','origin'),'No fetch, auth or writes in origin validation'
            return 0,'\n'.join(self.effective)+'\n'
        assert argv[:4]==['ssh','-G','-o','BatchMode=yes'] and argv[-2]=='--'
        self.calls.append(tuple(argv))
        host=argv[-1];host='github.com' if host=='github-vecher' else host
        fields={'hostname':host,'user':argv[argv.index('-l')+1] if '-l' in argv else 'git',
                'port':argv[argv.index('-p')+1] if '-p' in argv else '22','stricthostkeychecking':'ask',
                'identityfile':'/private/identity/path','permitlocalcommand':'no',**self.fields}
        return 0,'\n'.join(key+' '+value for key,value in fields.items())+'\n'


def access_report(repo,runner,tmp_path,**options):
    return r.verified_origin_report(repo,run=runner,environ=options.pop('environ',{}),
        home=tmp_path/'home',system=tmp_path/'system',**options)


@pytest.fixture
def origin_preflight(tmp_path,monkeypatch):
    root=tmp_path/'live';staging=root/'repo/infra/staging'
    (staging/'secrets').mkdir(parents=True)
    (staging/'.env').write_text('fixture-only')
    instance=r.Release(root=root,backups=tmp_path/'backups')
    # This test can run as an unprivileged CI user. Root and private-file
    # validation are covered separately; keep the real deploy/preflight order.
    monkeypatch.setattr(r.os,'geteuid',lambda:0)
    monkeypatch.setattr(r,'regular',lambda path,**kwargs:Path(path))
    calls=[]
    def configure(origin,**options):
        urls=origin if isinstance(origin,list) else [origin]
        answers={('rev-parse','HEAD'):r.OLD_SOURCE,('status','--porcelain'):''}
        def git(*args,strip=True):
            calls.append(args)
            assert args in answers,'Origin checking must never mutate Git configuration or source'
            if args[0]=='config':assert strip is False,'Keep all configured values, including empty ones'
            return answers[args]
        instance.git=git
        runner=AccessCommands(urls,calls=calls,**options)
        instance.origin_report=lambda:access_report(instance.repo,runner,tmp_path)
        return instance,calls
    class Docker:
        def service(self,*args,**kwargs):
            calls.append(('docker',*args));raise NextPreflightGate()
    instance.docker=Docker()
    def forbidden(*args,**kwargs):pytest.fail('Rejected origin reached a deployment mutation')
    for name in ('begin','prepare','freeze','rehearsal','migrate_live','activate_core'):
        setattr(instance,name,forbidden)
    return configure


@pytest.mark.parametrize('origin',[
    'https://github.com/ais0930271103-glitch/vecher',
    'https://github.com/ais0930271103-glitch/vecher.git',
    'git@github.com:ais0930271103-glitch/vecher',
    'git@github.com:ais0930271103-glitch/vecher.git',
    'ssh://git@github.com/ais0930271103-glitch/vecher',
    'ssh://git@github.com/ais0930271103-glitch/vecher.git',
    'github-vecher:ais0930271103-glitch/vecher.git',
    'git@github-vecher:ais0930271103-glitch/vecher.git',
    'ssh://github-vecher/ais0930271103-glitch/vecher.git',
    'ssh://git@github.com:22/ais0930271103-glitch/vecher.git',
])
def test_preflight_accepts_exact_repository_transport_forms(origin_preflight,origin):
    instance,calls=origin_preflight(origin)
    with pytest.raises(NextPreflightGate):instance.preflight()
    assert calls[:3]==[('rev-parse','HEAD'),('status','--porcelain'),
                       ('config','--null','--get-all','remote.origin.url')]
    assert ('remote','get-url','--all','origin') in calls and calls[-1]==('docker','api')
    assert not instance.current.exists() and instance.journal is None


@pytest.mark.parametrize('origin',[
    '',
    'http://github.com/ais0930271103-glitch/vecher.git',
    'git://github.com/ais0930271103-glitch/vecher.git',
    'https://github.com.evil.example/ais0930271103-glitch/vecher.git',
    'https://github.com@evil.example/ais0930271103-glitch/vecher.git',
    'https://token@github.com/ais0930271103-glitch/vecher.git',
    'https://github.com/another-owner/vecher.git',
    'https://github.com/ais0930271103-glitch/vecher-copy.git',
    'https://github.com/ais0930271103-glitch/vecher.git/extra',
    'https://github.com/ais0930271103-glitch/vecher.git?ref=main',
    'https://github.com/ais0930271103-glitch/vecher.git#main',
    'https://github.com/ais0930271103-glitch/%76echer.git',
    'git@github.com.evil.example:ais0930271103-glitch/vecher.git',
    'git@github.com:another-owner/vecher.git',
    'git@github.com:ais0930271103-glitch/vecher-copy.git',
    'ssh://git@github.com.evil.example/ais0930271103-glitch/vecher.git',
    'ssh://root@github.com/ais0930271103-glitch/vecher.git',
    'ssh://git@github.com:2222/ais0930271103-glitch/vecher.git',
    'ssh://git@github.com/ais0930271103-glitch/../vecher.git',
    'file:///opt/github.com/ais0930271103-glitch/vecher.git',
])
def test_wrong_origin_stops_deploy_before_checkpoint_or_live_mutations(origin_preflight,origin):
    instance,calls=origin_preflight(origin)
    with pytest.raises(r.ReleaseError,match='^WRONG_ORIGIN$'):instance.deploy()
    assert calls[:3]==[('rev-parse','HEAD'),('status','--porcelain'),
                       ('config','--null','--get-all','remote.origin.url')]
    assert not any(call[0] in {'docker','fetch','checkout'} for call in calls)
    assert not instance.current.exists() and instance.journal is None
    assert not instance.backups.exists()


@pytest.mark.parametrize('origins',[
    [r.REPOSITORY,'https://example.invalid/other.git'],
    ['https://example.invalid/other.git',r.REPOSITORY],
    [r.REPOSITORY,r.REPOSITORY+'.git'],
    [r.REPOSITORY,''],
    ['',r.REPOSITORY],
])
def test_multiple_origin_urls_never_pass_the_repository_gate(origin_preflight,origins):
    instance,calls=origin_preflight(origins)
    with pytest.raises(r.ReleaseError,match='^WRONG_ORIGIN$'):instance.deploy()
    assert calls[-1]==('remote','get-url','--all','origin')
    assert not any(call[0] in {'docker','fetch','checkout','ssh'} for call in calls)
    assert not instance.backups.exists() and instance.journal is None


def test_real_git_and_ssh_config_resolve_alias_without_forcing_user(tmp_path):
    if not shutil.which('ssh') or not shutil.which('git'):pytest.skip('Git and OpenSSH required')
    repo=tmp_path/'repository';repo.mkdir()
    home=tmp_path/'home';ssh_config=home/'.ssh/config';ssh_config.parent.mkdir(parents=True)
    ssh_config.write_text('Host github-vecher\n HostName github.com\n User git\n Port 22\n StrictHostKeyChecking ask\n IdentityFile /not-opened/private-key\n')
    subprocess.run(['git','init','-q',str(repo)],check=True)
    origin='github-vecher:ais0930271103-glitch/vecher.git'
    subprocess.run(['git','-C',str(repo),'config','remote.origin.url',origin],check=True)
    original_config=(repo/'.git/config').read_bytes()
    helper=r.access_helper();ssh_calls=[]
    def execute(argv,env,**kwargs):
        if argv[0]=='ssh':
            assert '-l' not in argv and '-p' not in argv,'Resolve the actual user/port, never manufacture an accepted result'
            ssh_calls.append(argv)
            argv=[shutil.which('ssh'),'-F',str(ssh_config),*argv[1:]]
        return helper.command(argv,env,**kwargs)
    result=access_report(repo,execute,tmp_path)
    assert result['origin_verified'] is True and result['origin_verification_reason']=='verified_ssh'
    assert result['origin_ssh_alias_resolved'] is True and result['origin_credentials_present'] is False
    assert result['origin_fetch_url_rewritten'] is False and len(ssh_calls)==1
    assert (repo/'.git/config').read_bytes()==original_config
    assert 'private-key' not in json.dumps(result) and origin not in json.dumps(result)
    # Exercise Git's real insteadOf expansion, not just a mocked string matcher.
    subprocess.run(['git','-C',str(repo),'config','url.ssh://git@github.com/other-owner/.insteadOf','github-vecher:'],check=True)
    result=access_report(repo,execute,tmp_path)
    assert result['origin_verified'] is False and result['origin_fetch_url_rewritten'] is True
    assert result['origin_verification_reason']=='effective_repository_mismatch'
    assert len(ssh_calls)==1,'Foreign paths must not even reach SSH configuration resolution'


@pytest.mark.parametrize('fields,reason',[
    ({'hostname':'other.example'},'ssh_host_not_github'),
    ({'hostname':'ssh.github.com','port':'443'},'ssh_host_not_github'),
    ({'user':'root'},'ssh_user_not_git'),
    ({'port':'443'},'ssh_port_not_allowed'),
    ({'proxycommand':'PRIVATE_PROXY_COMMAND'},'ssh_command_or_proxy_configured'),
    ({'proxyjump':'PRIVATE_PROXY_HOST'},'ssh_command_or_proxy_configured'),
    ({'knownhostscommand':'PRIVATE_COMMAND'},'ssh_command_or_proxy_configured'),
    ({'permitlocalcommand':'yes','localcommand':'PRIVATE_LOCAL_COMMAND'},'ssh_command_or_proxy_configured'),
    ({'hostkeyalias':'PRIVATE_KEY_ALIAS'},'ssh_command_or_proxy_configured'),
    ({'stricthostkeychecking':'no'},'ssh_host_key_verification_disabled'),
    ({'stricthostkeychecking':'false'},'ssh_host_key_verification_disabled'),
    ({'stricthostkeychecking':'off'},'ssh_host_key_verification_disabled'),
])
def test_unsafe_resolved_alias_stops_before_any_deployment_mutation(origin_preflight,fields,reason):
    instance,calls=origin_preflight('github-vecher:ais0930271103-glitch/vecher.git',ssh_fields=fields)
    assert instance.origin_report()['origin_verification_reason']==reason
    with pytest.raises(r.ReleaseError,match='^WRONG_ORIGIN$'):instance.deploy()
    assert not instance.current.exists() and instance.journal is None and not instance.backups.exists()
    assert not any(call[0] in {'docker','fetch','checkout'} for call in calls)
    assert 'PRIVATE_' not in json.dumps(instance.origin_report())


@pytest.mark.parametrize('effective',[
    ['https://other.example/ais0930271103-glitch/vecher.git'],
    ['git@github.com:other-owner/vecher.git'],
    ['git@other.example:ais0930271103-glitch/vecher.git'],
    [r.REPOSITORY,r.REPOSITORY+'.git'],
])
def test_effective_url_cannot_redirect_fetch_to_another_repository_or_destination(origin_preflight,effective):
    instance,calls=origin_preflight(r.REPOSITORY,effective=effective)
    with pytest.raises(r.ReleaseError,match='^WRONG_ORIGIN$'):instance.deploy()
    assert not instance.backups.exists() and not any(call[0]=='docker' for call in calls)


@pytest.mark.parametrize('name',[
    'GIT_SSH','GIT_SSH_COMMAND','GIT_CONFIG','GIT_CONFIG_COUNT','GIT_CONFIG_GLOBAL',
    'GIT_SSH_VARIANT','GIT_DIR','GIT_WORK_TREE','GIT_COMMON_DIR',
])
def test_custom_git_environment_is_never_authorized(tmp_path,name):
    runner=AccessCommands('github-vecher:ais0930271103-glitch/vecher.git')
    result=access_report(tmp_path/'repo',runner,tmp_path,environ={name:'PRIVATE_OVERRIDE'})
    assert result['origin_verified'] is False and result['origin_verification_reason']=='git_environment_override'
    assert 'PRIVATE_OVERRIDE' not in json.dumps(result)


@pytest.mark.parametrize('config,reason',[
    ({'core.sshCommand':['PRIVATE_COMMAND']},'ssh_command_override'),
    ({'ssh.variant':['plink']},'ssh_variant_unsupported'),
])
def test_custom_ssh_command_and_unsupported_variant_stop_before_deploy(origin_preflight,config,reason):
    instance,calls=origin_preflight('github-vecher:ais0930271103-glitch/vecher.git',config=config)
    assert instance.origin_report()['origin_verification_reason']==reason
    with pytest.raises(r.ReleaseError,match='^WRONG_ORIGIN$'):instance.deploy()
    assert not any(call[0]=='docker' for call in calls) and not instance.backups.exists()


@pytest.mark.parametrize('config',[
    'Match exec "touch SHOULD_NEVER_EXECUTE"\n HostName github.com\n',
    'Match command "git-upload-pack"\n HostName github.com\n',
    'Include %h/private.conf\n',
    'Include /outside-root/private.conf\n',
])
def test_dynamic_or_unreviewable_ssh_config_never_reaches_ssh_g(origin_preflight,tmp_path,config):
    path=tmp_path/'home/.ssh/config';path.parent.mkdir(parents=True);path.write_text(config)
    instance,calls=origin_preflight('github-vecher:ais0930271103-glitch/vecher.git')
    with pytest.raises(r.ReleaseError,match='^WRONG_ORIGIN$'):instance.deploy()
    assert instance.origin_report()['origin_verification_reason']=='ssh_resolution_unavailable'
    assert not any(call[0] in {'docker','ssh'} for call in calls) and not instance.backups.exists()


def test_origin_is_rechecked_immediately_before_fetch(release,monkeypatch):
    reports=iter(({'origin_verified':True},{'origin_verified':False}))
    calls=[]
    release.origin_report=lambda:(calls.append('origin') or next(reports))
    release.verify_origin()  # The earlier preflight succeeded.
    release.docker=SimpleNamespace(inspect=lambda _:{'Config':{'Env':['VECHER_DATA_KEYS=private-fixture']}})
    release.git=lambda *args:pytest.fail('Changed origin must stop before fetch or checkout')
    with pytest.raises(r.ReleaseError,match='^WRONG_ORIGIN$'):release.prepare()
    assert calls==['origin','origin'] and release.journal['phase']=='preparing_images'
    assert release.journal['traffic_may_have_opened'] is False


def test_fetch_command_uses_strict_known_host_check_without_mutating_configuration(monkeypatch):
    seen=[]
    def execute(argv,**kwargs):
        seen.append((argv,kwargs['env']))
        return SimpleNamespace(returncode=0,stdout=b'',stderr=b'')
    monkeypatch.setattr(r.subprocess,'run',execute)
    r.Commands().run(['git','fetch','--no-tags','origin',r.SOURCE])
    assert len(seen)==1 and seen[0][1]['GIT_SSH_COMMAND']==(
        'ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o UpdateHostKeys=no')
    assert '-F' not in seen[0][1]['GIT_SSH_COMMAND'] and 'IdentityFile' not in seen[0][1]['GIT_SSH_COMMAND']


def test_access_dependency_is_hash_bound_before_any_code_is_evaluated(tmp_path,monkeypatch):
    marker=tmp_path/'must-not-exist'
    fake=tmp_path/'diagnose_access.py';fake.write_text('open('+repr(str(marker))+',"w").write("bad")\n')
    monkeypatch.setattr(r,'__file__',str(tmp_path/'server_release.py'))
    result=r.verified_origin_report(tmp_path/'repo',environ={})
    assert result['origin_verified'] is False and result['origin_verification_reason']=='access_helper_hash_mismatch'
    assert not marker.exists()


@pytest.fixture
def no_checkpoint_status(tmp_path):
    calls=[]
    root,backups=tmp_path/'live',tmp_path/'backups'
    def configure(urls,*,dirty=False):
        values={('rev-parse','HEAD'):(r.OLD_SOURCE+'\n').encode(),
                ('--no-optional-locks','status','--porcelain'):b' M private-name\n' if dirty else b'',
                ('config','--null','--get-all','remote.origin.url'):('\0'.join(urls)+'\0').encode()}
        class Commands:
            def run(self,args,**kwargs):
                assert args[:3]==['git','-C',str(root/'repo')],'Status must not invoke Docker or other commands'
                command=tuple(args[3:]);calls.append(command)
                assert command in values,'Status must use read-only Git commands'
                return values[command]
        instance=r.Release(commands=Commands(),root=root,backups=backups)
        runner=AccessCommands(urls,calls=calls)
        instance.origin_report=lambda:access_report(instance.repo,runner,tmp_path)
        return instance,calls
    return configure


def test_status_without_checkpoint_is_read_only_and_does_not_claim_history(no_checkpoint_status):
    instance,calls=no_checkpoint_status(['git@github.com:ais0930271103-glitch/vecher.git'])
    result=instance.status()
    assert result['phase']=='no_checkpoint' and result['release_checkpoint_present'] is False
    assert result['ready_for_apk14'] is False and result['checkout_readable'] is True
    assert result['checkout_head']==r.OLD_SOURCE and result['checkout_clean'] is True
    assert result['origin_url_count']==1 and result['origin_transport']=='ssh_scp'
    assert result['origin_matches_repository'] is True
    assert result['origin_verified'] is True and result['origin_verification_reason']=='verified_ssh'
    assert not instance.backups.exists() and not instance.root.exists()
    assert instance.journal is None


def test_no_checkpoint_status_sanitizes_credential_url_and_dirty_paths(no_checkpoint_status,capsys):
    url='https://PRIVATE_USER:PRIVATE_TOKEN@github.com/ais0930271103-glitch/vecher.git?token=QUERY_SECRET#FRAGMENT_SECRET'
    instance,_=no_checkpoint_status([url],dirty=True)
    result=instance.status()
    assert result['origin_transport']=='https' and result['origin_matches_repository'] is False
    assert result['origin_credentials_present'] is True and result['origin_host_matches_github'] is True
    assert result['origin_repository_path_matches'] is True and result['checkout_clean'] is False
    captured=capsys.readouterr()
    serialized=json.dumps(result)+captured.out+captured.err
    assert all(value not in serialized for value in (url,'PRIVATE_USER','PRIVATE_TOKEN','QUERY_SECRET','FRAGMENT_SECRET','private-name'))
    assert not instance.backups.exists() and not instance.root.exists()


def test_no_checkpoint_status_reports_multiple_origins_without_selecting_one(no_checkpoint_status):
    urls=[r.REPOSITORY,'https://PRIVATE_USER:PRIVATE_TOKEN@other.example/private/repo.git']
    instance,_=no_checkpoint_status(urls)
    result=instance.status()
    assert result['origin_url_count']==2 and result['origin_transport']=='unknown'
    assert result['origin_matches_repository'] is False and result['origin_credentials_present'] is True
    assert result['origin_host_matches_github'] is False and result['origin_repository_path_matches'] is False
    assert all(value not in json.dumps(result) for value in (*urls,'PRIVATE_TOKEN','other.example','private/repo'))


def test_no_checkpoint_status_keeps_git_failures_safe(tmp_path):
    class Commands:
        def run(self,*args,**kwargs):raise r.ReleaseError('SOURCE_FAILED')
    instance=r.Release(commands=Commands(),root=tmp_path/'live',backups=tmp_path/'backups')
    result=instance.status()
    assert result['phase']=='no_checkpoint' and result['checkout_readable'] is False
    assert result['checkout_head'] is None and result['ready_for_apk14'] is False
    assert not instance.backups.exists()


def test_existing_checkpoint_status_retains_live_source_gate(release):
    release.phase('core_ready',traffic_may_have_opened=True)
    release.git=lambda *args:r.OLD_SOURCE
    with pytest.raises(r.ReleaseError,match='ACTIVE_SOURCE_CHANGED'):release.status()


def test_unfinished_checkpoint_status_never_becomes_ready(release):
    result=release.status()
    assert result['phase']=='images_pinned' and result['release_checkpoint_present'] is True
    assert result['ready_for_apk14'] is False


def test_status_reports_verified_alias_without_exposing_its_spelling(no_checkpoint_status):
    origin='github-vecher:ais0930271103-glitch/vecher.git'
    instance,_=no_checkpoint_status([origin])
    result=instance.status()
    assert result['origin_verified'] is True and result['origin_verification_reason']=='verified_ssh'
    assert result['origin_ssh_alias_resolved'] is True and result['origin_host_matches_github'] is False
    assert result['ready_for_apk14'] is False and origin not in json.dumps(result)


def public_metadata(**changes):
    return {'version_name':'0.8.0','version_code':13,'sha256':r.APK_SHA,'bytes':r.APK_BYTES,'required':False,**changes}


def public_probe(monkeypatch,capsys,*,health=None,version=None,failure=None,raw_version=None):
    bodies={'/health':health if health is not None else {'ok':True,'env':'staging','version':'0.9.0'},
            '/app/version':version if version is not None else public_metadata()}
    calls=[]
    build_opener=urllib.request.build_opener
    existing_proxies={'https':'http://SECRET_PROXY_USER:SECRET_PROXY_PASSWORD@proxy.invalid:8080'}
    monkeypatch.setattr(urllib.request,'getproxies',lambda:dict(existing_proxies))
    class Response:
        status=200
        def __init__(self,path):self.path=path
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,limit):
            assert limit==16385
            return raw_version if self.path=='/app/version' and raw_version is not None else json.dumps(bodies[self.path]).encode()
    class Opener:
        def open(self,request,timeout):
            assert timeout==8 and request.full_url.startswith('https://api-test.xn--b1aga8b7a.online/')
            path=request.full_url.removeprefix('https://api-test.xn--b1aga8b7a.online')
            assert path in bodies and request.get_method()=='GET'
            calls.append(path)
            if failure is not None and path=='/app/version':raise failure
            return Response(path)
    def opener(*handlers):
        tls=next(handler for handler in handlers if isinstance(handler,urllib.request.HTTPSHandler))
        assert tls._context.check_hostname and tls._context.verify_mode==ssl.CERT_REQUIRED
        redirect=next(handler for handler in handlers if isinstance(handler,urllib.request.HTTPRedirectHandler))
        assert redirect.redirect_request(None,None,302,'SECRET_MESSAGE',None,'http://127.0.0.1:8092') is None
        assert not any(isinstance(handler,urllib.request.ProxyHandler) for handler in handlers)
        # Build the real handler chain, without opening a connection: urllib
        # must retain the existing proxy settings instead of forcing direct DNS.
        actual=build_opener(*handlers)
        proxy=next(handler for handler in actual.handlers if isinstance(handler,urllib.request.ProxyHandler))
        assert proxy.proxies==existing_proxies
        return Opener()
    monkeypatch.setattr(urllib.request,'build_opener',opener)
    exec(compile(r.PUBLIC_HTTPS_PROBE,'<public-https-probe>','exec'),{'__name__':'public_probe_test'})
    output=capsys.readouterr()
    assert output.err=='' and 'SECRET' not in output.out
    assert sorted(calls)==['/app/version','/health']
    return json.loads(output.out)


class PublicCommands:
    def __init__(self,result):self.result=result;self.calls=[]
    def run(self,argv,**kwargs):
        self.calls.append(argv)
        assert argv==[sys.executable,'-I','-']
        assert kwargs['timeout']==20 and kwargs['data']==r.PUBLIC_HTTPS_PROBE.encode()
        assert kwargs['label']=='PUBLIC_HTTPS'
        return json.dumps(self.result).encode()


@pytest.mark.parametrize('api,metadata',[
    ('0.8.0',public_metadata()),
    ('0.9.0',public_metadata()),
    ('0.9.0',public_metadata(version_code=14,version_name='0.9.0',sha256='b'*64,bytes=r.APK_BYTES+100)),
])
def test_public_https_reports_actual_predeploy_and_postdeploy_versions(monkeypatch,capsys,api,metadata):
    result=public_probe(monkeypatch,capsys,health={'ok':True,'env':'staging','version':api},version={**metadata,'notes':'PRIVATE_NOTES','download_url':'PRIVATE_URL'})
    command=PublicCommands(result)
    assert r.public_status(command)=={'api_version':api,'env':'staging',**metadata}
    assert len(command.calls)==1 and 'PRIVATE' not in json.dumps(result)


def test_public_https_retains_standard_network_handlers_without_disclosing_proxy_credentials(monkeypatch,capsys):
    result=public_probe(monkeypatch,capsys)
    assert r.public_status(PublicCommands(result))=={'api_version':'0.9.0','env':'staging',**public_metadata()}
    assert 'SECRET_PROXY' not in json.dumps(result)


@pytest.mark.parametrize('error,category',[
    (urllib.error.HTTPError('https://SECRET_URL',302,'SECRET_REDIRECT',{},None),'redirect_blocked'),
    (urllib.error.HTTPError('https://SECRET_URL',503,'SECRET_BODY',{},None),'http_error'),
    (urllib.error.URLError(ssl.SSLCertVerificationError('SECRET_CERTIFICATE')),'tls_verification_failed'),
    (urllib.error.URLError(TimeoutError('SECRET_TIMEOUT')),'timeout'),
    (OSError('SECRET_NETWORK'),'network_error'),
])
def test_public_https_errors_are_redacted_and_never_fall_back_to_loopback(monkeypatch,capsys,error,category):
    result=public_probe(monkeypatch,capsys,failure=error)
    assert result=={'error_category':category}
    with pytest.raises(r.ReleaseError,match='^PUBLIC_HTTPS_'+category.upper()+'$'):
        r.public_status(PublicCommands(result))


@pytest.mark.parametrize('raw',[
    b'x'*16385,b'not json',b'[]',
    b'{"version_code":13,"version_code":14}',
])
def test_public_https_response_is_bounded_and_unambiguous(monkeypatch,capsys,raw):
    result=public_probe(monkeypatch,capsys,raw_version=raw)
    assert result=={'error_category':'invalid_response'}
    with pytest.raises(r.ReleaseError,match='PUBLIC_HTTPS_INVALID_RESPONSE'):r.public_status(PublicCommands(result))


@pytest.mark.parametrize('field,value',[
    ('api_version','PRIVATE_VERSION'),('env','production'),('version_name','0.9'),
    ('version_code',True),('version_code',0),('version_code',2147483648),
    ('bytes',True),('bytes',0),('bytes',256*r.MIB+1),
    ('sha256','A'*64),('sha256','PRIVATE_TOKEN'),('required','false'),
])
def test_public_https_normalized_metadata_requires_exact_types_and_bounds(field,value):
    result={'api_version':'0.9.0','env':'staging',**public_metadata(),field:value}
    with pytest.raises(r.ReleaseError,match='^PUBLIC_HTTPS_INVALID_RESPONSE$'):r.public_status(PublicCommands(result))


@pytest.mark.parametrize('health',[
    {'ok':False,'env':'staging','version':'0.9.0'},
    {'ok':1,'env':'staging','version':'0.9.0'},
    {'ok':True,'env':'production','version':'0.9.0'},
])
def test_public_https_requires_healthy_staging_api(monkeypatch,capsys,health):
    result=public_probe(monkeypatch,capsys,health=health)
    assert result=={'error_category':'unhealthy_api'}
    with pytest.raises(r.ReleaseError,match='PUBLIC_HTTPS_UNHEALTHY_API'):r.public_status(PublicCommands(result))


@pytest.mark.parametrize('json_flag',[[],['--json']])
def test_public_status_cli_json_marker_without_deploy_locks_or_dependency(monkeypatch,capsys,json_flag):
    result={'api_version':'0.9.0','env':'staging',**public_metadata()}
    monkeypatch.setattr(r.os,'geteuid',lambda:0)
    monkeypatch.setattr(r,'public_status',lambda:result)
    monkeypatch.setattr(r,'locks',lambda *_:pytest.fail('Public HTTPS check must not acquire deployment locks'))
    monkeypatch.setattr(r,'access_helper',lambda:pytest.fail('Public HTTPS check must not probe Git/SSH'))
    assert r.main(['public-status',*json_flag])==0
    lines=capsys.readouterr().out.splitlines()
    assert len(lines)==2 and json.loads(lines[0])==result and lines[1]=='RELEASE14_PUBLIC_HTTPS_OK'


def test_public_status_failure_has_no_success_marker_or_exception_details(monkeypatch,capsys):
    monkeypatch.setattr(r.os,'geteuid',lambda:0)
    def fail():raise r.ReleaseError('PUBLIC_HTTPS_TLS_VERIFICATION_FAILED')
    monkeypatch.setattr(r,'public_status',fail)
    assert r.main(['public-status','--json'])==1
    output=capsys.readouterr()
    assert output.out=='' and output.err=='RELEASE14_STOPPED: PUBLIC_HTTPS_TLS_VERIFICATION_FAILED\n'


def test_journal_private_durable_and_pointer_scoped(release):
    pointer=r.load_json(release.current)
    assert pointer=={'schema':1,'release':14,'checkpoint':str(release.checkpoint)}
    assert stat.S_IMODE(release.current.stat().st_mode)==0o600
    assert stat.S_IMODE(release.checkpoint.stat().st_mode)==0o700
    assert release.read()['source_sha']==r.SOURCE
    r.durable(release.current,{**pointer,'checkpoint':'/tmp/not-a-release-checkpoint'})
    with pytest.raises(r.ReleaseError,match='UNSAFE_CHECKPOINT_PATH'):release.read()


def test_regular_and_directory_reject_links(tmp_path):
    original=tmp_path/'original';original.write_bytes(b'private')
    link=tmp_path/'link';link.symlink_to(original)
    with pytest.raises(r.ReleaseError):r.regular(link)
    hard=tmp_path/'hard';os.link(original,hard)
    with pytest.raises(r.ReleaseError):r.regular(hard)
    target=tmp_path/'directory';target.mkdir()
    alias=tmp_path/'alias';alias.symlink_to(target,target_is_directory=True)
    with pytest.raises(r.ReleaseError):r.directory(alias)


def test_deploy_and_backup_locks_are_both_exclusive(tmp_path):
    root=tmp_path/'locks'
    with r.locks(root):
        with pytest.raises(r.ReleaseError,match='ANOTHER_DEPLOYMENT_OR_BACKUP_RUNNING'):
            with r.locks(root):pass
    with r.locks(root):pass


@pytest.mark.parametrize('name,kind',[('../outside','file'),('/tmp/outside','file'),('link','symlink'),('hard','hardlink')])
def test_restore_rejects_unsafe_tar_members(tmp_path,name,kind):
    source=tmp_path/'bad.tar.gz'
    with tarfile.open(source,'w:gz') as archive:
        member=tarfile.TarInfo(name)
        if kind=='symlink':member.type=tarfile.SYMTYPE;member.linkname='../secret'
        elif kind=='hardlink':member.type=tarfile.LNKTYPE;member.linkname='../secret'
        else:member.size=1
        archive.addfile(member,io.BytesIO(b'x') if member.isfile() else None)
    with pytest.raises(r.ReleaseError,match='UNSAFE_BACKUP_MEMBER'):
        r.extract_tree(source,tmp_path/'restore')
    assert not (tmp_path/'outside').exists()


def test_archive_roundtrip_and_file_hashes(tmp_path):
    source=tmp_path/'source';source.mkdir()
    (source/'nested').mkdir();(source/'nested/file').write_bytes(b'encrypted-placeholder')
    os.chmod(source/'nested/file',0o600)
    expected=r.archive_tree(source,tmp_path/'backup.tar.gz')
    restored=tmp_path/'restored';r.extract_tree(tmp_path/'backup.tar.gz',restored)
    assert r.tree_manifest(restored)==expected
    with pytest.raises(r.ReleaseError,match='RESTORE_DIRECTORY_NOT_EMPTY'):
        r.extract_tree(tmp_path/'backup.tar.gz',restored)


def test_pg_restore_input_is_streamed_file_not_read_to_memory(tmp_path):
    path=tmp_path/'dump';path.write_bytes(b'restore-stream'*100)
    class NeverRead(io.BufferedReader):
        def read(self,*_):raise AssertionError('Parent must not read backup into memory')
    with NeverRead(io.FileIO(path,'rb')) as incoming:
        output=r.Commands().run([sys.executable,'-c','import sys; print(len(sys.stdin.buffer.read()))'],input_file=incoming)
    assert output.strip()==str(path.stat().st_size).encode()


def test_command_never_interprets_shell_metacharacters(tmp_path):
    marker=tmp_path/'must-not-exist'
    value='$(touch '+str(marker)+') ; `touch '+str(marker)+'`'
    output=r.Commands().run([sys.executable,'-c','import sys; print(sys.argv[1])',value])
    assert output.decode().strip()==value and not marker.exists()


def test_production_command_error_is_redacted_but_private_log_retained(tmp_path,capsys):
    command=r.Commands(diagnostic_dir=tmp_path/'logs')
    with pytest.raises(r.ReleaseError,match='PROBE_FAILED'):
        command.run([sys.executable,'-c',"import sys; print('private-value',file=sys.stderr); sys.exit(1)"],label='PROBE')
    assert 'private-value' not in capsys.readouterr().err
    log=next((tmp_path/'logs').iterdir())
    assert 'private-value' in log.read_text() and stat.S_IMODE(log.stat().st_mode)==0o600


def test_synthetic_diagnostic_masks_tokens_and_database_url(capsys):
    command=r.Commands(synthetic=True)
    command.failure('TEST',b'',b'postgresql+asyncpg://user:password@db/dbname Bearer abc eyJaa.bbb.ccc')
    output=capsys.readouterr().err
    assert 'password' not in output and 'Bearer abc' not in output and 'eyJaa.bbb.ccc' not in output
    assert 'RELEASE14_SYNTHETIC_DIAGNOSTIC' in output


def test_legacy_fingerprint_requires_same_rows_and_only_six_new_empty_tables():
    before={'users':{'rows':2,'sha256':'kept'}}
    valid={**before,**{name:{'rows':0,'sha256':'empty'} for name in r.NEW_TABLES}}
    r.verify_legacy(before,valid)
    bad={**valid,'users':{'rows':2,'sha256':'changed'}}
    with pytest.raises(r.ReleaseError,match='LEGACY_DATA_CHANGED'):r.verify_legacy(before,bad)
    bad={**valid,'public_posts':{'rows':1,'sha256':'unexpected'}}
    with pytest.raises(r.ReleaseError,match='MIGRATION_TABLES_MISMATCH'):r.verify_legacy(before,bad)


def test_frozen_environment_survives_old_container_recreation(release):
    private_json(release.checkpoint/'original-api-env.json',{'VECHER_DATABASE_URL':'private-db-url','VECHER_DATA_KEYS':'private-ring',
        'VECHER_SMTP_HOST':'mail.example.invalid','VECHER_SCHEDULER_ENABLED':'true','VECHER_OUTBOX_ENABLED':'true'})
    class Gone:
        def inspect(self,*_):raise AssertionError('Old container was replaced')
    release.docker=Gone()
    environment=release.live_environment()
    assert environment['VECHER_DATA_KEYS']=='private-ring' and environment['VECHER_DATABASE_URL']=='private-db-url'
    assert environment['VECHER_SMTP_HOST']==''
    assert environment['VECHER_SCHEDULER_ENABLED']==environment['VECHER_OUTBOX_ENABLED']=='false'


def test_lab_environment_is_isolated_and_synthetic_uses_separate_keys(tmp_path):
    helper=tmp_path/'helper';helper.write_text('pass')
    lab=r.Lab(None,tmp_path/'lab',{'api':IMAGE},keys='REAL_BACKUP_RING',helper=helper)
    env=lab.environment('vecher_release14_smoke_'+lab.token,synthetic=True)
    assert env['VECHER_DATA_KEYS']!='REAL_BACKUP_RING'
    assert env['VECHER_RELEASE14_SYNTHETIC_REHEARSAL']=='1'
    assert env['VECHER_FIREBASE_CREDENTIALS']=='/run/vecher-release14-disabled-push.json'
    assert all(env[name]=='false' for name in ('VECHER_OUTBOX_ENABLED','VECHER_SCHEDULER_ENABLED','VECHER_REALTIME_ENABLED'))
    assert all(env[name]=='' for name in ('VECHER_SMTP_HOST','VECHER_SMTP_USER','VECHER_SMTP_PASSWORD','VECHER_SMTP_FROM'))


def test_lab_restore_uses_owned_database_and_streaming_input(tmp_path):
    helper=tmp_path/'helper';helper.write_text('pass')
    dump=tmp_path/'db.dump';dump.write_bytes(b'bounded fixture')
    events=[]
    class Docker:
        def run(self,args,**kwargs):
            assert kwargs.get('data') is None
            assert kwargs['input_file'].fileno()>=0
            events.append(args)
        def sql(self,*args):events.append(args)
        def exists(self,*_):return False
    lab=r.Lab(Docker(),tmp_path/'lab',{'api':IMAGE,'db':IMAGE},keys='ring',helper=helper)
    lab.restore(dump,'vecher_restore')
    assert '--single-transaction' in events[-1] and '--exit-on-error' in events[-1]
    # PGDATABASE alone leaves pg_restore in SQL-output mode; an explicit
    # database option is essential to actually apply this archive.
    assert events[-1][events[-1].index('--dbname')+1]=='vecher_restore'
    assert events[-1][0]=='run' and '--name' in events[-1] and '--label' in events[-1]
    with pytest.raises(r.ReleaseError,match='UNSAFE_DATABASE_NAME'):
        lab.restore(dump,'vecher; DROP DATABASE postgres')


def test_public_traffic_boundary_is_durable_before_compose(release):
    def lost_ack(*args,**kwargs):
        journal=r.load_json(release.checkpoint/'journal.json')
        assert journal['traffic_may_have_opened'] is True
        assert journal['phase']=='traffic_may_have_opened'
        raise r.ReleaseError('COMPOSE_ACK_LOST')
    release.compose=lost_ack
    with pytest.raises(r.ReleaseError,match='COMPOSE_ACK_LOST'):release.activate_core()
    with pytest.raises(r.ReleaseError,match='FORWARD_RECOVERY_ONLY_AFTER_TRAFFIC'):release.rollback()


def test_unfinished_journal_refuses_deploy_before_any_mutation(release):
    release.phase('live_migration_started')
    release.preflight=lambda:pytest.fail('Unsafe retry must not reach preflight')
    with pytest.raises(r.ReleaseError,match='UNFINISHED_RELEASE_REFUSES_AUTOMATIC_RETRY'):release.deploy()


def test_successful_deploy_retry_only_rechecks_live_state(release):
    release.phase('blogs_ready',traffic_may_have_opened=True)
    release.status=lambda:{'ready_for_apk14':True}
    release.preflight=lambda:pytest.fail('Do not redeploy an accepted release')
    assert release.deploy()=={'ready_for_apk14':True}


def test_rollback_old_marker_still_requires_equal_data_before_restart(release):
    release.phase('live_migration_started',traffic_may_have_opened=False)
    private_json(release.checkpoint/'fingerprint.json',{'users':{'rows':1,'sha256':'original'}})
    commands=[]
    class Docker:
        def inspect(self,*args):return {'Image':IMAGE,'State':{'Running':False}}
        def sql(self,*args):return (r.OLD_SCHEMA+'\n').encode()
        def run(self,args,**kwargs):commands.append(args);return b''
    release.docker=Docker();release.verify_backup=lambda:{'trees':{}}
    def one(command,**kwargs):
        if 'app.migrate' in command:return ('SCHEMA_HEAD_OK='+r.OLD_SCHEMA).encode()
        return json.dumps({'users':{'rows':1,'sha256':'changed'}}).encode()
    release.one_live=one
    with pytest.raises(r.ReleaseError,match='OLD_IMAGE_RESTORE_DATA_MISMATCH'):release.rollback()
    assert not any(command[0]=='start' for command in commands)


def test_live_timeout_stops_pinned_container_and_clears_durable_entry(release):
    events=[]
    release.live_environment=lambda:{'VECHER_DATABASE_URL':'test-placeholder'}
    class Docker:
        def run(self,args,**kwargs):
            if args[0]=='run':
                recorded=r.load_json(release.checkpoint/'journal.json')['temporary_containers']
                assert recorded[0]['name']==args[args.index('--name')+1]
                events.append('launch')
                raise r.ReleaseError('LIVE_ISOLATED_COMMAND_UNAVAILABLE_OR_TIMEOUT')
            assert args==['rm','-f','owned-id'];events.append('remove');return b''
        def exists(self,*_):return True
        def inspect(self,*_):
            events.append('verify')
            return {'Id':'owned-id','Image':OTHER_IMAGE,'Config':{'Labels':{
                'vecher.release14.checkpoint':release.checkpoint.name}}}
    release.docker=Docker()
    with pytest.raises(r.ReleaseError,match='UNAVAILABLE_OR_TIMEOUT'):
        release.one_live(['python','-m','app.migrate','upgrade'])
    assert events==['launch','verify','remove']
    assert r.load_json(release.checkpoint/'journal.json')['temporary_containers']==[]


def test_lab_timeout_stops_owned_container_before_return(tmp_path):
    helper=tmp_path/'helper';helper.write_text('pass')
    events=[]
    lab=r.Lab(None,tmp_path/'lab',{'api':IMAGE,'db':IMAGE},keys='ring',helper=helper)
    class Docker:
        def run(self,args,**kwargs):
            if args[0]=='run':events.append('run');raise r.ReleaseError('TIMEOUT')
            assert args==['rm','-f','owned'];events.append('cleanup');return b''
        def exists(self,*_):return True
        def inspect(self,*_):return {'Id':'owned','Config':{'Labels':{'vecher.release14':lab.token}}}
    lab.docker=Docker()
    with pytest.raises(r.ReleaseError,match='TIMEOUT'):
        lab.one(IMAGE,lab.environment('vecher'),['python','-m','app.migrate','upgrade'])
    assert events==['run','cleanup'] and lab.containers==[]


def test_crash_recovery_cleans_migrators_before_any_database_command(release):
    name=release.temporary(OTHER_IMAGE,'once')
    events=[]
    private_json(release.checkpoint/'fingerprint.json',{'users':{'rows':1,'sha256':'original'}})
    release.verify_backup=lambda:{'trees':{}}
    class Docker:
        def exists(self,*_):return True
        def inspect(self,value):
            if value==name:
                return {'Id':'migrator','Image':OTHER_IMAGE,'Config':{'Labels':{
                    'vecher.release14.checkpoint':release.checkpoint.name}}}
            return {'Image':IMAGE,'State':{'Running':False}}
        def run(self,args,**kwargs):events.append(args[0]);return b''
        def sql(self,*args):events.append('sql');return (r.OLD_SCHEMA+'\n').encode()
    release.docker=Docker()
    def one(command,**kwargs):
        if 'app.migrate' in command:return ('SCHEMA_HEAD_OK='+r.OLD_SCHEMA).encode()
        return b'{}'
    release.one_live=one
    with pytest.raises(r.ReleaseError,match='OLD_IMAGE_RESTORE_DATA_MISMATCH'):release.rollback()
    assert events.index('rm')<events.index('sql') and 'start' not in events


def test_recovery_refuses_unjournaled_owned_command_before_database_access(release):
    class Docker:
        def run(self,*args,**kwargs):return b'unknown-container'
        def sql(self,*_):pytest.fail('Unknown process must be resolved first')
        def inspect(self,*_):pytest.fail('Recovery must stop before database work')
    release.docker=Docker()
    with pytest.raises(r.ReleaseError,match='UNJOURNALED_TEMPORARY_CONTAINER'):release.rollback()


def test_recovery_pg_restore_explicitly_targets_recovery_database(release):
    (release.checkpoint/'db.dump').write_bytes(b'archive-placeholder')
    private_json(release.checkpoint/'fingerprint.json',{'users':{'rows':1,'sha256':'original'}})
    release.verify_backup=lambda:{'trees':{}}
    release.live_environment=lambda:{'VECHER_DATABASE_URL':'postgresql+asyncpg://vecher:fake@db:5432/vecher'}
    commands=[]
    class Docker:
        def inspect(self,*_):return {'Image':IMAGE,'State':{'Running':False}}
        def run(self,*args,**kwargs):return b''
        def sql(self,*_):return (r.SCHEMA+'\n').encode()
    release.docker=Docker()
    def one(command,**kwargs):
        commands.append(command)
        if command[0]=='pg_restore':
            target=command[command.index('--dbname')+1]
            assert target.startswith('vecher_r14_restore_')
            assert target==kwargs['env']['PGDATABASE']
            assert kwargs['input_file'].fileno()>=0
            assert kwargs['image']==IMAGE
            return b''
        return b'{}'
    release.one_live=one
    # Deliberately fail fingerprint verification before any database swap or
    # old API restart; this test exercises only the real recovery command.
    with pytest.raises(r.ReleaseError,match='RECOVERY_DATA_MISMATCH'):release.rollback()
    assert len([command for command in commands if command[0]=='pg_restore'])==1


@pytest.mark.parametrize('rollback',[False,True])
def test_database_swap_is_one_bounded_transaction_from_postgres(rollback):
    calls=[]
    class Docker:
        def sql(self,*args):calls.append(args);return b''
    r.swap_databases(Docker(),'db','vecher','recovered','retained',rollback_after_first=rollback)
    assert len(calls)==1 and calls[0][2]=='postgres'
    sql=calls[0][1]
    assert sql.index('BEGIN;')<sql.index('ALTER DATABASE "vecher" RENAME TO "retained";')
    assert 'statement_timeout' in sql and 'lock_timeout' in sql
    if rollback:
        assert sql.endswith('ROLLBACK;') and sql.count('ALTER DATABASE')==1
    else:
        assert sql.endswith('COMMIT;') and sql.count('ALTER DATABASE')==2
        assert sql.index('RENAME TO "retained";')<sql.index('ALTER DATABASE "recovered" RENAME TO "vecher";')


@pytest.mark.parametrize('names',[('postgres','safe','retained'),('vecher','vecher','retained'),('vecher','safe; DROP DATABASE vecher','retained')])
def test_database_swap_rejects_unsafe_names(names):
    with pytest.raises(r.ReleaseError,match='UNSAFE_DATABASE_SWAP'):
        r.swap_databases(None,'db',*names)


def test_backup_tampering_blocks_restore(release):
    data=release.checkpoint/'db.dump';data.write_bytes(b'original')
    manifest={'schema':1,'files':{'db.dump':r.sha(data)},'trees':{}}
    private_json(release.checkpoint/'backup-manifest.json',manifest)
    release.phase('backup_verified',backup_manifest_sha256=r.sha(release.checkpoint/'backup-manifest.json'))
    release.verify_backup();data.write_bytes(b'changed')
    with pytest.raises(r.ReleaseError,match='BACKUP_FILE_CHANGED'):release.verify_backup()


def test_media_network_cannot_connect_db_to_external_network(release):
    mutations=[]
    class Docker:
        def run(self,args,**kwargs):mutations.append(args);return b'existing'
        def inspect(self,*_):return {'Internal':False,'Labels':{'com.docker.compose.project':'vecher-test','com.docker.compose.network':'media-processing'}}
    release.docker=Docker()
    with pytest.raises(r.ReleaseError,match='INVALID_MEDIA_NETWORK'):release.media_network()
    assert not any('connect' in argv or 'create' in argv for argv in mutations)


def test_media_network_requires_database_dns_alias_when_already_attached(release):
    class Docker:
        def run(self,*args,**kwargs):return b'existing'
        def inspect(self,*_):return {'Internal':True,'Labels':{
            'com.docker.compose.project':'vecher-test','com.docker.compose.network':'media-processing'}}
        def service(self,*_):return {'Id':'db','Image':IMAGE,'NetworkSettings':{'Networks':{
            'vecher-test_media-processing':{'Aliases':['container-name']}}}}
    release.docker=Docker()
    with pytest.raises(r.ReleaseError,match='MEDIA_DATABASE_ALIAS_MISSING'):release.media_network()


@pytest.mark.parametrize('reachable',[True,False])
def test_keyless_worker_probe_requires_actual_database_response(monkeypatch,capsys,reachable):
    monkeypatch.delenv('VECHER_DATA_KEYS',raising=False)
    monkeypatch.setenv('VECHER_JWT_SECRET','parent-placeholder')
    events=[]
    async def dispose():events.append('disposed')
    engine=SimpleNamespace(dispose=dispose)
    async def check(value):
        assert value is engine and len(os.environ['VECHER_JWT_SECRET'])>=48
        events.append('queried')
        if not reachable:raise ConnectionError('synthetic database unavailable')
        return r.SCHEMA
    for name,attributes in {'app':{},'app.config':{'get_settings':lambda:SimpleNamespace(blogs_enabled=True)},
                            'app.db':{'engine':engine},'app.migrate':{'check':check}}.items():
        module=ModuleType(name);module.__dict__.update(attributes);monkeypatch.setitem(sys.modules,name,module)
    if reachable:
        exec(r.WORKER_DB_CODE,{})
        assert json.loads(capsys.readouterr().out)=={'schema_head':r.SCHEMA,'worker_database_ok':True}
        assert events==['queried','disposed']
    else:
        with pytest.raises(ConnectionError):exec(r.WORKER_DB_CODE,{})
        assert capsys.readouterr().out=='' and events==['queried']


def test_compose_scope_is_explicit_and_never_starts_dependencies(release):
    captured=[]
    class Docker:
        def run(self,args,**kwargs):captured.append(args);return b''
    release.docker=Docker();release.override(True)
    release.compose('up','-d','--no-deps','--no-build','--pull','never','public-video-worker')
    args=captured[0]
    assert args[:3]==['compose','--project-name','vecher-test']
    assert args[-7:]==['up','-d','--no-deps','--no-build','--pull','never','public-video-worker']
    config=r.load_json(release.checkpoint/'runtime-compose.json')
    assert set(config['services'])=={'api','public-video-worker'}
    assert config['services']['api']['image']==OTHER_IMAGE


def test_production_cli_rejects_image_and_path_overrides(capsys):
    assert r.main(['deploy','--workspace','/tmp/override'])==1
    assert 'PRODUCTION_PATH_OR_IMAGE_OVERRIDES_FORBIDDEN' in capsys.readouterr().err


def test_ci_rehearsal_rejects_missing_immutable_images(capsys,tmp_path):
    assert r.main(['rehearse','--workspace',str(tmp_path/'lab')])==1
    assert 'REHEARSAL_IMAGE_IDS_REQUIRED' in capsys.readouterr().err
