import h5py
import numpy as np
import sys

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

class H5DataLoader:
    def __init__(self, filename):
        self.filename = filename
        self.h5file = h5py.File(filename, "r")
    
    def list_datasets(self):
        """Return the list of available datasets."""
        return list(self.h5file.keys())
    
    def load_dataset(self, dataset_name):
        """
        Load a dataset by name.
        
        Parameters:
            dataset_name (str): The dataset to load.
        
        Returns:
                {
                    "records": records,            # full numpy array
                    "audio_data": audio_data,      # processed audio data (np.array)
                    "adc_data": adc_data,          # processed ADC data (dict of np.array)
                    "audio_boundaries": audio_boundaries,  # list of boundaries
                    "adc_boundaries": adc_boundaries,      # dict of boundaries
                }
        """
        dataset = self.h5file[dataset_name]
        # Force read the entire dataset into memory
        records = dataset[:]  
        # Process the data using your helper functions
        audio_data, adc_data = process_records(records)
        audio_boundaries, adc_boundaries = get_record_boundaries(records)
        return {
            "records": records,
            "audio_data": audio_data,
            "adc_data": adc_data,
            "audio_boundaries": audio_boundaries,
            "adc_boundaries": adc_boundaries,
        }

    
    def close(self):
        self.h5file.close()

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python DataLoader.py <path_to_h5_file>")
        sys.exit(1)
    
    h5_file_path = sys.argv[1]
    loader = H5DataLoader(h5_file_path)
    datasets = loader.list_datasets()
    print("Available datasets:", datasets)

    # Load a specific dataset in eager mode (more memory intensive)
    data = loader.load_dataset("Europe")
    print("Audio samples count:", len(data["audio_data"]))
    print("Data dict keys:", data.keys())