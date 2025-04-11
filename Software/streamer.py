#!/usr/bin/env python3

import sys
import socket
import struct
import time
import numpy as np
import h5py

# This is the IP & port where we'll bind and wait for a connection from the PyQt5 client.
HOST = "0.0.0.0"  # or "127.0.0.1", etc.
PORT = 5001

# Header format: (source=1B), (reserved=1B), (length=2B), (timestamp=8B)
HEADER_FORMAT = "<BBHQ"
HEADER_SIZE   = struct.calcsize(HEADER_FORMAT)

# For chunking large data
CHUNK_SIZE = 16000

# Define the sampling rates
AUDIO_SAMPLING_RATE = 48000  # Hz
ADC_SAMPLING_RATE = 8000     # Hz

def pack_and_send_audio(conn, samples):
    """
    Send an array of audio samples (int16 or uint16) with source=0.
    Splits into CHUNK_SIZE chunks and streams at AUDIO_SAMPLING_RATE.
    """
    source = 0
    # First ensure samples are a numpy array
    samples = np.array(samples, dtype=np.int32).ravel()
    
    # Apply the inverse transformation that matches the receiver's logic
    # In the receiver: sample - 32768 if sample >= 16384 else sample
    # So here we do: sample + 32768 if we want it to be >= 16384 in the output
    transformed_samples = np.array([
        sample + 32768 if sample < 0 else sample
        for sample in samples
    ], dtype=np.uint16)

    idx = 0
    total = len(transformed_samples)
    start_time = time.time()
    
    while idx < total:
        # Calculate how many samples we should send to maintain the correct sampling rate
        end_idx = min(idx + CHUNK_SIZE, total)
        chunk = transformed_samples[idx:end_idx]
        chunk_length = len(chunk)
        if chunk_length == 0:
            break

        # Send the chunk
        ts = int(time.time() * 1e6)
        header = struct.pack(HEADER_FORMAT, source, 0, chunk_length, ts)
        payload = struct.pack("<" + "H"*chunk_length, *chunk)
        conn.sendall(header + payload)
        
        # Calculate the time we should wait to maintain proper sampling rate
        # Each chunk represents (chunk_length / AUDIO_SAMPLING_RATE) seconds of audio
        chunk_duration = chunk_length / AUDIO_SAMPLING_RATE  # seconds
        
        # Calculate elapsed time since we started this chunk
        elapsed = time.time() - start_time
        
        # If we've sent idx+chunk_length samples in less than the expected time,
        # sleep to maintain the correct rate
        expected_time = (idx + chunk_length) / AUDIO_SAMPLING_RATE
        if elapsed < expected_time:
            sleep_time = expected_time - elapsed
            time.sleep(sleep_time)
        
        idx = end_idx

def pack_and_send_adc(conn, rec):
    """
    Parse a single ADC record (source=1) and send it over socket `conn`.
    Streams at ADC_SAMPLING_RATE.
    """
    source = 1
    channels_field = rec['channels']
    if isinstance(channels_field, bytes):
        channels_str = channels_field.decode('utf-8')
    else:
        channels_str = channels_field

    if not channels_str.strip():
        return  # no channel info => skip

    # Important: Read as uint16 to avoid sign extension issues
    data_arr = np.array(rec['data'], dtype=np.uint16)
    
    # Process the ADC channels data
    idx = 0
    all_samples = []
    
    # Parse channel info: "ch0:100, ch1:100"
    parts = channels_str.split(',')
    for part in parts:
        part = part.strip()
        ch_str, count_str = part.split(':')
        ch = int(ch_str.replace("ch", ""))
        count = int(count_str)
        
        # Extract that slice from data_arr
        channel_samples = data_arr[idx:idx + count]
        idx += count
        
        # Clip to 0..4095 if needed
        channel_samples = np.clip(channel_samples, 0, 4095)
        
        # Pack each sample with channel information
        for val in channel_samples:
            s_val = (ch << 12) | (val & 0xFFF)
            all_samples.append(s_val)
    
    # Ensure samples are uint16
    all_samples = np.array(all_samples, dtype=np.uint16)
    
    # Send in chunks, respecting the ADC sampling rate
    total = len(all_samples)
    idx = 0
    start_time = time.time()
    
    while idx < total:
        # Calculate how many samples to send to maintain correct sampling rate
        end_idx = min(idx + CHUNK_SIZE, total)
        chunk = all_samples[idx:end_idx]
        chunk_length = len(chunk)
        if chunk_length == 0:
            break
            
        # Send the chunk
        ts = int(time.time() * 1e6)
        header = struct.pack(HEADER_FORMAT, source, 0, chunk_length, ts)
        payload = struct.pack("<" + "H"*chunk_length, *chunk)
        conn.sendall(header + payload)
        
        # Calculate the time we should wait to maintain proper sampling rate
        # ADC samples represent (chunk_length / ADC_SAMPLING_RATE) seconds of data
        chunk_duration = chunk_length / ADC_SAMPLING_RATE  # seconds
        
        # Calculate elapsed time
        elapsed = time.time() - start_time
        
        # If we've sent idx+chunk_length samples in less than the expected time,
        # sleep to maintain the correct rate
        expected_time = (idx + chunk_length) / ADC_SAMPLING_RATE
        if elapsed < expected_time:
            sleep_time = expected_time - elapsed
            time.sleep(sleep_time)
        
        idx = end_idx

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
        print(f"Audio sampling rate: {AUDIO_SAMPLING_RATE} Hz")
        print(f"ADC sampling rate: {ADC_SAMPLING_RATE} Hz")
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