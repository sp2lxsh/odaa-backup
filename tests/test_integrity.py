import gzip
import os
import stat as st
from types import SimpleNamespace

import pytest

from odaa_backup.config import FileSpec, Network
from odaa_backup.manifest import read_manifest, sha256_file, verify_snapshot, write_manifest
from odaa_backup.mysql import DumpError, validate_dump
from odaa_backup.paths import safe_component, safe_relpath
from odaa_backup.sync import Excluder, sync_tree

NET = Network(timeout=5, retries=1, retry_delay=0)


# --------------------------------------------------------------- «сервер» на локальной папке


class LocalRemote:
    """Имитация SFTP поверх локальной папки (тот же интерфейс, что у SFTPRemote)."""

    def __init__(self, root):
        self.root = root
        self.reconnects = 0
        self.fail_reads = 0

    def _p(self, path):
        return os.path.join(self.root, path.lstrip("/"))

    def _attr(self, full, name=""):
        s = os.lstat(full)
        return SimpleNamespace(filename=name, st_mode=s.st_mode, st_size=s.st_size, st_mtime=int(s.st_mtime))

    def stat(self, path):
        return self._attr(self._p(path))

    def listdir_attr(self, path):
        full = self._p(path)
        return [self._attr(os.path.join(full, n), n) for n in os.listdir(full)]

    def open(self, path):
        remote = self

        class F:
            def __init__(self):
                self.f = open(remote._p(path), "rb")
                self.reads = 0

            def seek(self, n):
                self.f.seek(n)

            def read(self, n):
                if remote.fail_reads:
                    self.reads += 1
                    if self.reads > 1:
                        remote.fail_reads -= 1
                        raise EOFError("связь оборвалась")
                return self.f.read(min(n, 4))  # маленькие куски, чтобы оборвать посередине

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.f.close()

        return F()

    def reconnect(self):
        self.reconnects += 1

    def sha256_many(self, paths):
        return {p: sha256_file(self._p(p)) for p in paths}


def write(path, data, mtime=1_700_000_000):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    os.utime(path, (mtime, mtime))


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "server"
    site = root / "site.ru" / "public_html"
    write(str(site / "index.php"), b"<?php echo 1;")
    write(str(site / "wp-content" / "uploads" / "фото кота.jpg"), b"\xff\xd8 jpeg data")
    write(str(site / "wp-content" / "cache" / "big.html"), b"cache")
    write(str(site / "debug.log"), b"log")
    write(str(site / "wp-content" / "plugins" / "a:b.txt"), b"colon")
    return root


def run_sync(remote, snap, prev=None, prev_manifest=None, **kw):
    manifest = {}
    spec = FileSpec(remote="site.ru/public_html", name="site", exclude=["wp-content/cache", "*.log"])
    stats = sync_tree(remote, spec, snap, prev, prev_manifest or {}, NET, manifest, **kw)
    return stats, manifest


def test_full_then_incremental_with_hardlinks(server, tmp_path):
    remote = LocalRemote(str(server))
    s1 = tmp_path / "b" / "2026-11-01_0300"
    stats, manifest = run_sync(remote, s1)
    assert stats.files == 3 and stats.downloaded == 3 and stats.excluded == 2
    assert not stats.errors
    assert (s1 / "files/site/wp-content/uploads/фото кота.jpg").read_bytes() == b"\xff\xd8 jpeg data"
    # двоеточие недопустимо в Windows — имя закодировано
    assert (s1 / "files/site/wp-content/plugins/a%3Ab.txt").exists()
    assert stats.renamed
    write_manifest(s1, manifest)
    assert verify_snapshot(s1).ok

    # меняем один файл на сервере
    write(str(server / "site.ru/public_html/index.php"), b"<?php echo 2;", mtime=1_700_000_100)
    s2 = tmp_path / "b" / "2026-11-02_0300"
    stats2, manifest2 = run_sync(remote, s2, prev=s1, prev_manifest=read_manifest(s1))
    assert stats2.downloaded == 1 and stats2.linked + stats2.copied_local == 2
    write_manifest(s2, manifest2)
    assert verify_snapshot(s2).ok
    a = s1 / "files/site/wp-content/uploads/фото кота.jpg"
    b = s2 / "files/site/wp-content/uploads/фото кота.jpg"
    if stats2.linked:
        assert os.stat(a).st_ino == os.stat(b).st_ino  # жёсткая ссылка, место не тратится
    assert (s2 / "files/site/index.php").read_bytes() == b"<?php echo 2;"
    assert (s1 / "files/site/index.php").read_bytes() == b"<?php echo 1;"


def test_dry_run_writes_nothing(server, tmp_path):
    remote = LocalRemote(str(server))
    snap = tmp_path / "b" / "2026-11-01_0300"
    stats, manifest = run_sync(remote, snap, dry_run=True)
    assert stats.plan_new == 3 and stats.plan_bytes > 0
    assert not snap.exists() and manifest == {}


def test_resume_after_connection_drop(server, tmp_path):
    big = server / "site.ru/public_html/big.bin"
    write(str(big), os.urandom(5000))
    remote = LocalRemote(str(server))
    remote.fail_reads = 1  # первая попытка оборвётся
    snap = tmp_path / "b" / "2026-11-01_0300"
    stats, manifest = run_sync(remote, snap)
    assert not stats.errors
    assert remote.reconnects >= 1
    assert sha256_file(snap / "files/site/big.bin") == sha256_file(big)
    assert not list(snap.rglob("*.part"))


def test_verify_detects_corruption_and_missing(tmp_path):
    snap = tmp_path / "s"
    write(str(snap / "files/a.txt"), b"hello")
    write(str(snap / "db/x.sql.gz"), b"dump")
    write_manifest(snap, {"files/a.txt": sha256_file(snap / "files/a.txt"),
                          "db/x.sql.gz": sha256_file(snap / "db/x.sql.gz")})
    assert verify_snapshot(snap).ok
    (snap / "files/a.txt").write_bytes(b"HELLO")
    os.remove(snap / "db/x.sql.gz")
    write(str(snap / "extra.txt"), b"?")
    r = verify_snapshot(snap)
    assert not r.ok
    assert r.mismatched == ["files/a.txt"] and r.missing == ["db/x.sql.gz"] and r.extra == ["extra.txt"]


def test_verify_without_manifest(tmp_path):
    assert verify_snapshot(tmp_path).no_manifest


def test_manifest_roundtrip_with_spaces_and_cyrillic(tmp_path):
    entries = {"files/Папка с пробелом/файл 1.txt": "a" * 64, "db/b.sql.gz": "b" * 64}
    write_manifest(tmp_path, entries)
    assert read_manifest(tmp_path) == entries


# --------------------------------------------------------------- исключения и имена


def test_excluder():
    ex = Excluder(["node_modules", "*.log", "wp-content/cache/**", "storage/tmp/"])
    assert ex.excluded("node_modules")
    assert ex.excluded("a/b/node_modules")
    assert ex.excluded("x/error.log")
    assert ex.excluded("wp-content/cache")
    assert ex.excluded("storage/tmp")
    assert not ex.excluded("wp-content/cache2")
    assert not ex.excluded("sub/wp-content/cache")
    assert not ex.excluded("storage/data.json")


def test_safe_names():
    assert safe_component("файл.txt") == "файл.txt"
    assert safe_component('a<b>:"c|?*') == "a%3Cb%3E%3A%22c%7C%3F%2A"
    assert safe_component("CON") == "CON_"
    assert safe_component("nul.txt") == "nul_.txt"
    assert safe_component("name. ") == "name%2E%20"
    assert safe_relpath("a/b:c/d") == "a/b%3Ac/d"


# --------------------------------------------------------------- дампы


GOOD_SQL = (
    b"-- MySQL dump 10.13  Distrib 8.0.36\n--\n-- Host: localhost    Database: wp\n"
    + b"CREATE TABLE `wp_posts` (id int);\n" + b"INSERT INTO `wp_posts` VALUES (1);\n" * 200
    + b"-- Dump completed on 2026-11-12  3:00:01\n"
)


def gz(path, data):
    with gzip.open(path, "wb") as f:
        f.write(data)
    return path


def test_dump_ok(tmp_path):
    assert validate_dump(gz(tmp_path / "ok.sql.gz", GOOD_SQL)) == len(GOOD_SQL)


@pytest.mark.parametrize("data,needle", [
    (GOOD_SQL.replace(b"-- MySQL dump", b"-- hello"), "заголовка"),
    (GOOD_SQL.replace(b"CREATE TABLE", b"CREATE VIEW"), "CREATE TABLE"),
    (GOOD_SQL.replace(b"-- Dump completed", b"-- oops"), "оборван"),
    (b"-- MySQL dump\n-- Dump completed", "маленький"),
])
def test_dump_bad(tmp_path, data, needle):
    with pytest.raises(DumpError) as e:
        validate_dump(gz(tmp_path / "bad.sql.gz", data))
    assert needle in str(e.value)


def test_dump_truncated_gzip(tmp_path):
    p = gz(tmp_path / "t.sql.gz", GOOD_SQL)
    raw = p.read_bytes()
    p.write_bytes(raw[: len(raw) // 2])
    with pytest.raises(DumpError) as e:
        validate_dump(p)
    assert "повреждён" in str(e.value)


def test_dump_mariadb_header_and_empty_allowed(tmp_path):
    data = b"-- MariaDB dump 10.19\n" + b"-- nothing\n" * 200 + b"-- Dump completed\n"
    validate_dump(gz(tmp_path / "e.sql.gz", data), allow_empty=True)


def test_symlinks_are_skipped(tmp_path):
    root = tmp_path / "srv"
    write(str(root / "site/a.txt"), b"a")
    os.symlink("/etc/passwd", root / "site/link")
    remote = LocalRemote(str(root))
    manifest = {}
    stats = sync_tree(remote, FileSpec(remote="site", name="s"), tmp_path / "snap", None, {}, NET, manifest)
    assert stats.files == 1 and stats.skipped_links == 1
    assert st.S_ISREG(os.stat(tmp_path / "snap/files/s/a.txt").st_mode)
