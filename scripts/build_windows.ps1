$ErrorActionPreference = 'Stop'
# Install the checked-out source itself before analysis.  Build isolation can
# wait indefinitely for an index in an offline Windows environment and would
# otherwise let PyInstaller import an older globally installed package.
python -m pip install --no-build-isolation -e '.[build]'
python -m PyInstaller --noconfirm --clean --onefile --noconsole --paths src --name funpay-operations scripts/windows_entrypoint.py
python -m PyInstaller --noconfirm --clean --onefile --console --paths src --name funpay-operations-cli scripts/windows_entrypoint.py
