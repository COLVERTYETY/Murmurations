import sys
import h5py
import numpy as np
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QSpinBox, QFileDialog, QComboBox
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

def find_nearest_boundary(boundaries, target_ts):
    """
    Given a list of boundaries (tuples: (sample_index, local_ts, data_ts)),
    returns the boundary with data_ts closest to target_ts.
    """
    if not boundaries:
        return None
    best = boundaries[0]
    best_diff = abs(best[2] - target_ts)
    for b in boundaries:
        diff = abs(b[2] - target_ts)
        if diff < best_diff:
            best_diff = diff
            best = b
    return best

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

        # Data storage.
        self.audio_data = np.array([])
        self.adc_data = {}  # channel -> np.array

        # Controls.
        self.dataset_combo = QComboBox()
        self.dataset_combo.addItems(list(self.h5file.keys()))
        self.dataset_combo.currentTextChanged.connect(self.load_dataset)

        self.decimation_spin = QSpinBox()
        self.decimation_spin.setRange(1, 1024)
        self.decimation_spin.setValue(32)
        self.decimation_spin.valueChanged.connect(self.update_plots)

        controls_layout = QHBoxLayout()
        controls_layout.addWidget(QLabel("Dataset:"))
        controls_layout.addWidget(self.dataset_combo)
        controls_layout.addWidget(QLabel("Decimation:"))
        controls_layout.addWidget(self.decimation_spin)

        # Audio plot.
        self.audio_plot = pg.PlotWidget(title="Audio Data (Source=0)")
        self.audio_curve = self.audio_plot.plot(pen='y')
        self.audio_text_items = []  # to display the event info near the vertical line
        # Vertical line for audio selection.
        self.audio_line = pg.InfiniteLine(angle=90, movable=True, pen='w')
        self.audio_plot.addItem(self.audio_line)

        # ADC plot.
        self.adc_plot = pg.PlotWidget(title="ADC Data (Source=1)")
        self.adc_plot.addLegend()
        self.adc_curves = {}       # channel -> curve
        self.adc_text_items = {}   # for each channel, the TextItem near the vertical line
        self.adc_line = pg.InfiniteLine(angle=90, movable=True, pen='w')
        self.adc_plot.addItem(self.adc_line)

        # Connect vertical lines so that moving one updates the other.
        self.audio_line.sigPositionChanged.connect(self.sync_lines)
        self.adc_line.sigPositionChanged.connect(self.sync_lines)

        main_layout = QVBoxLayout()
        main_layout.addLayout(controls_layout)
        main_layout.addWidget(self.audio_plot)
        main_layout.addWidget(self.adc_plot)
        central_widget = QWidget()
        central_widget.setLayout(main_layout)
        self.setCentralWidget(central_widget)

        # For displaying info next to the vertical lines.
        self.audio_line_text = None
        self.adc_line_text = {}  # channel -> TextItem

        # Load initial dataset.
        self.load_dataset(self.dataset_combo.currentText())

    def load_dataset(self, dataset_name):
        try:
            records = self.h5file[dataset_name][:]
            self.records = records
        except Exception as e:
            print("Error loading dataset:", e)
            return
        self.audio_data, self.adc_data = process_records(records)
        self.audio_boundaries, self.adc_boundaries = get_record_boundaries(records)
        print(f"Loaded dataset '{dataset_name}': audio samples={len(self.audio_data)}, ADC channels={list(self.adc_data.keys())}")
        self.update_plots()
        self.sync_lines()  # update vertical lines based on boundaries

    def update_plots(self):
        decimation = self.decimation_spin.value()
        # Update audio curve.
        if self.audio_data.size > 0:
            full_x_audio = np.arange(len(self.audio_data))
            decimated_x = full_x_audio[::decimation]
            decimated_y = self.audio_data[::decimation]
            self.audio_curve.setData(decimated_x, decimated_y)
        else:
            self.audio_curve.clear()

        # Update ADC curves.
        self.adc_plot.clear()
        self.adc_plot.addLegend()
        self.adc_curves = {}
        self.adc_text_items = {}
        for ch, data_arr in self.adc_data.items():
            if data_arr.size > 0:
                full_x_adc = np.arange(len(data_arr))
                decimated_x_adc = full_x_adc[::decimation]
                decimated_y_adc = data_arr[::decimation]
                pen_color = {0:"r",1:"c",2:"b",3:"g",4:"m",5:"y"}.get(ch, "w")
                curve = self.adc_plot.plot(decimated_x_adc, decimated_y_adc, pen=pen_color, name=f"Ch {ch}")
                self.adc_curves[ch] = curve
        # Re-add vertical line to ADC plot.
        self.adc_plot.addItem(self.adc_line)
        # Re-add vertical line to audio plot.
        self.audio_plot.addItem(self.audio_line)

    def sync_lines(self):
        # Determine which line moved.
        sender = self.sender()
        decimation = self.decimation_spin.value()
        if sender == self.audio_line:
            # Audio line moved.
            audio_x = self.audio_line.value()
            # Find the audio record boundary whose sample index is nearest to the current x.
            candidate_audio = min(self.audio_boundaries, key=lambda b: abs(b[0] - audio_x)) if self.audio_boundaries else None
            if candidate_audio is not None:
                candidate_ts = candidate_audio[2]  # use data_ts as reference
                # Snap audio_line to the boundary (in decimated coordinates).
                snap_audio = (candidate_audio[0] // decimation) * decimation
                self.audio_line.blockSignals(True)
                self.audio_line.setValue(snap_audio)
                self.audio_line.blockSignals(False)
                # Update audio line text.
                y_val = self.audio_data[candidate_audio[0]] if candidate_audio[0] < len(self.audio_data) else self.audio_data[-1]
                audio_text = f"LT: {candidate_audio[1]:.2f}\nDT: {candidate_audio[2]:.2f}"
                if self.audio_line_text is None:
                    self.audio_line_text = pg.TextItem(audio_text, anchor=(0,1), color='r')
                    self.audio_plot.addItem(self.audio_line_text)
                else:
                    self.audio_line_text.setText(audio_text)
                self.audio_line_text.setPos(snap_audio, y_val)
                # Now update ADC line by finding the nearest ADC boundary (using a reference channel).
                ref_ch = 0 if 0 in self.adc_boundaries else (list(self.adc_boundaries.keys())[0] if self.adc_boundaries else None)
                if ref_ch is not None:
                    candidate_adc = find_nearest_boundary(self.adc_boundaries[ref_ch], candidate_ts)
                    if candidate_adc is not None:
                        snap_adc = (candidate_adc[0] // decimation) * decimation
                        self.adc_line.blockSignals(True)
                        self.adc_line.setValue(snap_adc)
                        self.adc_line.blockSignals(False)
                        # Update ADC line texts for each channel.
                        for ch, boundaries in self.adc_boundaries.items():
                            # For each channel, find the boundary with timestamp closest to candidate_ts.
                            cand = find_nearest_boundary(boundaries, candidate_ts)
                            if cand is not None and ch in self.adc_data:
                                data_arr = self.adc_data[ch]
                                y_val_adc = data_arr[cand[0]] if cand[0] < len(data_arr) else data_arr[-1]
                                text_adc = f"LT: {cand[1]:.2f}\nDT: {cand[2]:.2f}"
                                if ch not in self.adc_line_text:
                                    pen_color = {0:"r",1:"c",2:"b",3:"g",4:"m",5:"y"}.get(ch, "w")
                                    self.adc_line_text[ch] = pg.TextItem(text_adc, anchor=(0,1), color=pen_color)
                                    self.adc_plot.addItem(self.adc_line_text[ch])
                                else:
                                    self.adc_line_text[ch].setText(text_adc)
                                self.adc_line_text[ch].setPos(snap_adc, y_val_adc)
        elif sender == self.adc_line:
            # ADC line moved. Use reference channel (as above) to get candidate ADC event.
            adc_x = self.adc_line.value()
            ref_ch = 0 if 0 in self.adc_boundaries else (list(self.adc_boundaries.keys())[0] if self.adc_boundaries else None)
            if ref_ch is not None:
                candidate_adc = min(self.adc_boundaries[ref_ch], key=lambda b: abs(b[0] - adc_x)) if self.adc_boundaries[ref_ch] else None
                if candidate_adc is not None:
                    candidate_ts = candidate_adc[2]
                    snap_adc = (candidate_adc[0] // decimation) * decimation
                    self.adc_line.blockSignals(True)
                    self.adc_line.setValue(snap_adc)
                    self.adc_line.blockSignals(False)
                    # Update ADC line text for reference channel.
                    data_arr = self.adc_data[ref_ch]
                    y_val_adc = data_arr[candidate_adc[0]] if candidate_adc[0] < len(data_arr) else data_arr[-1]
                    text_adc = f"LT: {candidate_adc[1]:.2f}\nDT: {candidate_adc[2]:.2f}"
                    if ref_ch not in self.adc_line_text:
                        pen_color = {0:"r",1:"c",2:"b",3:"g",4:"m",5:"y"}.get(ref_ch, "w")
                        self.adc_line_text[ref_ch] = pg.TextItem(text_adc, anchor=(0,1), color=pen_color)
                        self.adc_plot.addItem(self.adc_line_text[ref_ch])
                    else:
                        self.adc_line_text[ref_ch].setText(text_adc)
                    self.adc_line_text[ref_ch].setPos(snap_adc, y_val_adc)
                    # Now update audio line using audio boundaries.
                    candidate_audio = find_nearest_boundary(self.audio_boundaries, candidate_ts)
                    if candidate_audio is not None:
                        snap_audio = (candidate_audio[0] // decimation) * decimation
                        self.audio_line.blockSignals(True)
                        self.audio_line.setValue(snap_audio)
                        self.audio_line.blockSignals(False)
                        y_val_audio = self.audio_data[candidate_audio[0]] if candidate_audio[0] < len(self.audio_data) else self.audio_data[-1]
                        audio_text = f"LT: {candidate_audio[1]:.2f}\nDT: {candidate_audio[2]:.2f}"
                        if self.audio_line_text is None:
                            self.audio_line_text = pg.TextItem(audio_text, anchor=(0,1), color='w')
                            self.audio_plot.addItem(self.audio_line_text)
                        else:
                            self.audio_line_text.setText(audio_text)
                        self.audio_line_text.setPos(snap_audio, y_val_audio)

if __name__ == "__main__":
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
    main()
