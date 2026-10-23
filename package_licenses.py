"""Collect dependency license notices for the redistributable Windows folder."""
from importlib.metadata import distributions
from pathlib import Path
import shutil
import sys


def package(destination):
    target = Path(destination) / 'LICENSES'
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(Path(__file__).parent / 'licenses', target / 'Qt-LGPL', dirs_exist_ok=True)
    inventory = []
    for distribution in distributions():
        name = distribution.metadata.get('Name', 'unknown')
        inventory.append(f'{name}=={distribution.version}')
        for file in distribution.files or []:
            if any(word in file.name.lower() for word in ('license', 'licence', 'copying', 'notice')):
                original = Path(distribution.locate_file(file))
                if original.is_file():
                    folder = target / name
                    folder.mkdir(exist_ok=True)
                    # Preserve the package-relative path to avoid name collisions.
                    relative = Path(*file.parts)
                    if '..' not in relative.parts:
                        out = folder / relative
                        out.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(original, out)
    (target / 'DEPENDENCIES.txt').write_text('\n'.join(sorted(inventory)) + '\n', 'utf-8')
    (target / 'QT-NOTICE.txt').write_text(
        'This application dynamically links to unmodified Qt and PySide6 6.10.2.\n'
        'Qt Core, GUI, Widgets and Network are used under GNU LGPL version 3.\n'
        'You may replace the Qt/PySide shared libraries with compatible modified versions.\n'
        'Reverse engineering for debugging such modifications is permitted.\n'
        'Application source and build instructions: https://github.com/eaprelsky/MeetingScribe\n'
        'Qt source: https://download.qt.io/archive/qt/6.10/6.10.2/single/\n'
        'PySide source: https://code.qt.io/cgit/pyside/pyside-setup.git/?h=v6.10.2\n'
        'Qt third-party notices: https://doc.qt.io/qt-6.10/licenses-used-in-qt.html\n'
        'PySide third-party notices: https://doc.qt.io/qtforpython-6.10/licenses.html\n', 'utf-8')


if __name__ == '__main__':
    package(sys.argv[1])
