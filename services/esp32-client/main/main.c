/**
 * ESP32-S3 Voice Pipeline Client
 *
 * Hardware:
 *   INMP441 I2S mic  : WS=GPIO4, SCK=GPIO5, SD=GPIO6
 *   MAX98357A amp    : DIN=GPIO7, BCLK=GPIO15, LRC=GPIO16
 *   SSD1306 display  : SDA=GPIO41, SCL=GPIO42  (optional)
 *
 * Data flow:
 *   I2S mic → 16-bit PCM 16kHz → WebSocket (binary) → VAD service
 *   VAD / ASR result (JSON text frame) → serial log + display
 *   LLM reply TTS URL (JSON) → HTTP download → I2S speaker playback
 */

#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <math.h>
#ifndef MIN
#define MIN(a,b) ((a)<(b)?(a):(b))
#endif
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "freertos/queue.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_system.h"
#include "nvs_flash.h"
#include "esp_netif.h"
#include "esp_http_client.h"
#include "driver/i2s_std.h"
#include "esp_websocket_client.h"
#include "cJSON.h"

#include "wifi_config.h"
#include "ssd1306.h"

/* ─── logging tag ──────────────────────────────────────────────── */
#define TAG "VOICE_CLIENT"

/* ─── I2S mic pins (INMP441) ────────────────────────────────────── */
#define I2S_WS_IO    GPIO_NUM_4
#define I2S_SCK_IO   GPIO_NUM_5
#define I2S_SD_IO    GPIO_NUM_6

/* ─── I2S speaker pins (MAX98357A) ──────────────────────────────── */
#define I2S_TX_BCLK  GPIO_NUM_15
#define I2S_TX_WS    GPIO_NUM_16
#define I2S_TX_DO    GPIO_NUM_7

/* ─── audio parameters ───────────────────────────────────────────── */
#define SAMPLE_RATE      16000
#define CHUNK_SAMPLES    512
#define CHUNK_BYTES      (CHUNK_SAMPLES * 2)
#define I2S_RAW_BYTES    (CHUNK_SAMPLES * 8)
#define TTS_SAMPLE_RATE  32000           /* TTS WAV output rate */
#define TTS_URL_MAX_LEN  256
#define TTS_BUF_MAX      (512 * 1024)    /* 512 KB max TTS audio in PSRAM */

/* ─── display ────────────────────────────────────────────────────── */
#define DISP_SDA  GPIO_NUM_41
#define DISP_SCL  GPIO_NUM_42
static bool s_display_ok = false;

/* ─── state ──────────────────────────────────────────────────────── */
static EventGroupHandle_t s_wifi_eg;
#define WIFI_CONNECTED_BIT BIT0
#define WIFI_FAIL_BIT      BIT1

static esp_websocket_client_handle_t s_ws_client       = NULL;
static i2s_chan_handle_t             s_tx_handle        = NULL;
static QueueHandle_t                 s_tts_url_queue    = NULL;
static volatile bool  s_ws_connected   = false;
static volatile bool  s_tts_playing    = false;
static volatile int   s_recog_count    = 0;
static char           s_last_text[128]         = "";
static char           s_last_speaker[64]       = "";
static char           s_last_speaker_score[32] = "";

/* ─── display helpers ─────────────────────────────────────────────── */

/* Count the number of UTF-8 code-points (characters) in a string.
 * Skips continuation bytes (0x80-0xBF) so multi-byte sequences count as 1. */
static int utf8_charcount(const char *s) {
    int n = 0;
    while (*s) {
        if ((*s & 0xC0) != 0x80) n++;   /* leading byte of any code-point */
        s++;
    }
    return n;
}

static void disp_update(void) {
    if (!s_display_ok) return;
    ssd1306_clear();

    /* Row 0: device name */
    ssd1306_puts(0, 0, DEVICE_NAME);

    /* Row 1: WebSocket connection status */
    ssd1306_puts(0, 1, s_ws_connected ? "WS: Connected   " : "WS: Connecting..");

    /* Row 2: recognition count + char count of last text */
    int nchar = utf8_charcount(s_last_text);
    ssd1306_printf(0, 2, "N:%-4d Len:%-4d", s_recog_count, nchar);

    /* Row 3: TTS playing indicator OR speaker match + score */
    int has_spk = (s_last_speaker[0] != '\0');
    if (s_tts_playing) {
        ssd1306_puts(0, 3, "TTS: Playing... ");
    } else if (has_spk) {
        ssd1306_printf(0, 3, "Spk:Y %s", s_last_speaker_score);
    } else {
        ssd1306_puts(0, 3, "Spk:N          ");
    }
    ssd1306_flush();
}

/* ─── WAV header parser ───────────────────────────────────────────── */
/* Scan the first bytes of a WAV file to find the PCM data chunk offset.
 * Standard WAV has 44 bytes header, but TTS service adds a LIST chunk → 78 bytes. */
static int find_wav_data_offset(const uint8_t *buf, int len) {
    int i = 12;  /* skip RIFF header */
    while (i < len - 8) {
        if (buf[i]=='d' && buf[i+1]=='a' && buf[i+2]=='t' && buf[i+3]=='a') {
            return i + 8;  /* skip 'data' id + size field */
        }
        uint32_t chunk_size = (uint32_t)buf[i+4] | ((uint32_t)buf[i+5]<<8)
                            | ((uint32_t)buf[i+6]<<16) | ((uint32_t)buf[i+7]<<24);
        i += 8 + (int)(chunk_size & ~1u);  /* pad to even */
    }
    return 44;  /* fallback to standard header */
}

/* ─── I2S TX (MAX98357A speaker) init ────────────────────────────── */
static void init_i2s_tx(void) {
    i2s_chan_config_t tx_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_1, I2S_ROLE_MASTER);
    tx_cfg.dma_desc_num  = 8;
    tx_cfg.dma_frame_num = 512;
    ESP_ERROR_CHECK(i2s_new_channel(&tx_cfg, &s_tx_handle, NULL));

    i2s_std_config_t tx_std = {
        .clk_cfg  = I2S_STD_CLK_DEFAULT_CONFIG(TTS_SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(
                        I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = I2S_TX_BCLK,
            .ws   = I2S_TX_WS,
            .dout = I2S_TX_DO,
            .din  = I2S_GPIO_UNUSED,
            .invert_flags = { .mclk_inv = false, .bclk_inv = false, .ws_inv = false },
        },
    };
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_tx_handle, &tx_std));
    ESP_ERROR_CHECK(i2s_channel_enable(s_tx_handle));
    ESP_LOGI(TAG, "✅ I2S TX (MAX98357A) 已初始化 (%d Hz mono)", TTS_SAMPLE_RATE);
}

/* ─── TTS playback task ──────────────────────────────────────────── */
static void tts_play_task(void *arg) {
    char url[TTS_URL_MAX_LEN];

    for (;;) {
        if (xQueueReceive(s_tts_url_queue, url, portMAX_DELAY) != pdTRUE) continue;

        ESP_LOGI(TAG, "TTS: 开始下载 %s", url);
        s_tts_playing = true;
        disp_update();

        /* Use PSRAM for audio buffer (ESP32-S3 N16R8 has 8 MB PSRAM) */
        uint8_t *audio_buf = heap_caps_malloc(TTS_BUF_MAX, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (!audio_buf) {
            /* Fallback to internal RAM with smaller buffer */
            audio_buf = malloc(64 * 1024);
        }
        if (!audio_buf) {
            ESP_LOGE(TAG, "TTS: 内存不足");
            s_tts_playing = false;
            disp_update();
            continue;
        }

        esp_http_client_config_t cfg = {
            .url        = url,
            .timeout_ms = 15000,
        };
        esp_http_client_handle_t client = esp_http_client_init(&cfg);
        esp_err_t err = esp_http_client_open(client, 0);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "TTS: HTTP open 失败: %d", err);
            free(audio_buf);
            esp_http_client_cleanup(client);
            s_tts_playing = false;
            disp_update();
            continue;
        }

        esp_http_client_fetch_headers(client);

        int total = 0;
        int max_bytes = TTS_BUF_MAX;
        int n;
        while ((n = esp_http_client_read(client, (char *)audio_buf + total,
                                         MIN(1024, max_bytes - total))) > 0) {
            total += n;
            if (total >= max_bytes) break;
        }
        esp_http_client_close(client);
        esp_http_client_cleanup(client);
        ESP_LOGI(TAG, "TTS: 下载完成 %d 字节", total);

        if (total > 80) {
            /* Skip WAV header to reach raw PCM data */
            int data_offset = find_wav_data_offset(audio_buf, MIN(total, 256));
            int pcm_len = total - data_offset;
            if (pcm_len > 0) {
                size_t written;
                i2s_channel_write(s_tx_handle, audio_buf + data_offset, pcm_len,
                                  &written, pdMS_TO_TICKS(30000));
                ESP_LOGI(TAG, "TTS: 播放完成 (%d/%d 字节写入 I2S)", (int)written, pcm_len);

                /* Flush DMA ring buffers with silence to prevent last PCM block
                 * from repeating after playback ends.
                 * Size = dma_desc_num(8) * dma_frame_num(512) * 2 bytes = 8192 bytes.
                 * Reuse audio_buf (still allocated) — safe because WAV data is done. */
                memset(audio_buf, 0, 8192);
                i2s_channel_write(s_tx_handle, audio_buf, 8192,
                                  &written, pdMS_TO_TICKS(1000));
                ESP_LOGD(TAG, "TTS: DMA 静音冲洗完成");
            }
        }

        free(audio_buf);
        s_tts_playing = false;
        disp_update();
    }
    vTaskDelete(NULL);
}

/* ─── WebSocket event handler ─────────────────────────────────────── */
static void ws_event_handler(void *arg, esp_event_base_t base,
                              int32_t event_id, void *event_data) {
    esp_websocket_event_data_t *data = (esp_websocket_event_data_t *)event_data;
    switch (event_id) {
        case WEBSOCKET_EVENT_CONNECTED:
            ESP_LOGI(TAG, "WebSocket 已连接: %s", VAD_WS_URL);
            s_ws_connected = true;
            disp_update();
            break;

        case WEBSOCKET_EVENT_DISCONNECTED:
            ESP_LOGW(TAG, "WebSocket 已断开，5s 后重连...");
            s_ws_connected = false;
            disp_update();
            break;

        case WEBSOCKET_EVENT_DATA:
            /* log every data event at INFO level for debugging */
            ESP_LOGI(TAG, "WS DATA rcv: op=0x%02x len=%d payload_len=%d offset=%d",
                     data->op_code, data->data_len, data->payload_len, data->payload_offset);

            /* Accept any frame that looks like JSON (starts with '{') */
            if (data->data_len > 2 && data->data_ptr != NULL && data->data_ptr[0] == '{') {
                char *json_str = malloc(data->data_len + 1);
                if (!json_str) break;
                memcpy(json_str, data->data_ptr, data->data_len);
                json_str[data->data_len] = '\0';

                cJSON *root = cJSON_Parse(json_str);
                if (root) {
                    cJSON *text    = cJSON_GetObjectItem(root, "text");
                    cJSON *speaker = cJSON_GetObjectItem(root, "speaker");
                    cJSON *spk_id  = speaker ? cJSON_GetObjectItem(speaker, "id") : NULL;
                    cJSON *score   = speaker ? cJSON_GetObjectItem(speaker, "score") : NULL;
                    cJSON *type    = cJSON_GetObjectItem(root, "type");

                    /* TTS playback command from LLM service */
                    if (type && cJSON_IsString(type) &&
                        strcmp(type->valuestring, "tts_url") == 0) {
                        cJSON *url = cJSON_GetObjectItem(root, "url");
                        if (url && cJSON_IsString(url) && s_tts_url_queue) {
                            char url_buf[TTS_URL_MAX_LEN];
                            strncpy(url_buf, url->valuestring, sizeof(url_buf) - 1);
                            url_buf[sizeof(url_buf) - 1] = '\0';
                            if (xQueueSend(s_tts_url_queue, url_buf, 0) == pdTRUE) {
                                ESP_LOGI(TAG, "TTS URL 入队: %s", url_buf);
                            } else {
                                ESP_LOGW(TAG, "TTS 队列满，丢弃");
                            }
                        }
                        cJSON_Delete(root);
                        free(json_str);
                        break;
                    }

                    if (text && cJSON_IsString(text)) {
                        strncpy(s_last_text, text->valuestring, sizeof(s_last_text) - 1);
                        s_recog_count++;
                    }
                    if (spk_id && cJSON_IsString(spk_id)) {
                        strncpy(s_last_speaker, spk_id->valuestring, sizeof(s_last_speaker) - 1);
                        if (score && cJSON_IsNumber(score)) {
                            snprintf(s_last_speaker_score, sizeof(s_last_speaker_score),
                                     "Score:%.2f", score->valuedouble);
                        } else {
                            strncpy(s_last_speaker_score, "Score:--", sizeof(s_last_speaker_score) - 1);
                        }
                    } else {
                        s_last_speaker[0] = '\0';
                        s_last_speaker_score[0] = '\0';
                    }

                    ESP_LOGI(TAG, "=== 识别结果 [%d] ===", s_recog_count);
                    ESP_LOGI(TAG, "  文本: %s", s_last_text);
                    ESP_LOGI(TAG, "  说话人: %s", s_last_speaker);
                    cJSON_Delete(root);
                    disp_update();
                } else {
                    ESP_LOGW(TAG, "非JSON数据(len=%d): %.80s", data->data_len, json_str);
                }
                free(json_str);
            } else if (data->op_code == 0x01 && data->data_len > 0) {
                ESP_LOGW(TAG, "文本帧: %.*s", data->data_len, data->data_ptr);
            }
            break;

        case WEBSOCKET_EVENT_ERROR:
            ESP_LOGE(TAG, "WebSocket 错误");
            break;

        default:
            break;
    }
}

/* ─── WiFi event handler ──────────────────────────────────────────── */
static int s_wifi_retry = 0;
#define WIFI_MAX_RETRY 10

static void wifi_event_handler(void *arg, esp_event_base_t base,
                                int32_t event_id, void *event_data) {
    if (base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        if (s_wifi_retry < WIFI_MAX_RETRY) {
            esp_wifi_connect();
            s_wifi_retry++;
            ESP_LOGW(TAG, "WiFi 重连 (%d/%d)...", s_wifi_retry, WIFI_MAX_RETRY);
        } else {
            xEventGroupSetBits(s_wifi_eg, WIFI_FAIL_BIT);
            ESP_LOGE(TAG, "WiFi 连接失败");
        }
    } else if (base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *evt = (ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG, "✅ WiFi 已连接, IP: " IPSTR, IP2STR(&evt->ip_info.ip));
        s_wifi_retry = 0;
        xEventGroupSetBits(s_wifi_eg, WIFI_CONNECTED_BIT);
    }
}

static bool wifi_connect(void) {
    s_wifi_eg = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    esp_event_handler_instance_t inst_any, inst_got_ip;
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL, &inst_any));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL, &inst_got_ip));

    wifi_config_t wcfg = {
        .sta = {
            .ssid     = WIFI_SSID,
            .password = WIFI_PASS,
            .threshold.authmode = WIFI_AUTH_WPA2_PSK,
        },
    };
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wcfg));
    ESP_ERROR_CHECK(esp_wifi_start());

    ESP_LOGI(TAG, "正在连接 WiFi: %s ...", WIFI_SSID);
    EventBits_t bits = xEventGroupWaitBits(s_wifi_eg,
                                           WIFI_CONNECTED_BIT | WIFI_FAIL_BIT,
                                           pdFALSE, pdFALSE,
                                           pdMS_TO_TICKS(20000));
    return (bits & WIFI_CONNECTED_BIT) != 0;
}

/* ─── audio task: I2S → WebSocket ────────────────────────────────── */
static void audio_task(void *arg) {
    i2s_chan_handle_t rx_handle;

    /* Create I2S channel */
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    chan_cfg.dma_desc_num  = 8;
    chan_cfg.dma_frame_num = CHUNK_SAMPLES;
    ESP_ERROR_CHECK(i2s_new_channel(&chan_cfg, NULL, &rx_handle));

    /* Configure standard mode (INMP441 = Philips I2S) */
    i2s_std_config_t std_cfg = {
        .clk_cfg  = I2S_STD_CLK_DEFAULT_CONFIG(SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(
                        I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = I2S_SCK_IO,
            .ws   = I2S_WS_IO,
            .dout = I2S_GPIO_UNUSED,
            .din  = I2S_SD_IO,
            .invert_flags = { .mclk_inv = false, .bclk_inv = false, .ws_inv = false },
        },
    };
    /* INMP441: L/R=GND → left channel; read STEREO to see both channels */
    /* Note: with STEREO, raw_buf has interleaved L,R samples */
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(rx_handle, &std_cfg));
    ESP_ERROR_CHECK(i2s_channel_enable(rx_handle));

    ESP_LOGI(TAG, "✅ I2S 麦克风已初始化 (16kHz mono 32-bit → 16-bit)");

    int32_t  *raw_buf = heap_caps_malloc(I2S_RAW_BYTES, MALLOC_CAP_DMA);
    int16_t  *pcm_buf = malloc(CHUNK_BYTES);
    assert(raw_buf && pcm_buf);

    static int audio_chunk_count = 0;
    while (1) {
        size_t bytes_read = 0;
        esp_err_t ret = i2s_channel_read(rx_handle, raw_buf,
                                         I2S_RAW_BYTES, &bytes_read,
                                         pdMS_TO_TICKS(500));
        if (ret != ESP_OK || bytes_read == 0) continue;

        int n_stereo = bytes_read / 4;  /* number of 32-bit words (2 per sample-pair) */
        int n = n_stereo / 2;           /* mono samples (left channel) */
        int64_t sum_sq_l = 0, sum_sq_r = 0;
        for (int i = 0; i < n; i++) {
            /* INMP441 outputs left-justified 24-bit audio in bits[31:8] of a 32-bit
             * I2S frame, i.e. raw_32 = audio_24bit << 8.
             * Shift right 8 to recover the 24-bit value, then clamp to int16 range.
             * This gives ~256× more amplitude vs >>16 — critical for VAD detection. */
            int32_t l32 = raw_buf[i * 2]     >> 8;
            int32_t r32 = raw_buf[i * 2 + 1] >> 8;
            if (l32 >  32767) l32 =  32767;
            if (l32 < -32768) l32 = -32768;
            if (r32 >  32767) r32 =  32767;
            if (r32 < -32768) r32 = -32768;
            int16_t l = (int16_t)l32;
            int16_t r = (int16_t)r32;
            pcm_buf[i] = l;  /* send left channel to VAD */
            sum_sq_l += (int64_t)l * l;
            sum_sq_r += (int64_t)r * r;
        }

        /* Log audio level every 50 chunks (~1.6 s) to verify mic is working */
        audio_chunk_count++;
        if (audio_chunk_count % 50 == 0) {
            int rms_l = (n > 0) ? (int)sqrt((double)sum_sq_l / n) : 0;
            int rms_r = (n > 0) ? (int)sqrt((double)sum_sq_r / n) : 0;
            /* Also print first 4 raw 32-bit values to verify bit alignment */
            ESP_LOGI(TAG, "[Audio] chunk=%d  L_RMS=%d  R_RMS=%d  raw[0]=0x%08lx raw[1]=0x%08lx  %s",
                     audio_chunk_count, rms_l, rms_r,
                     (unsigned long)raw_buf[0], (unsigned long)raw_buf[1],
                     (rms_l < 50 && rms_r < 50) ? "(silence)" :
                     (rms_l > 100 || rms_r > 100) ? "(active)"  : "(low)");
        }

        if (s_ws_connected && s_ws_client && !s_tts_playing) {
            int sent = esp_websocket_client_send_bin(
                s_ws_client, (const char *)pcm_buf, n * 2,
                pdMS_TO_TICKS(200));
            if (sent < 0) {
                ESP_LOGW(TAG, "WebSocket 发送失败");
            }
        }
    }
    /* unreachable */
    free(raw_buf);
    free(pcm_buf);
    vTaskDelete(NULL);
}

/* ─── app_main ────────────────────────────────────────────────────── */
void app_main(void) {
    ESP_LOGI(TAG, "=========================================");
    ESP_LOGI(TAG, "  ESP32-S3 Voice Pipeline Client v1.0");
    ESP_LOGI(TAG, "=========================================");

    /* NVS */
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    /* Optional SSD1306 display (gracefully skip if not present) */
    if (ssd1306_init(I2C_NUM_0, DISP_SDA, DISP_SCL, 0x3C) == ESP_OK) {
        s_display_ok = true;
        ssd1306_puts(0, 0, DEVICE_NAME);
        ssd1306_puts(0, 1, "Connecting WiFi ");
        ssd1306_flush();
        ESP_LOGI(TAG, "✅ SSD1306 显示屏已初始化");
    } else {
        ESP_LOGW(TAG, "⚠️  SSD1306 未检测到 (GPIO41/42)，跳过显示");
        /* I2C driver may have been partially installed – uninstall it to avoid conflict */
        i2c_driver_delete(I2C_NUM_0);
    }

    /* WiFi */
    if (!wifi_connect()) {
        ESP_LOGE(TAG, "WiFi 连接失败，重启...");
        esp_restart();
    }

    if (s_display_ok) {
        ssd1306_clear();
        ssd1306_puts(0, 0, DEVICE_NAME);
        ssd1306_puts(0, 1, "WiFi Connected! ");
        ssd1306_puts(0, 2, "Connecting WS...");
        ssd1306_flush();
    }

    /* TTS playback queue + I2S TX must be ready BEFORE WebSocket connects,
     * because VAD sends cached TTS URL immediately on connection. */
    s_tts_url_queue = xQueueCreate(4, TTS_URL_MAX_LEN);
    assert(s_tts_url_queue);
    init_i2s_tx();
    xTaskCreatePinnedToCore(tts_play_task, "tts_play", 12288, NULL, 8, NULL, 0);

    /* WebSocket client */
    esp_websocket_client_config_t ws_cfg = {
        .uri                  = VAD_WS_URL,
        .reconnect_timeout_ms = 5000,
        .network_timeout_ms   = 60000,   /* 60 s – allow silence between speech */
        .task_stack           = 8192,
    };
    s_ws_client = esp_websocket_client_init(&ws_cfg);
    ESP_ERROR_CHECK(esp_websocket_register_events(s_ws_client,
                    WEBSOCKET_EVENT_ANY, ws_event_handler, NULL));
    ESP_ERROR_CHECK(esp_websocket_client_start(s_ws_client));

    ESP_LOGI(TAG, "正在连接 VAD WebSocket: %s", VAD_WS_URL);

    /* Wait for WebSocket connection */
    for (int i = 0; i < 30 && !s_ws_connected; i++) {
        vTaskDelay(pdMS_TO_TICKS(500));
    }
    if (!s_ws_connected) {
        ESP_LOGW(TAG, "WebSocket 未连接，继续启动音频任务（将在连接后发送）");
    }

    /* Audio capture task pinned to core 1 */
    xTaskCreatePinnedToCore(audio_task, "audio", 8192, NULL, 10, NULL, 1);

    ESP_LOGI(TAG, "✅ 系统启动完成 - 开始采集音频并发送到 VAD");

    /* Main loop: periodic status log */
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(10000));
        ESP_LOGI(TAG, "[状态] WS=%s  识别次数=%d  最后文本=%s  说话人=%s",
                 s_ws_connected ? "已连接" : "断开",
                 s_recog_count, s_last_text, s_last_speaker);
        disp_update();
    }
}
