from pathlib import Path

import pytest
import yaml

from odaa_backup.config import ConfigError, apply_overrides, load_config, parse_config

ROOT = Path(__file__).resolve().parent.parent


def minimal(**src):
    s = {
        "id": "beget-site",
        "provider": "beget",
        "ssh": {"host": "h.example", "user": "u", "password_secret": "beget-ssh"},
        "files": [{"remote": "site.ru/public_html"}],
    }
    s.update(src)
    return {"backup_root": "/tmp/b", "sources": [s]}


def test_example_config_is_valid(tmp_path):
    cfg = load_config(ROOT / "config.example.yaml", apply_user_overrides=False)
    assert [s.id for s in cfg.sources] == ["beget-mysite", "regru-site", "timeweb-vybor"]
    beget = cfg.source("beget-mysite")
    assert beget.mysql.databases[0].name == "mylogin_wp"
    assert beget.api.type == "beget"
    tw = cfg.source("timeweb-vybor")
    assert tw.files[0].exclude == ["node_modules", ".git", "*.log"]
    assert tw.ssh.key_file.endswith("id_ed25519")
    assert cfg.retention.daily == 7 and cfg.retention.monthly == 6
    assert "beget-ssh" in cfg.all_secret_names()


def test_defaults_and_name_derivation():
    cfg = parse_config(minimal(), base_dir=Path("/tmp"))
    s = cfg.sources[0]
    assert s.files[0].name == "public_html"
    assert s.enabled and s.schedule == "daily 03:00"
    assert cfg.network.retries == 5
    assert cfg.data_dir == Path("/tmp/data")


def test_plain_password_is_rejected():
    data = minimal()
    data["sources"][0]["ssh"]["password"] = "secret123"
    with pytest.raises(ConfigError) as e:
        parse_config(data)
    assert any("password" in m and "запрещён" in m for m in e.value.errors)


def test_collects_all_errors():
    data = {
        "backup_root": "/tmp/b",
        "sources": [
            {"id": "bad id", "provider": "x"},
            {"id": "ok", "schedule": "sometimes", "files": ["a"]},
            {"id": "ok", "api": {"type": "nope"}},
        ],
    }
    with pytest.raises(ConfigError) as e:
        parse_config(data)
    text = "\n".join(e.value.errors)
    assert "id" in text
    assert "расписание" in text
    assert "ssh" in text
    assert "уже есть" in text
    assert "допустимо" in text


def test_duplicate_file_names_rejected():
    data = minimal(files=[{"remote": "a/www"}, {"remote": "b/www"}])
    with pytest.raises(ConfigError):
        parse_config(data)


def test_overrides_apply():
    cfg = parse_config(minimal())
    cfg2 = apply_overrides(cfg, {"beget-site": {"enabled": False, "schedule": "every 6h",
                                                "retention": {"daily": 3}}})
    s = cfg2.sources[0]
    assert s.enabled is False and s.schedule == "every 6h"
    assert cfg2.retention_for(s).daily == 3 and cfg2.retention_for(s).weekly == 4
    # мусор в overrides игнорируется
    cfg3 = apply_overrides(cfg, {"beget-site": {"schedule": "иногда", "enabled": "yes"}})
    assert cfg3.sources[0].schedule == "daily 03:00" and cfg3.sources[0].enabled is True


def test_cyrillic_and_bom(tmp_path):
    p = tmp_path / "config.yaml"
    data = minimal(title="Сайт «Ромашка»")
    data["backup_root"] = str(tmp_path / "Бэкапы")
    p.write_bytes(b"\xef\xbb\xbf" + yaml.safe_dump(data, allow_unicode=True).encode("utf-8"))
    cfg = load_config(p)
    assert cfg.sources[0].title == "Сайт «Ромашка»"
    assert cfg.backup_root.name == "Бэкапы"


def test_yaml_syntax_error(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("sources: [\n  - id: x", encoding="utf-8")
    with pytest.raises(ConfigError) as e:
        load_config(p)
    assert "YAML" in str(e.value)


def test_private_key_loading(tmp_path):
    """Ключи ed25519 и RSA (как у ssh-keygen в Windows), с паролем и без."""
    import paramiko

    from odaa_backup.config import SSHConf
    from odaa_backup.ssh import AuthError, load_private_key

    k = paramiko.RSAKey.generate(2048)
    plain = tmp_path / "id_rsa"
    k.write_private_key_file(str(plain))
    assert load_private_key(SSHConf(host="h", user="root", key_file=str(plain))).get_name() == "ssh-rsa"
    locked = tmp_path / "id_rsa_locked"
    k.write_private_key_file(str(locked), password="pw")
    with pytest.raises(AuthError):
        load_private_key(SSHConf(host="h", user="root", key_file=str(locked)))
    with pytest.raises(AuthError):
        load_private_key(SSHConf(host="h", user="root", key_file=str(tmp_path / "nope")))


def test_windows_paths_in_double_quotes_are_tolerated(tmp_path):
    """Типичная ошибка: "C:\\Users\\..." с одной чертой в двойных кавычках (\\U ломает YAML)."""
    p = tmp_path / "config.yaml"
    p.write_text(
        'backup_root: "C:\\Users\\sp\\Backups"\n'
        "sources:\n"
        "  - id: vps\n"
        '    title: "VPS \\"мой\\""\n'
        "    ssh: {host: 1.2.3.4, user: root, key_file: \"C:\\Users\\sp\\.ssh\\id_ed25519\"}\n"
        '    files: ["/opt/vybor"]\n'
        'second_copy: {path: "E:\\\\Backups"}\n',
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert str(cfg.backup_root).replace("/", "\\").endswith("C:\\Users\\sp\\Backups")
    assert cfg.sources[0].ssh.key_file == "C:\\Users\\sp\\.ssh\\id_ed25519"
    assert cfg.second_copy.path == "E:\\Backups"


def test_unfixable_yaml_error_has_hint(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text('backup_root: "C:\\Users\\it\'s"\nsources: [\n', encoding="utf-8")
    with pytest.raises(ConfigError) as e:
        load_config(p)
    assert "одинарных кавычках" in str(e.value)
