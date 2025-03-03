import sys
import h5py
import numpy as np
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QPushButton, QLabel, QSpinBox, QFileDialog, QComboBox, QCheckBox
)
from PyQt5.QtCore import Qt
import pyqtgraph as pg

# --- Helper functions to process recorded data ---

def process_records(records):
    """
    Given a numpy array of records (compound dtype), separate out audio and ADC data.
    Returns:
      audio_data: concatenated 1D np.array of audio samples.
      adc_data: dict mapping channel -> concatenated 1D np.array of ADC samples.
    Each record has fields:
      'local_ts', 'data_ts', 'source', 'channels', 'data'
    For audio (source==0), 'data' is the list of samples.
    For ADC (source==1), 'data' is a concatenated array and 'channels' is a bytes or string
    like "ch0:10, ch1:15" indicating sample counts per channel.
    """
    audio_list = []
    adc_dict = {}  # key: channel, value: list of arrays
    for rec in records:
        source = rec['source']
        if source == 0:
            audio_list.append(rec['data'])
        elif source == 1:
            channels_field = rec['channels']
            if isinstance(channels_field, bytes):
                channels_str = channels_field.decode("utf-8")
            else:
                channels_str = channels_field
            data_arr = rec['data']
            if channels_str.strip() != "":
                parts = channels_str.split(',')
                idx = 0
                for part in parts:
                    part = part.strip()
                    try:
                        # Expecting a string like "ch0:10"
                        ch_str, count_str = part.split(':')
                        ch = int(ch_str.replace("ch", ""))
                        count = int(count_str)
                        samples = data_arr[idx: idx + count]
                        idx += count
                        if ch not in adc_dict:
                            adc_dict[ch] = []
                        adc_dict[ch].append(samples)
                    except Exception as e:
                        print("Error parsing channels info:", e)
        else:
            continue
    audio_data = np.concatenate(audio_list) if audio_list else np.array([])
    for ch in adc_dict:
        adc_dict[ch] = np.concatenate(adc_dict[ch])
    return audio_data, adc_dict

def get_record_boundaries(records):
    """
    Computes the starting index (in the concatenated arrays) of each record.
    Returns:
      audio_boundaries: list of tuples (start_index, local_ts, data_ts)
         for audio records (source==0).
      adc_boundaries: dict mapping channel -> list of tuples (start_index, local_ts, data_ts)
         for ADC records (source==1). The start_index is relative to the concatenated ADC data for that channel.
    """
    audio_boundaries = []
    adc_boundaries = {}
    audio_counter = 0
    adc_counters = {}  # per channel
    
    for rec in records:
        if rec['source'] == 0:
            count = len(rec['data'])
            audio_boundaries.append((audio_counter, rec['local_ts'], rec['data_ts']))
            audio_counter += count
        elif rec['source'] == 1:
            channels_field = rec['channels']
            if isinstance(channels_field, bytes):
                channels_str = channels_field.decode("utf-8")
            else:
                channels_str = channels_field
            if channels_str.strip() != "":
                parts = channels_str.split(',')
                for part in parts:
                    part = part.strip()
                    try:
                        ch_str, count_str = part.split(':')
                        ch = int(ch_str.replace("ch", ""))
                        count = int(count_str)
                        start_index = adc_counters.get(ch, 0)
                        if ch not in adc_boundaries:
                            adc_boundaries[ch] = []
                        adc_boundaries[ch].append((start_index, rec['local_ts'], rec['data_ts']))
                        adc_counters[ch] = start_index + count
                    except Exception as e:
                        print("Error processing ADC boundary:", e)
    return audio_boundaries, adc_boundaries

# --- Main Inspection Window ---

class InspectionMainWindow(QMainWindow):
    def __init__(self, h5file):
        super().__init__()
        self.setWindowTitle("Recording Inspection")
        self.h5file = h5file

        # Storage for raw records and boundaries.
        self.records = None
        self.audio_boundaries = []
        self.adc_boundaries = {}

        # Combo box to select dataset (PID)
        self.dataset_combo = QComboBox()
        self.dataset_combo.addItems(list(self.h5file.keys()))
        self.dataset_combo.currentTextChanged.connect(self.load_dataset)

        # Spin box for decimation factor.
        self.decimation_spin = QSpinBox()
        self.decimation_spin.setRange(1, 1024)
        self.decimation_spin.setValue(32)
        self.decimation_spin.valueChanged.connect(self.update_plots)

        # Audio plot.
        self.audio_plot = pg.PlotWidget(title="Audio Data (Source=0)")
        self.audio_curve = self.audio_plot.plot(pen='y')
        self.audio_text_items = []  # to hold TextItems for audio

        # ADC plot.
        self.adc_plot = pg.PlotWidget(title="ADC Data (Source=1)")
        self.adc_plot.addLegend()
        self.adc_curves = {}       # channel -> curve
        self.adc_text_items = {}   # channel -> list of TextItems

        # Checkbox to toggle timestamp display.
        self.show_timestamps_checkbox = QCheckBox("Show Timestamps")
        self.show_timestamps_checkbox.setChecked(False)
        self.show_timestamps_checkbox.stateChanged.connect(self.update_plots)

        # Controls layout.
        controls_layout = QHBoxLayout()
        controls_layout.addWidget(QLabel("Dataset:"))
        controls_layout.addWidget(self.dataset_combo)
        controls_layout.addWidget(self.show_timestamps_checkbox)
        controls_layout.addWidget(QLabel("Decimation:"))
        controls_layout.addWidget(self.decimation_spin)

        # Main layout.
        main_layout = QVBoxLayout()
        main_layout.addLayout(controls_layout)
        main_layout.addWidget(self.audio_plot)
        main_layout.addWidget(self.adc_plot)
        central_widget = QWidget()
        central_widget.setLayout(main_layout)
        self.setCentralWidget(central_widget)

        # Data storage.
        self.audio_data = np.array([])
        self.adc_data = {}  # channel -> np.array

        # Load the initial dataset.
        self.load_dataset(self.dataset_combo.currentText())

    def load_dataset(self, dataset_name):
        try:
            records = self.h5file[dataset_name][:]
            self.records = records
        except Exception as e:
            print("Error loading dataset:", e)
            return

        # Process records.
        self.audio_data, self.adc_data = process_records(records)
        # Compute boundaries.
        self.audio_boundaries, self.adc_boundaries = get_record_boundaries(records)
        print(f"Loaded dataset '{dataset_name}': audio samples={len(self.audio_data)}, ADC channels={list(self.adc_data.keys())}")
        self.update_plots()

    def update_plots(self):
        decimation = self.decimation_spin.value()
        # Update audio plot.
        if self.audio_data.size > 0:
            full_x_audio = np.arange(len(self.audio_data))
            decimated_x = full_x_audio[::decimation]
            decimated_y = self.audio_data[::decimation]
            self.audio_curve.setData(decimated_x, decimated_y)
        else:
            self.audio_curve.clear()

        # Remove old audio text items.
        for item in self.audio_text_items:
            self.audio_plot.removeItem(item)
        self.audio_text_items = []
        # If timestamps should be shown, add audio record annotations.
        if self.show_timestamps_checkbox.isChecked():
            for (orig_idx, local_ts, data_ts) in self.audio_boundaries:
                if orig_idx < len(self.audio_data):
                    disp_idx = (orig_idx // decimation) * decimation
                    if disp_idx < len(self.audio_data):
                        y_val = self.audio_data[disp_idx]
                    else:
                        y_val = self.audio_data[-1]
                    text = f"LT: {local_ts:.2f}\nDT: {data_ts:.2f}"
                    text_item = pg.TextItem(text, anchor=(0,1), color='w')
                    text_item.setPos(disp_idx, y_val)
                    self.audio_plot.addItem(text_item)
                    self.audio_text_items.append(text_item)
                
        # Update ADC plot.
        self.adc_plot.clear()
        self.adc_plot.addLegend()
        self.adc_curves = {}
        self.adc_text_items = {}
        for ch, data_arr in self.adc_data.items():
            if data_arr.size > 0:
                full_x_adc = np.arange(len(data_arr))
                decimated_x_adc = full_x_adc[::decimation]
                decimated_y_adc = data_arr[::decimation]
                pen_color = {0: "r", 1: "c", 2: "b", 3: "g", 4: "m", 5: "y"}.get(ch, "w")
                curve = self.adc_plot.plot(decimated_x_adc, decimated_y_adc, pen=pen_color, name=f"Ch {ch}")
                self.adc_curves[ch] = curve

                self.adc_text_items[ch] = []
                if self.show_timestamps_checkbox.isChecked() and ch in self.adc_boundaries:
                    for (orig_idx, local_ts, data_ts) in self.adc_boundaries[ch]:
                        if orig_idx < len(data_arr):
                            disp_idx = (orig_idx // decimation) * decimation
                            if disp_idx < len(data_arr):
                                y_val = data_arr[disp_idx]
                            else:
                                y_val = data_arr[-1]
                            text = f"LT: {local_ts:.2f}\nDT: {data_ts:.2f}"
                            text_item = pg.TextItem(text, anchor=(0,1), color=pen_color)
                            text_item.setPos(disp_idx, y_val)
                            self.adc_plot.addItem(text_item)
                            self.adc_text_items[ch].append(text_item)


# --- File Loader Dialog and App Startup ---

def main():
    app = QApplication(sys.argv)
    fname, _ = QFileDialog.getOpenFileName(
        None, "Select Recording File", "", "HDF5 Files (*.h5 *.hdf5)"
    )
    if not fname:
        sys.exit("No file selected.")
    try:
        h5file = h5py.File(fname, "r")
    except Exception as e:
        sys.exit(f"Error opening file: {e}")
    main_window = InspectionMainWindow(h5file)
    main_window.show()
    ret = app.exec_()
    h5file.close()
    sys.exit(ret)

if __name__ == "__main__":
    main()
