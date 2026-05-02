#pragma once
#include "driver/i2c.h"
#include "esp_err.h"

#define SSD1306_W  128
#define SSD1306_H  64

esp_err_t ssd1306_init(i2c_port_t port, int sda_io, int scl_io, uint8_t addr);
void      ssd1306_clear(void);
void      ssd1306_flush(void);
void      ssd1306_puts(int col, int row, const char *text);   // col=0..21, row=0..7
void      ssd1306_printf(int col, int row, const char *fmt, ...);
