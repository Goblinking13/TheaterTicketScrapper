"""Deploy a private runtime outside macOS Desktop privacy restrictions."""
from pathlib import Path
import shutil

project = Path(__file__).resolve().parent.parent
runtime = Path.home() / 'Library' / 'Application Support' / 'TicketCollector'
runtime.mkdir(parents=True, exist_ok=True)
runtime.chmod(0o700)
for name in ('flightwatch', 'scripts'):
    shutil.copytree(project / name, runtime / name, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
for name in ('config.json', 'requirements.txt', '.env'):
    shutil.copy2(project / name, runtime / name)
(runtime / '.env').chmod(0o600)
# Direct interpreter invocation avoids relocated console-script shebangs.
# The copied venv must be recreated if the underlying Python is removed/updated.
if not (runtime / '.venv' / 'bin' / 'python').exists():
    shutil.copytree(project / '.venv', runtime / '.venv', symlinks=True)
if not (runtime / 'state').exists() and (project / 'state').exists():
    shutil.copytree(project / 'state', runtime / 'state',
                    ignore=shutil.ignore_patterns('logs', '*.lock'))
print(f'Prepared runtime: {runtime}')
