$ErrorActionPreference = 'Stop'
python -m pip install '.[build]'
python -m PyInstaller --noconfirm --clean --onefile --noconsole --name funpay-operations scripts/windows_entrypoint.py
python -m PyInstaller --noconfirm --clean --onefile --console --name funpay-operations-cli scripts/windows_entrypoint.py
