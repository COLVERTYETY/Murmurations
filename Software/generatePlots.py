#!/usr/bin/env python3
"""
Generate Matplotlib plots from ADC data (source=1) stored in .h5 files.

For each dataset (PID) in the H5 file:
  1. Collect all records where source=1 (ADC data).
  2. Parse their "channels" string (e.g. "ch0:10, ch1:15") to determine
     how many channels and how many samples per channel in each record.
  3. Concatenate all these samples (per channel) in time order.
  4. Chunk by 'length_in_seconds' if > 0, otherwise create a single chunk
     spanning the entire dataset.
  5. Plot each chunk as a single figure, with one line per channel.
  6. Save the plot as "<PID>_<chunk_index>.png".

Usage:
    python convert_h5_adc_plots.py \
        --input_file myrecordings.h5 \
        --length_in_seconds 5 \
        --sample_rate 16000
"""

import argparse
import h5py
import numpy as np
import math
import os
import matplotlib
matplotlib.use("Agg")  # Use a non-interactive backend (important for headless environments)
import matplotlib.pyplot as plt

OUTFOLDER = "data/"

def parse_channels_string(ch_str):
    """
    Given a string like "ch0:10, ch1:15", return a list of (channel_index, channel_length).
    e.g. [(0, 10), (1, 15)]
    """
    # If ch_str is bytes, decode it to str
    if isinstance(ch_str, bytes):
        ch_str = ch_str.decode("utf-8", errors="replace")

    parts = [p.strip() for p in ch_str.split(",")]
    ch_info = []
    for p in parts:
        # Each part is like "ch0:10"
        sub = p.split(":")
        if len(sub) != 2:
            continue
        ch_name = sub[0].strip()  # e.g. "ch0"
        ch_len_str = sub[1].strip()  # e.g. "10"
        try:
            ch_idx = int(ch_name.replace("ch", ""))  # from "ch0" to 0
            ch_len = int(ch_len_str)
            ch_info.append((ch_idx, ch_len))
        except ValueError:
            pass
    # Sort by channel index
    ch_info.sort(key=lambda x: x[0])
    return ch_info

def main():
    parser = argparse.ArgumentParser(description="Plot ADC (source=1) data from H5 and save as PNG.")
    parser.add_argument("--input_file", "-i", required=True,
                        help="Path to input .h5 file containing recorded ADC data.")
    parser.add_argument("--length_in_seconds", "-l", type=float, default=-1,
                        help="Length (in seconds) of each plot chunk. "
                             "Use -1 for a single plot of the entire set.")
    parser.add_argument("--sample_rate", "-r", type=int, default=16000,
                        help="Sampling rate of the ADC data (default=16000).")
    args = parser.parse_args()

    input_file = args.input_file
    length_in_seconds = args.length_in_seconds
    sample_rate = args.sample_rate

    if not os.path.isfile(input_file):
        print(f"Error: File '{input_file}' does not exist.")
        return

    # Open the H5 file
    with h5py.File(input_file, "r") as h5f:
        # Loop over top-level keys (datasets), which are the PIDs
        for pid in h5f.keys():
            dset = h5f[pid]
            print(f"\nProcessing dataset (PID): {pid}")

            # Filter only rows where source=1 (ADC)
            data_array = dset[:]
            adc_mask = (data_array["source"] == 1)
            adc_rows = data_array[adc_mask]

            if len(adc_rows) == 0:
                print(f"No ADC (source=1) data in '{pid}'. Skipping.")
                continue

            # We will collect each row's channels and data into channel-wise arrays
            # But first let's confirm or infer the channel layout from the first row
            first_channels_str = adc_rows[0]["channels"]
            ch_info = parse_channels_string(first_channels_str)
            if not ch_info:
                print(f"Could not parse channels for first row in {pid}. Skipping.")
                continue

            # The total number of channels (e.g. 2 if we have ch0 and ch1)
            num_channels = len(ch_info)

            # We'll store all channel data in a list of lists.
            # channels_data[0] => list of all samples from channel 0, etc.
            channels_data = [[] for _ in range(num_channels)]

            # Collect and concatenate in time order:
            # If you want strictly by data_ts, you can sort by that:
            # adc_rows = np.sort(adc_rows, order='data_ts')
            for row in adc_rows:
                row_ch_str = row["channels"]
                row_ch_info = parse_channels_string(row_ch_str)
                if len(row_ch_info) != num_channels:
                    # If channel layout changes from row to row, handle it as needed.
                    print("Warning: channel layout differs from first row. Skipping this row.")
                    continue

                row_samples = row["data"]  # entire data array (flattened int16)

                idx_start = 0
                for i, (ch_idx, ch_len) in enumerate(row_ch_info):
                    idx_end = idx_start + ch_len
                    ch_samples = row_samples[idx_start:idx_end]
                    channels_data[i].extend(ch_samples)
                    idx_start = idx_end

            # Now convert each channel's data list to a NumPy array
            channel_arrays = [np.array(ch, dtype=np.int16) for ch in channels_data]

            # If there's no data at all, skip
            total_samples = len(channel_arrays[0])
            if total_samples == 0:
                print(f"No valid ADC samples found for PID {pid}. Skipping.")
                continue

            # Confirm that all channels have the same length (should be if everything is consistent)
            for ch_arr in channel_arrays:
                if len(ch_arr) != total_samples:
                    print("Warning: Channels differ in sample length. Plot may be incomplete.")

            # Let's chunk the data by length_in_seconds (if >= 0)
            # Convert chunk length to sample frames
            if length_in_seconds < 0:
                # Single chunk for entire dataset
                n_chunks = 1
                chunk_size = total_samples  # entire set
            else:
                chunk_size = int(round(sample_rate * length_in_seconds))
                if chunk_size <= 0:
                    print(f"Invalid chunk size ({chunk_size}). Using entire dataset as one chunk.")
                    n_chunks = 1
                    chunk_size = total_samples
                else:
                    n_chunks = math.ceil(total_samples / chunk_size)

            print(f"Found {num_channels} channels, total {total_samples} samples.")
            if length_in_seconds > 0:
                print(f"Splitting into {n_chunks} chunks of ~{chunk_size} samples each "
                      f"({length_in_seconds}s at {sample_rate}Hz).")

            # Generate the plots
            start = 0
            for count in range(n_chunks):
                end = min(start + chunk_size, total_samples)

                # Prepare a new figure
                plt.figure()

                # Plot each channel on the same figure
                for i, ch_arr in enumerate(channel_arrays):
                    # Slice the channel array
                    chunk_data = ch_arr[start:end]
                    # X-axis in samples or convert to seconds:
                    time_axis = np.arange(len(chunk_data)) / sample_rate
                    plt.plot(time_axis, chunk_data, label=f"Channel {i}")

                # Labeling
                plt.xlabel("Time (seconds)")
                plt.ylabel("ADC Value (int16)")
                # You can also do: plt.title(f"{pid} - chunk {count}")
                plt.title(f"{pid} (chunk {count})")
                plt.legend()

                # Build output filename
                out_name = f"{OUTFOLDER}{pid}_{count}.png"
                plt.savefig(out_name, dpi=150, bbox_inches='tight')
                plt.close()
                print(f"  Saved: {out_name} [{end - start} samples]")

                start += chunk_size

    print("\nDone.")

if __name__ == "__main__":
    main()
