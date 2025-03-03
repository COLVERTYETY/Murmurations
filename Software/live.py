import sys
import socket
import struct
import time
import h5py as h5
import numpy as np
from PyQt5.QtCore import Qt, QThread, pyqtSignal, pyqtSlot
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLineEdit, QLabel, QSpinBox
)
import pyqtgraph as pg

ch2c = {
    0: "r",
    1: "c",
    2: "b",
    3: "g",
    4: "m",
    5: "y",
}

# Constants
ESP32_DEFAULT_IP = "192.168.41.114"
PORT = 5000
HEADER_FORMAT = "<BBHQ"  # source (1B), reserved (1B), length (2B), timestamp (8B)
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)

class DataRecordThread(QThread):
    """
    This thread is responsible for recording data to disk using h5py.
    For each record the following fields are saved:
      - local_ts: the local timestamp when the record is written,
      - data_ts: the timestamp from the incoming data,
      - source: the source (0 for audio, 1 for ADC),
      - channels: for ADC records, a string describing the number of samples
          per channel (e.g. "ch0:10, ch1:15"); for audio records this is empty.
      - data: a variable-length array of int16 samples;
          for ADC records, the samples from each channel (sorted by channel)
          are concatenated into one array.
    """
    def __init__(self, filename, parent=None):
        super().__init__(parent)
        self.filename = filename
        self.PID = "records"
        self.recording = False
        self.running = False
        self.data = []  # Will hold tuples: (source, data_ts, data)

    def run(self):
        self.running = True
        file = None
        dataset = None
        while self.running:
            if self.recording:
                # Open the file if not already open.
                if file is None:
                    try:
                        file = h5.File(self.filename, "a")
                    except Exception as e:
                        print("Error opening file:", e)
                        self.recording = False
                        continue
                    # Create or open the dataset.
                    vlen_int16 = h5.special_dtype(vlen=np.dtype('int16'))
                    str_dtype = h5.string_dtype(encoding='utf-8')
                    record_dtype = np.dtype([
                        ('local_ts', 'f8'),
                        ('data_ts', 'f8'),
                        ('source', 'i4'),
                        ('channels', str_dtype),
                        ('data', vlen_int16),
                    ])
                    if self.PID in file:
                        dataset = file[self.PID]
                    else:
                        dataset = file.create_dataset(
                            self.PID, shape=(0,), maxshape=(None,),
                            dtype=record_dtype, chunks=True
                        )
                    print("Recording started: file opened and dataset ready.")
                # If any new data has been added, write it out.
                if self.data:
                    records_to_write = []
                    while self.data:
                        rec = self.data.pop(0)
                        source, data_ts, data_val = rec
                        local_ts = time.time()
                        if source == 0:
                            # Audio: data_val is a list of samples.
                            channels_str = ""
                            data_array = np.array(data_val, dtype=np.int16)
                        elif source == 1:
                            # ADC: data_val is a dict mapping channel -> list of samples.
                            sorted_channels = sorted(data_val.keys())
                            channels_info = []
                            data_list = []
                            for ch in sorted_channels:
                                samples = data_val[ch]
                                channels_info.append(f"ch{ch}:{len(samples)}")
                                data_list.extend(samples)
                            channels_str = ", ".join(channels_info)
                            data_array = np.array(data_list, dtype=np.int16)
                        else:
                            continue
                        records_to_write.append((local_ts, data_ts, source, channels_str, data_array))
                    if records_to_write:
                        try:
                            rec_array = np.array(records_to_write, dtype=dataset.dtype)
                            old_size = dataset.shape[0]
                            new_size = old_size + rec_array.shape[0]
                            dataset.resize((new_size,))
                            dataset[old_size:new_size] = rec_array
                            file.flush()
                            print(f"Wrote {rec_array.shape[0]} records to file.")
                        except Exception as e:
                            print("Error writing records:", e)
                # Short sleep to avoid busy-looping.
                time.sleep(0.1)
            else:
                # Not recording; if file is open, close it.
                if file is not None:
                    file.close()
                    file = None
                    dataset = None
                    print("Recording stopped: file closed.")
                # Clear any buffered data.
                self.data.clear()
                time.sleep(0.1)
        # On thread exit, close file if still open.
        if file is not None:
            file.close()

    def stop(self):
        self.running = False
        self.wait()

    @pyqtSlot(str, str, bool)
    def record(self, filename, PID, recording):
        self.filename = filename
        self.PID = PID
        self.recording = recording
        if not self.recording:
            self.data.clear()

    @pyqtSlot(int, float, object)
    def addData(self, source, ts, data):
        if self.recording:
            self.data.append((source, ts, data))

    



class DataReceiverThread(QThread):
    # Signal: (source, timestamp, data)
    # For audio (source==0), data is a list of audio samples.
    # For ADC (source==1), data is a dict mapping channel -> list of samples.
    newData = pyqtSignal(int, float, object)
    bytesPerSecondSignal = pyqtSignal(float)

    def __init__(self, ip, parent=None):
        super().__init__(parent)
        self.ip = ip
        self.running = False

    def run(self):
        self.running = True
        bytes_received = 0
        start_time = time.time()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect((self.ip, PORT))
                s.settimeout(5.0)
                while self.running:
                    # Read the header
                    header_data = b""
                    while len(header_data) < HEADER_SIZE and self.running:
                        chunk = s.recv(HEADER_SIZE - len(header_data))
                        if not chunk:
                            self.running = False
                            break
                        header_data += chunk
                    if not self.running or len(header_data) < HEADER_SIZE:
                        break
                    bytes_received += len(header_data)
                    source, reserved, length, ts = struct.unpack(HEADER_FORMAT, header_data)

                    # Calculate expected payload size (each sample is 2 bytes)
                    expected_payload_size = length * 2
                    payload_data = b""
                    while len(payload_data) < expected_payload_size and self.running:
                        chunk = s.recv(expected_payload_size - len(payload_data))
                        if not chunk:
                            self.running = False
                            break
                        payload_data += chunk
                    if not self.running or len(payload_data) < expected_payload_size:
                        break
                    bytes_received += len(payload_data)
                    samples = struct.unpack("<" + "H" * length, payload_data)

                    # Process based on source:
                    if source == 0:
                        # Audio: pass all samples as a list.
                        # data = [sample for sample in samples]
                        # data = list(samples)
                        # data = [int(sample) for sample in samples]
                        # data = [sample - 32768 for sample in samples]
                        data = [sample - 32768 if sample >= 16384 else sample for sample in samples]
                    elif source == 1:
                        # Convert uint16_t to int16_t by shifting.
                        # ADC: separate channels.
                        adc_channels = {}
                        for s_val in samples:
                            ch = (s_val >> 12) & 0xF
                            val = s_val & 0x0FFF
                            adc_channels.setdefault(ch, []).append(val)
                        # print("ADC channels:", adc_channels)
                        data = adc_channels
                    else:
                        # Ignore other sources
                        continue

                    self.newData.emit(source, ts, data)

                    # Update bytes per second every second.
                    current_time = time.time()
                    if current_time - start_time >= 1.0:
                        bps = bytes_received / (current_time - start_time)
                        self.bytesPerSecondSignal.emit(bps)
                        start_time = current_time
                        bytes_received = 0
        except Exception as e:
            print("Socket error:", e)

    def stop(self):
        self.running = False
        self.wait()

class MainWindow(QMainWindow):
    recordingConfigSignal = pyqtSignal(str, str, bool)
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Live Murmurations")

        self.recordFile = "recordings.h5"
        self.recordingPID = "records"

        self.data_record_thread = DataRecordThread(self.recordFile)
        self.recordingConfigSignal.connect(self.data_record_thread.record)

        # Data buffers for audio.
        self.audio_data = []
        self.audio_x = []
        self.audio_counter = 0

        # Data buffers for ADC channels.
        # Keys: channel numbers; Values: list of samples.
        self.adc_data = {}
        # For each ADC channel, store x-axis indices.
        self.adc_x = {}

        # Default decimation factor (display every Nth sample).
        self.decimation_factor = 32
        self.max_display_samples = 10000

        # Create the audio plot.
        self.audio_plot = pg.PlotWidget(title="Audio Data (Source=0)")
        self.audio_curve = self.audio_plot.plot(pen='y')

        # Create the ADC plot.
        self.adc_plot = pg.PlotWidget(title="ADC Data (Source=1)")
        self.adc_plot.addLegend()
        self.adc_curves = {}  # channel -> plot curve
        

        # Controls.
        self.ip_edit = QLineEdit(ESP32_DEFAULT_IP)
        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(self.toggle_connection)
        
        #data recording 
        
        self.recording = False
        self.record_button = QPushButton("Record")
        self.record_button.clicked.connect(self.toggle_recording)
        self.record_button.setEnabled(False)

        self.recordFile_edit = QLineEdit(self.recordFile)
        self.recordFile_edit.setEnabled(True)
        self.recordFile_edit.setText(self.recordFile)
        self.recordingPID_edit = QLineEdit(self.recordingPID)
        self.recordingPID_edit.setEnabled(True)



        # Spin box for decimation factor: display 1 out of N samples.
        self.decimation_spin = QSpinBox()
        self.decimation_spin.setRange(1, 1024)
        self.decimation_spin.setSingleStep(5)
        self.decimation_spin.setValue(self.decimation_factor)
        self.decimation_spin.valueChanged.connect(self.change_decimation)
        

        #spinbox for maximum number of samples to display
        self.max_samples_spin = QSpinBox()
        self.max_samples_spin.setRange(100, 50000)
        self.max_samples_spin.setSingleStep(1000)
        self.max_samples_spin.setValue(self.max_display_samples)
        self.max_samples_spin.valueChanged.connect(self.max_samples)

        # Bytes per second label.
        self.bps_label = QLabel("Bytes/sec: 0")

        # controls_layout = QHBoxLayout()
        controls_layout = QGridLayout()
        network_controls = QHBoxLayout()
        network_controls.addWidget(QLabel("ESP32 IP:"))
        network_controls.addWidget(self.ip_edit)
        network_controls.addWidget(self.connect_button)
        controls_layout.addLayout(network_controls, 0, 0)
        recording_controls = QHBoxLayout()
        recording_controls.addWidget(QLabel("Record File:"))
        recording_controls.addWidget(self.recordFile_edit)
        recording_controls.addWidget(QLabel("Recording PID:"))
        recording_controls.addWidget(self.recordingPID_edit)
        recording_controls.addWidget(self.record_button)
        controls_layout.addLayout(recording_controls, 1, 0)
        display_controls = QHBoxLayout()
        display_controls.addWidget(QLabel("Max samples:"))
        display_controls.addWidget(self.max_samples_spin)
        display_controls.addWidget(QLabel("Decimation:"))
        display_controls.addWidget(self.decimation_spin)
        display_controls.addWidget(self.bps_label)
        controls_layout.addLayout(display_controls, 2, 0)
        controls_widget = QWidget()
        controls_widget.setLayout(controls_layout)

        # Main layout: two plots (audio and ADC) and the control panel.
        main_layout = QVBoxLayout()
        main_layout.addWidget(self.audio_plot)
        main_layout.addWidget(self.adc_plot)
        main_layout.addWidget(controls_widget)
        central_widget = QWidget()
        central_widget.setLayout(main_layout)
        self.setCentralWidget(central_widget)

        self.data_thread = None
        self.data_record_thread.start()

    def toggle_recording(self):
        self.recording = not self.recording
        if self.recording:
            self.record_button.setText("Stop Recording")
            self.recordFile = self.recordFile_edit.text()
            self.recordingPID = self.recordingPID_edit.text()
            self.recordFile_edit.setEnabled(False)
            self.recordingPID_edit.setEnabled(False)
            # print("recording started:", self.recordFile, self.recordingPID)
            self.recordingConfigSignal.emit(self.recordFile, self.recordingPID, True)
        else:
            self.record_button.setText("Record")
            # print("recording stopped:", self.recordFile, self.recordingPID)
            self.recordFile = self.recordFile_edit.text()
            self.recordingPID = self.recordingPID_edit.text()
            self.recordFile_edit.setEnabled(True)
            self.recordingPID_edit.setEnabled(True)
            self.recordingConfigSignal.emit(self.recordFile, self.recordingPID, False)

    def change_decimation(self, value):
        self.decimation_factor = value
        self.update_plots()

    def max_samples(self, value):
        self.max_display_samples = value
        self.update_plots()

    def toggle_connection(self):
        if self.data_thread is None:
            # Start connection.
            self.connect_button.setText("Disconnect")
            self.ip_edit.setEnabled(False)
            # Clear previous buffers.
            self.audio_data.clear()
            self.audio_x.clear()
            self.audio_counter = 0
            self.adc_data.clear()
            self.adc_x.clear()
            self.adc_curves.clear()
            self.adc_plot.clear()
            self.adc_plot.addLegend()

            ip = self.ip_edit.text()
            self.data_thread = DataReceiverThread(ip)
            self.data_thread.newData.connect(self.handle_new_data)
            self.data_thread.newData.connect(self.data_record_thread.addData)
            self.data_thread.bytesPerSecondSignal.connect(self.update_bps)
            self.data_thread.start()
            self.record_button.setEnabled(True)
        else:
            # Disconnect.
            self.record_button.setEnabled(False)
            self.recording = False
            self.record_button.setText("Record")
            self.recordingConfigSignal.emit(self.recordFile, self.recordingPID, False)
            self.recordFile_edit.setEnabled(True)
            self.recordingPID_edit.setEnabled(True)
            self.data_thread.stop()
            self.data_thread = None
            self.connect_button.setText("Connect")
            self.ip_edit.setEnabled(True)

    @pyqtSlot(int, float, object)
    def handle_new_data(self, source, ts, data):
        max_samples = 50000
        if source == 0:
            # Audio data: add every sample.
            for sample in data:
                self.audio_data.append(sample)
                self.audio_x.append(self.audio_counter)
                self.audio_counter += 1
            # Limit the size of the audio data buffer.
            if len(self.audio_data) > max_samples:
                self.audio_data = self.audio_data[-max_samples:]
                self.audio_x = self.audio_x[-max_samples:]
        elif source == 1:
            # ADC data: data is a dict mapping channel -> list of samples.
            for ch, samples in data.items():
                if ch not in self.adc_data:
                    self.adc_data[ch] = []
                    self.adc_x[ch] = []
                for sample in samples:
                    self.adc_data[ch].append(sample)
                    # Use current length as the x-axis value.
                    self.adc_x[ch].append(len(self.adc_data[ch]))
                # Limit the size of the ADC data buffer for each channel.
                if len(self.adc_data[ch]) > max_samples:
                    self.adc_data[ch] = self.adc_data[ch][-max_samples:]
                    self.adc_x[ch] = self.adc_x[ch][-max_samples:]
        self.update_plots()

    @pyqtSlot(float)
    def update_bps(self, bps):
        if bps < 1024:
            self.bps_label.setText(f"Bytes/sec: {bps:.2f} B")
        elif bps < 1024**2:
            self.bps_label.setText(f"Bytes/sec: {bps/1024:.2f} KB")
        else:
            self.bps_label.setText(f"Bytes/sec: {bps/1024**2:.2f} MB")

    def update_plots(self):
        # Update the audio plot.
        if self.audio_data and self.audio_x:
            decimated_x = self.audio_x[-self.max_display_samples::self.decimation_factor]
            decimated_y = self.audio_data[-self.max_display_samples::self.decimation_factor]
            self.audio_curve.setData(decimated_x, decimated_y)

        # Update the ADC plot for each channel.
        for ch, data_list in self.adc_data.items():
            x_list = self.adc_x[ch]
            decimated_x = x_list[-self.max_display_samples::self.decimation_factor]
            decimated_y = data_list[-self.max_display_samples::self.decimation_factor]
            if ch not in self.adc_curves:
                # Create a new curve for this channel with a legend entry.
                self.adc_curves[ch] = self.adc_plot.plot(decimated_x, decimated_y, pen=ch2c[ch], name=f"Ch {ch}")
                # self.adc_curves[ch] = self.adc_plot.plot(decimated_x, decimated_y, pen=None, name=f"Ch {ch}")
            else:
                self.adc_curves[ch].setData(decimated_x, decimated_y)
        


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())
