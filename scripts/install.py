from __future__ import annotations

import argparse
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import venv


ROOT = Path(__file__).resolve().parents[1]
CHECK_ENVIRONMENT = """
import sys

if sys.prefix == sys.base_prefix:
    raise SystemExit('The selected Python environment is not a virtual environment.')
if sys.version_info < (3, 11):
    raise SystemExit('The selected Python environment requires Python 3.11 or newer.')
"""
CHECK_RUNTIME = """
from zoneinfo import ZoneInfo
import app.main

for name in ('Asia/Shanghai', 'Asia/Singapore', 'Asia/Hong_Kong', 'America/New_York'):
    ZoneInfo(name)
"""


def install(args: argparse.Namespace) -> None:
    if sys.version_info < (3, 11):
        raise RuntimeError("Python 3.11 or newer is required.")

    curl = shutil.which("curl.exe" if os.name == "nt" else "curl")
    if not curl:
        raise RuntimeError("Install curl and make it available on PATH before running this script.")
    subprocess.run([curl, "--version"], check=True, stdout=subprocess.DEVNULL, timeout=10)

    environment = args.venv.expanduser().resolve()
    interpreter = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not interpreter.is_file():
        print(f"Creating Python environment at {environment}", flush=True)
        venv.EnvBuilder(with_pip=True).create(environment)

    subprocess.run([str(interpreter), "-c", CHECK_ENVIRONMENT], check=True, cwd=ROOT)
    pip = [str(interpreter), "-m", "pip"]
    if subprocess.run([*pip, "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        subprocess.run([str(interpreter), "-m", "ensurepip", "--upgrade"], check=True, cwd=ROOT)
    print("Installing Python dependencies", flush=True)
    subprocess.run([*pip, "install", "-r", "requirements.txt"], check=True, cwd=ROOT)
    subprocess.run([str(interpreter), "-c", CHECK_RUNTIME], check=True, cwd=ROOT)

    env_file = ROOT / ".env"
    if not env_file.exists():
        template = (ROOT / ".env.example").read_bytes()
        try:
            with env_file.open("xb") as stream:
                stream.write(template)
        except FileExistsError:
            pass

    database_args = ["--db-path", str(args.db_path.expanduser().resolve())] if args.db_path else []
    subprocess.run([str(interpreter), "scripts/init_db.py", *database_args], check=True, cwd=ROOT)

    launch = [str(interpreter), "-m", "app.main", "--port", "51327", *database_args]
    if os.name == "nt":
        command = "& " + " ".join("'" + argument.replace("'", "''") + "'" for argument in launch)
    else:
        command = shlex.join(launch)
    print(f"Start the application from {ROOT}\n{command}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Install the backtest application and initialize its SQLite database.")
    parser.add_argument("--venv", type=Path, default=ROOT / ".venv", help="Python environment path, default is .venv in the repository.")
    parser.add_argument("--db-path", type=Path, help="Database path, default is the configured DATABASE_PATH.")
    try:
        install(parser.parse_args())
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Installation failed. {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
