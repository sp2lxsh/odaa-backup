import os
from types import SimpleNamespace

import pytest

from odaa_backup import discover
from odaa_backup.config import parse_config
from odaa_backup.discover import FoundItem, ScanResult, build_sources, find_items, parse_db_config

WP = r"""<?php
// ** MySQL settings ** //
/* define('DB_NAME', 'old_commented'); */
define( 'DB_NAME', 'u123_wp' );
define( 'DB_USER', "u123_wp" );
define( 'DB_PASSWORD', 'p@ss\'word"1' );
define( 'DB_HOST', 'localhost' );
$table_prefix = 'wp_';
"""

JOOMLA = """<?php
class JConfig {
	public $dbtype = 'mysqli';
	public $host = 'localhost:3307';
	public $user = 'jo_user';
	public $password = 'jo-pass';
	public $db = 'jo_db';
}"""

BITRIX = """<?php return array (
  'connections' => array ('value' => array ('default' => array (
        'className' => '\\\\Bitrix\\\\Main\\\\DB\\\\MysqliConnection',
        'host' => 'localhost',
        'database' => 'bx_db',
        'login' => 'bx_user',
        'password' => 'bx-pass',
  ))));"""

OPENCART = """<?php
define('DB_DRIVER', 'mysqli');
define('DB_HOSTNAME', 'localhost');
define('DB_USERNAME', 'oc_user');
define('DB_PASSWORD', 'oc-pass');
define('DB_DATABASE', 'oc_db');
define('DB_PORT', '3306');
"""


def test_parse_wordpress_with_quotes_and_comments():
    d = parse_db_config("wordpress", WP)
    assert d == {"name": "u123_wp", "user": "u123_wp", "password": "p@ss'word\"1", "host": "localhost", "port": None}


def test_parse_other_cms():
    assert parse_db_config("joomla", JOOMLA) == {"name": "jo_db", "user": "jo_user", "password": "jo-pass",
                                                 "host": "localhost", "port": 3307}
    b = parse_db_config("bitrix", BITRIX)
    assert (b["name"], b["user"], b["password"]) == ("bx_db", "bx_user", "bx-pass")
    o = parse_db_config("opencart", OPENCART)
    assert (o["name"], o["user"], o["port"]) == ("oc_db", "oc_user", 3306)


def test_parse_env():
    env = 'APP=x\nDB_CONNECTION=mysql\nDB_HOST=127.0.0.1\nDB_DATABASE="shop"\nDB_USERNAME=shop\nDB_PASSWORD="a b#c"\n'
    d = parse_db_config("env", env)
    assert (d["name"], d["user"], d["password"], d["host"]) == ("shop", "shop", "a b#c", "127.0.0.1")
    url = "DATABASE_URL=mysql://app:s%40cret@db.local:3307/appdb?charset=utf8\n"
    d = parse_db_config("env", url)
    assert (d["name"], d["user"], d["password"], d["host"], d["port"]) == ("appdb", "app", "s@cret", "db.local", 3307)
    # .env Node-приложения без базы MySQL — не база
    assert parse_db_config("env", "PORT=3000\nTOKEN=abc\n") is None
    assert parse_db_config("env", "DB_CONNECTION=pgsql\nDB_DATABASE=x\nDB_USERNAME=y\nDB_PASSWORD=z") is None


# --------------------------------------------------------------- поиск на «сервере»


class FakeSFTP:
    """SFTP поверх локальной папки: пути с '/' — от корня «сервера», без — от домашней папки."""

    def __init__(self, root, home="home/u"):
        self.root, self.home = root, home

    def _abs(self, p):
        return os.path.join(self.root, p.lstrip("/")) if p.startswith("/") else os.path.join(self.root, self.home, p)

    def stat(self, p):
        s = os.stat(self._abs(p))
        return SimpleNamespace(st_mode=s.st_mode, st_size=s.st_size, st_mtime=s.st_mtime)

    def listdir_attr(self, p):
        full = self._abs(p)
        return [SimpleNamespace(filename=n, st_mode=os.lstat(os.path.join(full, n)).st_mode) for n in os.listdir(full)]

    def open(self, p, mode="rb"):
        return open(self._abs(p), mode)


def put(root, rel, text=""):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


@pytest.fixture
def shared(tmp_path):
    r = str(tmp_path)
    put(r, "home/u/mysite.ru/public_html/wp-config.php", WP)
    put(r, "home/u/mysite.ru/public_html/index.php", "<?php")
    put(r, "home/u/shop.ru/public_html/configuration.php", JOOMLA)
    put(r, "home/u/landing.ru/public_html/index.html", "<html>")
    put(r, "home/u/www/second.ru/bitrix/.settings.php", BITRIX)
    put(r, "home/u/tmp/x", "")
    return r


def test_find_sites_on_shared_hosting(shared):
    sites, extras = find_items(FakeSFTP(shared), "beget")
    by = {s.title: s for s in sites}
    assert set(by) == {"mysite.ru", "shop.ru", "landing.ru", "second.ru"}
    assert by["mysite.ru"].kind == "wordpress" and by["mysite.ru"].remote == "mysite.ru/public_html"
    assert by["mysite.ru"].dbs[0].name == "u123_wp"
    assert by["mysite.ru"].dbs[0].from_file == "mysite.ru/public_html/wp-config.php"
    assert "wp-content/cache" in by["mysite.ru"].exclude
    assert by["shop.ru"].kind == "joomla" and by["shop.ru"].dbs[0].port == 3307
    assert by["second.ru"].remote == "www/second.ru" and by["second.ru"].kind == "bitrix"
    assert by["landing.ru"].kind == "site" and not by["landing.ru"].dbs
    assert extras == []


def test_find_on_vps(tmp_path):
    r = str(tmp_path)
    put(r, "opt/vybor/package.json", "{}")
    put(r, "opt/vybor/.env", "PORT=3000\nSECRET=1")
    put(r, "opt/vybor/storage/data.json", "{}")
    put(r, "opt/containerd/x", "")
    put(r, "var/www/html/index.nginx-debian.html", "")
    put(r, "etc/nginx/nginx.conf", "")
    put(r, "etc/systemd/system/vybor.service", "[Unit]")
    put(r, "etc/letsencrypt/live/x", "")
    os.makedirs(os.path.join(r, "home/u"), exist_ok=True)
    sites, extras = find_items(FakeSFTP(r), "timeweb")
    assert [(s.title, s.remote, s.kind) for s in sites] == [("vybor", "/opt/vybor", "app")]
    assert "node_modules" in sites[0].exclude and not sites[0].dbs
    keys = {x.title: x for x in extras}
    assert "Настройки nginx" in keys and "Служба vybor.service" in keys
    assert keys["SSL-сертификаты (Let's Encrypt)"].checked is False


# --------------------------------------------------------------- сборка источников


def _res(provider, sites, extras=(), key_file=None):
    return ScanResult(provider=provider, host="h.example", port=22, user="u123", fingerprint="SHA256:x",
                      password=None if key_file else "pw", key_file=key_file, sites=list(sites), extras=list(extras))


def test_build_sources_one_per_site_and_valid_config(shared):
    sites, _ = find_items(FakeSFTP(shared), "beget")
    res = _res("beget", sites)
    srcs, secrets = build_sources(res, [s.key for s in sites], "daily 03:00", set())
    assert len(srcs) == 4
    assert secrets == {"beget-u123-ssh": "pw"}
    wp = next(s for s in srcs if s["title"] == "mysite.ru")
    assert wp["mysql"]["databases"][0]["from_file"].endswith("wp-config.php")
    assert "password_secret" not in wp["mysql"]["databases"][0]
    # итог проходит проверку как обычный config.yaml
    cfg = parse_config({"backup_root": "/tmp/b", "sources": srcs, "_managed_ids": [s["id"] for s in srcs]})
    assert all(s.managed for s in cfg.sources)
    assert cfg.all_secret_names() == ["beget-u123-ssh"]


def test_build_sources_vps_single_source_with_key(tmp_path):
    items = [FoundItem("s0", "vybor", "/opt/vybor", "app", ["node_modules"])]
    extras = [FoundItem("x-nginx", "Настройки nginx", "/etc/nginx", "config"),
              FoundItem("x-unit", "Служба vybor.service", "/etc/systemd/system/vybor.service", "config")]
    res = _res("timeweb", items, extras, key_file="C:/Users/sp/.ssh/id_ed25519")
    srcs, secrets = build_sources(res, ["s0", "x-nginx", "x-unit"], "every 6h", {"timeweb-h.example"})
    assert len(srcs) == 1 and secrets == {}
    s = srcs[0]
    assert s["id"] == "timeweb-h.example-2"  # id не совпадает с уже существующим
    assert s["ssh"]["key_file"].endswith("id_ed25519")
    assert [f["name"] for f in s["files"]] == ["vybor", "nginx", "vybor.service"]
    parse_config({"backup_root": "/tmp/b", "sources": srcs})


def test_manual_path_and_cyrillic_domain():
    res = _res("regru", [FoundItem("s0", "сайт.рф", "www/сайт.рф", "site")])
    srcs, _ = build_sources(res, ["s0"], "daily 03:00", set(), manual_path="www/extra.ru")
    assert srcs[0]["id"] == "regru-xn--80aswg.xn--p1ai"
    assert srcs[1]["files"][0]["remote"] == "www/extra.ru"
    parse_config({"backup_root": "/tmp/b", "sources": srcs})


def test_beget_host_derived(monkeypatch, tmp_path):
    seen = {}

    def fake_connect(host, port, user, *a, **k):
        seen["host"] = host
        raise discover.DiscoverError("stop")

    monkeypatch.setattr(discover, "wizard_connect", fake_connect)
    with pytest.raises(discover.DiscoverError):
        discover.scan("beget", "", 22, "mylogin", "pw", tmp_path)
    assert seen["host"] == "mylogin.beget.tech"


def test_user_known_hosts_mismatch_blocks_trust(tmp_path):
    """Если компьютер помнит другой ключ сервера (~/.ssh/known_hosts) — не доверять молча."""
    import paramiko

    from odaa_backup.ssh import HostKeyError, KnownHosts

    old = paramiko.RSAKey.generate(1024)
    new = paramiko.RSAKey.generate(1024)
    kh = tmp_path / "known_hosts"
    kh.write_text(f"other.host ssh-ed25519 AAAA\n@revoked x y z\nsrv.example {old.get_name()} {old.get_base64()}\n")
    assert discover.user_known_key("srv.example", 22, "ssh-rsa", kh).asbytes() == old.asbytes()
    ours = KnownHosts(tmp_path / "ours")
    pol = discover._TrustOnFirstUse(ours, "srv.example", 22, kh)
    with pytest.raises(HostKeyError) as e:
        pol.missing_host_key(None, "srv.example", new)
    assert e.value.mismatch and "ИЗМЕНИЛСЯ" in str(e.value)
    assert not (tmp_path / "ours").exists()  # новый ключ не сохранён
    pol.missing_host_key(None, "srv.example", old)  # тот же ключ — принимается
    assert ours.check("srv.example", 22, old) == "ok"
    # хешированные записи (HashKnownHosts) тоже понимаются
    hashed = paramiko.HostKeys()
    hashed.add(paramiko.HostKeys.hash_host("srv2.example"), old.get_name(), old)
    hashed.save(str(tmp_path / "kh2"))
    assert discover.user_known_key("srv2.example", 22, "ssh-rsa", tmp_path / "kh2") is not None
