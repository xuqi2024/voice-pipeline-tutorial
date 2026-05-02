idf_component_register(
    SRCS "main.c" "ssd1306.c"
    INCLUDE_DIRS "."
    REQUIRES
        esp_wifi
        esp_event
        nvs_flash
        esp_netif
        driver
        json
        esp_http_client
)
