import sys
import socket
import struct
import time
import numpy as np
import json
from PyQt5.QtCore import Qt, QThread, pyqtSignal, pyqtSlot, QTimer, QObject
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLineEdit, QLabel, QSpinBox, QCheckBox
)
import pyqtgraph as pg
import onnxruntime as ort

import colorsys
import zlib 

def name_to_color(name, s=0.7, v=0.95):
    """
    Compute a unique color for a given name using a CRC32 hash.
    """
    hue = zlib.crc32(name.encode('utf-8')) % 360
    r, g, b = colorsys.hsv_to_rgb(hue / 360.0, s, v)
    return f'#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}'

# Constants for socket communication.
ESP32_DEFAULT_IP = "10.42.0.24"
PORT = 5000
HEADER_FORMAT = "<BBHQ"  # source (1B), reserved (1B), length (2B), timestamp (8B)
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)

# ----- Utility Functions -----

def load_descriptor(descriptor_path):
    with open(descriptor_path, 'r') as f:
        descriptor = json.load(f)
    return descriptor

def id_to_dataset(dataset_map, id_val):
    """
    Return the dataset string for the given ID,
    or "noise" if id=0.
    """
    if id_val == 0:
        return "noise"
    for k, v in dataset_map.items():
        if v == id_val:
            return k
    return "Unknown"

class normalizer():
    def __init__(self, mean, std, augment=None):
        self.mean = mean
        self.std = std
        self.augment = augment

    def __call__(self, sample):
        sample = (sample - self.mean) / (self.std*10)
        # adc1, adc2 = sample
        # adc1 = (adc1 - self.mean) / (self.std*10)
        # adc2 = (adc2 - self.mean) / (self.std*10)
        return sample

# ----- Inference Worker -----

class InferenceWorker(QObject):
    inferenceDone = pyqtSignal(object, float)
    
    def __init__(self):
        super().__init__()
        # Load the descriptor (hardcoded path)
        descriptor_path = "train_descriptor.json"
        self.descriptor = load_descriptor(descriptor_path)
        self.dataset_map = self.descriptor['dataset_mapping']
        
        # Create a normalizer using the descriptor's parameters.
        adc_mean = self.descriptor.get('adc_mean', 0.0)
        adc_std = self.descriptor.get('adc_std', 1.0)
        self.normalizer = normalizer(mean=adc_mean, std=adc_std, augment=False)
        
        # Load the ONNX model (hardcoded path)
        onnx_model_path = "best_v1dTransformer59.onnx"
        available_providers = ort.get_available_providers()
        print("\nAvailable providers:", available_providers, "\n")
        providers = ['CoreMLExecutionProvider','CUDAExecutionProvider', 'CPUExecutionProvider']
        self.ort_session = ort.InferenceSession(onnx_model_path, providers=providers)
    
    @pyqtSlot(object)
    def runInference(self, data_buffer):
        """
        Expects data_buffer as a dict of {channel: numpy_array}.
        Applies normalization to each channel and constructs an input
        of shape [1, num_channels, seq_len] for the ONNX model.
        """
        channels = []
        for ch in sorted(data_buffer.keys()):
            samples = data_buffer[ch].astype(np.float32)
            normalized_samples = self.normalizer(samples)
            channels.append(normalized_samples)
        
        # Stack channels to form input [num_channels, seq_len] then add batch dimension.
        input_array = np.stack(channels, axis=0)  # shape: [num_channels, seq_len]
        input_array = np.expand_dims(input_array, axis=0)  # => [1, num_channels, seq_len]
        
        # Run ONNX inference.
        start_time = time.time()
        ort_inputs = {'input': input_array}
        ort_outs = self.ort_session.run(None, ort_inputs)
        # Assume model output shape is [1, seq_len, num_classes]; take argmax along classes.
        logits = np.array(ort_outs[0])
        predictions = np.argmax(logits, axis=2).flatten()
        elapsed = time.time() - start_time
        self.inferenceDone.emit(predictions, elapsed)

# ----- Data Receiver Thread -----

class DataReceiverThread(QThread):
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
                    
                    if source == 0:
                        # Audio: adjust samples (for display only).
                        data = [sample - 32768 if sample >= 16384 else sample for sample in samples]
                    elif source == 1:
                        # ADC: group samples by channel.
                        adc_channels = {}
                        for s_val in samples:
                            ch = (s_val >> 12) & 0xF
                            val = s_val & 0xFFF
                            adc_channels.setdefault(ch, []).append(val)
                        data = adc_channels
                    else:
                        continue

                    self.newData.emit(source, ts, data)

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

# ----- Main Window -----

class MainWindow(QMainWindow):
    inferenceRequest = pyqtSignal(object)
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Live Inference")
        
        # Load the descriptor for dataset mapping and normalization.
        descriptor_path = "train_descriptor.json"
        with open(descriptor_path, 'r') as f:
            self.descriptor = json.load(f)
        self.dataset_map = self.descriptor['dataset_mapping']
        
        # Label queue display (at the top).
        self.label_display = QLabel("")
        font = self.label_display.font()
        font.setPointSize(32)
        self.label_display.setFont(font)
        self.label_display.setAlignment(Qt.AlignCenter)
        self.label_queue = []  # Stores the filtered label queue.
        
        # Network controls.
        self.ip_edit = QLineEdit(ESP32_DEFAULT_IP)
        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(self.toggle_connection)
        
        # Inference controls.
        self.inference_freq_spin = QSpinBox()
        self.inference_freq_spin.setRange(1, 100)
        self.inference_freq_spin.setValue(10)  # Default: 10 Hz inference.
        self.inference_freq_spin.valueChanged.connect(self.change_inference_freq)
        self.max_inference_label = QLabel("Max Inference Speed: 0 Hz")
        
        # Spin box for the number of ADC samples sent to inference.
        self.inference_samples_spin = QSpinBox()
        self.inference_samples_spin.setRange(100, 1000000)
        self.inference_samples_spin.setValue(16000)
        self.inference_samples_spin.setSingleStep(1000)
        
        # Checkbox to show/hide audio data.
        self.audio_checkbox = QCheckBox("Show Audio Data")
        self.audio_checkbox.setChecked(True)
        self.audio_checkbox.stateChanged.connect(self.toggle_audio_display)
        
        # Bytes per second label.
        self.bps_label = QLabel("Bytes/sec: 0")
        
        # Spin box for Label Queue Length.
        self.label_queue_length_spin = QSpinBox()
        self.label_queue_length_spin.setRange(1, 20)
        self.label_queue_length_spin.setValue(5)
        
        # Data buffers.
        self.audio_data = []
        self.audio_x = []
        self.audio_counter = 0
        self.adc_data = {}  # ADC data per channel.
        
        # Plot for audio data.
        self.audio_plot = pg.PlotWidget(title="Audio Data (Source=0)")
        self.audio_curve = self.audio_plot.plot(pen='y')
        
        # Plot for ADC data.
        self.adc_plot = pg.PlotWidget(title="ADC Data (Source=1)")
        self.adc_plot.addLegend()
        self.adc_curves = {}
        
        # Display controls.
        self.decimation_spin = QSpinBox()
        self.decimation_spin.setRange(1, 1024)
        self.decimation_spin.setValue(64)
        self.decimation_spin.valueChanged.connect(self.update_plots)
        self.max_display_samples = 5000
        self.max_samples_spin = QSpinBox()
        self.max_samples_spin.setRange(100, 10000)
        self.max_samples_spin.setValue(self.max_display_samples)
        self.max_samples_spin.valueChanged.connect(self.set_max_samples)
        
        # Layout for controls.
        controls_layout = QGridLayout()
        network_controls = QHBoxLayout()
        network_controls.addWidget(QLabel("ESP32 IP:"))
        network_controls.addWidget(self.ip_edit)
        network_controls.addWidget(self.connect_button)
        controls_layout.addLayout(network_controls, 0, 0)
        
        inference_controls = QHBoxLayout()
        inference_controls.addWidget(QLabel("Inference Frequency (Hz):"))
        inference_controls.addWidget(self.inference_freq_spin)
        inference_controls.addWidget(self.max_inference_label)
        inference_controls.addWidget(QLabel("Inference Sample Count:"))
        inference_controls.addWidget(self.inference_samples_spin)
        controls_layout.addLayout(inference_controls, 1, 0)
        
        display_controls = QHBoxLayout()
        display_controls.addWidget(QLabel("Max samples:"))
        display_controls.addWidget(self.max_samples_spin)
        display_controls.addWidget(QLabel("Decimation:"))
        display_controls.addWidget(self.decimation_spin)
        display_controls.addWidget(self.bps_label)
        controls_layout.addLayout(display_controls, 2, 0)
        
        additional_controls = QHBoxLayout()
        additional_controls.addWidget(self.audio_checkbox)
        controls_layout.addLayout(additional_controls, 3, 0)
        
        # Label queue length control.
        label_queue_layout = QHBoxLayout()
        label_queue_layout.addWidget(QLabel("Label Queue Length:"))
        label_queue_layout.addWidget(self.label_queue_length_spin)
        controls_layout.addLayout(label_queue_layout, 4, 0)
        
        controls_widget = QWidget()
        controls_widget.setLayout(controls_layout)
        
        # Main layout: label display at the top, then graphs, then controls.
        main_layout = QVBoxLayout()
        main_layout.addWidget(self.label_display)
        main_layout.addWidget(self.audio_plot)
        main_layout.addWidget(self.adc_plot)
        main_layout.addWidget(controls_widget)
        central_widget = QWidget()
        central_widget.setLayout(main_layout)
        self.setCentralWidget(central_widget)
        
        # Threads and timers.
        self.data_thread = None
        self.inference_timer = QTimer(self)
        self.inference_timer.timeout.connect(self.request_inference)
        self.change_inference_freq(self.inference_freq_spin.value())
        self.inference_max_speed = 0.0
        
        # Set up the inference worker in its own thread.
        self.setup_inference_thread()
        self.inferenceRequest.connect(self.inference_worker.runInference)
        
    def setup_inference_thread(self):
        self.inference_thread = QThread()
        self.inference_worker = InferenceWorker()
        self.inference_worker.moveToThread(self.inference_thread)
        self.inference_worker.inferenceDone.connect(self.handle_inference_result)
        self.inference_thread.start()
        
    def set_max_samples(self, value):
        self.max_display_samples = value
        self.update_plots()
        
    def change_inference_freq(self, value):
        interval_ms = int(1000 / value)
        self.inference_timer.setInterval(interval_ms)
        
    def toggle_audio_display(self, state):
        self.audio_plot.setVisible(state == Qt.Checked)
        
    def toggle_connection(self):
        if self.data_thread is None:
            self.connect_button.setText("Disconnect")
            self.ip_edit.setEnabled(False)
            self.audio_data.clear()
            self.audio_x.clear()
            self.audio_counter = 0
            self.adc_data.clear()
            self.adc_curves.clear()
            self.adc_plot.clear()
            self.adc_plot.addLegend()
            
            ip = self.ip_edit.text()
            self.data_thread = DataReceiverThread(ip)
            self.data_thread.newData.connect(self.handle_new_data)
            self.data_thread.bytesPerSecondSignal.connect(self.update_bps)
            self.data_thread.start()
            self.inference_timer.start()
        else:
            self.inference_timer.stop()
            self.data_thread.stop()
            self.data_thread = None
            self.connect_button.setText("Connect")
            self.ip_edit.setEnabled(True)
            
    @pyqtSlot(int, float, object)
    def handle_new_data(self, source, ts, data):
        max_samples = 50000
        if source == 0:
            for sample in data:
                self.audio_data.append(sample)
                self.audio_x.append(self.audio_counter)
                self.audio_counter += 1
            if len(self.audio_data) > max_samples:
                self.audio_data = self.audio_data[-max_samples:]
                self.audio_x = self.audio_x[-max_samples:]
        elif source == 1:
            for ch, samples in data.items():
                if ch not in self.adc_data:
                    self.adc_data[ch] = []
                self.adc_data[ch].extend(samples)
                if len(self.adc_data[ch]) > max_samples:
                    self.adc_data[ch] = self.adc_data[ch][-max_samples:]
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
        if self.audio_data and self.audio_x and self.audio_plot.isVisible():
            decimated_x = self.audio_x[-self.max_display_samples::self.decimation_spin.value()]
            decimated_y = self.audio_data[-self.max_display_samples::self.decimation_spin.value()]
            self.audio_curve.setData(decimated_x, decimated_y)
            
        for ch, data_list in self.adc_data.items():
            x = np.arange(len(data_list))
            decimated_x = x[-self.max_display_samples:][::self.decimation_spin.value()]
            decimated_y = np.array(data_list)[-self.max_display_samples:][::self.decimation_spin.value()]
            if ch not in self.adc_curves:
                self.adc_curves[ch] = self.adc_plot.plot(decimated_x, decimated_y, pen=pg.intColor(ch), name=f"Ch {ch}")
            else:
                self.adc_curves[ch].setData(decimated_x, decimated_y)
                
    def request_inference(self):
        if not self.adc_data:
            return
        sample_count = self.inference_samples_spin.value()
        data_buffer = {}
        for ch, samples in self.adc_data.items():
            if len(samples) < sample_count:
                return
            # Keep only the most recent samples.
            self.adc_data[ch] = samples[-sample_count:]
            data_buffer[ch] = np.array(self.adc_data[ch])
        # Optionally clear or trim audio data for inference.
        self.audio_data = self.audio_data[-sample_count:]
        self.inferenceRequest.emit(data_buffer)
        
    @pyqtSlot(object, float)
    def handle_inference_result(self, predictions, elapsed):
        if elapsed > 0:
            current_speed = 1.0 / elapsed
            if current_speed > self.inference_max_speed:
                self.inference_max_speed = current_speed
                self.max_inference_label.setText(f"Max Inference Speed: {self.inference_max_speed:.2f} Hz")
        if len(predictions) == 0:
            return
        # Use majority vote on predictions.
        dominant = int(np.argmax(np.bincount(predictions)))
        # Convert the numeric prediction to a label string using the descriptor mapping.
        label_str = id_to_dataset(self.dataset_map, dominant)
        # Update label queue if the new label is different from the last.
        if self.label_queue and self.label_queue[-1] == label_str:
            return
        self.label_queue.append(label_str)
        max_queue_len = self.label_queue_length_spin.value()
        if len(self.label_queue) > max_queue_len:
            self.label_queue = self.label_queue[-max_queue_len:]
        highlighted_labels = [
            f'<span style="color:{name_to_color(label)}">{label}</span>'
            for label in self.label_queue
        ]
        self.label_display.setText(" | ".join(highlighted_labels))

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())
