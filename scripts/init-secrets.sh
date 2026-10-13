#!/bin/sh
set -eu
# Requires Python 3; secrets remain local and values are never printed.
project_directory=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
umask 077
python3 - "$project_directory" <<'PY'
import os
import pathlib
import secrets
import sys

directory = pathlib.Path(sys.argv[1]) / ".secrets"
directory.mkdir(mode=0o700, exist_ok=True)
os.chmod(directory, 0o700)
values = {
    "database_url": "mock://local/" + secrets.token_hex(32),
    "provider_api_key": "mock-only-" + secrets.token_hex(32),
    "grafana_admin_password": secrets.token_hex(32),
}
for name, value in values.items():
    path = directory / name
    try:
        with path.open("x", encoding="utf-8") as target:
            target.write(value)
        # Compose mounts file secrets without remapping ownership/mode. A
        # non-root container can read the mounted file; host users cannot
        # traverse the 0700 parent directory to read it.
        os.chmod(path, 0o444)
        print(f"Created .secrets/{name} (value hidden)")
    except FileExistsError:
        os.chmod(path, 0o444)
        print(f"Keeping existing .secrets/{name}")
print("Mock credentials are ready. Keep .secrets out of source control.")
PY
