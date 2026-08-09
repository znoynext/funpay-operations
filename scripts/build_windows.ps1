$ErrorActionPreference = 'Stop'
# Install only pinned runtime/build dependencies.  The PyInstaller search path
# below always points at this checkout, so a global package copy is never used.
python -m pip install -r requirements.txt 'pyinstaller==6.22.0'
python -m PyInstaller --noconfirm --clean --onefile --noconsole --paths src --name funpay-operations scripts/windows_entrypoint.py
python -m PyInstaller --noconfirm --clean --onefile --console --paths src --name funpay-operations-cli scripts/windows_entrypoint.py
