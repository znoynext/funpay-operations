$ErrorActionPreference = 'Stop'
python -m pip install . pyinstaller
pyinstaller --noconfirm --clean --onefile --noconsole --name funpay-operations scripts/windows_entrypoint.py
pyinstaller --noconfirm --clean --onefile --console --name funpay-operations-cli scripts/windows_entrypoint.py
