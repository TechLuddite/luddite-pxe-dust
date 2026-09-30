"""Load the actual panel against installed Omarchy types without enabling it."""
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SHELL = Path('/usr/share/omarchy/shell')
with tempfile.TemporaryDirectory(prefix='pxe-dust-qml-') as temporary:
    directory = Path(temporary)
    for name in ('Commons', 'Ui', 'services'):
        (directory / name).symlink_to(SHELL / name, target_is_directory=True)
    (directory / 'plugin').symlink_to(ROOT, target_is_directory=True)
    (directory / 'shell.qml').write_text('''
import QtQuick
import Quickshell
import "plugin" as Plugin
ShellRoot {
    QtObject {
        id: mockBar
        property bool vertical: false
        property int barSize: 32
        property color foreground: "#e5e7eb"
        property color barForeground: "#e5e7eb"
        property color urgent: "#ff5555"
        property bool foregroundAnimationEnabled: false
        property color background: "#111827"
        property string fontFamily: "monospace"
        property bool isTop: true
        property bool isBottom: false
        property bool isLeft: false
        property bool isRight: false
        property string position: "top"
        property var screen: null
        function showTooltip() {}
        function hideTooltip() {}
    }
    Plugin.Panel { bar: mockBar }
    Timer { interval: 1800; running: true; onTriggered: Qt.quit() }
}
''')
    result = subprocess.run(['/usr/bin/quickshell', '--no-color', '-p', str(directory)],
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            timeout=15, env=dict(os.environ))
    print(result.stdout)
    failures = ('Failed to load', 'TypeError', 'ReferenceError', 'Cannot assign', 'is not a type', 'is not available', 'Unable to assign')
    if result.returncode or any(word in result.stdout for word in failures):
        raise SystemExit(1)
