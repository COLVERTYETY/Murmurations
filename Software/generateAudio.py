#!/usr/bin/env python3
"""
Convert audio data recorded in an .h5 file (as saved by your DataRecordThread)
to .wav or .mp3 files.

Each dataset in the .h5 file is named by the 'PID' used in the recording.
For each dataset:
  - Concatenate all records where source=0 (audio data).
  - If length_in_seconds == -1, produce a single output file for the entire set.
  - Otherwise, chunk by length_in_seconds and produce multiple output files,
    naming them <PID>_<chunk_index>.<format>.
"""

import argparse
import h5py
import numpy as np
from pydub import AudioSegment
import math
import os

OUTFOLDER = "data/"

def main():
    parser = argparse.ArgumentParser(description="Convert H5 audio to WAV/MP3.")
    parser.add_argument("--input_file", "-i", required=True,
                        help="Path to input .h5 file containing recorded audio.")
    parser.add_argument("--length_in_seconds", "-l", type=float, default=-1,
                        help="Length (in seconds) of each output chunk. "
                             "Use -1 for a single file covering the entire recording.")
    parser.add_argument("--sample_rate", "-r", type=int, default=48000,
                        help="Sample rate of the audio (default=48000).")
    parser.add_argument("--format", "-f", choices=["wav", "mp3"], default="wav",
                        help="Output audio format (wav or mp3).")
    args = parser.parse_args()

    input_file = args.input_file
    length_in_seconds = args.length_in_seconds
    sample_rate = args.sample_rate
    output_format = args.format

    if not os.path.isfile(input_file):
        print(f"Error: File '{input_file}' does not exist.")
        return

    # Open the H5 file for reading.
    with h5py.File(input_file, "r") as h5f:
        # Loop over all items at the top level; these should be the PIDs/datasets.
        for pid in h5f.keys():
            dset = h5f[pid]

            # The recorded data has a compound dtype:
            #   ('local_ts','f8'), ('data_ts','f8'), ('source','i4'),
            #   ('channels', str_dtype), ('data', vlen_int16)
            # We only care about rows where source=0 for audio.
            print(f"\nProcessing dataset: {pid}")

            # Extract all data (or you could iterate in chunks if the dataset is huge).
            data_array = dset[:]

            # Filter for source=0
            audio_mask = (data_array["source"] == 0)
            audio_rows = data_array[audio_mask]

            if len(audio_rows) == 0:
                print(f"No audio (source=0) found in dataset '{pid}'. Skipping.")
                continue

            # Each row's "data" field is a variable-length int16 array (the actual samples).
            # We'll concatenate them all in time order. If there's a reason to reorder by
            # data_ts or local_ts, you can do so, but we'll assume the stored order is correct.
            # Sort by data_ts if you want strictly chronological:
            # audio_rows = sorted(audio_rows, key=lambda r: r['data_ts'])

            all_samples = []
            for row in audio_rows:
                samples = row["data"]  # This is a np.array(int16) for that row
                all_samples.append(samples)

            # Concatenate all int16 arrays.
            if not all_samples:
                print("No valid audio samples in dataset. Skipping.")
                continue
            audio_data = np.concatenate(all_samples)

            # Convert to a PyDub AudioSegment (mono)
            audio_segment = AudioSegment(
                audio_data.tobytes(),        # raw audio data (bytes)
                frame_rate=sample_rate,
                sample_width=2,              # int16 -> 2 bytes per sample
                channels=1
            )

            # If user wants the entire track in one file
            if length_in_seconds < 0:
                out_name = f"{OUTFOLDER}{pid}_0.{output_format}"
                print(f"Writing single {output_format} file: {out_name}")
                audio_segment.export(out_name, format=output_format)
            else:
                # We'll chunk it into segments of length_in_seconds
                chunk_milliseconds = length_in_seconds * 1000.0
                total_duration = len(audio_segment)  # in ms
                n_chunks = math.ceil(total_duration / chunk_milliseconds)

                print(f"Total audio length for '{pid}': {total_duration/1000.0:.2f} sec")
                print(f"Generating {n_chunks} chunks of {length_in_seconds} sec each (last may be shorter).")

                start_ms = 0
                for count in range(n_chunks):
                    end_ms = min(start_ms + chunk_milliseconds, total_duration)
                    chunk = audio_segment[start_ms:end_ms]
                    out_name = f"{OUTFOLDER}{pid}_{count}.{output_format}"
                    print(f"  Writing {out_name} ({(end_ms - start_ms)/1000:.2f} sec)")
                    chunk.export(out_name, format=output_format)
                    start_ms += chunk_milliseconds

    print("\nDone.")


if __name__ == "__main__":
    main()
