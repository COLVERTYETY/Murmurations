#!/usr/bin/env python3

import sys
import socket
import struct
import time
import numpy as np
import h5py

# This is the IP & port where we'll bind and wait for a connection from the PyQt5 client.
HOST = "0.0.0.0"  # or "127.0.0.1", etc.
PORT = 5000

# Header format: (source=1B), (reserved=1B), (length=2B), (timestamp=8B)
HEADER_FORMAT = "<BBHQ"
HEADER_SIZE   = struct.calcsize(HEADER_FORMAT)

# For chunking large data
CHUNK_SIZE = 16000

def pack_and_send_audio(conn, samples):
    """
    Send an array of audio samples (int16 or uint16) with source=0.
    Splits into CHUNK_SIZE chunks.
    """
    source = 0
    # Ensure samples are a 1D array of 16-bit.
    samples = np.array(samples, dtype=np.uint16).ravel()

    idx = 0
    total = len(samples)
    while idx < total:
        end_idx = min(idx + CHUNK_SIZE, total)
        chunk = samples[idx:end_idx]
        idx = end_idx

        length = len(chunk)
        if length == 0:
            break

        # Timestamp (microseconds)
        ts = int(time.time() * 1e6)

        # Build header (1-byte source, 1-byte reserved=0, 2-byte length, 8-byte timestamp)
        header = struct.pack(HEADER_FORMAT, source, 0, length, ts)

        # Payload: little-endian 16-bit samples
        payload = struct.pack("<" + "H"*length, *chunk)

        conn.sendall(header + payload)
        # print(f"  Sent {length} audio samples (source=0).")
        time.sleep(0.01)  # optional slow-down

def pack_and_send_adc(conn, rec):
    """
    Parse a single ADC record (source=1) and send it over socket `conn`.
    - rec['channels']: string like "ch0:100, ch1:100"
    - rec['data']: array with sum of all channel samples
    We pack each sample as: (channel << 12) | (sample & 0x0FFF).
    Then we send them in CHUNK_SIZE chunks with source=1.
    """
    source = 1
    channels_field = rec['channels']
    if isinstance(channels_field, bytes):
        channels_str = channels_field.decode('utf-8')
    else:
        channels_str = channels_field

    if not channels_str.strip():
        return  # no channel info => skip

    data_arr = np.array(rec['data'], dtype=np.int32)
    idx = 0
    all_samples = []

    # Example channels_str: "ch0:100, ch1:100"
    parts = channels_str.split(',')
    for part in parts:
        part = part.strip()
        ch_str, count_str = part.split(':')
        ch = int(ch_str.replace("ch", ""))
        count = int(count_str)

        # Extract that slice from data_arr
        channel_samples = data_arr[idx : idx + count]
        idx += count

        # Clip to 0..4095 if needed
        channel_samples = np.clip(channel_samples, 0, 4095)

        # Pack each sample into 16 bits => top nibble = channel, bottom 12 bits = sample
        for val in channel_samples:
            s_val = (ch << 12) | (val & 0xFFF)
            all_samples.append(s_val)

    # Convert to np.uint16
    all_samples = np.array(all_samples, dtype=np.uint16)

    # Send in chunks
    total = len(all_samples)
    idx = 0
    while idx < total:
        end_idx = min(idx + CHUNK_SIZE, total)
        chunk = all_samples[idx:end_idx]
        idx = end_idx

        length = len(chunk)
        if length == 0:
            break

        ts = int(time.time() * 1e6)

        header = struct.pack(HEADER_FORMAT, source, 0, length, ts)
        payload = struct.pack("<" + "H"*length, *chunk)

        conn.sendall(header + payload)
        # print(f"  Sent {length} ADC samples (source=1).")
        time.sleep(0.01)  # optional slow-down

def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} file1.h5 [file2.h5 ...]")
        sys.exit(1)

    file_paths = sys.argv[1:]

    # 1) Create a server socket & listen
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_sock:
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind((HOST, PORT))
        server_sock.listen(1)

        print(f"Streamer server listening on {HOST}:{PORT}")
        print("Waiting for a client (PyQt5) to connect...")

        # 2) Accept a connection
        conn, addr = server_sock.accept()
        print(f"Received connection from {addr}")

        with conn:
            # 3) For each provided HDF5 file
            for hdf5_path in file_paths:
                print(f"\n--- Reading HDF5 file: {hdf5_path} ---")
                with h5py.File(hdf5_path, "r") as h5f:
                    # Stream each dataset in the file
                    for ds_name in h5f.keys():
                        print(f"Preparing to stream dataset '{ds_name}'")
                        records = h5f[ds_name][:]

                        # For each record in this dataset
                        for rec_idx, rec in enumerate(records):
                            source = rec['source']
                            if source == 0:
                                # Audio
                                audio_samples = rec['data']
                                pack_and_send_audio(conn, audio_samples)
                            elif source == 1:
                                # ADC
                                pack_and_send_adc(conn, rec)
                            else:
                                # Unknown source, ignore
                                continue

                        print(f"Done streaming dataset '{ds_name}' from '{hdf5_path}'")

        print("All files streamed. Connection closed.")

if __name__ == "__main__":
    main()
