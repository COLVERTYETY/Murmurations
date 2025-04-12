#!/usr/bin/env python3

import sys
import socket
import struct
import time
import numpy as np
import h5py
import argparse
from collections import defaultdict
from threading import Thread, Event

# This is the IP & port where we'll bind and wait for a connection
HOST = "0.0.0.0"  # Listen on all interfaces
PORT = 5000

# Header format: (source=1B), (reserved=1B), (length=2B), (timestamp=8B)
HEADER_FORMAT = "<BBHQ"
HEADER_SIZE   = struct.calcsize(HEADER_FORMAT)

# For chunking large data
CHUNK_SIZE = 16000

# Define the sampling rates and batch sizes to match ESP32
AUDIO_SAMPLING_RATE = 48000   # Hz
ADC_SAMPLING_RATE = 8000      # Hz (total rate for all channels)
ADC_CHANNELS = 2              # Number of ADC channels being sampled
ADC_PER_CHANNEL_RATE = ADC_SAMPLING_RATE / ADC_CHANNELS  # Rate per channel
AUDIO_BATCH_SIZE = 256        # Samples per batch, matching ESP32
ADC_BATCH_SIZE = 256          # Samples per batch, matching ESP32

# Event to signal threads to stop
stop_event = Event()

# Queue for data to be sent
audio_buffer = []
adc_buffers = defaultdict(list)  # channel -> samples

def send_audio_thread(conn):
    """Thread that continuously sends audio data at the correct rate"""
    print(f"Audio thread started, sending at {AUDIO_SAMPLING_RATE} Hz with batch size {AUDIO_BATCH_SIZE}")
    
    # Calculate exact sleep times
    # In ESP32 code, each audio batch of 256 samples should take 256/48000 = 5.33ms
    batch_time = AUDIO_BATCH_SIZE / AUDIO_SAMPLING_RATE
    print(f"Audio batch time: {batch_time*1000:.2f}ms")
    
    last_send_time = time.time()
    
    while not stop_event.is_set():
        if audio_buffer:
            # Get a batch of samples
            if len(audio_buffer) >= AUDIO_BATCH_SIZE:
                current_time = time.time()
                elapsed = current_time - last_send_time
                
                # If we're sending too fast, wait until the proper interval has passed
                if elapsed < batch_time:
                    time.sleep(batch_time - elapsed)
                
                # Take a chunk from the buffer
                samples = audio_buffer[:AUDIO_BATCH_SIZE]
                audio_buffer[:AUDIO_BATCH_SIZE] = []
                
                # Send the samples
                try:
                    source = 0  # Audio source
                    ts = int(time.time() * 1e6)
                    length = len(samples)
                    
                    header = struct.pack(HEADER_FORMAT, source, 0, length, ts)
                    payload = struct.pack("<" + "H"*length, *samples)
                    
                    conn.sendall(header + payload)
                    
                    last_send_time = time.time()
                except Exception as e:
                    print(f"Error sending audio: {e}")
                    break
            else:
                # Not enough samples yet, sleep a bit
                time.sleep(0.001)
        else:
            # Buffer empty, sleep a bit
            time.sleep(0.01)

def send_adc_thread(conn):
    """Thread that continuously sends ADC data at the correct rate"""
    print(f"ADC thread started, total ADC rate: {ADC_SAMPLING_RATE} Hz")
    print(f"Rate per channel: {ADC_PER_CHANNEL_RATE} Hz with batch size {ADC_BATCH_SIZE}")
    
    # Calculate exact sleep times
    # For ESP32 with 2 ADC channels, each channel effectively runs at 4kHz (8kHz/2)
    # So each batch of 256 samples should take 256/4000 = 64ms per channel
    batch_time = ADC_BATCH_SIZE / ADC_PER_CHANNEL_RATE
    print(f"ADC batch time per channel: {batch_time*1000:.2f}ms")
    
    last_send_time = time.time()
    
    while not stop_event.is_set():
        # Check if we have data for any channels
        has_data = False
        for ch in adc_buffers:
            if len(adc_buffers[ch]) >= ADC_BATCH_SIZE:
                has_data = True
                break
        
        if has_data:
            current_time = time.time()
            elapsed = current_time - last_send_time
            
            # If we're sending too fast, wait until the proper interval has passed
            if elapsed < batch_time:
                time.sleep(batch_time - elapsed)
            
            # Collect samples from all channels
            all_samples = []
            channels_with_data = []
            
            for ch in sorted(adc_buffers.keys()):
                if len(adc_buffers[ch]) >= ADC_BATCH_SIZE:
                    # Take samples from this channel
                    samples = adc_buffers[ch][:ADC_BATCH_SIZE]
                    adc_buffers[ch][:ADC_BATCH_SIZE] = []
                    
                    # Pack each sample with channel information
                    for val in samples:
                        s_val = (ch << 12) | (val & 0xFFF)
                        all_samples.append(s_val)
                    
                    channels_with_data.append(ch)
            
            # Send if we have samples
            if all_samples:
                try:
                    source = 1  # ADC source
                    ts = int(time.time() * 1e6)
                    length = len(all_samples)
                    
                    header = struct.pack(HEADER_FORMAT, source, 0, length, ts)
                    payload = struct.pack("<" + "H"*length, *all_samples)
                    
                    conn.sendall(header + payload)
                    
                    last_send_time = time.time()
                    
                except Exception as e:
                    print(f"Error sending ADC: {e}")
                    break
        else:
            # Not enough samples yet, sleep a bit
            time.sleep(0.01)

def load_h5_file(file_path):
    """Load data from an H5 file into the audio and ADC buffers"""
    with h5py.File(file_path, "r") as h5f:
        # Process each dataset in the file
        for ds_name in h5f.keys():
            print(f"Processing dataset '{ds_name}' from '{file_path}'")
            records = h5f[ds_name][:]

            # For each record in this dataset
            for rec_idx, rec in enumerate(records):
                source = rec['source']
                if source == 0:
                    # Audio: transform the samples to match the expected format
                    audio_samples = np.array(rec['data'], dtype=np.int32)
                    transformed_samples = np.array([
                        sample + 32768 if sample < 0 else sample
                        for sample in audio_samples
                    ], dtype=np.uint16)
                    
                    # Add to audio buffer
                    audio_buffer.extend(transformed_samples)
                    
                elif source == 1:
                    # ADC: parse channel info and separate data by channel
                    channels_str = rec['channels']
                    if isinstance(channels_str, bytes):
                        channels_str = channels_str.decode('utf-8')
                    
                    if channels_str.strip():
                        data_arr = np.array(rec['data'], dtype=np.uint16)
                        idx = 0
                        
                        parts = channels_str.split(',')
                        for part in parts:
                            part = part.strip()
                            ch_str, count_str = part.split(':')
                            ch = int(ch_str.replace("ch", ""))
                            count = int(count_str)
                            
                            # Extract samples for this channel
                            channel_samples = data_arr[idx:idx + count] & 0xFFF  # Ensure 12-bit values
                            adc_buffers[ch].extend(channel_samples)
                            idx += count
            
            print(f"Loaded {len(audio_buffer)} audio samples and ADC data for {len(adc_buffers)} channels")

def main():
    parser = argparse.ArgumentParser(description='Stream data from HDF5 files to clients')
    parser.add_argument('files', metavar='file', type=str, nargs='+',
                        help='HDF5 files to stream')
    parser.add_argument('--host', type=str, default=HOST,
                        help=f'Host to bind to (default: {HOST})')
    parser.add_argument('--port', type=int, default=PORT,
                        help=f'Port to bind to (default: {PORT})')
    parser.add_argument('--loop', action='store_true',
                        help='Loop playback when finished')
    
    args = parser.parse_args()
    
    # Load data from all files
    for file_path in args.files:
        try:
            load_h5_file(file_path)
        except Exception as e:
            print(f"Error loading {file_path}: {e}")
    
    if not audio_buffer and not adc_buffers:
        print("No data loaded from files. Exiting.")
        return
    
    # Create a server socket & listen for connections
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_sock:
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind((args.host, args.port))
        server_sock.listen(1)
        
        print(f"Server listening on {args.host}:{args.port}")
        print("Waiting for a client to connect...")
        
        try:
            while True:
                # Accept a connection
                conn, addr = server_sock.accept()
                print(f"Received connection from {addr}")
                
                with conn:
                    # Start sender threads
                    audio_thread = Thread(target=send_audio_thread, args=(conn,))
                    adc_thread = Thread(target=send_adc_thread, args=(conn,))
                    
                    audio_thread.start()
                    adc_thread.start()
                    
                    # Wait for threads to finish or user interrupt
                    try:
                        # Main thread just waits until data is exhausted
                        while (audio_buffer or any(adc_buffers.values())) and not stop_event.is_set():
                            time.sleep(0.1)
                        
                        if args.loop and not stop_event.is_set():
                            print("End of data reached. Reloading files due to --loop option.")
                            # Reload data and continue
                            for file_path in args.files:
                                try:
                                    load_h5_file(file_path)
                                except Exception as e:
                                    print(f"Error reloading {file_path}: {e}")
                    
                    except KeyboardInterrupt:
                        print("Interrupted by user. Stopping...")
                        stop_event.set()
                    
                    # Wait for threads to finish
                    audio_thread.join()
                    adc_thread.join()
                    
                    print(f"Connection to {addr} closed.")
                
                # After connection is closed, check if we should exit
                if stop_event.is_set():
                    break
                
                # Reset stop event for next connection
                stop_event.clear()
                
                # If we're not looping and all data is sent, exit
                if not args.loop and not audio_buffer and not any(adc_buffers.values()):
                    print("All data sent. Exiting.")
                    break
                
                # If looping or there's still data, reload if needed and wait for next connection
                if args.loop and not audio_buffer and not any(adc_buffers.values()):
                    print("Reloading files for next connection...")
                    for file_path in args.files:
                        try:
                            load_h5_file(file_path)
                        except Exception as e:
                            print(f"Error reloading {file_path}: {e}")
                
                print("Waiting for next client connection...")
        
        except KeyboardInterrupt:
            print("Server interrupted by user. Shutting down.")
            stop_event.set()

if __name__ == "__main__":
    main()