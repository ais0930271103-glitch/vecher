import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    'turn_merge', Path(__file__).resolve().parents[1] / 'enable-backend.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
VALUES = {
    m.KEYS[0]: ("'" + json.dumps([f'stun:{m.DOMAIN}:3478']) + "'").encode(),
    m.KEYS[1]: ("'" + json.dumps([
        f'turn:{m.DOMAIN}:3478?transport=udp',
        f'turn:{m.DOMAIN}:3478?transport=tcp',
        f'turns:{m.DOMAIN}:5349?transport=tcp']) + "'").encode(),
    m.KEYS[2]: b'a' * 64,
    m.KEYS[3]: b'15000',
}
GENERATED = b''.join(key + b'=' + value + b'\n' for key, value in VALUES.items())

class MergeTests(unittest.TestCase):
    def test_preserves_unrelated_bytes_comments_prefixes_and_crlf(self):
        unaffected = (b'# \xff opaque comment\r\nSECRET="x#y \\" z" # retained\r\n'
                      b'OTHER=value#literal   # another comment\r\n\r\n')
        old = unaffected + (b'  export VECHER_TURN_SECRET = "old"   # keep this\r\n'
                            b'VECHER_STUN_URLS=  # empty\r\n'
                            b'VECHER_TURN_URLS=\'[]\'\t# urls\r\n'
                            b'VECHER_TURN_CREDENTIAL_TTL_SECONDS=3600\t \r\n')
        expected = unaffected + (b'  export VECHER_TURN_SECRET = ' + VALUES[m.KEYS[2]] + b'   # keep this\r\n'
                               + b'VECHER_STUN_URLS=' + VALUES[m.KEYS[0]] + b'  # empty\r\n'
                               + b'VECHER_TURN_URLS=' + VALUES[m.KEYS[1]] + b'\t# urls\r\n'
                               + b'VECHER_TURN_CREDENTIAL_TTL_SECONDS=15000\t \r\n')
        self.assertEqual(m.merge_bytes(old, GENERATED), expected)

    def test_append_to_no_final_newline_and_idempotence(self):
        old = b'UNRELATED=abc#value\n# final comment'
        result = m.merge_bytes(old, GENERATED)
        self.assertEqual(result, old + b'\n' + GENERATED)
        self.assertEqual(m.merge_bytes(result, GENERATED), result)

    def test_comment_immediately_after_quote_remains_comment(self):
        old = b'VECHER_TURN_SECRET="old"#keep\n'
        result = m.merge_bytes(old, GENERATED)
        self.assertTrue(result.startswith(b'VECHER_TURN_SECRET=' + b'a' * 64 + b' #keep\n'))

    def test_empty_value_comment_after_equals_cannot_join_secret(self):
        for old_suffix in (b'#keep', b' #keep', b'\t#keep'):
            result = m.merge_bytes(b'VECHER_TURN_SECRET=' + old_suffix + b'\n', GENERATED)
            new_suffix = b' #keep' if old_suffix == b'#keep' else old_suffix
            self.assertTrue(result.startswith(b'VECHER_TURN_SECRET=' + b'a' * 64 + new_suffix + b'\n'))

    def test_stat_guard_ignores_only_access_time(self):
        with tempfile.NamedTemporaryFile() as f:
            before = os.stat(f.name)
            os.utime(f.name, ns=(before.st_atime_ns + 1_000_000_000, before.st_mtime_ns))
            after = os.stat(f.name)
            # utime also changes ctime: keep that real protection separately.
            from types import SimpleNamespace
            values = {name: getattr(before, name) for name in (
                'st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')}
            self.assertEqual(m.stamp(before), m.stamp(SimpleNamespace(**values, st_atime_ns=after.st_atime_ns)))
            for field in values:
                changed = dict(values)
                changed[field] += 1
                self.assertNotEqual(m.stamp(before), m.stamp(SimpleNamespace(**changed)))

    def test_rejects_duplicate_targets(self):
        with self.assertRaises(ValueError):
            m.merge_bytes(b'VECHER_TURN_SECRET=x\n export VECHER_TURN_SECRET=y\n', GENERATED)

    def test_rejects_quoted_multiline_and_ambiguous_values(self):
        for old in (b'VECHER_TURN_SECRET="one\ntwo"\n',
                    b'OTHER="one\nVECHER_TURN_SECRET=inside\n"\n',
                    b'VECHER_STUN_URLS="[]"garbage\n'):
            with self.subTest(old=old), self.assertRaises(ValueError):
                m.merge_bytes(old, GENERATED)

    def test_rejects_all_invalid_generated_shapes(self):
        cases = [GENERATED + b'OTHER=value\n', GENERATED + GENERATED.splitlines(True)[0],
                 b''.join(GENERATED.splitlines(True)[:-1]),
                 GENERATED.replace(b'a' * 64, b'bad'),
                 GENERATED.replace(b'15000', b'3600'),
                 GENERATED.replace(m.DOMAIN.encode(), b'wrong.example'),
                 GENERATED.replace(b"'", b'')]
        for generated in cases:
            with self.subTest(), self.assertRaises(ValueError):
                m.merge_bytes(b'EXISTING=untouched\n', generated)

    @unittest.skipUnless(os.geteuid() == 0, 'Filesystem root-ownership contract needs root')
    def test_atomic_write_private_backup_idempotence_and_failure(self):
        with tempfile.TemporaryDirectory(prefix='vecher-turn-merge-fixture-') as directory:
            base = Path(directory)
            folder, backups = base / 'repo', base / 'backups'
            folder.mkdir()
            src, dst = base / 'generated.env', folder / '.env'
            src.write_bytes(GENERATED)
            src.chmod(0o600)
            old = b'EXISTING=untouched\r\n# unchanged\r\n'
            dst.write_bytes(old)
            dst.chmod(0o644)
            inode = dst.stat().st_ino
            backup = m.apply(src, dst, backups)
            self.assertEqual(backup.parent, backups)
            self.assertEqual(backup.suffix, '.env')
            self.assertEqual(stat.S_IMODE(backups.stat().st_mode), 0o700)
            self.assertEqual(backup.read_bytes(), old)
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(dst.stat().st_mode), 0o600)
            self.assertEqual(dst.stat().st_uid, 0)
            self.assertNotEqual(dst.stat().st_ino, inode)
            self.assertEqual(dst.read_bytes(), m.merge_bytes(old, GENERATED))
            self.assertFalse(list(folder.glob('.turn-new-*.env')))
            self.assertEqual(list(folder.iterdir()), [dst])
            self.assertIsNone(m.apply(src, dst, backups))
            self.assertEqual(len(list(backups.glob('before-turn-*.env'))), 1)
            dst.write_bytes(old)
            def fail_replace(source, destination):
                self.assertEqual(Path(source).parent, folder)
                self.assertTrue(Path(source).name.startswith('.turn-new-'))
                self.assertEqual(Path(source).suffix, '.env')
                self.assertEqual(stat.S_IMODE(Path(source).stat().st_mode), 0o600)
                raise OSError('synthetic failure')
            with patch.object(m.os, 'replace', side_effect=fail_replace):
                with self.assertRaises(OSError):
                    m.apply(src, dst, backups)
            self.assertEqual(dst.read_bytes(), old)
            self.assertFalse(list(folder.glob('.turn-new-*.env')))
            before_files = set(base.rglob('*'))
            src.write_bytes(GENERATED + GENERATED)
            with self.assertRaises(ValueError):
                m.apply(src, dst, backups)
            self.assertEqual(set(base.rglob('*')), before_files)
            self.assertEqual(dst.read_bytes(), old)
            link = folder / 'symlink.env'
            link.symlink_to(dst)
            with self.assertRaises(ValueError):
                m.apply(src, link, backups)
            self.assertEqual(dst.read_bytes(), old)

if __name__ == '__main__':
    unittest.main()
