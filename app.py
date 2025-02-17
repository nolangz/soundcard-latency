#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import datetime
import json
import os
import pathlib
import random
import sys
import time
import threading
import traceback
import wave

import numpy as np
import pyqtgraph as pg

import sounddevice as sd


if getattr(sys, 'frozen', False):
    application_path = os.path.dirname(sys.executable)
elif __file__:
    application_path = os.path.dirname(__file__)


sd.default.latency = ("low", "low")
sd.default.prime_output_buffers_using_stream_callback = True


# convolve with fft, not depend on scipy
def convolve(x, y, *args, **kargs):
    n = len(x) + len(y)
    X = np.fft.rfft(x, n)
    Y = np.fft.rfft(y, n)
    return np.fft.irfft(X * Y, n)


def gcc_phat(sig, refsig, fs=1, max_tau=None, interp=1):
    """
    This function computes the offset between the signal sig and the reference signal refsig
    using the Generalized Cross Correlation - Phase Transform (GCC-PHAT)method.
    """

    # make sure the length for the FFT is larger or equal than len(sig) + len(refsig)
    n = sig.shape[0] + refsig.shape[0]

    # Generalized Cross Correlation Phase Transform
    SIG = np.fft.rfft(sig, n=n)
    REFSIG = np.fft.rfft(refsig, n=n)
    R = SIG * np.conj(REFSIG)

    cc = np.fft.irfft(R / (np.abs(R) + np.finfo(float).eps), n=(interp * n))

    max_shift = int(interp * n / 2)
    if max_tau:
        max_shift = np.minimum(int(interp * fs * max_tau), max_shift)

    cc = np.concatenate((cc[-max_shift:], cc[: max_shift + 1]))

    # find max cross correlation index
    shift = np.argmax(np.abs(cc)) - max_shift

    tau = shift / float(interp * fs)

    return tau, cc


class DeviceManager:
    _instance = None

    @staticmethod
    def get_instance():
        if DeviceManager._instance is None:
            DeviceManager._instance = DeviceManager()
        return DeviceManager._instance

    def __init__(self):
        self.scan()

    def scan(self, api="Windows WASAPI"):
        sd._terminate()
        sd._initialize()
        self.apis = sd.query_hostapis()
        self.devices = sd.query_devices()

        self.api_list = tuple(map(lambda api: api["name"], self.apis))
        try:
            api_index = self.api_list.index(api)
        except ValueError:
            api_index = 0

        self.api_index = api_index

        self.input_devices = tuple(
            filter(
                lambda d: d["max_input_channels"] > 0 and d["hostapi"] == api_index,
                self.devices,
            )
        )
        self.output_devices = tuple(
            filter(
                lambda d: d["max_output_channels"] > 0 and d["hostapi"] == api_index,
                self.devices,
            )
        )

        self.device_list = tuple(map(lambda d: d["name"], self.devices))
        self.input_list = tuple(map(lambda d: d["name"], self.input_devices))
        self.output_list = tuple(map(lambda d: d["name"], self.output_devices))

        input_index = self.apis[api_index]["default_input_device"]
        output_index = self.apis[api_index]["default_output_device"]
        input_device = self.devices[input_index]
        output_device = self.devices[output_index]

        self.default_input_index = self.input_devices.index(input_device)
        self.default_output_index = self.output_devices.index(output_device)


class Task(pg.QtCore.QThread):
    finished = pg.QtCore.Signal(int)

    def __init__(self):
        super().__init__()
        self.x = []
        self.y = []
        self.input_time = 0
        self.output_time = 0
        self.input_blocks = []
        self.output_index = 0
        self.output_data = []

    def on_input(self, data, frames, t, status):
        if not self.input_blocks:
            self.input_time = time.time()

        if status:
            print(status)

        self.input_blocks.append(data.copy())

    def on_ouput(self, outdata, frames, t, status):
        if self.output_index == 0:
            self.output_time = time.time()

        if status:
            print(status)

        data = self.output_data[self.output_index : self.output_index + frames, :]
        size = data.shape[0]
        self.output_index += size
        if size < frames:
            outdata[:size, :] = data
            outdata[size:, :].fill(0)
            raise sd.CallbackStop
        else:
            outdata[:, :] = data

    def test(self, output_name, input_name):
        self.output_name = output_name
        self.input_name = input_name
        self.start()

    def run(self):
        try:
            result = self.exec()
        except Exception:
            traceback.print_exc()
            result = -1
        self.finished.emit(result)

    def exec(self):
        device_manager = DeviceManager.get_instance()
        input_idx = device_manager.input_list.index(self.input_name)
        input_idx = device_manager.devices.index(
            device_manager.input_devices[input_idx]
        )
        output_idx = device_manager.output_list.index(self.output_name)
        output_idx = device_manager.devices.index(
            device_manager.output_devices[output_idx]
        )

        input_rate, output_rate = 48000, 48000
        input_channels = device_manager.devices[input_idx]['max_input_channels']
        output_channels = device_manager.devices[output_idx]['max_output_channels']
        period_time_ms = 4

        print(f'channels: {(input_channels, output_channels)}')

        np.random.seed(random.SystemRandom().randint(1, 1024))
        noise = np.random.normal(0.0, 1.0, output_rate // 5) * np.hanning(
            output_rate // 5
        )
        noise /= np.amax(np.abs(noise))
        noise = noise.astype("float32")
        zeros = np.zeros(output_rate // 10, dtype="float32")
        mono = np.concatenate((zeros, noise, zeros))
        source = np.zeros((len(mono), output_channels), dtype="float32")
        source[:, 0] = mono
        self.output_data = source
        self.output_index = 0
        
        self.input_blocks = []

        print(f'index {(input_idx, output_idx)}')

        event = threading.Event()
        with sd.InputStream(
            device=input_idx,
            samplerate=input_rate,
            channels=input_channels,
            blocksize=input_rate * period_time_ms // 1000,
            callback=self.on_input,
        ):
            with sd.OutputStream(
                device=output_idx,
                samplerate=output_rate,
                channels=output_channels,
                blocksize=output_rate * period_time_ms // 1000,
                callback=self.on_ouput,
                finished_callback=event.set,
            ):
                event.wait()

            time.sleep(1)

        recording = np.concatenate(self.input_blocks)
        print(recording.shape)


        ref = mono
        sig = recording[:, 0] if input_channels > 1 else recording.flatten()
        # if input_rate == output_rate:
        #     ref = mono
        # else:
        #     # ref = resampy.resample(mono, output_rate, input_rate)
        #     converter = 'sinc_best'  # or 'sinc_medium', 'sinc_fastest', ...
        #     ref = sr.resample(mono, input_rate / output_rate, converter)
        #     print(f'mono {mono.shape}')
        #     print(f'ref {ref.shape}')

        offset, cc = gcc_phat(sig, ref, fs=1)


        dt = offset * 1000 / input_rate
        delay = (self.output_time - self.input_time) * 1000 - period_time_ms
        latency = dt - delay
        print(f"dt = {dt} ms")
        print(f"delay = {delay} ms")
        print(f"latency = {latency} ms")

        offset = int(offset)
        zero_point = int((len(sig) + len(ref)) // 2)
        peak_point = zero_point + offset
        t = np.linspace(0, len(cc), len(cc), endpoint=False) - zero_point
        t = t * 1000 / input_rate
        t -= delay

        print((offset, peak_point - zero_point))

        print(f'len(cc) = {len(cc)}')
        self.latency = latency
        self.x = t[zero_point:]
        self.y = cc[zero_point:]

        print("done")

        return 0


class ComboBox(pg.QtGui.QComboBox):
    clicked = pg.QtCore.Signal()

    def showPopup(self):
        self.clicked.emit()
        super(ComboBox, self).showPopup()


class MainWindow(pg.QtGui.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowIcon(self.style().standardIcon(pg.QtGui.QStyle.SP_MediaPlay))
        self.setWindowTitle("Soundcard Latency 🎵")
        self.resize(1024, 640)
        self.widget = pg.PlotWidget()
        self.setCentralWidget(self.widget)

        self.widget.setLabel("bottom", "t/ms")

        # self.widget.showButtons()

        self.widget.setXRange(0, 1000)


        # self.widget.setXRange(np.log10(100), np.log10(8000), padding=0)
        # self.widget.setYRange(0, 50., padding=0)
        # self.widget.setLimits(minYRange=-100, maxYRange=300, yMin=-100, yMax=300)
        # self.widget.setLimits(xMin=np.log10(20), xMax=np.log10(24000))
        # self.widget.setLimits(minXRange=np.log10(20), maxXRange=np.log10(24000))

        # self.widget.setLogMode(True, False)

        self.widget.setMouseEnabled(True, False)
        self.widget.enableAutoRange(x=True, y=True)

        self.widget.hideAxis("left")

        self.toolbar = self.addToolBar("toolbar")
        self.toolbar.setMovable(False)

        refreshAction = pg.QtGui.QAction("↻", self)
        refreshAction.setToolTip("Rresh (r)")
        refreshAction.setShortcut("r")
        refreshAction.triggered.connect(self.refresh)
        self.toolbar.addAction(refreshAction)

        # self.toolbar.addWidget(pg.QtGui.QLabel(" 🧩 "))

        self.apiComboBox = ComboBox()
        self.apiComboBox.setMaximumWidth(24)
        self.toolbar.addWidget(self.apiComboBox)

        # self.toolbar.addWidget(pg.QtGui.QLabel(" 🔈 "))

        self.outputComboBox = ComboBox()
        # self.outputComboBox.setMaximumWidth(240)
        self.toolbar.addWidget(self.outputComboBox)

        # self.toolbar.addWidget(pg.QtGui.QLabel(" 🎤 "))

        self.inputComboBox = ComboBox()
        # self.inputComboBox.setMaximumWidth(240)
        self.toolbar.addWidget(self.inputComboBox)

        # ▶️
        startAction = pg.QtGui.QAction("|    ▶️    |", self)
        startAction.setToolTip("Press Space to run")
        startAction.setShortcut(" ")
        startAction.triggered.connect(self.start)
        # self.toolbar.addAction(startAction)

        button = pg.QtGui.QPushButton("▶️")
        self.toolbar.addWidget(button)
        button.clicked.connect(self.start)

        spacer = pg.QtGui.QWidget()
        spacer.setSizePolicy(
            pg.QtGui.QSizePolicy.Expanding, pg.QtGui.QSizePolicy.Preferred
        )
        self.toolbar.addWidget(spacer)

        pinAction = pg.QtGui.QAction("📌", self)
        pinAction.setToolTip("Always On Top (Ctrl+t)")
        pinAction.setShortcut("Ctrl+t")
        pinAction.setCheckable(True)
        pinAction.setChecked(False)
        pinAction.toggled.connect(self.pin)
        self.toolbar.addAction(pinAction)

        infoAction = pg.QtGui.QAction("💡", self)
        infoAction.setToolTip("Help (?)")
        infoAction.setShortcut("Shift+/")
        infoAction.triggered.connect(self.showInfo)
        self.toolbar.addAction(infoAction)

        self.toolbar.setStyleSheet(
            "QToolButton {color: #20C020}"
            "QComboBox {color: #20C020; background: #212121; padding: 2px; border: none}"
            "QComboBox::drop-down {border: none; width: 0px}"
            "QListView {color: #20C020; background: #212121; border: none; min-width: 160px;}"
            "QPushButton {color: #212121; background: #20A020; border: none; border-radius: 2px; padding: 2px 24px; margin: 0px 16px}"
            "QToolBar {background: #212121; border: 2px solid #212121}")

        self.device_manager = DeviceManager.get_instance()

        self.task = Task()
        self.task.finished.connect(self.display)

        self.setup()

        self.apiComboBox.currentTextChanged.connect(self.on_api_changed)

    def setup(self):
        self.apiComboBox.clear()
        api_list = map(lambda api: "🧩 " + api, self.device_manager.api_list)
        self.apiComboBox.addItems(api_list)
        self.apiComboBox.setCurrentIndex(self.device_manager.api_index)

        self.inputComboBox.clear()
        input_list = map(lambda api: "🎤 " + api, self.device_manager.input_list)
        self.inputComboBox.addItems(input_list)
        self.inputComboBox.setCurrentIndex(self.device_manager.default_input_index)

        self.outputComboBox.clear()
        output_list = map(lambda api: "🔈 " + api, self.device_manager.output_list)
        self.outputComboBox.addItems(output_list)
        self.outputComboBox.setCurrentIndex(self.device_manager.default_output_index)

    def refresh(self):
        api = self.apiComboBox.currentText()[2:]
        self.device_manager.scan(api)
        self.on_api_changed(api)

    def on_api_changed(self, api):
        self.device_manager.scan(api)

        self.inputComboBox.clear()
        input_list = map(lambda api: "🎤 " + api, self.device_manager.input_list)
        self.inputComboBox.addItems(input_list)
        self.inputComboBox.setCurrentIndex(self.device_manager.default_input_index)

        self.outputComboBox.clear()
        output_list = map(lambda api: "🔈 " + api, self.device_manager.output_list)
        self.outputComboBox.addItems(output_list)
        self.outputComboBox.setCurrentIndex(self.device_manager.default_output_index)

    def display(self, error):
        if error:
            msg = '<font style="font-size:32px; color:red">Error</font>'
            self.widget.setTitle(msg)
            return
        plot = self.widget.getPlotItem()
        plot.clear()

        plot.plot(self.task.x, self.task.y)

        vertical = pg.InfiniteLine(pos=self.task.latency, angle=90, movable=False)
        plot.addItem(vertical, ignoreBounds=True)

        self.widget.setXRange(0, self.task.latency * 3)
        self.widget.setLimits(xMin=self.task.x[0], xMax=self.task.x[-1])

        self.widget.setTitle(f"latency: {self.task.latency}")

        print("update data")

    def closeEvent(self, event):
        event.accept()

    def start(self):
        if self.task.isRunning():
            return
        self.widget.getPlotItem().clear()
        output_device = self.outputComboBox.currentText()[2:]
        input_device = self.inputComboBox.currentText()[2:]
        # self.task.test(0, 0)

        self.widget.setTitle(
            '<font style="font-size:16px; color:yellow">TESTING...</font>'
        )

        print((output_device, input_device))
        self.task.test(output_device, input_device)

    def showInfo(self):
        pg.QtGui.QDesktopServices.openUrl(
            pg.QtCore.QUrl("https://github.com/xiongyihui/soundcard-latency")
        )

    def pin(self, checked):
        flags = self.windowFlags()
        if checked:
            flags |= pg.QtCore.Qt.WindowStaysOnTopHint
        else:
            flags &= ~pg.QtCore.Qt.WindowStaysOnTopHint

        self.setWindowFlags(flags)
        self.show()


def main():
    app = pg.QtGui.QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
