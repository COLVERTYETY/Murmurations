#include <stdio.h>
#include <string.h>
#include <sys/param.h>
#include <sys/unistd.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <errno.h>
// #include <sys/uio.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"

#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "nvs_flash.h"

#include "driver/i2s_std.h"
#include "driver/adc.h"
#include "esp_adc/adc_continuous.h"
#include "esp_timer.h"
// #include "driver/adc_continuous.h"  // ADC continuous mode (requires ESP-IDF v4.3+)

#include "secrets.h" // Must define: #define SSID "MLdev" and #define PWORD "wifi_password"

static const char *TAG = "MURMURATOR";

// --- Microphone (I2S) Settings ---
#define I2S_MIC_SAMPLE_RATE    48000
#define ADC_SAMPLE_RATE        16000
#define MIC_BUFFER_SIZE        256    // number of 16-bit samples in a pa
#define ADC_BUFFER_SIZE        256    // number of raw samples


// --- Packet Header Definition ---
// 1 byte: source (0 = mic, 1 = ADC)
// 1 byte: metadata
// 2 bytes: length (number of 16-bit samples in the packet)
#define SOURCE_MIC 0
#define SOURCE_ADC 1
typedef struct __attribute__((packed)) {
    uint8_t source;
    uint8_t metadata;
    uint16_t length;
    uint64_t timestamp;
} packet_header_t;

typedef struct __attribute__((packed)) {
    int16_t data[MAX(MIC_BUFFER_SIZE, ADC_BUFFER_SIZE)];
    size_t end;
} buffer_t;

typedef struct {
    packet_header_t header;
    buffer_t buffer;
} msg_t;


// --- WiFi & TCP Server Settings ---
#define SERVER_PORT 5000
static int server_socket = -1;
static int client_socket = -1;

QueueHandle_t outbound_queue;

// --- WiFi Initialization (Station Mode) ---
static void wifi_init_sta(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
      ESP_ERROR_CHECK(nvs_flash_erase());
      ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    
    
    wifi_config_t wifi_config = {
        .sta = {
            .ssid = SSID,
            .password = PWORD,
            .threshold.authmode = WIFI_AUTH_WPA2_PSK,
        },
    };
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());
    esp_wifi_set_ps(WIFI_PS_NONE);
    ESP_LOGI(TAG, "WiFi initialization finished. Connecting...");
    ESP_ERROR_CHECK(esp_wifi_connect());
}

// --- TCP Server Task ---
// Creates a listening socket on SERVER_PORT and waits for a client connection.
static void tcp_server_task(void *arg)
{
    struct sockaddr_in server_addr;
    server_socket = socket(AF_INET, SOCK_STREAM, IPPROTO_IP);

    if (server_socket < 0) {
        ESP_LOGE(TAG, "Unable to create socket: errno %d", errno);
        vTaskDelete(NULL);
        return;
    }
    server_addr.sin_family = AF_INET;
    server_addr.sin_addr.s_addr = htonl(INADDR_ANY);
    server_addr.sin_port = htons(SERVER_PORT);
    if (bind(server_socket, (struct sockaddr *)&server_addr, sizeof(server_addr)) < 0) {
        ESP_LOGE(TAG, "Socket unable to bind: errno %d", errno);
        close(server_socket);
        vTaskDelete(NULL);
        return;
    }
    if (listen(server_socket, 1) < 0) {
        ESP_LOGE(TAG, "Error during listen: errno %d", errno);
        close(server_socket);
        vTaskDelete(NULL);
        return;
    }
    ESP_LOGI(TAG, "TCP server listening on port %d", SERVER_PORT);
    while (1) {
        struct sockaddr_in client_addr;
        socklen_t addr_len = sizeof(client_addr);
        client_socket = accept(server_socket, (struct sockaddr *)&client_addr, &addr_len);
        if (client_socket < 0) {
            ESP_LOGE(TAG, "Unable to accept connection: errno %d", errno);
            break;
        }
        ESP_LOGI(TAG, "Client connected.");
        // Keep the client socket open.
        while (client_socket >= 0) {
            vTaskDelay(pdMS_TO_TICKS(100));
        }
    }
    close(server_socket);
    vTaskDelete(NULL);
}

// static void send_msg(msg_t *msg)
// {
//     static int errors = 0;
//     if (client_socket < 0) return;

//     // Prepare iovec structures for the header and data parts.
//     struct iovec iov[2];
//     iov[0].iov_base = (void *)&(msg->header);
//     iov[0].iov_len  = sizeof(packet_header_t);
//     iov[1].iov_base = (void *)&(msg->buffer);
//     iov[1].iov_len  = msg->buffer.end * sizeof(int16_t);

//     // Set up the message header.
//     struct msghdr message;
//     memset(&message, 0, sizeof(message));
//     message.msg_iov    = iov;
//     message.msg_iovlen = 2;

//     // Send both parts in one call.
//     int ret = sendmsg(client_socket, &message, 0);
//     if (ret < 0) {
//         if (errors++ > 10) {
//             close(client_socket);
//             client_socket = -1;
//         }
//         ESP_LOGE("SEND MSG", "Error sending message: errno %d", errno);
//         return;
//     }
//     errors = 0;
// }

static void send_msg(msg_t *msg)
{
    static int errors = 0;
    if (client_socket < 0) return;
    int ret = send(client_socket, &(msg->header), sizeof(packet_header_t), 0);
    if (ret < 0) {
        if (errors++ > 10) {
            close(client_socket);
            client_socket = -1;
            ESP_LOGI(TAG, "Client disconnected.");
        }
        ESP_LOGE("SEND MSG", "Error sending header: errno %d", errno);
        return;
    }
    ret = send(client_socket, &(msg->buffer), msg->buffer.end * sizeof(int16_t), 0);
    if (ret < 0) {
        if (errors++ > 10) {
            close(client_socket);
            client_socket = -1;
            ESP_LOGI(TAG, "Client disconnected.");
        }
        ESP_LOGE("SEND MSG", "Error sending data: errno %d", errno);
        return;
    }
    errors = 0;
}

void OutBoundTask(void *arg){
    outbound_queue = xQueueCreate(256, sizeof(msg_t));
    msg_t sample;
    for(;;) {
        if(xQueueReceive(outbound_queue, &sample, portMAX_DELAY)){
            send_msg(&sample);
        }
    }
    vTaskDelete(NULL);
}


void QI2Smsg(int16_t *buffer, int size) {
    if (client_socket < 0) return;

    int num_samples = size/2;
    assert(num_samples <= MIC_BUFFER_SIZE);
    
    msg_t sample;
    sample.header.source = SOURCE_MIC;
    sample.header.metadata = 0;
    sample.header.length = num_samples;  // Number of 16-bit samples produced
    sample.header.timestamp = esp_timer_get_time();
    sample.buffer.end = num_samples;
    // int skipFirst = (buffer[0] == 0);
    int skipFirst = 1;
    for (int j = 0; j < num_samples; j++) {
        sample.buffer.data[j] = buffer[2 * j + skipFirst];
    }
    xQueueSend(outbound_queue, &sample, portMAX_DELAY);
    // vTaskDelay(pdMS_TO_TICKS(10));
}


void QADCmsg(uint8_t * buffer, int size){
    if (client_socket < 0) return;

    int num_conv = size / SOC_ADC_DIGI_RESULT_BYTES;
    assert(num_conv <= ADC_BUFFER_SIZE);

    msg_t sample;
    sample.header.source = SOURCE_ADC;
    sample.header.metadata = 0;
    sample.header.length = num_conv;
    sample.header.timestamp = esp_timer_get_time();
    sample.buffer.end = num_conv;

    for (int i = 0; i < num_conv; i++) {
        // Each conversion result occupies SOC_ADC_DIGI_RESULT_BYTES (likely 4 bytes for TYPE2).
        adc_digi_output_data_t *p = (adc_digi_output_data_t *)(buffer + i * SOC_ADC_DIGI_RESULT_BYTES);
        uint32_t chan = p->type2.channel;
        uint32_t data = p->type2.data;
        // Format into 16 bits: upper 4 bits for channel, lower 12 bits for ADC data.
        sample.buffer.data[i] = ((chan & 0xF) << 12) | (data & 0x0FFF);
    }
    xQueueSend(outbound_queue, &sample, portMAX_DELAY);
    // vTaskDelay(pdMS_TO_TICKS(10));
}


// --- Microphone Task ---
// Configures I2S to read microphone data using DMA and sends packets when the buffer fills.
static void mic_task(void *arg)
{

    i2s_chan_handle_t rx_handle;
    /* Get the default channel configuration by helper macro.*/
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_1, I2S_ROLE_MASTER);
    /* Allocate a new RX channel and get the handle of this channel */
    i2s_new_channel(&chan_cfg, NULL, &rx_handle);

    /* Setting the configurations, the slot configuration and clock configuration can be generated by the macros*/
    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(I2S_MIC_SAMPLE_RATE),
        .slot_cfg = I2S_STD_MSB_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = GPIO_NUM_9,
            .ws = GPIO_NUM_7,
            .dout = I2S_GPIO_UNUSED,
            .din = GPIO_NUM_8,
            .invert_flags = {
                .mclk_inv = false,
                .bclk_inv = false,
                .ws_inv = false,
            },
        },
    };

    /* Initialize the channel */
    i2s_channel_init_std_mode(rx_handle, &std_cfg);

    /* Before reading data, start the RX channel first */
    i2s_channel_enable(rx_handle);

    gpio_set_direction(GPIO_NUM_44, GPIO_MODE_OUTPUT);
    gpio_set_level(GPIO_NUM_44, 0);

    size_t bytes_read = 0;
    // Temporary buffer for raw I2S data (each sample is 32 bits)
    int16_t i2s_read_buf[MIC_BUFFER_SIZE*2];
    while (1) {
        esp_err_t ret = i2s_channel_read(rx_handle, i2s_read_buf, sizeof(i2s_read_buf), &bytes_read, portMAX_DELAY);
        if (ret == ESP_OK && bytes_read > 0) {
            int samples = bytes_read / sizeof(int16_t);

            QI2Smsg(i2s_read_buf, samples);
        }
    }
    vTaskDelete(NULL);
}

// --- ADC Task ---
// Configures the ADC continuous driver to sample two channels using DMA.
// The ADC data (in TYPE2 format) is read into a buffer and sent as a packet.
static void adc_task(void *arg)
{
    adc_continuous_handle_t adc_handle = NULL;
    adc_continuous_handle_cfg_t adc_config = {
        .max_store_buf_size = ADC_BUFFER_SIZE*SOC_ADC_DIGI_RESULT_BYTES*4,
        .conv_frame_size = ADC_BUFFER_SIZE*SOC_ADC_DIGI_RESULT_BYTES,
    };
    ESP_ERROR_CHECK(adc_continuous_new_handle(&adc_config, &adc_handle));

    // Configure ADC continuous mode for two channels.
    adc_continuous_config_t adc_cont_config = {
        .pattern_num = 2,
        .sample_freq_hz = ADC_SAMPLE_RATE,  // ADC sampling frequency in Hz  // SOC_ADC_SAMPLE_FREQ_THRES_HIGH
        .conv_mode = ADC_CONV_SINGLE_UNIT_1,  // Using ADC1
        .format = ADC_DIGI_OUTPUT_FORMAT_TYPE2,
    };
    adc_digi_pattern_config_t adc_pattern[2] = {
        {
            .atten = ADC_ATTEN_DB_0,
            .channel = ADC1_CHANNEL_2,
            .unit = ADC_UNIT_1,
            .bit_width = SOC_ADC_DIGI_MIN_BITWIDTH,
        },
        {
            .atten = ADC_ATTEN_DB_0,
            .channel = ADC1_CHANNEL_3,
            .unit = ADC_UNIT_1,
            .bit_width = SOC_ADC_DIGI_MIN_BITWIDTH,
        },
    };
    adc_cont_config.adc_pattern = adc_pattern;
    
    ESP_ERROR_CHECK(adc_continuous_config(adc_handle, &adc_cont_config));
    ESP_ERROR_CHECK(adc_continuous_start(adc_handle));
    
    uint8_t adc_dma_buf[ADC_BUFFER_SIZE*SOC_ADC_DIGI_RESULT_BYTES];
    uint32_t adc_bytes_read = 0;
    while (1) {
        esp_err_t ret = adc_continuous_read(adc_handle, adc_dma_buf, sizeof(adc_dma_buf), &adc_bytes_read, pdMS_TO_TICKS(1000));
        if (ret == ESP_OK && adc_bytes_read > 0) {
            QADCmsg(adc_dma_buf, adc_bytes_read);
        }
    }
    vTaskDelete(NULL);
}

void periodiclogger(void *arg)
{
    while (1) {
        esp_netif_ip_info_t ip_info;
        esp_netif_t *netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
        if (netif) {
            esp_netif_get_ip_info(netif, &ip_info);
            ESP_LOGI(TAG, "Device IP: " IPSTR, IP2STR(&ip_info.ip));
        } else {
            ESP_LOGI(TAG, "Failed to get network interface");
        }

        int outBoundmsgs = uxQueueMessagesWaiting(outbound_queue);
        if (outBoundmsgs > 0 ){
            ESP_LOGI(TAG, "Outbound messages in queue: %d", outBoundmsgs);
        }
        vTaskDelay(pdMS_TO_TICKS(3000));
    }
}

void app_main(void)
{
    ESP_LOGI(TAG, "Starting streaming application");
    wifi_init_sta();
    
    // Create the TCP server task.
    xTaskCreate(tcp_server_task, "tcp_server", 4096, NULL, 5, NULL);
    xTaskCreate(OutBoundTask, "outBound", 4096*4, NULL, 7, NULL);
    // Create periodic logger task.
    xTaskCreate(periodiclogger, "periodiclogger", 4096, NULL, 1, NULL);
    // Create microphone task.
    xTaskCreate(mic_task, "mic_task", 4096*2, NULL, 5, NULL);
    // Create ADC task.
    xTaskCreate(adc_task, "adc_task", 4096*2, NULL, 5, NULL);
    ESP_LOGI(TAG, "Application started");
}
