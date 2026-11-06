"""Предоставляет настоящий изолированный venv worker-тестам на hosted Python."""

import atexit
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory


_temporary = None
_python = None


def worker_python() -> Path:
    """Возвращает venv без system site; при system Python создаёт один на suite.

    Returns:
        Python проектного venv либо отдельной тестовой среды с requirements.

    Raises:
        subprocess.CalledProcessError: Изолированная среда не подготовлена.
    """
    global _temporary, _python
    if _python is not None:
        return _python
    configuration = Path(sys.prefix) / "pyvenv.cfg"
    if (sys.prefix != sys.base_prefix and configuration.is_file()
            and "include-system-site-packages = false" in configuration.read_text(encoding="utf-8").lower()):
        _python = Path(sys.executable)
        return _python
    _temporary = TemporaryDirectory(prefix="compatibility-test-venv-")
    atexit.register(_temporary.cleanup)
    root = Path(_temporary.name)
    environment = root / "venv"
    subprocess.run([sys.executable, "-I", "-B", "-m", "venv", str(environment)], check=True, timeout=60)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    # Не подключаем site-packages hosted Python: worker обязан самостоятельно
    # доказать изоляцию. pip использует обычный cache, но ставит копии в venv.
    with (root / "install.log").open("w", encoding="utf-8") as log:
        subprocess.run([str(python), "-I", "-B", "-m", "pip", "--isolated", "install", "-r",
                        str(Path(__file__).resolve().parents[1] / "requirements.txt")],
                       check=True, stdout=log, stderr=subprocess.STDOUT, timeout=600)
        subprocess.run([str(python), "-I", "-B", "-m", "pip", "--isolated", "check"],
                       check=True, stdout=log, stderr=subprocess.STDOUT, timeout=60)
    _python = python
    return _python
