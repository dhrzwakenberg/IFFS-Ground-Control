from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from .config import load_config
from .gui import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("IFFS Ground Control")
    app.setOrganizationName("IFFS")
    config, warning = load_config()
    window = MainWindow(config, startup_warning=warning)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
