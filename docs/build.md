# 构建说明

## 环境

| 项目 | 要求 | 说明 |
|---|---|---|
| ESP-IDF | **5.5.4** | 必须 5.5.x。5.5.0 的芯片版本上限是 v1.99，本板是 v3.2，会直接报错 |
| 芯片 | ESP32-P4 rev ≥ 3.0 | 本板实测 v3.2 |
| Python | 3.8 以上 | 采集端另需 `pyserial`、`psutil` |
| Node.js | 任何较新版本 | 仅用于 `lv_font_conv` 生成字库 |

ESP-IDF 5.5.4 与 5.5.0 的差别不只是版本号：P4 的 3.x 芯片需要 5.5.1 以上才被识别。
若板上是 3.x 芯片而工具链是 5.5.0，编译期就会因芯片版本超出支持范围而失败。

## 一、生成字库

字体源码体积很大（中文 30 号字库源码约 15 MB），不纳入版本库。

```sh
npm install -g lv_font_conv
python3 scripts/gen_fonts.py
```

脚本会做三件事：

1. 从 jsDelivr 下载 Noto Sans SC 与 Orbitron（结果缓存到系统临时目录，
   重跑不会重复下载）
2. 构造字符集：ASCII 全段 + 中文标点 + GB2312 全部 6886 字
3. 调 `lv_font_conv` 生成五个字库，并把生成文件里的
   `#include "lvgl/lvgl.h"` 改成 `#include "lvgl.h"`
   （前者在 ESP-IDF 下不存在）

生成结果：

| 文件 | 字号 | 用途 |
|---|---|---|
| `lv_font_noto30.c` | 30 | 正文中文 |
| `lv_font_noto16.c` | 16 | 小字注释、提示行 |
| `lv_font_orbitron48.c` | 48 | 大号耗时数字 |
| `lv_font_orbitron32.c` | 32 | 计数数字、徽章字母（旧版） |
| `lv_font_orbitron20.c` | 20 | 顶栏、页码 |

`main/CMakeLists.txt` 会在字库缺失时直接给出明确报错，不会让你对着
一堆 undefined reference 猜原因。

## 二、配置与编译

```sh
. $IDF_PATH/export.sh
idf.py -B build set-target esp32p4
```

首次 `set-target` 之后需要打开 USB CDC 并设置设备名：

```sh
printf 'CONFIG_TINYUSB_CDC_ENABLED=y\n' >> sdkconfig
printf 'CONFIG_TINYUSB_DESC_PRODUCT_STRING="Agent Panel"\n' >> sdkconfig
printf 'CONFIG_TINYUSB_DESC_SERIAL_STRING="AGP001"\n' >> sdkconfig
```

这三项也可以写进 `sdkconfig.defaults`，但要注意：TinyUSB 的 Kconfig 符号
只有在依赖解析完成后才存在，写在 defaults 里需要在首次 `set-target` 之后再
`reconfigure` 一次才会生效。

然后编译：

```sh
idf.py -B build build
```

产物约 4.6 MB，8 MB 应用分区余量 45%。

## 三、烧录

```sh
# 板子插 USB OTG 口，按住 BOOT → 点 RESET → 松开 BOOT
sh scripts/flash_usb.sh build
```

必须用 `--no-compress`。压缩模式下 USB 传输会报：

```
A fatal error occurred: Failed to write compressed data to flash after seq 7
(result was C900: Too much data)
```

两个容易踩的参数位置问题：

- `idf.py flash` **不支持** `--no-compress`，会报 `No such option`
- 直接调 esptool 时，`--no-compress` 必须写在 `write_flash` **之后**，
  写在全局位置会报 `unrecognized arguments`

正确形式：

```sh
python -m esptool --chip esp32p4 -p <端口> write_flash --no-compress @flash_args
```

烧录失败后端口可能短暂消失（`Device not configured`），等 USB 重新枚举再重试即可，
不是硬件故障。

## 四、为什么构建目录放在 /tmp

本文档里所有命令都用 `-B build` 显式指定构建目录。建议把它指向临时目录：

```sh
idf.py -B /tmp/agent-panel-build build
```

两个原因：

1. **源码与产物分离**。`/tmp` 在 macOS 重启时会被清空，把源码放在那里会丢工程
   （本项目实际踩过）。构建产物丢了无所谓，重编即可。
2. **规避偶发的沙箱拦截**。某些受限环境下，构建目录内的文件删除会被拦截，
   ninja 删不掉自己的锁文件、CMake 清不掉临时目录，配置阶段直接失败。
   把构建目录放到项目之外可以绕开。

## 五、关键配置项

`sdkconfig.defaults` 已经包含全部必需项。以下四组**不可拆分**，改动时要整组检查。

### 图形库的 FreeRTOS 后端

```
CONFIG_FREERTOS_HZ=1000
CONFIG_LV_OS_FREERTOS=y
CONFIG_LV_USE_CLIB_MALLOC=y
CONFIG_LV_USE_CLIB_STRING=y
CONFIG_LV_USE_CLIB_SPRINTF=y
CONFIG_LV_DEF_REFR_PERIOD=15
CONFIG_LV_OBJ_STYLE_CACHE=y
CONFIG_LV_DRAW_SW_DRAW_UNIT_CNT=2
CONFIG_ESP_MAIN_TASK_STACK_SIZE=10240
```

### 伪静态存储器

```
CONFIG_SPIRAM=y
CONFIG_SPIRAM_MODE_HEX=y
CONFIG_SPIRAM_SPEED_80M=y
CONFIG_SPIRAM_BOOT_INIT=y
CONFIG_SPIRAM_MEMTEST=y
```

同时 `main/CMakeLists.txt` 的 `PRIV_REQUIRES` 必须有 `esp_psram`。

### 大字体

```
CONFIG_LV_FONT_FMT_TXT_LARGE=y
```

### 分区表

```
CONFIG_PARTITION_TABLE_CUSTOM=y
CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="partitions.csv"
```

`partitions.csv` 给应用 8 MB、素材 7 MB。默认的 1 MB 应用分区装不下四个字库。

## 六、常见报错

| 报错 | 原因 | 处理 |
|---|---|---|
| `字体源文件缺失` | 没跑 `gen_fonts.py` | 见本文第一节 |
| `unknown kconfig symbol 'SPIRAM'` | `main` 未声明 `esp_psram` 依赖，最小构建模式没加载该 Kconfig | 检查 `main/CMakeLists.txt` |
| `#error "Too large font or glyphs"` | 字库超出 16 位偏移上限 | 打开 `CONFIG_LV_FONT_FMT_TXT_LARGE` |
| `task_wdt: CPU 1: swdraw` | 图形库 OS 后端配置不成组 | 见上文第一组配置 |
| `C900: Too much data` | USB 烧录用了压缩模式 | 加 `--no-compress` |
| 屏幕只有背光没有画面 | 渲染线程卡死，或字库未生成 | 查 `task_wdt` 日志 |
| 界面正常但数据不更新 | 采集端没跑，或端口选错 | 看屏幕顶栏是 `LINK OK` 还是 `NO LINK` |
