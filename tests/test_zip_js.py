"""static/js/zip.js builds a zip Python can read, with the Unix execute bit. Needs node."""

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

ZIP_JS = Path(__file__).resolve().parent.parent / "static/js/zip.js"


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_zip_one_is_readable_and_executable(tmp_path):
    out = tmp_path / "t.zip"
    prog = (f"import {{ zipOne }} from {str(ZIP_JS)!r}; import fs from 'fs';"
            f"fs.writeFileSync({str(out)!r}, zipOne('bucketeer-setup.command', "
            "new TextEncoder().encode('#!/bin/sh\\necho h\\u00e9llo\\n'), 0o755));")
    subprocess.run(["node", "--input-type=module", "-e", prog], check=True, timeout=30)
    with zipfile.ZipFile(out) as z:
        assert z.testzip() is None
        (info,) = z.infolist()
        assert info.filename == "bucketeer-setup.command"
        assert z.read(info) == "#!/bin/sh\necho héllo\n".encode()
        assert (info.external_attr >> 16) & 0o777 == 0o755
