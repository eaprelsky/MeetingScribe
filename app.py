import base64
import ctypes
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time
import tempfile
import wave

import keyring
import pyaudiowpatch as pa
import requests
from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QMenu, QMessageBox, QPushButton, QSystemTrayIcon, QTextEdit, QVBoxLayout, QWidget)

APP = 'MeetingScribe'
CONFIG = Path(os.environ['APPDATA']) / APP
CONFIG.mkdir(parents=True, exist_ok=True)
BASE_URL = 'https://routerai.ru/api/v1'


def router_request(method, route, **kwargs):
    # Desktop launches must not inherit a short-lived proxy or CA bundle from
    # the build/assistant process. Keep TLS certificate verification enabled.
    with requests.Session() as session:
        session.trust_env = False
        return session.request(method, BASE_URL + route, **kwargs)


def connection_error(error):
    if isinstance(error, requests.exceptions.SSLError):
        return 'Не удалось проверить сертификат RouterAI. Проверь дату Windows и настройки HTTPS в антивирусе.'
    if isinstance(error, requests.exceptions.ConnectTimeout):
        return 'Не удалось подключиться к RouterAI за 15 секунд. Проверь доступ к routerai.ru.'
    if isinstance(error, requests.exceptions.ReadTimeout):
        return 'RouterAI не завершил распознавание за 5 минут. Аудио сохранено. Повтори расшифровку позже.'
    if isinstance(error, requests.exceptions.ProxyError):
        return 'Ошибка прокси при подключении к RouterAI. Перезапусти обновлённую утилиту.'
    return 'Не удалось установить соединение с routerai.ru. Проверь интернет, VPN и брандмауэр. Аудио сохранено.'


def documents():
    buf = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0:
        return Path(buf.value)
    return Path.home() / 'Documents'


def load_settings():
    defaults = {'folder': str(documents() / 'Расшифровки встреч'),
                'model': 'openai/gpt-4o-transcribe', 'language': 'ru', 'pin': False}
    try:
        defaults.update(json.loads((CONFIG / 'settings.json').read_text('utf-8')))
    except (OSError, ValueError):
        pass
    return defaults


def write_json(path, data):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), 'utf-8')
    temp.replace(path)


def icon(recording=False):
    pix = QPixmap(64, 64)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor('#c83c46' if recording else '#2962ad'))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(3, 3, 58, 58, 16, 16)
    p.setBrush(QColor('white'))
    for x, h in [(17, 14), (27, 32), (37, 24), (47, 12)]:
        p.drawRoundedRect(x-3, 32-h//2, 6, h, 3, 3)
    p.end()
    return QIcon(pix)


class Events(QObject):
    status = Signal(str)
    done = Signal(str)
    failed = Signal(str)


class Track:
    def __init__(self, audio, device, folder, source, origin):
        self.folder, self.source = folder, source
        self.rate = int(device['defaultSampleRate'])
        self.channels = min(2, int(device['maxInputChannels']))
        self.index, self.frames, self.writer = 0, 0, None
        self.offset = time.monotonic() - origin
        self.error = None
        self.stream = audio.open(format=pa.paInt16, channels=self.channels,
            rate=self.rate, input=True, input_device_index=device['index'],
            frames_per_buffer=1024, stream_callback=self.callback, start=False)

    def callback(self, data, count, info, status):
        try:
            if status:
                self.error = 'В аудиопотоке были пропуски. Проверь запись перед использованием.'
            if self.writer is None:
                self.writer = wave.open(str(self.folder / f'{self.source}-{self.index:05}.wav'), 'wb')
                self.writer.setparams((self.channels, 2, self.rate, 0, 'NONE', 'not compressed'))
            self.writer.writeframesraw(data)
            self.frames += count
            if self.frames >= self.rate * 60:
                self.writer.close()
                self.writer = None
                self.index += 1
                self.frames = 0
            return (None, pa.paContinue)
        except Exception:
            self.error = 'Ошибка записи аудио на диск. Проверь свободное место и папку встречи.'
            return (None, pa.paAbort)

    def close(self):
        self.stream.stop_stream()
        self.stream.close()
        if self.writer:
            self.writer.close()
            self.writer = None


class Recorder:
    def __init__(self, folder, mic_id, speaker_id):
        self.audio = pa.PyAudio()
        self.tracks = []
        self.origin = time.monotonic()
        try:
            for device_id, source in [(mic_id, 'microphone'), (speaker_id, 'system')]:
                if device_id is not None:
                    self.tracks.append(Track(self.audio, self.audio.get_device_info_by_index(device_id),
                                             folder, source, self.origin))
            for track in self.tracks:
                track.stream.start_stream()
        except Exception:
            self.close()
            raise

    def close(self):
        for track in self.tracks:
            track.close()
        self.audio.terminate()


def transcribe(folder, settings, key, events):
    try:
        if not key:
            raise ValueError('Введи API-ключ RouterAI в настройках, затем нажми «Повторить расшифровку».')
        manifest = json.loads((folder / 'meeting.json').read_text('utf-8'))
        results_path = folder / 'recognition.json'
        results = json.loads(results_path.read_text('utf-8')) if results_path.exists() else {}
        files = sorted(folder.glob('*.wav'))
        offsets = manifest.get('offsets', {})
        parts = []
        for i, file in enumerate(files):
            source, index = file.stem.rsplit('-', 1)
            with wave.open(str(file), 'rb') as wav:
                duration = wav.getnframes() / wav.getframerate()
            if duration < 0.1:
                continue
            if file.name not in results:
                events.status.emit(f'Расшифровка {i+1} из {len(files)}…')
                body = {'model': settings['model'], 'input_audio': {
                    'data': base64.b64encode(file.read_bytes()).decode('ascii'), 'format': 'wav'}}
                if settings.get('language'):
                    body['language'] = settings['language']
                try:
                    response = router_request('POST', '/audio/transcriptions',
                        headers={'Authorization': f'Bearer {key}'}, json=body, timeout=(15, 300))
                except requests.RequestException as error:
                    raise ValueError(connection_error(error)) from None
                if not response.ok:
                    hints = {401: 'Проверь API-ключ.', 402: 'Проверь баланс RouterAI.',
                             429: 'Лимит запросов. Повтори позже.', 400: 'Проверь модель и язык в настройках.'}
                    raise ValueError(f'RouterAI: HTTP {response.status_code}. ' + hints.get(response.status_code, 'Повтори позже или выбери другую модель.'))
                try:
                    text = response.json()['text']
                    if not isinstance(text, str):
                        raise ValueError()
                except (KeyError, ValueError):
                    raise ValueError('RouterAI вернул ответ без текста. Проверь выбранную модель.') from None
                results[file.name] = text
                write_json(results_path, results)
            # Index is only a chunk ordering fallback; durations are accumulated below.
            parts.append((source, int(index), duration, results[file.name]))
        timeline, elapsed = [], dict(offsets)
        for source, index, duration, text in sorted(parts, key=lambda x: (x[0], x[1])):
            stamp = elapsed.get(source, 0)
            timeline.append((stamp, source, text))
            elapsed[source] = stamp + duration
        title = manifest['title']
        lines = [f'# {title}', '', f"Дата: {manifest['started']}", '',
                 'Временные отметки обозначают начало фрагмента записи (около минуты).', '']
        for stamp, source, text in sorted(timeline):
            seconds = int(stamp)
            label = 'Микрофон' if source == 'microphone' else 'Звук компьютера'
            lines.extend([f'## {seconds//3600:02}:{seconds//60%60:02}:{seconds%60:02} · {label}', '', text, ''])
        target = folder / 'Расшифровка.md'
        target.write_text('\n'.join(lines), 'utf-8')
        events.done.emit(str(target))
    except Exception as exc:
        events.failed.emit(str(exc) if isinstance(exc, ValueError) else 'Не удалось обработать встречу. Аудио сохранено; проверь доступ к папке и повтори.')


class Settings(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle('Настройки')
        self.resize(490, 250)
        self.fields = {}
        form = QFormLayout(self)
        self.key = QLineEdit(keyring.get_password(APP, 'routerai') or '')
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow('API-ключ RouterAI', self.key)
        for key, label in [('model', 'Модель распознавания'), ('language', 'Язык (ru или пусто)'), ('folder', 'Папка встреч')]:
            edit = QLineEdit(parent.settings[key])
            self.fields[key] = edit
            form.addRow(label, edit)
        browse = QPushButton('Выбрать папку…')
        browse.clicked.connect(self.choose)
        form.addRow('', browse)
        note = QLabel('Ключ хранится в диспетчере учётных данных Windows.\nАудио отправляется в RouterAI после остановки записи.')
        note.setWordWrap(True)
        form.addRow(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def choose(self):
        folder = QFileDialog.getExistingDirectory(self, 'Папка встреч', self.fields['folder'].text())
        if folder:
            self.fields['folder'].setText(folder)

    def save(self):
        try:
            data = {key: field.text().strip() for key, field in self.fields.items()}
            if not data['model'] or not data['folder']:
                raise ValueError('Укажи модель и папку.')
            Path(data['folder']).mkdir(parents=True, exist_ok=True)
            if self.key.text().strip():
                keyring.set_password(APP, 'routerai', self.key.text().strip())
            elif keyring.get_password(APP, 'routerai'):
                keyring.delete_password(APP, 'routerai')
            self.parent().settings.update(data)
            write_json(CONFIG / 'settings.json', self.parent().settings)
            self.accept()
        except Exception:
            QMessageBox.warning(self, 'Настройки', 'Не удалось сохранить. Проверь папку, модель и доступ к хранилищу Windows.')


class Window(QWidget):
    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        Path(self.settings['folder']).mkdir(parents=True, exist_ok=True)
        self.recorder = None
        self.folder = None
        self.busy = False
        self.events = Events()
        self.events.status.connect(self.set_status)
        self.events.done.connect(self.finished)
        self.events.failed.connect(self.failed)
        self.setWindowTitle('Встреча • MeetingScribe')
        self.setWindowIcon(icon())
        self.resize(430, 480)
        self.setMinimumWidth(410)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 20)
        layout.setSpacing(12)
        header = QHBoxLayout()
        heading = QLabel('Встреча')
        heading.setFont(QFont('Segoe UI', 20, QFont.Weight.DemiBold))
        header.addWidget(heading)
        header.addStretch()
        self.pin = QPushButton('📌')
        self.pin.setCheckable(True)
        self.pin.setChecked(self.settings.get('pin', False))
        self.pin.setToolTip('Закрепить поверх всех окон')
        self.pin.setAccessibleName('Закрепить поверх всех окон')
        self.pin.clicked.connect(self.pin_window)
        header.addWidget(self.pin)
        setup = QPushButton('⚙')
        setup.setToolTip('Настройки')
        setup.clicked.connect(lambda: Settings(self).exec())
        header.addWidget(setup)
        layout.addLayout(header)
        self.title = QLineEdit()
        self.title.setPlaceholderText('Название встречи')
        layout.addWidget(self.title)
        self.mic = QComboBox()
        self.speaker = QComboBox()
        form = QFormLayout()
        form.addRow('Микрофон', self.mic)
        form.addRow('Компьютер', self.speaker)
        layout.addLayout(form)
        refresh = QPushButton('Обновить устройства')
        refresh.clicked.connect(self.devices)
        layout.addWidget(refresh)
        self.refresh = refresh
        self.clock = QLabel('00:00:00')
        self.clock.setFont(QFont('Segoe UI', 30, QFont.Weight.Light))
        layout.addWidget(self.clock)
        self.status = QLabel('Готово к записи')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.start = QPushButton('●  Начать запись')
        self.start.setObjectName('record')
        self.start.setMinimumHeight(46)
        self.start.clicked.connect(self.toggle)
        layout.addWidget(self.start)
        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText('Здесь появится расшифровка встречи')
        layout.addWidget(self.preview)
        actions = QHBoxLayout()
        self.export = QPushButton('Сохранить встречу в…')
        self.export.clicked.connect(self.export_meeting)
        self.export.setEnabled(False)
        actions.addWidget(self.export)
        self.open_folder = QPushButton('Папка')
        self.open_folder.clicked.connect(lambda: os.startfile(str(self.folder or self.settings['folder'])))
        actions.addWidget(self.open_folder)
        layout.addLayout(actions)
        self.retry = QPushButton('Открыть запись и расшифровать…')
        self.retry.clicked.connect(self.retry_meeting)
        layout.addWidget(self.retry)
        self.tray = QSystemTrayIcon(icon(), self)
        self.tray.setToolTip('MeetingScribe — готово к записи')
        menu = QMenu()
        menu.addAction('Открыть', self.reveal)
        menu.addAction('Выход', self.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.reveal() if reason in (
            QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick) else None)
        self.tray.show()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(500)
        self.devices()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, self.pin.isChecked())

    def devices(self):
        try:
            with pa.PyAudio() as audio:
                self.mic.clear()
                self.speaker.clear()
                self.mic.addItem('Не записывать', None)
                self.speaker.addItem('Не записывать', None)
                host = audio.get_host_api_info_by_type(pa.paWASAPI)
                for i in range(audio.get_device_count()):
                    d = audio.get_device_info_by_index(i)
                    if d['hostApi'] == host['index'] and d['maxInputChannels'] and not d.get('isLoopbackDevice'):
                        self.mic.addItem(d['name'], i)
                        if i == host['defaultInputDevice']:
                            self.mic.setCurrentIndex(self.mic.count()-1)
                for d in audio.get_loopback_device_info_generator():
                    self.speaker.addItem(d['name'], d['index'])
                try:
                    default = audio.get_default_wasapi_loopback()['index']
                    self.speaker.setCurrentIndex(self.speaker.findData(default))
                except OSError:
                    pass
        except Exception:
            self.set_status('Не найдены устройства WASAPI. Подключи микрофон или наушники и обнови список.')

    def pin_window(self):
        self.settings['pin'] = self.pin.isChecked()
        write_json(CONFIG / 'settings.json', self.settings)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, self.pin.isChecked())
        self.show()

    def reveal(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def set_status(self, text):
        self.status.setText(text)
        self.tray.setToolTip(('MeetingScribe — ' + text)[:127])

    def controls(self, enabled):
        for widget in (self.mic, self.speaker, self.title, self.refresh, self.retry):
            widget.setEnabled(enabled)

    def toggle(self):
        if self.recorder:
            recorder, self.recorder = self.recorder, None
            recorder.close()
            write_json(self.folder / 'meeting.json', {**self.manifest,
                'offsets': {t.source: t.offset for t in recorder.tracks}})
            self.tray.setIcon(icon())
            warnings = [t.error for t in recorder.tracks if t.error]
            if warnings:
                QMessageBox.warning(self, 'Качество записи', '\n'.join(set(warnings)))
            self.recognize()
            return
        try:
            if self.mic.currentData() is None and self.speaker.currentData() is None:
                raise ValueError('Выбери хотя бы один источник звука.')
            title = self.title.text().strip() or 'Встреча'
            now = dt.datetime.now().astimezone()
            safe = ''.join(c for c in title if c not in '<>:"/\\|?*' and ord(c) >= 32).strip(' .')[:70] or 'Встреча'
            self.folder = Path(self.settings['folder']) / (now.strftime('%Y-%m-%d %H-%M-%S') + ' ' + safe)
            self.folder.mkdir(parents=True, exist_ok=False)
            self.manifest = {'title': title, 'started': now.isoformat()}
            write_json(self.folder / 'meeting.json', self.manifest)
            self.recorder = Recorder(self.folder, self.mic.currentData(), self.speaker.currentData())
            self.controls(False)
            self.export.setEnabled(False)
            self.preview.clear()
            self.start.setText('■  Остановить и расшифровать')
            self.tray.setIcon(icon(True))
            self.set_status('Идёт запись. Можно свернуть окно в трей.')
        except Exception as exc:
            self.failed(str(exc) if isinstance(exc, ValueError) else 'Не удалось начать запись. Проверь устройства и доступ к папке.')

    def tick(self):
        if self.recorder:
            seconds = int(time.monotonic() - self.recorder.origin)
            self.clock.setText(f'{seconds//3600:02}:{seconds//60%60:02}:{seconds%60:02}')
            errors = [t.error for t in self.recorder.tracks if t.error]
            if errors:
                self.set_status(errors[0])

    def recognize(self):
        self.busy = True
        self.start.setEnabled(False)
        self.controls(False)
        self.export.setEnabled(False)
        self.set_status('Подготовка расшифровки…')
        try:
            key = keyring.get_password(APP, 'routerai') or ''
        except Exception:
            key = ''
        threading.Thread(target=transcribe, args=(self.folder, dict(self.settings), key, self.events), daemon=True).start()

    def finished(self, path):
        self.busy = False
        self.controls(True)
        self.start.setEnabled(True)
        self.start.setText('●  Начать запись')
        self.export.setEnabled(True)
        self.preview.setPlainText(Path(path).read_text('utf-8'))
        self.set_status('Расшифровка сохранена')
        self.tray.showMessage('Встреча расшифрована', 'Текст и аудио сохранены в папке встречи.')

    def failed(self, text):
        self.busy = False
        self.controls(True)
        self.start.setEnabled(True)
        self.start.setText('●  Начать запись')
        self.set_status(text)
        QMessageBox.warning(self, 'MeetingScribe', text)

    def retry_meeting(self):
        folder = QFileDialog.getExistingDirectory(self, 'Выбери папку записи с meeting.json', self.settings['folder'])
        if folder:
            if not (Path(folder) / 'meeting.json').exists() or not list(Path(folder).glob('*.wav')):
                QMessageBox.warning(self, 'Запись', 'В папке нужны meeting.json и аудиофайлы WAV.')
                return
            self.folder = Path(folder)
            self.recognize()

    def export_meeting(self):
        target = QFileDialog.getExistingDirectory(self, 'Куда сохранить эту встречу?')
        if target:
            destination = Path(target) / self.folder.name
            try:
                if destination.resolve() == self.folder.resolve() or self.folder.resolve() in destination.resolve().parents:
                    raise ValueError()
                shutil.copytree(self.folder, destination)
                self.set_status(f'Копия встречи сохранена: {destination}')
            except Exception:
                QMessageBox.warning(self, 'Сохранение', 'Не удалось скопировать встречу. Выбери другую папку; существующие файлы не перезаписываются.')

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    def quit(self):
        if self.recorder or self.busy:
            QMessageBox.information(self, 'Встреча', 'Сначала останови запись и дождись завершения расшифровки.')
            self.reveal()
            return
        self.tray.hide()
        QApplication.quit()


def check_connection():
    """Test the packaged network stack with synthetic silence, never meeting audio."""
    class Sink:
        def __init__(self):
            self.messages = []

        def emit(self, value):
            self.messages.append(value)

    class Signals:
        def __init__(self):
            self.status, self.done, self.failed = Sink(), Sink(), Sink()

    result = {'base_url': BASE_URL, 'uses_environment_proxy': False}
    try:
        key = keyring.get_password(APP, 'routerai') or ''
        result['key_stored'] = bool(key)
        with tempfile.TemporaryDirectory(prefix='MeetingScribe-check-') as temp:
            folder = Path(temp)
            write_json(folder / 'meeting.json', {'title': 'Проверка соединения', 'started': dt.datetime.now().isoformat()})
            with wave.open(str(folder / 'system-00000.wav'), 'wb') as audio:
                audio.setparams((1, 2, 16000, 0, 'NONE', 'none'))
                audio.writeframes(b'\0\0' * 16000)
            events = Signals()
            transcribe(folder, load_settings(), key, events)
            result['transcription_ok'] = bool(events.done.messages)
            result['errors'] = events.failed.messages
    except Exception as error:
        result['transcription_ok'] = False
        result['error_class'] = type(error).__name__
    write_json(CONFIG / 'connection-check.json', result)
    return 0 if result.get('transcription_ok') else 1


def main():
    if '--check-connection' in sys.argv:
        sys.exit(check_connection())
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setFont(QFont('Segoe UI', 10))
    socket = QLocalSocket()
    socket.connectToServer(APP)
    if socket.waitForConnected(500):
        socket.disconnectFromServer()
        return
    server = QLocalServer()
    QLocalServer.removeServer(APP)
    server.listen(APP)
    app.setStyleSheet('''
        QWidget { background: #f5f7fb; color: #203149; }
        QLineEdit, QComboBox, QTextEdit { background: white; border: 1px solid #c8d2e0; border-radius: 5px; padding: 7px; }
        QPushButton { background: #e8edf5; border: 1px solid #c8d2e0; border-radius: 5px; padding: 7px 10px; }
        QPushButton:hover { background: #dbe5f4; }
        QPushButton:checked { background: #b8d5fa; border-color: #2962ad; }
        QPushButton:disabled { color: #8993a3; }
        QPushButton#record { background: #2962ad; color: white; border: none; font-weight: 600; }
        QPushButton:focus { border: 2px solid #2962ad; }
    ''')
    window = Window()
    def connected():
        client = server.nextPendingConnection()
        if client:
            client.disconnectFromServer()
            client.deleteLater()
        window.reveal()
    server.newConnection.connect(connected)
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
