"""Builders for the one-time "set up this computer" scripts (pure functions).

`POST /setup/{token}` answers with one of these scripts. A script writes the user's
key into `~/.aws/credentials` and `~/.aws/config` as one profile, replacing only the
section with the same header, after it made a backup of each file. The values are the
same as the browser snippets in `static/js/awsconfig.js`. The secret is embedded here
and nowhere else: never log a script. All values are checked against strict alphabets
first, so nothing can break out of the single-quoted strings.
"""
# ruff: noqa: E501

import re
from dataclasses import dataclass

OSES = ("windows", "macos", "linux")

_PLAIN = re.compile(r"^[A-Za-z0-9._:/@+=~%-]{1,256}\Z")  # profile, region, endpoint, style
_ACCESS = re.compile(r"^[A-Za-z0-9/+=]{1,128}\Z")  # access key and secret key


@dataclass(frozen=True)
class SetupValues:
    profile: str
    region: str
    endpoint: str
    style: str
    access_key: str
    secret_key: str

    def valid(self) -> bool:
        return (all(_PLAIN.match(v) for v in (self.profile, self.region, self.endpoint, self.style))
                and all(_ACCESS.match(v) for v in (self.access_key, self.secret_key)))


# Windows PowerShell 5.1 may not offer TLS 1.2 by default.
TLS12 = ("[Net.ServicePointManager]::SecurityProtocol = "
         "[Net.ServicePointManager]::SecurityProtocol -bor 3072; ")


def command(os_name: str, url: str) -> str:
    """The line the user pastes into a terminal."""
    if os_name == "windows":
        return f"{TLS12}irm -Method Post '{url}' | iex"
    return f"curl -fsSL -X POST '{url}' | sh"


def _config_lines(v: SetupValues) -> list[str]:
    head = "[default]" if v.profile == "default" else f"[profile {v.profile}]"
    return [
        head,
        f"region = {v.region}",
        f"endpoint_url = {v.endpoint}",
        "# Newer AWS CLI/SDKs add CRC checksums that many Ceph RGW versions reject.",
        "request_checksum_calculation = when_required",
        "response_checksum_validation = when_required",
        "s3 =",
        f"  addressing_style = {v.style}",
    ]


def _cred_lines(v: SetupValues) -> list[str]:
    return [f"[{v.profile}]", f"aws_access_key_id = {v.access_key}",
            f"aws_secret_access_key = {v.secret_key}"]


def _test_cmd(profile: str) -> str:
    return "aws s3 ls" if profile == "default" else f"aws --profile {profile} s3 ls"


def error_script(os_name: str, message: str) -> str:
    """A script that only prints `message` as an error. `message` is fixed text."""
    msg = message.replace("'", "")
    if os_name == "windows":
        return f"Write-Host '{msg}' -ForegroundColor Red\n"
    return f"echo '{msg}' >&2\nexit 1\n"


_SH = r"""#!/bin/sh
# Bucketeer setup: installs one AWS profile on this computer. Safe to run again.
set -eu
umask 077

install_section() {
  file=$1; header=$2; body=$3
  mkdir -p "$(dirname "$file")"
  tmp="$file.bucketeer-tmp.$$"
  if [ -f "$file" ]; then
    cp -p "$file" "$file.bucketeer-backup-$STAMP"
    chmod 600 "$file.bucketeer-backup-$STAMP"
    awk -v h="$header" '
      { t = $0; sub(/\r$/, "", t); sub(/^[ \t]+/, "", t); sub(/[ \t]+$/, "", t) }
      substr(t, 1, 1) == "[" { skip = (t == h) }
      !skip { print }
    ' "$file" > "$tmp"
  else
    : > "$tmp"
  fi
  if [ -s "$tmp" ] && [ -n "$(tail -n 1 "$tmp" | tr -d ' \t\r')" ]; then echo >> "$tmp"; fi
  printf '%s\n' "$body" >> "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$file"
}

main() {
  CRED="${AWS_SHARED_CREDENTIALS_FILE:-$HOME/.aws/credentials}"
  CFG="${AWS_CONFIG_FILE:-$HOME/.aws/config}"
  STAMP=$(date +%Y%m%d%H%M%S)
  install_section "$CRED" '@CRED_HEADER@' '@CRED_BODY@'
  install_section "$CFG" '@CFG_HEADER@' '@CFG_BODY@'
  chmod 600 "$CRED"
  echo "Done. The AWS profile '@PROFILE@' is installed."
  echo "  keys:   $CRED"
  echo "  config: $CFG"
  echo "Earlier versions of these files were kept next to them as *.bucketeer-backup-$STAMP."
  if command -v aws >/dev/null 2>&1; then
    echo "Testing: @TEST@"
    if @TEST@ </dev/null; then
      echo "It works."
    else
      echo "The test did not work. Check your network and VPN, then run: @TEST@"
    fi
  else
    echo "The AWS CLI is not installed. Install it from https://aws.amazon.com/cli/ , then run: @TEST@"
  fi
}

main
"""

_PS = r"""# Bucketeer setup: installs one AWS profile on this computer. Safe to run again.
& {
  $ErrorActionPreference = 'Stop'
  try {
    $credFile = $env:AWS_SHARED_CREDENTIALS_FILE
    if (-not $credFile) { $credFile = Join-Path $HOME '.aws\credentials' }
    $cfgFile = $env:AWS_CONFIG_FILE
    if (-not $cfgFile) { $cfgFile = Join-Path $HOME '.aws\config' }
    $stamp = Get-Date -Format 'yyyyMMddHHmmss'
    $utf8 = New-Object System.Text.UTF8Encoding $false
    $nl = [Environment]::NewLine

    function Install-Section($file, $header, $body) {
      $dir = Split-Path -Parent $file
      if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
      $lines = @()
      if (Test-Path -LiteralPath $file) {
        Copy-Item -LiteralPath $file -Destination "$file.bucketeer-backup-$stamp"
        $lines = [System.IO.File]::ReadAllLines($file)
      }
      $out = New-Object System.Collections.Generic.List[string]
      $skip = $false
      foreach ($l in $lines) {
        $t = $l.Trim()
        if ($t.StartsWith('[')) { $skip = ($t -ceq $header) }
        if (-not $skip) { $out.Add($l) }
      }
      if ($out.Count -gt 0 -and $out[$out.Count - 1].Trim() -ne '') { $out.Add('') }
      foreach ($l in $body) { $out.Add($l) }
      $tmp = "$file.bucketeer-tmp"
      [System.IO.File]::WriteAllText($tmp, (($out -join $nl) + $nl), $utf8)
      Move-Item -LiteralPath $tmp -Destination $file -Force
    }

    Install-Section $credFile '@CRED_HEADER@' @(@CRED_ITEMS@)
    Install-Section $cfgFile '@CFG_HEADER@' @(@CFG_ITEMS@)
    try {
      $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
      icacls $credFile /inheritance:r /grant:r "*${sid}:F" | Out-Null
    } catch { }

    Write-Host "Done. The AWS profile '@PROFILE@' is installed."
    Write-Host "  keys:   $credFile"
    Write-Host "  config: $cfgFile"
    Write-Host "Earlier versions of these files were kept next to them as *.bucketeer-backup-$stamp."
    if (Get-Command aws -ErrorAction SilentlyContinue) {
      Write-Host "Testing: @TEST@"
      $ErrorActionPreference = 'Continue'
      & aws @TEST_ARGS@
      if ($LASTEXITCODE -eq 0) { Write-Host 'It works.' }
      else { Write-Host 'The test did not work. Check your network and VPN, then run: @TEST@' }
    } else {
      Write-Host 'The AWS CLI is not installed. Install it from https://aws.amazon.com/cli/ , then run: @TEST@'
    }
  } catch {
    Write-Host "Setup failed: $($_.Exception.Message)" -ForegroundColor Red
  }
}
"""


def _fill(template: str, **parts: str) -> str:
    for k, val in parts.items():
        template = template.replace(f"@{k}@", val)
    return template


def sh_script(v: SetupValues) -> str:
    assert v.valid()
    cred, cfg = _cred_lines(v), _config_lines(v)
    return _fill(_SH, CRED_HEADER=cred[0], CRED_BODY="\n".join(cred),
                 CFG_HEADER=cfg[0], CFG_BODY="\n".join(cfg),
                 PROFILE=v.profile, TEST=_test_cmd(v.profile))


def ps_script(v: SetupValues) -> str:
    assert v.valid()
    cred, cfg = _cred_lines(v), _config_lines(v)
    items = lambda ls: ",".join(f"'{x}'" for x in ls)  # noqa: E731
    test = _test_cmd(v.profile)
    return _fill(_PS, CRED_HEADER=cred[0], CFG_HEADER=cfg[0],
                 CRED_ITEMS=items(cred), CFG_ITEMS=items(cfg),
                 PROFILE=v.profile, TEST=test, TEST_ARGS=" ".join(test.split()[1:]))


def script(os_name: str, v: SetupValues) -> str:
    return ps_script(v) if os_name == "windows" else sh_script(v)
