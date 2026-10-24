#!/usr/bin/env python3
"""Release14 only: a cold, rehearsed 0005 -> 0006 deployment.

This is an operator tool, not a migration bypass for deploy-realtime.sh.
Production paths and source/runtime/APK pins are deliberately not configurable.
Commands are argv lists; command stderr and environment values are never echoed.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import time
from types import ModuleType
from urllib.parse import unquote, urlsplit
import uuid

SOURCE = "40e43d85a2e434d4ee2090884a872a507f60fc93"
SOURCE_TREE = "eb38addc70e020c698e7663b8f3fd0ce39def646"
OLD_SOURCE = "713b6b34819a6df382da03d5213135986d7c0c0c"
RUNTIME = "520fb35ae85b4778f584fd4023d048fb69ad7b9afffcaba3a6d750b9fb1b0618"
OLD_RUNTIME = "c2e422a7612dc64d7bc74a0b284b59a242027e8898b0de8ffa85dcf6087172b6"
OLD_SCHEMA, SCHEMA = "0005_content", "0006_public_blogs"
APK_SHA = "a87d14f11e874f81509e4c6964f1c7355103a61e24d4e279ff61e454102bc1bc"
APK_BYTES = 36591005
APK_METADATA_SHA = "9b1e1ad5c86f865e0bc3498874c28b62b44805092d357d1b4fa82dcc0e4e660e"
PROJECT = "vecher-test"
REPOSITORY = "https://github.com/ais0930271103-glitch/vecher"
# Canonical spellings remain useful for diagnostics. SSH aliases are authorized
# separately against the effective fetch URL and the resolved OpenSSH config.
REPOSITORY_ORIGINS = frozenset(
    origin + suffix
    for origin in (REPOSITORY, "git@github.com:ais0930271103-glitch/vecher",
                   "ssh://git@github.com/ais0930271103-glitch/vecher")
    for suffix in ("", ".git")
)
HTTPS_ORIGINS = frozenset((REPOSITORY, REPOSITORY + '.git'))
ACCESS_HELPER_SHA = '3632ae72ffcfb1d66abb70bc58e7439cb292083bf14c127a0a98ea376075fb8b'
SSH_COMMAND = 'ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o UpdateHostKeys=no'
NEW_TABLES = {"public_blogs", "public_blog_consents", "blog_media_assets", "public_posts",
              "public_post_approvals", "public_post_reports"}
GIB, MIB = 1024**3, 1024**2
IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
IDENT = re.compile(r"[a-z][a-z0-9_]{0,62}\Z")
TERMINAL = {"core_ready", "blogs_ready", "blogs_disabled", "rolled_back", "failed_prelive"}


class ReleaseError(RuntimeError):
    """Stable diagnostics only. Never include subprocess output or secrets."""


def require(value, code):
    if not value:
        raise ReleaseError(code)


def origin_diagnostics(urls):
    """Only enums/booleans/counts leave this function, never URL components.

    credentials_present denotes userinfo, including the normal git SSH user;
    it does not claim that a password or token is present. Classification
    never authorizes a URL; verified_origin_report performs that check.
    """
    details=[]
    for origin in urls:
        transport,host,path,credentials='unknown',None,'',False
        try:
            if '://' in origin:
                parsed=urlsplit(origin)
                transport={'https':'https','ssh':'ssh_url'}.get(parsed.scheme,'other')
                host,path=parsed.hostname,parsed.path.removeprefix('/')
                credentials=parsed.username is not None or parsed.password is not None
            else:
                match=re.fullmatch(r'(?:(?P<user>[^/@:\s]+)@)?(?P<host>[^/:\s]+):(?P<path>[^\s]+)',origin)
                if match:
                    transport,host,path='ssh_scp',match['host'],match['path']
                    credentials=match['user'] is not None
        except ValueError:
            pass
        details.append((transport,credentials,host=='github.com',
                        path.removesuffix('.git')==REPOSITORY.removeprefix('https://github.com/')))
    return {'origin_url_count':len(urls),
            'origin_transport':details[0][0] if len(details)==1 else 'unknown',
            'origin_matches_repository':len(urls)==1 and urls[0] in REPOSITORY_ORIGINS,
            'origin_credentials_present':any(item[1] for item in details),
            'origin_host_matches_github':bool(details) and all(item[2] for item in details),
            'origin_repository_path_matches':bool(details) and all(item[3] for item in details)}


def access_helper():
    """Load the reviewed, hash-bound config reader without running its CLI."""
    path=Path(__file__).with_name('diagnose_access.py')
    try:
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd,'rb') as handle:
            info=os.fstat(handle.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_nlink==1,'UNSAFE_ACCESS_HELPER')
            raw=handle.read(128*1024+1)
        require(len(raw)<=128*1024 and hashlib.sha256(raw).hexdigest()==ACCESS_HELPER_SHA,'ACCESS_HELPER_HASH_MISMATCH')
        module=ModuleType('release14_verified_access')
        module.__file__=str(path)
        exec(compile(raw,str(path),'exec'),module.__dict__)
        return module
    except OSError:
        raise ReleaseError('ACCESS_HELPER_UNAVAILABLE') from None


def verified_origin_report(repo, *, environ=None, run=None, home=None, system=None):
    """Read only; raw URLs/configuration never leave this function.

    The dependency rejects executable/dynamic config before ssh -G. The URL
    captured here is Git's expanded *fetch* URL, not its display/push spelling.
    No SSH authentication, config mutation, or fetch takes place in this probe.
    """
    report={**origin_diagnostics([]),'origin_matches_repository':False,'origin_verified':False,
            'origin_verification_reason':'origin_unavailable','origin_effective_url_count':None,
            'origin_effective_transport':'unknown','origin_fetch_url_rewritten':None,
            'origin_ssh_alias_resolved':None}
    observed={}
    try:
        helper=access_helper()
        original=dict(os.environ if environ is None else environ)
        execute=run or helper.command
        def capture(argv,env,**kwargs):
            argv=list(argv);env={**env,'GIT_SSH_COMMAND':SSH_COMMAND}
            if argv[:3]==['git','-C',str(helper.REPO)]:
                argv[2]=str(repo)
            code,output=execute(argv,env,**kwargs)
            if argv[:3]==['git','-C',str(repo)]:
                if argv[3:]==['config','--null','--get-all','remote.origin.url'] and code==0 and output.endswith('\0'):
                    observed['configured']=output[:-1].split('\0')
                elif argv[3:]==['remote','get-url','--all','origin'] and code==0:
                    observed['effective']=output.splitlines()
            return code,output
        options={'run':capture,'environ':original,'home':home}
        if system is not None:options['system']=system
        git,ssh=helper.access_facts(**options)
        origins,effective=observed.get('configured',[]),observed.get('effective',[])
        report.update(origin_diagnostics(origins))
        report.update(origin_matches_repository=False,origin_effective_url_count=git['effective_url_count'],
            origin_effective_transport=git['effective_category'],origin_fetch_url_rewritten=git['fetch_url_rewritten'],
            origin_ssh_alias_resolved=ssh['alias_resolved'])
        def decision():
            if any(git[key] is not None and git[key]!=1 for key in ('origin_url_count','effective_url_count')):
                return 'origin_not_unique'
            if git['reason'] is not None:return 'origin_unavailable'
            if len(origins)!=1 or len(effective)!=1:return 'origin_not_unique'
            raw,target=helper.parse_origin(origins[0]),helper.parse_origin(effective[0])
            if not raw['path_matches'] or not (origins[0] in HTTPS_ORIGINS or raw['safe_ssh']):
                return 'configured_repository_mismatch'
            if not target['path_matches'] or not (effective[0] in HTTPS_ORIGINS or target['safe_ssh']):
                return 'effective_repository_mismatch'
            if git['core_ssh_command_present'] is not False:return 'ssh_command_override'
            if git['ssh_env_override_present'] or git['git_config_env_override_present'] or any(
                key in original for key in ('GIT_SSH_VARIANT','GIT_CONFIG','GIT_DIR','GIT_WORK_TREE','GIT_COMMON_DIR')):
                return 'git_environment_override'
            if git['ssh_variant_supported'] is not True:return 'ssh_variant_unsupported'
            if effective[0] in HTTPS_ORIGINS:return 'verified_https'
            if ssh['reason'] is not None or ssh['resolution_performed'] is not True:return 'ssh_resolution_unavailable'
            if ssh['match_exec_present'] is not False:return 'ssh_dynamic_configuration'
            if ssh['resolved_host_is_github'] is not True:return 'ssh_host_not_github'
            if ssh['resolved_user_is_git'] is not True:return 'ssh_user_not_git'
            if ssh['resolved_port_allowed'] is not True:return 'ssh_port_not_allowed'
            if any(ssh[key] is not False for key in ('proxy_command_present','proxy_jump_present',
                    'known_hosts_command_present','local_command_enabled','host_key_alias_present')):
                return 'ssh_command_or_proxy_configured'
            if ssh['host_key_verification_disabled'] is not False:return 'ssh_host_key_verification_disabled'
            return 'verified_ssh'
        report['origin_verification_reason']=decision()
        report['origin_verified']=report['origin_verification_reason'] in {'verified_https','verified_ssh'}
        report['origin_matches_repository']=report['origin_verified']
    except ReleaseError as error:
        report['origin_verification_reason']={
            'ACCESS_HELPER_UNAVAILABLE':'access_helper_unavailable','ACCESS_HELPER_HASH_MISMATCH':'access_helper_hash_mismatch',
            'UNSAFE_ACCESS_HELPER':'unsafe_access_helper'}.get(str(error),'origin_unavailable')
    except (OSError,ValueError,KeyError,TypeError):
        report['origin_verification_reason']='origin_unavailable'
    return report


def utc():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    digest = hashlib.sha256()
    with regular(path).open("rb") as handle:
        while chunk := handle.read(MIB):
            digest.update(chunk)
    return digest.hexdigest()


def regular(path, *, private=False):
    path = Path(path)
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "UNSAFE_FILE")
    if private:
        require(info.st_uid == os.geteuid() and info.st_mode & 0o077 == 0, "UNSAFE_PRIVATE_FILE")
    return path


def directory(path, *, create=False, private=False):
    path = Path(path)
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    for part in (path, *path.parents):
        info = part.lstat()
        require(stat.S_ISDIR(info.st_mode), "UNSAFE_DIRECTORY")
    if private:
        info = path.stat()
        require(info.st_uid == os.geteuid() and info.st_mode & 0o077 == 0, "UNSAFE_PRIVATE_DIRECTORY")
    return path


def durable(path, value):
    path = Path(path)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        folder = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)
    finally:
        temporary.unlink(missing_ok=True)


def load_json(path):
    raw = regular(path, private=True).read_bytes()
    require(len(raw) < MIB, "RECEIPT_TOO_LARGE")
    return json.loads(raw)


@contextmanager
def locks(root):
    directory(root, create=True, private=True)
    descriptors = []
    try:
        for name in (".deploy.lock", ".backup.lock"):
            fd = os.open(root / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            descriptors.append(fd)
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "UNSAFE_LOCK")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ReleaseError("ANOTHER_DEPLOYMENT_OR_BACKUP_RUNNING") from None
        yield
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


class Commands:
    def __init__(self, *, diagnostic_dir=None, synthetic=False):
        self.diagnostic_dir,self.synthetic=diagnostic_dir,synthetic

    def failure(self,label,stdout,stderr):
        raw=(stdout or b'')[-32768:]+b'\nSTDERR\n'+(stderr or b'')[-32768:]
        if self.diagnostic_dir:
            folder=directory(self.diagnostic_dir,create=True,private=True)
            path=folder/(label+'-'+uuid.uuid4().hex+'.log')
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'wb') as handle:
                handle.write(raw);handle.flush();os.fsync(handle.fileno())
        if self.synthetic:
            message=raw.decode('utf-8','replace')[-6000:]
            message=re.sub(r'postgres(?:ql)?(?:\+asyncpg)?://[^\s]+','[isolated database URL]',message)
            message=re.sub(r'(?i)(Bearer\s+)[^\s]+',r'\1[redacted]',message)
            message=re.sub(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+','[synthetic token]',message)
            print('RELEASE14_SYNTHETIC_DIAGNOSTIC '+label+'\n'+message,file=sys.stderr,flush=True)

    def run(self, argv, *, data=None, input_file=None, output=None, timeout=120, label="COMMAND", check=True):
        require(isinstance(argv, list) and all(isinstance(a, str) for a in argv), "INVALID_ARGV")
        require(data is None or input_file is None, "AMBIGUOUS_COMMAND_INPUT")
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": SSH_COMMAND}
        for name in tuple(env):
            if name.startswith(("COMPOSE_","GIT_TRACE")) or name in {"DOCKER_HOST", "DOCKER_CONTEXT", "BASH_ENV", "ENV", "SSLKEYLOGFILE"}:
                env.pop(name, None)
        try:
            result = subprocess.run(argv, input=data, stdin=input_file if input_file is not None else (subprocess.DEVNULL if data is None else None),
                stdout=output if output else subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=timeout, env=env, check=False)
        except subprocess.TimeoutExpired as error:
            self.failure(label,error.output,error.stderr)
            raise ReleaseError(label + "_UNAVAILABLE_OR_TIMEOUT") from None
        except OSError:
            raise ReleaseError(label + "_UNAVAILABLE_OR_TIMEOUT") from None
        if check and result.returncode:
            self.failure(label,result.stdout,result.stderr)
            raise ReleaseError(label + "_FAILED")
        raw = result.stdout or b""
        require(len(raw) <= 32 * MIB, label + "_OUTPUT_LIMIT")
        return raw


# This child deadline includes DNS, TLS and both bounded response reads. It has
# no deployment, Docker, Git, local API, client-key or credential operations.
PUBLIC_HTTPS_PROBE = r'''
from concurrent.futures import ThreadPoolExecutor
import json, os, socket, ssl, urllib.error, urllib.request
os.environ.pop('SSLKEYLOGFILE',None)
class ProbeError(Exception): pass
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs): return None
def category(error):
 if isinstance(error,ssl.SSLCertVerificationError): return 'tls_verification_failed'
 if isinstance(error,ssl.SSLError): return 'tls_error'
 if isinstance(error,(TimeoutError,socket.timeout)): return 'timeout'
 if isinstance(error,socket.gaierror): return 'dns_error'
 return 'network_error'
def unique(pairs):
 result={}
 for key,value in pairs:
  if key in result: raise ValueError()
  result[key]=value
 return result
def fetch(path):
 try:
  opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect(),
      urllib.request.HTTPSHandler(context=ssl.create_default_context()))
  request=urllib.request.Request('https://api-test.xn--b1aga8b7a.online'+path,headers={'Accept':'application/json'})
  with opener.open(request,timeout=8) as response:
   if response.status!=200: raise ProbeError('http_error')
   raw=response.read(16385)
  if len(raw)>16384: raise ValueError()
  result=json.loads(raw,object_pairs_hook=unique)
  if not isinstance(result,dict): raise ValueError()
  return result
 except urllib.error.HTTPError as error:
  raise ProbeError('redirect_blocked' if 300<=error.code<400 else 'http_error') from None
 except urllib.error.URLError as error: raise ProbeError(category(error.reason)) from None
 except (ValueError,UnicodeError): raise ProbeError('invalid_response') from None
 except ProbeError: raise
 except Exception as error: raise ProbeError(category(error)) from None
try:
 with ThreadPoolExecutor(max_workers=2) as pool:
  health_job=pool.submit(fetch,'/health'); version_job=pool.submit(fetch,'/app/version')
  health,version=health_job.result(),version_job.result()
 if health.get('ok') is not True or health.get('env')!='staging': raise ProbeError('unhealthy_api')
 result={'api_version':health.get('version'),'env':'staging',
     **{key:version.get(key) for key in ('version_name','version_code','sha256','bytes','required')}}
except ProbeError as error: result={'error_category':str(error)}
except Exception: result={'error_category':'invalid_response'}
print(json.dumps(result,sort_keys=True))
'''


def public_status(commands=None):
    raw=(commands or Commands()).run([sys.executable,'-I','-'],data=PUBLIC_HTTPS_PROBE.encode(),
                                    timeout=20,label='PUBLIC_HTTPS')
    try:result=json.loads(raw)
    except (ValueError,UnicodeError):raise ReleaseError('PUBLIC_HTTPS_INVALID_RESPONSE') from None
    require(isinstance(result,dict),'PUBLIC_HTTPS_INVALID_RESPONSE')
    error=result.get('error_category')
    if error is not None:
        allowed={'tls_verification_failed','tls_error','timeout','dns_error','network_error',
                 'redirect_blocked','http_error','invalid_response','unhealthy_api'}
        raise ReleaseError('PUBLIC_HTTPS_'+(error.upper() if isinstance(error,str) and error in allowed else 'INVALID_RESPONSE'))
    require(set(result)=={'api_version','env','version_name','version_code','sha256','bytes','required'},'PUBLIC_HTTPS_INVALID_RESPONSE')
    require(result['env']=='staging' and all(isinstance(result[k],str) and re.fullmatch(r'[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}',result[k])
                for k in ('api_version','version_name')),'PUBLIC_HTTPS_INVALID_RESPONSE')
    require(type(result['version_code']) is int and 1<=result['version_code']<=2147483647
            and type(result['bytes']) is int and 1<=result['bytes']<=256*MIB and type(result['required']) is bool
            and isinstance(result['sha256'],str) and re.fullmatch(r'[0-9a-f]{64}',result['sha256']),'PUBLIC_HTTPS_INVALID_RESPONSE')
    return result


class Docker:
    def __init__(self, commands):
        self.commands = commands

    def run(self, args, **kwargs):
        return self.commands.run(["docker", "--context", "default", *args], **kwargs)

    def inspect(self, name, kind="container"):
        rows = json.loads(self.run([kind, "inspect", name], label="INSPECT"))
        require(len(rows) == 1, "AMBIGUOUS_DOCKER_OBJECT")
        return rows[0]

    def exists(self,name,kind='container'):
        if kind=='container':
            args=['ps','-aq','--filter','name=^/'+name+'$']
        else:
            args=[kind,'ls','-q','--filter','name=^'+name+'$']
        return bool(self.run(args,label='RESOURCE_LOOKUP').strip())

    def image(self, name):
        info = self.inspect(name, "image")
        require(IMAGE.fullmatch(info["Id"]), "INVALID_IMAGE_ID")
        return info["Id"]

    def service(self, service, *, optional=False):
        ids = self.run(["ps", "-aq", "--filter", "label=com.docker.compose.project=" + PROJECT,
                        "--filter", "label=com.docker.compose.service=" + service], label="SERVICE_INVENTORY").decode().split()
        if optional and not ids:
            return None
        require(len(ids) == 1, "SERVICE_NOT_UNIQUE_" + service.upper().replace("-", "_"))
        info = self.inspect(ids[0])
        labels = info["Config"].get("Labels") or {}
        require(labels.get("com.docker.compose.project") == PROJECT
                and labels.get("com.docker.compose.service") == service, "WRONG_SERVICE_SCOPE")
        return info

    def python(self, container, code, *args, timeout=120):
        return self.run(["exec", "-i", container, "python", "-", *args], data=code.encode(),
                        timeout=timeout, label="PYTHON_PROBE")

    def sql(self, container, sql, database="postgres"):
        require(IDENT.fullmatch(database), "UNSAFE_DATABASE_NAME")
        return self.run(["exec", "-i", container, "psql", "-X", "-q", "-t", "-A", "-v",
                         "ON_ERROR_STOP=1", "-U", "vecher", "-d", database], data=sql.encode(), label="SQL")


RUNTIME_CODE = r'''import hashlib, json, stat
from pathlib import Path
root=Path('/srv'); files=[root/'requirements.txt',root/'alembic.ini']
for folder in ('app','alembic'):
 for p in (root/folder).rglob('*'):
  if '__pycache__' in p.parts or p.suffix=='.pyc': continue
  if stat.S_ISDIR(p.lstat().st_mode): continue
  files.append(p)
manifest={}
for p in files:
 before=p.lstat(); assert stat.S_ISREG(before.st_mode) and before.st_nlink==1
 raw=p.read_bytes(); after=p.lstat()
 assert (before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)==(after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns)
 manifest[str(p.relative_to(root))]=hashlib.sha256(raw).hexdigest()
print(json.dumps({'runtime_sha256':hashlib.sha256(json.dumps(manifest,sort_keys=True,separators=(',',':')).encode()).hexdigest(),'runtime_files':len(manifest)}))
'''

API_CODE = r'''import asyncio, json, urllib.request
from app.config import get_settings
from app.db import engine
from app.migrate import check
async def main():
 s=get_settings(); head=await check(engine)
 health=json.load(urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=5))
 schema=json.load(urllib.request.urlopen('http://127.0.0.1:8000/openapi.json',timeout=5))
 from app.routers.content import _snapshot, _available
 snap=_snapshot(); items=_available(snap)
 result={'schema_head':head,'api_version':health.get('version'),'ok':health.get('ok') is True and health.get('env')=='staging','blogs_enabled':getattr(s,'blogs_enabled',False),'catalogue_items':len(items),'paths':list(schema['paths'])}
 if health.get('version')=='0.9.0':
  param=next(p for p in schema['paths']['/content/feed']['get']['parameters'] if p['name']=='hosted_video')
  assert param['schema'].get('default') is False
  assert '/blogs' in schema['paths'] and '/blogs/media/{asset_id}/video' in schema['paths']
 print(json.dumps(result)); await engine.dispose()
asyncio.run(main())
'''

FINGERPRINT_CODE = r'''import asyncio, hashlib, json
from sqlalchemy import text
from app.db import engine
async def main():
 result={}
 async with engine.connect() as c:
  await c.execute(text("SET timezone='UTC'"))
  names=(await c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"))).scalars().all()
  for name in names:
   if name=='alembic_version': continue
   escaped=name.replace('"','""'); digest=hashlib.sha256(); count=0
   stream=await c.stream(text('SELECT to_jsonb(t)::text FROM "'+escaped+'" t ORDER BY to_jsonb(t)::text COLLATE "C"'))
   async for row in stream:
    digest.update(row[0].encode()); digest.update(b'\n'); count+=1
   result[name]={'rows':count,'sha256':digest.hexdigest()}
 print(json.dumps(result,sort_keys=True)); await engine.dispose()
asyncio.run(main())
'''

DECRYPT_CODE = r'''import asyncio,json
from sqlalchemy import Text,select,type_coerce
from app.db import engine
from app.models import Base
from app.crypto import Encrypted,decrypt
async def main():
 count=0
 async with engine.connect() as c:
  for table in Base.metadata.sorted_tables:
   for col in table.columns:
    if isinstance(col.type,Encrypted):
     stream=await c.stream(select(type_coerce(col,Text)).where(col.is_not(None)))
     async for row in stream:
      decrypt(row[0]); count+=1
 print(json.dumps({'encrypted_values_verified':count})); await engine.dispose()
asyncio.run(main())
'''

WORKER_DB_CODE = r'''import asyncio,json,os,secrets
assert not os.environ.get('VECHER_DATA_KEYS')
os.environ['VECHER_JWT_SECRET']=secrets.token_urlsafe(48)
from app.config import get_settings
from app.db import engine
from app.migrate import check
async def main():
 assert get_settings().blogs_enabled
 head=await asyncio.wait_for(check(engine),timeout=10)
 print(json.dumps({'schema_head':head,'worker_database_ok':True}));await engine.dispose()
asyncio.run(main())
'''


def tree_manifest(root):
    root = directory(root)
    result = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        regular(path)
        result[str(path.relative_to(root))] = {"sha256": sha(path), "bytes": info.st_size,
                                               "mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "gid": info.st_gid}
    return result


def archive_tree(source, destination):
    before = tree_manifest(source)
    with tarfile.open(destination, "w:gz", dereference=False) as archive:
        archive.add(source, arcname=".", recursive=True)
    require(tree_manifest(source) == before, "FILES_CHANGED_DURING_BACKUP")
    regular(destination)
    return before


def extract_tree(archive, destination):
    directory(destination, create=True)
    require(not any(destination.iterdir()), "RESTORE_DIRECTORY_NOT_EMPTY")
    with tarfile.open(regular(archive), "r:gz") as handle:
        members = handle.getmembers()
        for member in members:
            path = Path(member.name)
            require(not path.is_absolute() and ".." not in path.parts
                    and (member.isdir() or member.isfile()) and not member.issym() and not member.islnk(), "UNSAFE_BACKUP_MEMBER")
        # Members were checked above; the destination is an empty, private tree.
        # Avoid depending on the Python3.12-only tar filter argument on the host.
        handle.extractall(destination, members=members)


def write_env(path, values):
    for key, value in values.items():
        require(re.fullmatch(r"[A-Z][A-Z0-9_]*", key) and "\n" not in value and "\r" not in value, "INVALID_ENV_VALUE")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as handle:
        for key, value in values.items():
            handle.write(key + "=" + value + "\n")
        handle.flush(); os.fsync(handle.fileno())


class Lab:
    """Fresh named resources on one internal network, never production binds."""
    def __init__(self, docker, workspace, images, *, keys, helper, checkpoint=None):
        self.docker, self.workspace = docker, directory(workspace, create=True, private=True)
        require(not self.workspace.is_relative_to(Path('/opt/vecher-test')), "REHEARSAL_LIVE_PATH_FORBIDDEN")
        self.images, self.keys, self.helper = images, keys, regular(helper)
        self.token = uuid.uuid4().hex[:12]
        self.prefix = "vecher-release14-" + self.token
        self.network = self.prefix + "-net"
        self.db = self.prefix + "-db"
        self.password = secrets.token_hex(24)
        self.containers = []
        self.volume = self.prefix + "-dbdata"
        self.checkpoint = checkpoint
        durable(self.workspace/'lab-resources.json',{'token':self.token,'network':self.network,'volume':self.volume,
            'database':self.db,'images':images,'production_resources':False})

    def __enter__(self):
        try:
            self.docker.run(["network", "create", "--internal", "--label", "vecher.release14=" + self.token, self.network], label="LAB_NETWORK")
            self.docker.run(["volume", "create", "--label", "vecher.release14=" + self.token, self.volume], label="LAB_VOLUME")
            env = self.workspace / "postgres.env"
            write_env(env, {"POSTGRES_USER":"vecher", "POSTGRES_PASSWORD":self.password, "POSTGRES_DB":"vecher"})
            self.containers.append(self.db)
            self.docker.run(["run", "-d", "--name", self.db, "--label", "vecher.release14=" + self.token,
                "--network", self.network, "--network-alias", "db", "--memory", "256m", "--cpus", "1",
                "--env-file", str(env), "--mount", "type=volume,source=" + self.volume + ",target=/var/lib/postgresql/data",
                self.images['db']], label="LAB_DATABASE")
            for _ in range(45):
                raw = self.docker.run(["exec", self.db, "pg_isready", "-U", "vecher"], check=False, label="LAB_WAIT")
                if b"accepting connections" in raw:
                    return self
                time.sleep(1)
            raise ReleaseError("LAB_DATABASE_NOT_READY")
        except BaseException:
            self.__exit__()
            raise

    def __exit__(self, *_):
        # A generated name is insufficient: check the ownership label too.
        for name in reversed(self.containers):
            if not self.docker.exists(name):continue
            info = self.docker.inspect(name)
            require((info['Config'].get('Labels') or {}).get('vecher.release14') == self.token, "LAB_CLEANUP_SCOPE")
            self.docker.run(["rm", "-f", name], label="LAB_CONTAINER_CLEANUP")
        for kind, name in (("volume",self.volume),("network",self.network)):
            if not self.docker.exists(name,kind):continue
            info = self.docker.inspect(name, kind)
            require((info.get('Labels') or {}).get('vecher.release14') == self.token, "LAB_CLEANUP_SCOPE")
            self.docker.run([kind,"rm",name], label="LAB_RESOURCE_CLEANUP")

    def environment(self, database, *, synthetic=False):
        require(IDENT.fullmatch(database), "UNSAFE_DATABASE_NAME")
        env = {"VECHER_ENV":"staging", "VECHER_DATABASE_URL":f"postgresql+asyncpg://vecher:{self.password}@db:5432/{database}",
               "VECHER_JWT_SECRET":secrets.token_hex(32), "VECHER_DATA_KEYS":self.keys,
               "VECHER_SCHEDULER_ENABLED":"false", "VECHER_OUTBOX_ENABLED":"false", "VECHER_REALTIME_ENABLED":"false",
               "VECHER_SMTP_HOST":"", "VECHER_SMTP_USER":"", "VECHER_SMTP_PASSWORD":"", "VECHER_SMTP_FROM":"",
               "VECHER_FIREBASE_CREDENTIALS":"/run/vecher-release14-disabled-push.json", "VECHER_RELEASE_TOKEN":"",
               "VECHER_BLOGS_ENABLED":"true" if synthetic else "false", "VECHER_BLOG_MEDIA_DIR":"/srv/blog-media",
               "VECHER_UPLOADS_DIR":"/srv/private-unused"}
        if synthetic:
            import base64
            env['VECHER_DATA_KEYS'] = 'rehearsal:' + base64.b64encode(secrets.token_bytes(32)).decode()
            env['VECHER_RELEASE14_SYNTHETIC_REHEARSAL'] = '1'
        return env

    def one(self, image, env, command, *, code=None, input_file=None, mounts=(), timeout=300):
        path = self.workspace / ("env-" + uuid.uuid4().hex)
        write_env(path, env)
        name=self.prefix+'-once-'+uuid.uuid4().hex[:8]
        self.containers.append(name)
        args = ["run", "--rm", "-i", "--name", name, "--label", "vecher.release14="+self.token,
                "--network", self.network, "--memory", "320m", "--cpus", "1",
                "--env-file", str(path)]
        for mount in mounts:
            args += ["--mount", mount]
        try:
            return self.docker.run([*args,image,*command], data=code.encode() if code else None,input_file=input_file,
                                   timeout=timeout, label="LAB_ONE_SHOT")
        finally:
            self.remove_container(name)

    def remove_container(self,name):
        if self.docker.exists(name):
            info=self.docker.inspect(name)
            require((info['Config'].get('Labels') or {}).get('vecher.release14')==self.token,'LAB_CLEANUP_SCOPE')
            self.docker.run(['rm','-f',info['Id']],label='LAB_CONTAINER_CLEANUP')
        if name in self.containers:self.containers.remove(name)

    def restore(self, dump, database):
        require(IDENT.fullmatch(database), "UNSAFE_DATABASE_NAME")
        self.docker.sql(self.db, 'CREATE DATABASE "' + database + '" OWNER vecher;')
        with regular(dump).open('rb') as incoming:
            # File-backed stdin avoids holding the backup in memory.
            self.one(self.images['db'],{'PGHOST':'db','PGUSER':'vecher','PGPASSWORD':self.password,
                     'PGDATABASE':database,'PGCONNECT_TIMEOUT':'15'},
                ['pg_restore','--exit-on-error','--single-transaction','--dbname',database],input_file=incoming,timeout=900)

    def fingerprint(self, image, database):
        return json.loads(self.one(image,self.environment(database),["python","-"],code=FINGERPRINT_CODE))

    def migration(self, image, database, action):
        raw = self.one(image,self.environment(database),["python","-m","app.migrate",action])
        return raw.decode().strip()

    def api_check(self,image,database,*,version,schema):
        env=self.workspace/('api-check-'+uuid.uuid4().hex+'.env')
        write_env(env,self.environment(database))
        name=self.prefix+'-check-'+uuid.uuid4().hex[:8]
        self.containers.append(name)
        self.docker.run(['run','-d','--name',name,'--label','vecher.release14='+self.token,'--network',self.network,
            '--env-file',str(env),'--memory','320m','--cpus','1',image],label='RESTORED_API_START')
        try:
            return wait_api(self.docker,name,blogs=False,version=version,schema=schema)
        finally:
            self.docker.run(['rm','-f',name],label='RESTORED_API_STOP')
            self.containers.remove(name)

    def media(self):
        database = "vecher_release14_smoke_" + self.token
        self.docker.sql(self.db, 'CREATE DATABASE "' + database + '" OWNER vecher;')
        env = self.environment(database, synthetic=True)
        self.one(self.images['api'],env,["python","-m","app.migrate","upgrade"])
        media = directory(self.workspace / 'synthetic-media', create=True)
        os.chown(media,10001,10001)
        mount = 'type=bind,source=' + str(media) + ',target=/srv/blog-media'
        common = ["--read-only","--tmpfs","/tmp:rw,noexec,nosuid,size=16m","--memory","512m","--cpus","1",
                  "--pids-limit","64","--cap-drop","ALL","--security-opt","no-new-privileges:true"]
        fixture_name=self.prefix+'-fixture';self.containers.append(fixture_name)
        try:
            self.docker.run(["run","--rm","--name",fixture_name,"--label","vecher.release14="+self.token,"--network","none",*common,"--mount",mount,"--entrypoint","ffmpeg",self.images['worker'],
                "-nostdin","-v","error","-y","-f","lavfi","-i","color=c=blue:s=64x48:r=10:d=0.6",
                "-f","lavfi","-i","sine=frequency=440:sample_rate=48000:duration=0.6","-c:v","libx264","-threads","1",
                "-pix_fmt","yuv420p","-c:a","aac","-shortest","-metadata","comment=source-metadata-must-be-removed",
                "/srv/blog-media/rehearsal-fixture.mp4"],timeout=60,label="SYNTHETIC_VIDEO")
        finally:self.remove_container(fixture_name)
        api_env = self.workspace / 'synthetic-api.env'; write_env(api_env,env)
        api = self.prefix + '-api'; worker = self.prefix + '-worker'
        # SCP commonly creates the uploaded helper as0600. A private bind copy
        # must be readable by the API's UID10001 inside the container.
        mounted_helper=self.workspace/'media_rehearsal.py'
        shutil.copyfile(self.helper,mounted_helper);os.chmod(mounted_helper,0o444)
        self.containers.append(api)
        self.docker.run(["run","-d","--name",api,"--label","vecher.release14="+self.token,"--network",self.network,
            "--env-file",str(api_env),"--memory","320m","--cpus","1","--mount",mount,"--mount",
            'type=bind,source='+str(mounted_helper)+',target=/srv/release14_media_rehearsal.py,readonly',self.images['api']],label="SYNTHETIC_API")
        worker_env = {k:v for k,v in env.items() if k in {'VECHER_ENV','VECHER_DATABASE_URL','VECHER_BLOGS_ENABLED','VECHER_BLOG_MEDIA_DIR','VECHER_SCHEDULER_ENABLED','VECHER_OUTBOX_ENABLED'}}
        path = self.workspace/'synthetic-worker.env'; write_env(path,worker_env)
        self.containers.append(worker)
        self.docker.run(["run","-d","--name",worker,"--label","vecher.release14="+self.token,"--network",self.network,
                        *common,"--env-file",str(path),"--mount",mount,self.images['worker']],label="SYNTHETIC_WORKER")
        wait_api(self.docker,api,blogs=True)
        result = self.docker.run(["exec",api,"python","/srv/release14_media_rehearsal.py","--base-url","http://127.0.0.1:8000",
            "--fixture","/srv/blog-media/rehearsal-fixture.mp4","--expected-database",database],timeout=150,label="MEDIA_REHEARSAL")
        require(b'RELEASE14_MEDIA_REHEARSAL_OK' in result,"MEDIA_REHEARSAL_MARKER_MISSING")
        state = self.docker.inspect(worker)
        require(state['State']['Running'] and not state['State'].get('OOMKilled') and state.get('RestartCount',0)==0,"SYNTHETIC_WORKER_UNHEALTHY")
        return True


def wait_api(docker, container, *, blogs, version="0.9.0", schema=SCHEMA):
    for _ in range(30):
        try:
            result = json.loads(docker.python(container,API_CODE))
            require(result['ok'] and result['api_version']==version and result['schema_head']==schema
                    and result['blogs_enabled'] is blogs,"API_CONTRACT_MISMATCH")
            return result
        except ReleaseError:
            time.sleep(1)
    raise ReleaseError("API_NOT_READY")


def verify_legacy(before, after):
    require(all(after.get(name)==value for name,value in before.items()),"LEGACY_DATA_CHANGED")
    require(set(after)-set(before)==NEW_TABLES and all(after[n]['rows']==0 for n in NEW_TABLES),"MIGRATION_TABLES_MISMATCH")


def swap_databases(docker, container, current, recovered, retained, *, rollback_after_first=False):
    """One PostgreSQL transaction; the CI rehearsal invokes this exact helper.

    Connect to postgres, which is never renamed. The first-rename rollback
    exercises the interruption boundary before the real two-rename commit.
    Server-side timeouts also bound a psql process whose Docker CLI was lost.
    """
    names=(current,recovered,retained)
    require(all(IDENT.fullmatch(name) and name not in {'postgres','template0','template1'}
                for name in names) and len(set(names))==3,'UNSAFE_DATABASE_SWAP')
    statements=["SET lock_timeout='30s';", "SET statement_timeout='60s';",
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN ('"+
        current+"','"+recovered+"') AND pid<>pg_backend_pid();", 'BEGIN;',
        'ALTER DATABASE "'+current+'" RENAME TO "'+retained+'";']
    if rollback_after_first:
        statements.append('ROLLBACK;')
    else:
        statements += ['ALTER DATABASE "'+recovered+'" RENAME TO "'+current+'";', 'COMMIT;']
    return docker.sql(container,'\n'.join(statements),'postgres')


def rehearse(docker, workspace, images, backup, keys, helper, *, fixture=False):
    """Shared real-Docker gate used by production and CI; no production writes."""
    for image in images.values():
        require(IMAGE.fullmatch(image),"REHEARSAL_REQUIRES_IMAGE_IDS")
    workspace = directory(workspace,create=True,private=True)
    backup = directory(backup,create=fixture,private=True)
    require(not backup.is_relative_to(Path('/opt/vecher-test')),'REHEARSAL_LIVE_PATH_FORBIDDEN')
    with Lab(docker,workspace,images,keys=keys,helper=helper) as lab:
        if fixture:
            lab.migration(images['old_api'],'vecher','upgrade')
            lab.one(images['old_api'],lab.environment('vecher'),['python','-'],code=r'''import asyncio,json
from app.db import SessionLocal
from app.models import User,ContentPreferences
async def main():
 async with SessionLocal() as s:
  u=User(email='restore-fixture@example.invalid');s.add(u);await s.flush()
  s.add(ContentPreferences(user_id=u.id,data=json.dumps({'topics':['digital']}),revision=1));await s.commit()
asyncio.run(main())
''')
            with (backup/'db.dump').open('xb') as output:
                docker.run(['exec',lab.db,'pg_dump','-U','vecher','-d','vecher','--format=custom'],output=output,timeout=300,label='FIXTURE_DUMP')
            durable(backup/'fingerprint.json',lab.fingerprint(images['old_api'],'vecher'))
        dump=regular(backup/'db.dump'); before=load_json(backup/'fingerprint.json')
        lab.restore(dump,'vecher_restore')
        require(lab.migration(images['old_api'],'vecher_restore','check')=='SCHEMA_HEAD_OK='+OLD_SCHEMA,'RESTORED_OLD_SCHEMA_MISMATCH')
        require(lab.fingerprint(images['old_api'],'vecher_restore')==before,'RESTORE_DATA_MISMATCH')
        lab.one(images['old_api'],lab.environment('vecher_restore'),['python','-'],code=DECRYPT_CODE)
        lab.api_check(images['old_api'],'vecher_restore',version='0.8.0',schema=OLD_SCHEMA)
        require(lab.migration(images['api'],'vecher_restore','upgrade')=='MIGRATION_OK='+SCHEMA,'REHEARSAL_MIGRATION_MISMATCH')
        verify_legacy(before,lab.fingerprint(images['api'],'vecher_restore'))
        lab.api_check(images['api'],'vecher_restore',version='0.9.0',schema=SCHEMA)
        lab.restore(dump,'vecher_recovered')
        require(lab.fingerprint(images['old_api'],'vecher_recovered')==before,'ROLLBACK_RESTORE_DATA_MISMATCH')
        require(lab.migration(images['old_api'],'vecher_recovered','check')=='SCHEMA_HEAD_OK='+OLD_SCHEMA,'ROLLBACK_SCHEMA_MISMATCH')
        swap_databases(docker,lab.db,'vecher_restore','vecher_recovered','vecher_retained',rollback_after_first=True)
        require(lab.migration(images['api'],'vecher_restore','check')=='SCHEMA_HEAD_OK='+SCHEMA,'RENAME_ROLLBACK_SCHEMA_MISMATCH')
        verify_legacy(before,lab.fingerprint(images['api'],'vecher_restore'))
        require(lab.fingerprint(images['old_api'],'vecher_recovered')==before,'RENAME_ROLLBACK_DATA_MISMATCH')
        swap_databases(docker,lab.db,'vecher_restore','vecher_recovered','vecher_retained')
        require(lab.migration(images['old_api'],'vecher_restore','check')=='SCHEMA_HEAD_OK='+OLD_SCHEMA,'SWAPPED_OLD_SCHEMA_MISMATCH')
        require(lab.fingerprint(images['old_api'],'vecher_restore')==before,'SWAPPED_OLD_DATA_MISMATCH')
        verify_legacy(before,lab.fingerprint(images['api'],'vecher_retained'))
        lab.api_check(images['old_api'],'vecher_restore',version='0.8.0',schema=OLD_SCHEMA)
        lab.media()
    return {'rehearsal_ok':True,'rollback_rehearsal_ok':True,'media_rehearsal_ok':True}


class Release:
    def __init__(self, *, commands=None, root=Path('/opt/vecher-test'), backups=Path('/root/backups/vecher-test'), helper=None):
        self.root,self.repo,self.staging = root,root/'repo',root/'repo/infra/staging'
        self.backups=backups;self.current=backups/'release14-current.json'
        self.commands=commands or Commands();self.docker=Docker(self.commands)
        self.helper=helper or Path(__file__).with_name('media_rehearsal.py')
        self.journal=None;self.checkpoint=None

    def git(self,*args,strip=True):
        value=self.commands.run(['git','-C',str(self.repo),*args],label='SOURCE').decode()
        return value.strip() if strip else value

    def origin_urls(self):
        value=self.git('config','--null','--get-all','remote.origin.url',strip=False)
        require(value.endswith('\0'),'INVALID_ORIGIN_CONFIGURATION')
        return value[:-1].split('\0')

    def origin_report(self):
        return verified_origin_report(self.repo)

    def verify_origin(self):
        report=self.origin_report()
        require(report['origin_verified'],'WRONG_ORIGIN')
        return report

    def read(self):
        if not self.current.exists():return None
        pointer=load_json(self.current)
        require(pointer.get('schema')==1 and pointer.get('release')==14,'INVALID_RELEASE_POINTER')
        checkpoint=Path(pointer['checkpoint'])
        require(checkpoint.parent==self.backups and re.fullmatch('release-14-[0-9a-f]{32}',checkpoint.name),'UNSAFE_CHECKPOINT_PATH')
        directory(checkpoint,private=True)
        journal=load_json(checkpoint/'journal.json')
        require(journal.get('schema')==1 and journal.get('release')==14 and journal.get('source_sha')==SOURCE
                and journal.get('runtime_sha256')==RUNTIME,'INVALID_RELEASE_JOURNAL')
        self.checkpoint,self.journal=checkpoint,journal
        return journal

    def phase(self,phase,**values):
        self.journal.update(values);self.journal['phase']=phase;self.journal['updated_at']=utc()
        durable(self.checkpoint/'journal.json',self.journal)
        print('RELEASE14_PHASE='+phase,flush=True)

    def temporary(self,image,kind):
        name='vecher-release14-'+kind+'-'+uuid.uuid4().hex[:12]
        self.journal.setdefault('temporary_containers',[]).append({'name':name,'image':image,'kind':kind})
        durable(self.checkpoint/'journal.json',self.journal)
        return name

    def remove_temporary(self,name):
        entries=self.journal.get('temporary_containers',[])
        entry=next((entry for entry in entries if entry['name']==name),None)
        require(entry is not None,'UNKNOWN_TEMPORARY_CONTAINER')
        if self.docker.exists(name):
            info=self.docker.inspect(name);labels=info['Config'].get('Labels') or {}
            require(labels.get('vecher.release14.checkpoint')==self.checkpoint.name
                    and info['Image']==entry['image'],'TEMPORARY_CONTAINER_SCOPE')
            self.docker.run(['rm','-f',info['Id']],label='STOP_TEMPORARY_COMMAND')
        self.journal['temporary_containers']=[item for item in entries if item['name']!=name]
        durable(self.checkpoint/'journal.json',self.journal)

    def cleanup_temporary(self):
        for entry in list(self.journal.get('temporary_containers',[])):
            self.remove_temporary(entry['name'])
        # A complete journal must account for every container carrying its label.
        found=self.docker.run(['ps','-aq','--filter','label=vecher.release14.checkpoint='+self.checkpoint.name],label='TEMPORARY_INVENTORY')
        require(not found.strip(),'UNJOURNALED_TEMPORARY_CONTAINER')

    def begin(self,baseline):
        self.checkpoint=directory(self.backups/('release-14-'+uuid.uuid4().hex),create=True,private=True)
        self.journal={'schema':1,'release':14,'source_sha':SOURCE,'source_tree':SOURCE_TREE,'previous_source_sha':OLD_SOURCE,
            'runtime_sha256':RUNTIME,'phase':'created','created_at':utc(),'traffic_may_have_opened':False,**baseline}
        self.phase('created')
        durable(self.current,{'schema':1,'release':14,'checkpoint':str(self.checkpoint)})
        self.commands.diagnostic_dir=self.checkpoint/'logs'

    def apk(self):
        downloads=directory(self.root/'downloads')
        require(sha(downloads/'vecher.apk')==APK_SHA and (downloads/'vecher.apk').stat().st_size==APK_BYTES,'APK13_CHANGED')
        require(sha(downloads/'version.json')==APK_METADATA_SHA,'APK13_METADATA_CHANGED')
        metadata=json.loads((downloads/'version.json').read_bytes())
        require(set(metadata)=={'version_code','version_name','notes','required','sha256','bytes'} and metadata['version_code']==13
                and metadata['version_name']=='0.8.0' and metadata['required'] is False and metadata['sha256']==APK_SHA
                and metadata['bytes']==APK_BYTES,'APK13_METADATA_INVALID')

    def runtime(self,container,expected):
        value=json.loads(self.docker.python(container,RUNTIME_CODE))
        require(value['runtime_sha256']==expected,'RUNTIME_SOURCE_MISMATCH')
        return value

    def budget(self,*,worker=False):
        mem={line.split(':')[0]:int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines() if ':' in line}
        require(mem.get('MemAvailable',0)>=(768 if worker else 1024)*MIB,'INSUFFICIENT_MEMORY_HEADROOM')
        disk=shutil.disk_usage(self.root)
        require(disk.free>=3*GIB,'INSUFFICIENT_DISK_HEADROOM')

    def preflight(self):
        require(os.geteuid()==0,'ROOT_REQUIRED');directory(self.root);directory(self.repo);directory(self.staging)
        regular(self.staging/'.env',private=True);directory(self.staging/'secrets')
        require(self.git('rev-parse','HEAD')==OLD_SOURCE,'LIVE_CHECKOUT_IS_NOT_RELEASE13')
        require(not self.git('status','--porcelain'),'DIRTY_SERVER_CHECKOUT')
        self.verify_origin()
        api=self.docker.service('api');db=self.docker.service('db');worker=self.docker.service('public-video-worker',optional=True)
        require(api['State']['Running'] and db['State']['Running'],'LIVE_SERVICES_NOT_RUNNING')
        require(not worker or not worker['State']['Running'],'UNEXPECTED_EXISTING_MEDIA_WORKER')
        require(api['HostConfig'].get('PortBindings',{}).get('8000/tcp')==[{'HostIp':'127.0.0.1','HostPort':'8092'}],'UNEXPECTED_API_BINDING')
        self.runtime(api['Id'],OLD_RUNTIME)
        wait_api(self.docker,api['Id'],blogs=False,version='0.8.0',schema=OLD_SCHEMA)
        version=int(self.docker.sql(db['Id'],'SHOW server_version_num;','vecher').strip())
        require(160000<=version<170000,'POSTGRES16_REQUIRED')
        self.apk();self.budget();regular(self.helper)
        env=dict(entry.split('=',1) for entry in api['Config'].get('Env',[]) if '=' in entry)
        require(env.get('VECHER_DATA_KEYS'),'DATA_KEYS_REQUIRED')
        resolved=json.loads(self.docker.run(['compose','--project-name',PROJECT,'--project-directory',str(self.staging),
            '--env-file',str(self.staging/'.env'),'-f',str(self.staging/'docker-compose.yml'),'config','--format','json'],label='OLD_COMPOSE_CONFIG'))
        configured=resolved['services']['api'].get('environment',{})
        require(all(str(value)==env.get(key) for key,value in configured.items() if key.startswith('VECHER_')),'LIVE_ENVIRONMENT_DRIFT')
        # Return no secret values; the live environment is read again privately.
        return {'old_api_container':api['Id'],'db_container':db['Id'],'old_api_image_id':api['Image'],'db_image_id':db['Image'],
                'old_env_sha256':sha(self.staging/'.env'),'media_helper_sha256':sha(self.helper),'old_api_version':'0.8.0'}

    def compose(self,*args,override=None):
        path=override or self.checkpoint/'runtime-compose.json'
        return self.docker.run(['compose','--project-name',PROJECT,'--project-directory',str(self.staging),
            '--env-file',str(self.staging/'.env'),'-f',str(self.staging/'docker-compose.yml'),'-f',str(path),
            '--profile','public-video',*args],timeout=180,label='SCOPED_COMPOSE')

    def override(self,blogs):
        durable(self.checkpoint/'runtime-compose.json',{'services':{'api':{'image':self.journal['api_image_id'],
             'environment':{'VECHER_BLOGS_ENABLED':'true' if blogs else 'false'}},'public-video-worker':{'image':self.journal['worker_image_id'],
             'environment':{'VECHER_BLOGS_ENABLED':'true' if blogs else 'false'}}}})

    def prepare(self):
        self.phase('preparing_images')
        old=self.docker.inspect(self.journal['old_api_container'])
        actual=dict(entry.split('=',1) for entry in old['Config'].get('Env',[]) if '=' in entry)
        durable(self.checkpoint/'original-api-env.json',{k:v for k,v in actual.items() if k.startswith('VECHER_')})
        # Re-read effective URLs and SSH configuration immediately before fetch.
        # No remote/key/configuration is rewritten, and known-host checking is strict.
        self.verify_origin()
        self.git('fetch','--no-tags','origin',SOURCE)
        require(self.git('rev-parse',SOURCE+'^{tree}')==SOURCE_TREE,'TARGET_TREE_MISMATCH')
        shutil.copyfile(self.staging/'.env',self.checkpoint/'staging.env');os.chmod(self.checkpoint/'staging.env',0o600)
        archive_tree(self.staging/'secrets',self.checkpoint/'secrets.tar.gz')
        shutil.copyfile(self.staging/'docker-compose.yml',self.checkpoint/'old-compose.yml');os.chmod(self.checkpoint/'old-compose.yml',0o600)
        self.git('checkout','--detach',SOURCE)
        require(not self.git('status','--porcelain'),'TARGET_CHECKOUT_DIRTY')
        for key,dockerfile in (('api','Dockerfile'),('worker','Dockerfile.media-worker')):
            output=self.checkpoint/(key+'.iid')
            self.docker.run(['build','--iidfile',str(output),'--label','org.opencontainers.image.revision='+SOURCE,
                '-f',str(self.repo/'backend'/dockerfile),str(self.repo/'backend')],timeout=900,label='BUILD_'+key.upper())
            image=output.read_text().strip();require(IMAGE.fullmatch(image),'INVALID_BUILT_IMAGE')
            self.journal[key+'_image_id']=image
            name=self.temporary(image,'runtime')
            try:
                probe=self.docker.run(['run','--rm','-i','--name',name,'--label','vecher.release14.checkpoint='+self.checkpoint.name,
                    '--network','none',image,'python','-'],data=RUNTIME_CODE.encode(),label='BUILT_RUNTIME')
            finally:self.remove_temporary(name)
            require(json.loads(probe)['runtime_sha256']==RUNTIME,'BUILT_RUNTIME_MISMATCH')
        image_ids=[self.journal[k] for k in ('old_api_image_id','db_image_id','api_image_id','worker_image_id')]
        image_bytes=sum(self.docker.inspect(i,'image')['Size'] for i in set(image_ids))
        file_bytes=sum(sum(v['bytes'] for v in tree_manifest(self.root/name).values()) for name in ('uploads','downloads','content') if (self.root/name).exists())
        db_bytes=int(self.docker.sql(self.journal['db_container'],"SELECT pg_database_size('vecher');",'vecher').strip())
        require(shutil.disk_usage(self.backups).free>=image_bytes+3*db_bytes+2*file_bytes+GIB,'INSUFFICIENT_CHECKPOINT_SPACE')
        self.docker.run(['image','save','-o',str(self.checkpoint/'images.tar'),*image_ids],timeout=900,label='SAVE_IMAGES')
        os.chmod(self.checkpoint/'images.tar',0o600)
        self.phase('images_pinned',images_archive_sha256=sha(self.checkpoint/'images.tar'))
        self.override(False)

    def freeze(self):
        require(sha(self.staging/'.env')==self.journal['old_env_sha256'],'ENV_CHANGED_BEFORE_FREEZE')
        self.apk();self.budget()
        self.phase('freezing')
        api=self.docker.service('api');require(api['Id']==self.journal['old_api_container'],'API_CHANGED_BEFORE_FREEZE')
        self.docker.run(['stop','--time','30',api['Id']],timeout=45,label='STOP_API')
        worker=self.docker.service('public-video-worker',optional=True)
        if worker and worker['State']['Running']:
            self.docker.run(['stop','--time','30',worker['Id']],timeout=45,label='STOP_WORKER')
        self.phase('frozen')
        # Required new mount is prepared only after the existing service stops.
        media=directory(self.root/'blog-media',create=True);os.chown(media,10001,10001);os.chmod(media,0o700)
        manifests={}
        for name in ('uploads','downloads','blog-media','content'):
            directory(self.root/name)
            manifests[name]=archive_tree(self.root/name,self.checkpoint/(name+'.tar.gz'))
        env=self.live_environment()
        before=self.one_live(['python','-'],code=FINGERPRINT_CODE,image=self.journal['old_api_image_id'],env=env)
        durable(self.checkpoint/'fingerprint.json',json.loads(before))
        with (self.checkpoint/'db.dump').open('xb') as output:
            self.docker.run(['exec',self.journal['db_container'],'pg_dump','-U','vecher','-d','vecher','--format=custom'],
                            output=output,timeout=900,label='COLD_DB_BACKUP')
        os.chmod(self.checkpoint/'db.dump',0o600)
        hashes={p.name:sha(p) for p in self.checkpoint.iterdir() if p.is_file() and p.name not in {'journal.json','runtime-compose.json'}}
        durable(self.checkpoint/'backup-manifest.json',{'schema':1,'files':hashes,'trees':manifests})
        self.phase('backup_verified',backup_manifest_sha256=sha(self.checkpoint/'backup-manifest.json'))

    def live_environment(self):
        env=load_json(self.checkpoint/'original-api-env.json')
        # For probes/migrations, never start delivery, release, or SMTP behavior.
        env.update({'VECHER_SCHEDULER_ENABLED':'false','VECHER_OUTBOX_ENABLED':'false','VECHER_REALTIME_ENABLED':'false',
                    'VECHER_SMTP_HOST':'','VECHER_FIREBASE_CREDENTIALS':'','VECHER_RELEASE_TOKEN':'','VECHER_BLOGS_ENABLED':'false'})
        return {k:v for k,v in env.items() if k.startswith('VECHER_')}

    def one_live(self,command,*,code=None,input_file=None,image=None,env=None,database=None,timeout=600):
        values=env or self.live_environment()
        if database:
            require(IDENT.fullmatch(database),'UNSAFE_DATABASE_NAME')
            values={**values,'VECHER_DATABASE_URL':values['VECHER_DATABASE_URL'].rsplit('/',1)[0]+'/'+database}
        path=self.checkpoint/('probe-'+uuid.uuid4().hex+'.env');write_env(path,values)
        image=image or self.journal['api_image_id']
        name=self.temporary(image,'once')
        args=['run','--rm','-i','--name',name,'--label','vecher.release14.checkpoint='+self.checkpoint.name,
              '--network',PROJECT+'_default','--env-file',str(path),'--memory','320m','--cpus','1']
        for host,target in (('content','/srv/public-content'),('downloads','/srv/downloads'),('uploads','/srv/uploads'),('blog-media','/srv/blog-media')):
            if (self.root/host).exists():args+=['--mount','type=bind,source='+str(self.root/host)+',target='+target+',readonly']
        try:
            return self.docker.run([*args,image,*command],data=code.encode() if code else None,input_file=input_file,
                                   timeout=timeout,label='LIVE_ISOLATED_COMMAND')
        finally:self.remove_temporary(name)

    def verify_backup(self):
        require(sha(self.checkpoint/'backup-manifest.json')==self.journal['backup_manifest_sha256'],'BACKUP_MANIFEST_CHANGED')
        manifest=load_json(self.checkpoint/'backup-manifest.json')
        for name,digest in manifest['files'].items():
            require(Path(name).name==name and sha(self.checkpoint/name)==digest,'BACKUP_FILE_CHANGED')
        return manifest

    def rehearsal(self):
        require(sha(self.helper)==self.journal['media_helper_sha256'],'MEDIA_HELPER_CHANGED')
        self.verify_backup()
        restore=directory(self.checkpoint/'restored-files',create=True,private=True)
        manifest=load_json(self.checkpoint/'backup-manifest.json')
        for name,expected in manifest['trees'].items():
            extract_tree(self.checkpoint/(name+'.tar.gz'),restore/name)
            require(tree_manifest(restore/name)==expected,'RESTORED_FILES_MISMATCH')
        images={key:self.journal[field] for key,field in [('old_api','old_api_image_id'),('api','api_image_id'),('worker','worker_image_id'),('db','db_image_id')]}
        result=rehearse(self.docker,self.checkpoint/'lab',images,self.checkpoint,self.live_environment()['VECHER_DATA_KEYS'],self.helper)
        self.phase('rehearsed',**result)

    def media_network(self):
        name=PROJECT+'_media-processing'
        existing=self.docker.run(['network','ls','--filter','name=^'+name+'$','-q'],label='MEDIA_NETWORK_LOOKUP').decode().split()
        if not existing:
            self.docker.run(['network','create','--internal','--label','com.docker.compose.project='+PROJECT,
                '--label','com.docker.compose.network=media-processing',name],label='MEDIA_NETWORK_CREATE')
        info=self.docker.inspect(name,'network');labels=info.get('Labels') or {}
        require(info.get('Internal') is True and labels.get('com.docker.compose.project')==PROJECT
                and labels.get('com.docker.compose.network')=='media-processing','INVALID_MEDIA_NETWORK')
        db=self.docker.service('db');require(db['Id']==self.journal['db_container'] and db['Image']==self.journal['db_image_id'],'DATABASE_CONTAINER_CHANGED')
        if name not in db['NetworkSettings']['Networks']:
            self.docker.run(['network','connect','--alias','db',name,db['Id']],label='MEDIA_DATABASE_CONNECT')
            db=self.docker.service('db')
        require('db' in (db['NetworkSettings']['Networks'][name].get('Aliases') or []),'MEDIA_DATABASE_ALIAS_MISSING')

    def shadow(self,blogs):
        env={**self.live_environment(),'VECHER_BLOGS_ENABLED':'true' if blogs else 'false'}
        path=self.checkpoint/('shadow-'+uuid.uuid4().hex+'.env');write_env(path,env)
        name=self.temporary(self.journal['api_image_id'],'shadow')
        args=['run','-d','--name',name,'--label','vecher.release14.checkpoint='+self.checkpoint.name,'--network',PROJECT+'_default',
              '--env-file',str(path),'--memory','320m','--cpus','1']
        for part,target in (('content','/srv/public-content'),('downloads','/srv/downloads'),('uploads','/srv/uploads'),('blog-media','/srv/blog-media')):
            args+=['--mount','type=bind,source='+str(self.root/part)+',target='+target+',readonly']
        try:
            self.docker.run([*args,self.journal['api_image_id']],label='SHADOW_API')
            return wait_api(self.docker,name,blogs=blogs)
        finally:
            self.remove_temporary(name)

    def migrate_live(self):
        self.phase('live_migration_started')
        result=self.one_live(['python','-m','app.migrate','upgrade'])
        require(result.decode().strip()=='MIGRATION_OK='+SCHEMA,'LIVE_MIGRATION_MARKER_MISMATCH')
        verify_legacy(load_json(self.checkpoint/'fingerprint.json'),json.loads(self.one_live(['python','-'],code=FINGERPRINT_CODE)))
        self.phase('live_migrated')
        self.shadow(False);self.media_network();self.apk()
        self.phase('core_shadow_ok')

    def activate_core(self):
        # Durable BEFORE compose can expose port8092: even a lost acknowledgement
        # must prohibit restoring a stale backup over new user actions.
        self.phase('traffic_may_have_opened',traffic_may_have_opened=True)
        self.override(False)
        self.compose('up','-d','--no-deps','--no-build','--pull','never','api')
        api=self.docker.service('api');require(api['Image']==self.journal['api_image_id'],'ACTIVATED_IMAGE_MISMATCH')
        wait_api(self.docker,api['Id'],blogs=False);self.runtime(api['Id'],RUNTIME);self.apk()
        self.phase('core_ready')

    def rollback(self):
        require(not self.journal.get('traffic_may_have_opened'),'FORWARD_RECOVERY_ONLY_AFTER_TRAFFIC')
        self.phase('rolling_back')
        self.cleanup_temporary()
        old=self.docker.inspect(self.journal['old_api_container'])
        require(old['Image']==self.journal['old_api_image_id'] and not old['State']['Running'],'OLD_CONTAINER_NOT_FROZEN')
        manifest=self.verify_backup()
        for name,expected in manifest['trees'].items():
            require(tree_manifest(self.root/name)==expected,'LIVE_FILES_CHANGED_RECOVERY_REQUIRES_REVIEW')
        # Wait for any killed migrator's transaction to finish before inspecting
        # its outcome. This is the same database-scoped lock as app.migrate.
        self.docker.sql(self.journal['db_container'],"SET lock_timeout='30s'; BEGIN; SELECT pg_advisory_xact_lock(726834902135); COMMIT;",'vecher')
        head=self.docker.sql(self.journal['db_container'],'SELECT version_num FROM alembic_version;','vecher').decode().strip()
        if head!=OLD_SCHEMA:
            require(head==SCHEMA,'UNEXPECTED_SCHEMA_DURING_RECOVERY')
            recovered='vecher_r14_restore_'+uuid.uuid4().hex[:12]
            failed='vecher_r14_retained_'+uuid.uuid4().hex[:12]
            self.docker.sql(self.journal['db_container'],'CREATE DATABASE "'+recovered+'" OWNER vecher;')
            database_url=urlsplit(self.live_environment()['VECHER_DATABASE_URL'].replace('postgresql+asyncpg:','postgresql:',1))
            require(database_url.scheme=='postgresql' and database_url.hostname=='db' and database_url.username=='vecher'
                    and database_url.password is not None,'RECOVERY_DATABASE_URL_UNSUPPORTED')
            client_env={'PGHOST':'db','PGPORT':str(database_url.port or 5432),'PGUSER':'vecher',
                        'PGPASSWORD':unquote(database_url.password),'PGDATABASE':recovered,'PGCONNECT_TIMEOUT':'15'}
            with (self.checkpoint/'db.dump').open('rb') as incoming:
                self.one_live(['pg_restore','--exit-on-error','--single-transaction','--dbname',recovered],input_file=incoming,
                              image=self.journal['db_image_id'],env=client_env,timeout=900)
            require(json.loads(self.one_live(['python','-'],code=FINGERPRINT_CODE,image=self.journal['old_api_image_id'],database=recovered))
                    ==load_json(self.checkpoint/'fingerprint.json'),'RECOVERY_DATA_MISMATCH')
            self.phase('recovery_database_swap',recovery_database=recovered,retained_database=failed)
            swap_databases(self.docker,self.journal['db_container'],'vecher',recovered,failed)
        require(self.one_live(['python','-m','app.migrate','check'],image=self.journal['old_api_image_id']).decode().strip()
                =='SCHEMA_HEAD_OK='+OLD_SCHEMA,'OLD_IMAGE_RESTORE_GATE_FAILED')
        require(json.loads(self.one_live(['python','-'],code=FINGERPRINT_CODE,image=self.journal['old_api_image_id']))
                ==load_json(self.checkpoint/'fingerprint.json'),'OLD_IMAGE_RESTORE_DATA_MISMATCH')
        self.git('checkout','--detach',OLD_SOURCE)
        require(sha(self.staging/'.env')==self.journal['old_env_sha256'],'ORIGINAL_ENV_CHANGED')
        self.docker.run(['start',self.journal['old_api_container']],label='RESTART_OLD_API')
        wait_api(self.docker,self.journal['old_api_container'],blogs=False,version='0.8.0',schema=OLD_SCHEMA)
        self.apk();self.phase('rolled_back')

    def deploy(self):
        prior=self.read()
        if prior:
            if prior['phase'] in {'core_ready','blogs_ready','blogs_disabled'}:
                return self.status()
            require(prior['phase'] in {'rolled_back','failed_prelive'},'UNFINISHED_RELEASE_REFUSES_AUTOMATIC_RETRY')
        self.begin(self.preflight())
        try:
            self.prepare();self.freeze();self.rehearsal();self.migrate_live();self.activate_core()
        except BaseException as error:
            code=str(error) if isinstance(error,ReleaseError) else type(error).__name__
            if self.journal.get('traffic_may_have_opened'):
                self.phase('needs_forward_recovery',error_code=code)
            elif self.journal['phase'] in {'created','preparing_images','images_pinned'}:
                try:self.git('checkout','--detach',OLD_SOURCE)
                finally:self.phase('failed_prelive',error_code=code)
            elif self.journal.get('backup_manifest_sha256'):
                try:self.rollback()
                except BaseException:self.phase('needs_manual_recovery',error_code=code)
            else:
                # No migration was attempted before a complete checkpoint. The
                # exact existing old container can safely resume without restore.
                try:
                    self.git('checkout','--detach',OLD_SOURCE)
                    self.docker.run(['start',self.journal['old_api_container']],label='RESUME_OLD_API')
                    self.phase('rolled_back',error_code=code)
                except BaseException:self.phase('needs_manual_recovery',error_code=code)
            raise ReleaseError('RELEASE14_FAILED_'+self.journal['phase'].upper()) from None
        return self.status()

    def enable(self):
        require(self.read(),'NO_RELEASE_CHECKPOINT')
        if self.journal['phase']=='blogs_ready':return self.status()
        require(self.journal['phase'] in {'core_ready','blogs_disabled'},'CORE_RELEASE_NOT_READY')
        require(all(self.journal.get(k) is True for k in ('rehearsal_ok','rollback_rehearsal_ok','media_rehearsal_ok')),'REHEARSAL_REQUIRED')
        self.status();self.budget(worker=True);self.shadow(True);self.media_network()
        self.phase('enabling_blogs');self.override(True)
        try:
            self.compose('up','-d','--no-deps','--no-build','--pull','never','public-video-worker')
            worker=self.docker.service('public-video-worker');time.sleep(4)
            worker=self.docker.service('public-video-worker')
            require(worker['Image']==self.journal['worker_image_id'] and worker['State']['Running']
                    and not worker['State'].get('OOMKilled') and worker.get('RestartCount',0)==0,'MEDIA_WORKER_NOT_READY')
            require(set(worker['NetworkSettings']['Networks'])=={PROJECT+'_media-processing'} and not worker['HostConfig'].get('PortBindings'),'MEDIA_WORKER_EXPOSURE')
            values=dict(x.split('=',1) for x in worker['Config'].get('Env',[]) if '=' in x)
            require(not values.get('VECHER_DATA_KEYS') and not values.get('VECHER_JWT_SECRET'),'MEDIA_WORKER_SECRET_BOUNDARY')
            self.runtime(worker['Id'],RUNTIME)
            proof=json.loads(self.docker.python(worker['Id'],WORKER_DB_CODE,timeout=20))
            require(proof.get('worker_database_ok') is True and proof.get('schema_head')==SCHEMA,'MEDIA_WORKER_DATABASE_UNREACHABLE')
            self.compose('up','-d','--no-deps','--no-build','--pull','never','api')
            api=self.docker.service('api');wait_api(self.docker,api['Id'],blogs=True)
            self.phase('blogs_ready');return self.status()
        except BaseException:
            self.phase('needs_forward_recovery',error_code='ENABLE_BLOGS_FAILED')
            raise ReleaseError('ENABLE_BLOGS_FAILED_USE_DISABLE_BLOGS') from None

    def disable(self):
        require(self.read() and self.journal.get('traffic_may_have_opened'),'NO_ACTIVE_RELEASE14')
        self.phase('disabling_blogs');self.override(False)
        try:
            self.compose('up','-d','--no-deps','--no-build','--pull','never','api')
            api=self.docker.service('api');wait_api(self.docker,api['Id'],blogs=False)
            worker=self.docker.service('public-video-worker',optional=True)
            if worker and worker['State']['Running']:
                self.docker.run(['stop','--time','30',worker['Id']],timeout=45,label='DISABLE_MEDIA_WORKER')
            self.phase('blogs_disabled');return self.status()
        except BaseException:
            self.phase('needs_forward_recovery',error_code='DISABLE_BLOGS_FAILED')
            raise ReleaseError('DISABLE_BLOGS_FAILED') from None

    def status(self):
        if not self.read():
            result={'schema':1,'release':14,'phase':'no_checkpoint','release_checkpoint_present':False,
                    'ready_for_apk14':False,'checkout_readable':False,'checkout_head':None,'checkout_clean':None,
                    **origin_diagnostics([]),'origin_verified':False,'origin_verification_reason':'origin_unavailable'}
            try:
                head=self.git('rev-parse','HEAD')
                require(re.fullmatch(r'[0-9a-f]{40}',head),'INVALID_CHECKOUT_HEAD')
                result['checkout_head']=head
                result['checkout_clean']=not self.git('--no-optional-locks','status','--porcelain')
                result['checkout_readable']=True
            except ReleaseError:
                pass
            result.update(self.origin_report())
            return result
        result={k:self.journal.get(k) for k in ('schema','release','phase','source_sha','source_tree','previous_source_sha',
            'runtime_sha256','api_image_id','worker_image_id','rehearsal_ok','rollback_rehearsal_ok','media_rehearsal_ok','traffic_may_have_opened',
            'error_code','recovery_database','retained_database','temporary_containers')}
        result['checkpoint']=str(self.checkpoint);result['release_checkpoint_present']=True;result['ready_for_apk14']=False
        result.update(self.origin_report())
        if self.journal['phase'] not in {'core_ready','blogs_ready','blogs_disabled'}:return result
        require(self.git('rev-parse','HEAD')==SOURCE and not self.git('status','--porcelain'),'ACTIVE_SOURCE_CHANGED')
        api=self.docker.service('api');require(api['Image']==self.journal['api_image_id'] and api['State']['Running'],'ACTIVE_API_CHANGED')
        self.runtime(api['Id'],RUNTIME)
        probe=json.loads(self.docker.python(api['Id'],API_CODE));result.update({k:probe[k] for k in ('schema_head','api_version','blogs_enabled')})
        require(probe['ok'] and probe['schema_head']==SCHEMA and probe['api_version']=='0.9.0','ACTIVE_API_CONTRACT_CHANGED')
        worker=self.docker.service('public-video-worker',optional=True)
        result.update(api_running=True,worker_running=bool(worker and worker['State']['Running']),
            worker_restart_count=worker.get('RestartCount',0) if worker else 0,worker_oom_killed=bool(worker and worker['State'].get('OOMKilled')))
        expected=self.journal['phase']=='blogs_ready'
        require(probe['blogs_enabled'] is expected and result['worker_running'] is expected,'ACTIVE_FEATURE_STATE_DRIFT')
        if expected:
            require(worker['Image']==self.journal['worker_image_id'] and result['worker_restart_count']==0
                    and not result['worker_oom_killed'],'ACTIVE_WORKER_CHANGED')
            self.runtime(worker['Id'],RUNTIME)
            proof=json.loads(self.docker.python(worker['Id'],WORKER_DB_CODE,timeout=20))
            require(proof.get('worker_database_ok') is True and proof.get('schema_head')==SCHEMA,'MEDIA_WORKER_DATABASE_UNREACHABLE')
            result['worker_database_ok']=True
        try:self.apk();result['apk13_unchanged']=True
        except ReleaseError:result['apk13_unchanged']=False
        result['ready_for_apk14']=(result['phase']=='blogs_ready' and result['blogs_enabled'] and result['worker_running']
            and worker['Image']==self.journal['worker_image_id'] and result['worker_restart_count']==0 and not result['worker_oom_killed']
            and all(result.get(k) is True for k in ('rehearsal_ok','rollback_rehearsal_ok','media_rehearsal_ok')))
        return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['preflight','deploy','status','public-status','enable-blogs','disable-blogs','recover','rehearse'])
    parser.add_argument('--json',action='store_true')
    parser.add_argument('--workspace',type=Path);parser.add_argument('--backup-dir',type=Path)
    for name in ('old-api-image','api-image','worker-image','postgres-image'):parser.add_argument('--'+name)
    parser.add_argument('--fixture',action='store_true')
    args=parser.parse_args(argv)
    os.umask(0o077)
    try:
        if args.action=='rehearse':
            require(args.workspace,'CI_REHEARSE_REQUIRES_PRIVATE_WORKSPACE')
            import base64
            images={'old_api':args.old_api_image,'api':args.api_image,'worker':args.worker_image,'db':args.postgres_image}
            require(all(images.values()),'REHEARSAL_IMAGE_IDS_REQUIRED')
            result=rehearse(Docker(Commands(diagnostic_dir=args.workspace/'command-logs',synthetic=True)),args.workspace,images,args.backup_dir or args.workspace/'fixture-backup',
                'fixture:'+base64.b64encode(secrets.token_bytes(32)).decode(),Path(__file__).with_name('media_rehearsal.py'),fixture=True)
        else:
            require(not any((args.workspace,args.backup_dir,args.old_api_image,args.api_image,args.worker_image,args.postgres_image,args.fixture)),
                    'PRODUCTION_PATH_OR_IMAGE_OVERRIDES_FORBIDDEN')
            release=Release()
            require(os.geteuid()==0,'ROOT_REQUIRED')
            if args.action=='public-status':result=public_status()
            elif args.action=='status':result=release.status()
            elif args.action=='preflight':result=release.preflight()
            else:
                with locks(release.backups):
                    if args.action=='deploy':result=release.deploy()
                    elif args.action=='enable-blogs':result=release.enable()
                    elif args.action=='disable-blogs':result=release.disable()
                    else:
                        require(release.read(),'NO_RELEASE_CHECKPOINT');release.rollback();result=release.status()
        print(json.dumps(result,sort_keys=True))
        if not args.json or args.action=='public-status':
            marker={'deploy':'RELEASE14_CORE_READY','enable-blogs':'RELEASE14_BLOGS_READY','disable-blogs':'RELEASE14_BLOGS_DISABLED',
                    'rehearse':'RELEASE14_RESTORE_REHEARSAL_OK','preflight':'RELEASE14_PREFLIGHT_OK','recover':'RELEASE14_OLD_API_RESTORED',
                    'status':'RELEASE14_STATUS_OK','public-status':'RELEASE14_PUBLIC_HTTPS_OK'}[args.action]
            print(marker)
        return 0
    except (ReleaseError,OSError,ValueError,KeyError,TypeError) as error:
        code=str(error) if isinstance(error,ReleaseError) else type(error).__name__
        print('RELEASE14_STOPPED: '+code,file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
