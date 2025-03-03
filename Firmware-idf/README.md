# FIRMWARE

# Overview

The **Murmurator** firmware uses FreeRTOS to perform multiple tasks concurrently. It captures data from two sources:
- **Microphone (via I2S):** Uses DMA to read data from an I2S-connected microphone.
- **Analog-to-Digital Converter (ADC):** Uses the ADC continuous mode to sample data from two channels.

Both data sources are formatted into a standardized packet and sent over a TCP connection once a client connects.


# Data Format and Data Sources

## Data Packet Structure

Each packet is composed of two parts: a header and a data buffer.

### Packet Header (`packet_header_t`)

- **source (1 byte):**  
  - `0` indicates data from the microphone (I2S).  
  - `1` indicates data from the ADC.
  
- **metadata (1 byte):**  
  - Reserved for additional flags or information (currently set to 0).

- **length (2 bytes):**  
  - Specifies the number of 16-bit samples in the packet.
  
- **timestamp (8 bytes):**  
  - Recorded using `esp_timer_get_time()`, it provides a time marker for the data.

### Data Buffer (`buffer_t`)

- **data:**  
  - An array of 16-bit samples. Its size is determined by the larger of the microphone or ADC buffer sizes.

- **end:**  
  - Indicates how many samples have been stored in this particular packet.

### Complete Message (`msg_t`)

- **header:**  
  - Contains the packet header structure.
  
- **buffer:**  
  - Contains the sample data that corresponds to the header information.



## Data Sources

### Microphone Data (I2S)

- **Task:** `mic_task`
- **How It Works:**  
  - Configures an I2S channel to read raw data from a microphone.
  - Uses DMA to transfer a block of samples into a temporary buffer.
  - Processes the buffer to extract a specified number of 16-bit samples.
  - Packages the data into a `msg_t` structure (with `source` set to `0`) and enqueues it for TCP transmission.

### ADC Data

- **Task:** `adc_task`
- **How It Works:**  
  - Configures the ADC in continuous mode to sample two channels.
  - Uses DMA to read the ADC conversion results into a buffer.
  - Processes each ADC result by combining channel and data values into a 16-bit format.
  - Packages the data into a `msg_t` structure (with `source` set to `1`) and enqueues it for TCP transmission.



# TCP Server & WiFi Operation

- **WiFi Initialization:**  
  - The device is set up in station mode.
  - Uses credentials defined in `secrets.h` to connect to a WiFi network.
  - After connecting, it starts a TCP server on port 5000.

- **TCP Server:**  
  - Listens for an incoming client connection.
  - Once a client is connected, the server continuously sends packets from the outbound queue.

- **Queue System:**  
  - A FreeRTOS queue (`outbound_queue`) is used to manage outgoing messages from both the microphone and ADC tasks.



# Operation Manual

## Setting WiFi Credentials

1. **Locate `secrets.h`:**  
   This file is where you define your WiFi credentials.

2. **Edit the File:**  
   Modify the file to include your network's SSID and password. For example:
   ```c
   #define SSID "YourWiFiSSID"
   #define PWORD "YourWiFiPassword"
   ```
3. **Save and Rebuild:**  
   Save your changes and rebuild the project to ensure the new credentials are used.



## Finding the Device IP Using Minicom

1. **Connect the ESP32:**  
   Attach the ESP32 board to your computer via USB.

2. **Open Minicom:**  
   - Launch Minicom (or any other serial terminal) on your computer.
   - Set the serial port (e.g., `/dev/ttyACM1` or similar) and use the default baud rate (commonly 115200).

3. **Reset/Power Cycle the Device:**  
   - Restart your ESP32 so it outputs the boot and log messages.

4. **Locate the IP Log Message:**  
   - Look for a log message similar to:
     ```
     Device IP: xxx.xxx.xxx.xxx
     ```
   - This IP address is assigned to your device on your local network.

5. **Connect to the TCP Server:**  
   - Use the noted IP address and port 5000 to connect to the TCP server from your client application.


# Additional Information

- **Multiple FreeRTOS Tasks:**  
  The application runs several tasks concurrently, handling WiFi initialization, TCP server operations, microphone data capture, ADC data capture, outbound messaging, and periodic logging.

- **Error Handling:**  
  The code includes error checks (e.g., socket creation, sending data) and logs errors to help diagnose issues during runtime.

- **Timestamping:**  
  Timestamps are added to each packet header to synchronize data with time.

# Network Debugging Tips & Tricks

For effective network debugging, first ensure you have a robust WiFi connection—consider using your host PC as a WiFi hotspot to prevent freeze-ups and data packet drops, which can be detected by monitoring queued messages in the serial monitor (via Minicom). Additionally, you can monitor the TCP connection in real time using netcat with the command:

```
nc -v <device_ip> 5000 | hexdump -C
```

This displays the incoming data in hexadecimal format. Lastly, perform a ping reliability test with:

```
ping -i 0.1 <device_ip>
```

This helps assess connection stability by sending rapid, continuous pings.

