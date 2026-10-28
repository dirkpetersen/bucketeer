"""The setup scripts: the sh one is really run, the PowerShell one only when pwsh exists."""
# ruff: noqa: E501

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from bucketeer.setup_script import SetupValues, command, error_script, ps_script, sh_script

SECRET = "s3cr3tSECRETvalue0123456789ABCDEFGHIJKLmn"
V = SetupValues("bucketeer", "us-east-1", "https://s3.example.org", "path", "AKIAEXAMPLE0123456789", SECRET)

OLD_CRED = ("[work]\naws_access_key_id = WORKKEY\naws_secret_access_key = worksecret\n\n"
            "[bucketeer]\naws_access_key_id = OLDKEY\naws_secret_access_key = OLDSECRET\n\n"
            "[other]\naws_access_key_id = OTHERKEY\n")
OLD_CFG = ("[default]\nregion = eu-west-1\n\n[profile bucketeer]\nregion = old\ns3 =\n"
           "  addressing_style = virtual\n\n[profile other]\nregion = ap-south-1\n")


def run_sh(tmp_path, values=V, extra_env=None, aws=False):
    script = tmp_path / "setup.sh"
    script.write_text(sh_script(values))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for tool in ("awk", "cp", "chmod", "mv", "mkdir", "date", "tail", "tr", "dirname", "cat"):
        shutil.copy(shutil.which(tool), bin_dir / tool)
    if aws:
        fake = bin_dir / "aws"
        fake.write_text(f'#!/bin/sh\necho "$@" > {tmp_path}/aws-args\n')
        fake.chmod(0o755)
    env = {"PATH": str(bin_dir), "HOME": str(tmp_path), **(extra_env or {})}
    return subprocess.run(["/bin/sh", str(script)], env=env, capture_output=True, text=True, timeout=30)


def test_sh_fresh_install_idempotent_and_private(tmp_path):
    r = run_sh(tmp_path)
    assert r.returncode == 0, r.stderr
    cred, cfg = tmp_path / ".aws/credentials", tmp_path / ".aws/config"
    assert cred.read_text() == f"[bucketeer]\naws_access_key_id = {V.access_key}\naws_secret_access_key = {SECRET}\n"
    assert cfg.read_text().startswith("[profile bucketeer]\nregion = us-east-1\nendpoint_url = https://s3.example.org\n")
    assert cfg.read_text().endswith("s3 =\n  addressing_style = path\n")
    assert stat.S_IMODE(cred.stat().st_mode) == 0o600
    assert SECRET not in r.stdout + r.stderr
    assert "AWS CLI is not installed" in r.stdout
    first = (cred.read_text(), cfg.read_text())
    assert run_sh(tmp_path).returncode == 0
    assert (cred.read_text(), cfg.read_text()) == first


def test_sh_replaces_only_its_section(tmp_path):
    (tmp_path / ".aws").mkdir()
    (tmp_path / ".aws/credentials").write_text(OLD_CRED)
    (tmp_path / ".aws/config").write_text(OLD_CFG)
    r = run_sh(tmp_path)
    assert r.returncode == 0, r.stderr
    cred = (tmp_path / ".aws/credentials").read_text()
    cfg = (tmp_path / ".aws/config").read_text()
    assert cred.count("[bucketeer]") == 1 and "OLDKEY" not in cred and "OLDSECRET" not in cred
    assert "[work]\naws_access_key_id = WORKKEY\naws_secret_access_key = worksecret\n" in cred
    assert "[other]\naws_access_key_id = OTHERKEY\n" in cred
    assert cfg.count("[profile bucketeer]") == 1 and "region = old" not in cfg
    assert "[default]\nregion = eu-west-1\n" in cfg and "[profile other]\nregion = ap-south-1\n" in cfg
    backups = sorted(p.name for p in (tmp_path / ".aws").glob("*.bucketeer-backup-*"))
    assert len(backups) == 2
    for p in (tmp_path / ".aws").glob("*.bucketeer-backup-*"):
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
        assert p.read_text() in (OLD_CRED, OLD_CFG)
    assert not list((tmp_path / ".aws").glob("*tmp*"))
    again = (cred, cfg)
    run_sh(tmp_path)
    assert ((tmp_path / ".aws/credentials").read_text(), (tmp_path / ".aws/config").read_text()) == again


def test_sh_honours_file_variables_and_runs_aws(tmp_path):
    cred, cfg = tmp_path / "x/creds", tmp_path / "y/conf"
    r = run_sh(tmp_path, extra_env={"AWS_SHARED_CREDENTIALS_FILE": str(cred), "AWS_CONFIG_FILE": str(cfg)},
               aws=True)
    assert r.returncode == 0, r.stderr
    assert cred.exists() and cfg.exists() and not (tmp_path / ".aws").exists()
    assert (tmp_path / "aws-args").read_text().strip() == "--profile bucketeer s3 ls"
    assert SECRET not in r.stdout + r.stderr


def test_sh_default_profile(tmp_path):
    v = SetupValues("default", "us-east-1", "https://s3.example.org", "path", V.access_key, SECRET)
    assert run_sh(tmp_path, v, aws=True).returncode == 0
    assert (tmp_path / ".aws/config").read_text().startswith("[default]\n")
    assert (tmp_path / ".aws/credentials").read_text().startswith("[default]\n")
    assert (tmp_path / "aws-args").read_text().strip() == "s3 ls"


def test_values_are_checked():
    bad = [SetupValues("a'b", "r", "https://e", "path", "AK", "S"),
           SetupValues("p", "r", "https://e", "path", "AK", "S'; rm -rf ~; '"),
           SetupValues("p", "r", "https://e\nx", "path", "AK", "S"),
           SetupValues("p", "r", "https://e", "path", "AK", "")]
    assert V.valid() and not any(v.valid() for v in bad)
    with pytest.raises(AssertionError):
        sh_script(bad[1])


def test_error_scripts_and_commands():
    assert error_script("linux", "It's gone.") == "echo 'Its gone.' >&2\nexit 1\n"
    assert error_script("windows", "Gone.").startswith("Write-Host 'Gone.'")
    assert command("linux", "https://h/setup/t") == "curl -fsSL -X POST 'https://h/setup/t' | sh"
    assert command("windows", "https://h/setup/t") == (
        "[Net.ServicePointManager]::SecurityProtocol = "
        "[Net.ServicePointManager]::SecurityProtocol -bor 3072; irm -Method Post 'https://h/setup/t' | iex")


def test_ps_text():
    t = ps_script(V)
    assert "WriteAllText" in t and "UTF8Encoding $false" in t
    assert "Set-Content" not in t and "Out-File" not in t and "-Encoding" not in t
    assert f"'aws_secret_access_key = {SECRET}'" in t and "'[profile bucketeer]'" in t
    assert "-ceq $header" in t and "-eq $header" not in t
    assert "GetCurrent().User.Value" in t and '"*${sid}:F"' in t and "USERNAME):F" not in t
    assert "icacls" in t and "AWS_SHARED_CREDENTIALS_FILE" in t and "& aws --profile bucketeer s3 ls" in t
    assert "@" not in t.replace("@(", "").replace("@'", "")  # no unfilled placeholder


@pytest.mark.skipif(not shutil.which("pwsh"), reason="pwsh not installed")
def test_ps_runs(tmp_path):
    (tmp_path / ".aws").mkdir()
    (tmp_path / ".aws/credentials").write_text(OLD_CRED)
    (tmp_path / ".aws/config").write_text(OLD_CFG)
    script = tmp_path / "setup.ps1"
    script.write_text(ps_script(V))
    env = {**os.environ, "HOME": str(tmp_path)}
    for _ in range(2):
        r = subprocess.run(["pwsh", "-NoProfile", "-File", str(script)], env=env,
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
    cred = Path(tmp_path / ".aws/credentials").read_bytes()
    assert not cred.startswith(b"\xef\xbb\xbf") and cred.count(b"[bucketeer]") == 1
    assert b"[other]" in cred and b"OLDKEY" not in cred and SECRET.encode() not in r.stdout.encode()
