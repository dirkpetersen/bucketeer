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


@dataclass(frozen=True)
class CloudValues:
    """The `[s3-cloud]` profile (P6-9): AWS's own endpoint, so no endpoint_url or style."""
    profile: str
    region: str
    access_key: str
    secret_key: str

    def valid(self) -> bool:
        return (all(_PLAIN.match(v) for v in (self.profile, self.region))
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


def _cloud_config_lines(v: CloudValues) -> list[str]:
    head = "[default]" if v.profile == "default" else f"[profile {v.profile}]"
    return [
        head,
        f"region = {v.region}",
        "request_checksum_calculation = when_required",
        "response_checksum_validation = when_required",
    ]


def _cred_lines(v: SetupValues | CloudValues) -> list[str]:
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
    if [ ! -e "$file.bucketeer-backup-$STAMP" ]; then
      cp -p "$file" "$file.bucketeer-backup-$STAMP"
      chmod 600 "$file.bucketeer-backup-$STAMP"
    fi
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
@INSTALLS@
  chmod 600 "$CRED"
  echo "Done. @DONE@"
  echo "  keys:   $CRED"
  echo "  config: $CFG"
  echo "Earlier versions of these files were kept next to them as *.bucketeer-backup-$STAMP."
  if command -v aws >/dev/null 2>&1; then
@TESTS@
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
        if (-not (Test-Path -LiteralPath "$file.bucketeer-backup-$stamp")) {
          Copy-Item -LiteralPath $file -Destination "$file.bucketeer-backup-$stamp"
        }
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

@INSTALLS@
    try {
      $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
      icacls $credFile /inheritance:r /grant:r "*${sid}:F" | Out-Null
    } catch { }

    Write-Host "Done. @DONE@"
    Write-Host "  keys:   $credFile"
    Write-Host "  config: $cfgFile"
    Write-Host "Earlier versions of these files were kept next to them as *.bucketeer-backup-$stamp."
    if (Get-Command aws -ErrorAction SilentlyContinue) {
      $ErrorActionPreference = 'Continue'
@TESTS@
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


def _profiles(v: SetupValues | None, cloud: CloudValues | None) -> list[tuple[str, list[str], list[str], bool]]:
    """(profile, credentials lines, config lines, is_cloud) for each profile to install.

    The setup follows the user's storage location: `v` (on premises) or `cloud` alone, never
    both (`v` None = the cloud-only script)."""
    out = []
    if v is not None:
        out.append((v.profile, _cred_lines(v), _config_lines(v), False))
    if cloud is not None:
        assert cloud.valid() and (v is None or cloud.profile != v.profile)
        out.append((cloud.profile, _cred_lines(cloud), _cloud_config_lines(cloud), True))
    assert out, "a setup script needs a profile"
    return out


def _done(names: list[str]) -> str:
    if len(names) == 1:
        return f"The AWS profile '{names[0]}' is installed."
    return "The AWS profiles " + " and ".join(f"'{n}'" for n in names) + " are installed."


# A new AWS access key is refused by S3 for a few seconds after CreateAccessKey (IAM is
# eventually consistent), so the cloud test waits for it: up to KEY_WAIT_TRIES x
# KEY_WAIT_SECONDS, and only while AWS answers with one of these codes. Any other error is
# shown at once. On-premises keys already exist, so their test runs once.
KEY_WAIT_TRIES = 12
KEY_WAIT_SECONDS = 5
KEY_NOT_ACTIVE = ("InvalidAccessKeyId", "SignatureDoesNotMatch")
_WAITING = "AWS is still activating the new key; waiting up to a minute"
_NOT_ACTIVE = "AWS has not activated the new key yet. Wait a minute, then run: {t}"
_CLOUD_FAILED = "The test did not work. Try again in a minute: {t}. If it keeps failing, ask an administrator."
_ONPREM_FAILED = "The test did not work. Check your network and VPN, then run: {t}"


def _sh_test(t: str, is_cloud: bool) -> list[str]:
    if not is_cloud:
        return [f'    echo "Testing: {t}"',
                f"    if {t} </dev/null; then",
                '      echo "It works."',
                "    else",
                f'      echo "{_ONPREM_FAILED.format(t=t)}"',
                "    fi"]
    codes = "|".join(f"*{c}*" for c in KEY_NOT_ACTIVE)
    return [f'    echo "Testing: {t}"',
            "    n=0",
            "    while :; do",
            f"      if out=$({t} </dev/null 2>&1); then ok=1; else ok=0; fi",
            "      waiting=0",
            f'      if [ $ok -eq 0 ]; then case "$out" in {codes}) waiting=1 ;; esac; fi',
            f"      if [ $waiting -eq 1 ] && [ $n -lt {KEY_WAIT_TRIES} ]; then",
            f"        [ $n -gt 0 ] || printf '%s' '{_WAITING}'",
            "        printf '.'",
            f"        n=$((n + 1)); sleep {KEY_WAIT_SECONDS}; continue",
            "      fi",
            "      [ $n -eq 0 ] || echo",
            "      if [ $ok -eq 1 ]; then",
            "        [ -z \"$out\" ] || printf '%s\\n' \"$out\"",
            '        echo "It works."',
            "      else",
            "        printf '%s\\n' \"$out\" >&2",
            f'        if [ $waiting -eq 1 ]; then echo "{_NOT_ACTIVE.format(t=t)}"; '
            f'else echo "{_CLOUD_FAILED.format(t=t)}"; fi',
            "      fi",
            "      break",
            "    done"]


def _ps_test(t: str, is_cloud: bool) -> list[str]:
    args = " ".join(t.split()[1:])
    if not is_cloud:
        return [f'      Write-Host "Testing: {t}"',
                f"      & aws {args}",
                "      if ($LASTEXITCODE -eq 0) { Write-Host 'It works.' }",
                f"      else {{ Write-Host '{_ONPREM_FAILED.format(t=t)}' }}"]
    codes = "|".join(KEY_NOT_ACTIVE)
    # "$_" turns Windows PowerShell's stderr ErrorRecords back into the plain lines aws wrote.
    return [f'      Write-Host "Testing: {t}"',
            "      $n = 0",
            "      while ($true) {",
            f'        $out = (& aws {args} 2>&1 | ForEach-Object {{ "$_" }}) -join $nl',
            "        $ok = ($LASTEXITCODE -eq 0)",
            f"        $waiting = (-not $ok) -and ($out -match '{codes}')",
            f"        if ($waiting -and $n -lt {KEY_WAIT_TRIES}) {{",
            f"          if ($n -eq 0) {{ Write-Host -NoNewline '{_WAITING}' }}",
            "          Write-Host -NoNewline '.'",
            f"          $n++; Start-Sleep -Seconds {KEY_WAIT_SECONDS}; continue",
            "        }",
            "        if ($n -gt 0) { Write-Host '' }",
            "        if ($out.Trim()) { Write-Host $out.TrimEnd() }",
            "        if ($ok) { Write-Host 'It works.' }",
            f"        elseif ($waiting) {{ Write-Host '{_NOT_ACTIVE.format(t=t)}' }}",
            f"        else {{ Write-Host '{_CLOUD_FAILED.format(t=t)}' }}",
            "        break",
            "      }"]


def sh_script(v: SetupValues | None, cloud: CloudValues | None = None) -> str:
    assert v is None or v.valid()
    profiles = _profiles(v, cloud)
    installs, tests = [], []
    for name, cred, cfg, is_cloud in profiles:
        installs += [f"  install_section \"$CRED\" '{cred[0]}' '" + "\n".join(cred) + "'",
                     f"  install_section \"$CFG\" '{cfg[0]}' '" + "\n".join(cfg) + "'"]
        tests += _sh_test(_test_cmd(name), is_cloud)
    return _fill(_SH, INSTALLS="\n".join(installs), TESTS="\n".join(tests),
                 DONE=_done([p[0] for p in profiles]),
                 TEST=" and ".join(_test_cmd(p[0]) for p in profiles))


def ps_script(v: SetupValues | None, cloud: CloudValues | None = None) -> str:
    assert v is None or v.valid()
    profiles = _profiles(v, cloud)
    items = lambda ls: ",".join(f"'{x}'" for x in ls)  # noqa: E731
    installs, tests = [], []
    for name, cred, cfg, is_cloud in profiles:
        installs += [f"    Install-Section $credFile '{cred[0]}' @({items(cred)})",
                     f"    Install-Section $cfgFile '{cfg[0]}' @({items(cfg)})"]
        tests += _ps_test(_test_cmd(name), is_cloud)
    return _fill(_PS, INSTALLS="\n".join(installs), TESTS="\n".join(tests),
                 DONE=_done([p[0] for p in profiles]),
                 TEST=" and ".join(_test_cmd(p[0]) for p in profiles))


def script(os_name: str, v: SetupValues | None, cloud: CloudValues | None = None) -> str:
    return ps_script(v, cloud) if os_name == "windows" else sh_script(v, cloud)
