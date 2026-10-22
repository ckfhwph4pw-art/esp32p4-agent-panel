# 踩坑记录

这份文档记录本项目开发过程中实际踩到的坑，每条都带现象、根因与验证方式。
写下来是因为其中多数坑**不会在编译期报错**，只在运行期以难以归因的现象暴露。

---

## 一、伪静态存储器配置被静默丢弃

**现象**：编译成功，日志末尾也显示 `Project build complete`，但日志里夹着五条警告：

```
warning: unknown kconfig symbol 'SPIRAM' assigned to 'y' in sdkconfig.defaults
warning: unknown kconfig symbol 'SPIRAM_MODE_HEX' ...
warning: unknown kconfig symbol 'SPIRAM_SPEED_80M' ...
```

**后果**：32 MB 伪静态存储器对固件完全不可见。等到接屏、跑图形库时才会暴露，
而现场现象很难与"内存不够"区分。

**根因**：ESP-IDF 5.5 默认开启最小构建模式（日志首行 `Minimal build - ON`）。
只有真正进入构建的组件，其 Kconfig 才会被加载。`esp_psram` 没有被任何组件依赖，
配置项就没加载，`CONFIG_SPIRAM` 被当成拼写错误的符号丢弃。

**验证方式**：

```sh
grep -c "esp_psram/Kconfig" build/kconfigs.in   # 0 表示未加载
```

**修法**：`main/CMakeLists.txt` 里声明依赖。

```cmake
idf_component_register(... PRIV_REQUIRES spi_flash esp_psram)
```

**试过但无效**：`idf.py -DMINIMAL_BUILD=OFF`。仍然五条警告，关最小构建绕不过去。

---

## 二、显示锁超时传 0 等于立即失败

**现象**：烧录后约 7 秒触发任务看门狗复位。

```
E (7168) task_wdt: - IDLE0 (CPU 0)
E (7168) task_wdt: CPU 0: lvgl
```

**根因**：`bsp_display_lock(0)` 的参数单位是**毫秒**，传 0 表示超时 0 毫秒，
立即返回失败。锁没拿到，后续却在无锁状态下创建图形对象，与渲染任务撞车。

**修法**：给足超时并检查返回值。

```c
if (bsp_display_lock(2000)) {
    /* 建界面 */
    bsp_display_unlock();
}
```

日志特征记住：`Failed to acquire LVGL lock` 紧跟 `task_wdt: IDLE0`，就是这个原因。

---

## 三、图形库的 OS 后端必须成组配置

**现象**：只加 `CONFIG_LV_OS_FREERTOS=y` 之后，看门狗不再报了，但屏幕只剩背光、
没有任何画面。日志显示：

```
E (7172) task_wdt: CPU 1: swdraw
```

**根因**：图形库在 FreeRTOS 后端下会创建独立的渲染线程，需要配套的线程节拍、
内存分配器、刷新周期与绘制单元数。只开 OS 后端而不补齐这些，渲染线程会卡死。

这个坑比第一个更隐蔽：**日志比之前更干净，屏幕却更空**。

**修法**：整组采用。

```
CONFIG_FREERTOS_HZ=1000
CONFIG_LV_USE_CLIB_MALLOC=y
CONFIG_LV_USE_CLIB_STRING=y
CONFIG_LV_USE_CLIB_SPRINTF=y
CONFIG_LV_DEF_REFR_PERIOD=15
CONFIG_LV_OBJ_STYLE_CACHE=y
CONFIG_LV_DRAW_SW_DRAW_UNIT_CNT=2
CONFIG_ESP_MAIN_TASK_STACK_SIZE=10240
```

**教训**：用板级支持包这类组件时，最小 `sdkconfig` 不够用，**厂商示例的
`sdkconfig.defaults` 属于必要输入**，而且应当整段照搬，不要按需挑拣。

---

## 四、背光反相输出，上电即亮

**现象**：背光明显亮着，于是判断"程序在正常运行"。

**事实**：这块板的背光由 LEDC 驱动且 `output_invert=1`，上电时占空比为 0
恰好输出高电平。**背光天生是亮的**，与程序状态无关。

**后果**：曾据此误判程序正常，实际画面是空的（渲染线程卡死，见第三条）。

**判据修正**：屏幕是否点亮不能作为程序运行的证据，必须看**内容**。

---

## 五、控制台串口只有一个 256 字节的接收环

**现象**：连续跑十几分钟后界面停在旧数据，板子对后续 JSON 完全无反应。

**根因**：控制台 UART 的接收环形缓冲只有 256 字节。采集端一轮写三条消息
（约 270 字节）就撑爆，字节从中间被丢掉，两条消息拼成一行；坏行又被解析器丢弃，
界面因此长期收不到更新。

**验证方式**：把解析失败的原始行打印出来，能看到两条消息被拼接：

```
解析失败:[...,"task":"压测第 4 条"..."st":"busy","task":"压测第 5 条"...]
```

**当时的规避办法**：

- 紧凑 JSON（去掉空格），每行压到约 60 字节
- 三条之间错峰 0.5 秒发送
- 解析失败不打印（打印本身又占用发送缓冲，形成恶性循环）

**根治**：改走 USB CDC。TinyUSB 有流控，不再有 256 字节的限制。

**试过但失败的方案**：

- `uart_driver_delete` + `uart_driver_install(8192)` 重建驱动：成功率从 2/32
  掉到 0/40。控制台 VFS 与自建驱动不兼容，此路不通。
- 直接 `uart_read_bytes` 读 UART0：控制台已占用驱动，拿不到数据。

---

## 六、USB 方式烧录必须关压缩

**现象**：

```
A fatal error occurred: Failed to write compressed data to flash after seq 7
(result was C900: Too much data)
```

**根因**：USB OTG 口传压缩块会超限。

**两个参数位置陷阱**：

| 错误写法 | 结果 |
|---|---|
| `idf.py flash --no-compress` | `No such option`，idf.py 不支持这个参数 |
| `esptool --no-compress write_flash @args` | `unrecognized arguments`，位置错了 |

正确：

```sh
python -m esptool --chip esp32p4 -p <端口> write_flash --no-compress @flash_args
```

---

## 七、烧录前必须先释放串口

**现象**：`idf.py flash` 退出码 2，但没有明显报错，界面看起来"没有变化"，
很容易误以为已经烧进去了。

**根因**：采集端还在占用 CDC 端口。

**修法**：烧录前 `pkill -f agent_panel_daemon.py`。

---

## 八、需要人工配合的采集，窗口必须够长

**现象**：连续三次让用户触摸屏幕，都收到"零事件"，据此判断触摸硬件故障。

**实情**：三次监听窗口都是 60 秒，而窗口在消息送达用户之前就已经跑完。
用户读到消息再去操作时，窗口早过期了。

**后果**：差点把正常硬件判成坏件，方向上也被误导到"驱动或硬件"而非"测量方法"。

**修法**：改成 20 至 30 分钟长窗口加心跳，一次就取到了完整数据。

**规则**：任何需要人工配合的实时采集，窗口长度必须显著大于"用户看到消息并反应"
的延迟。短窗口得出的零值不可作为证据。

---

## 九、压测脚本本身可能说谎

**现象**：压测显示 32 条消息只解析成功 2 条，字节"被从中间啃掉又重复拼接"，
据此判断固件解析有 bug，改了一轮。

**实情**：压测脚本用**两个线程**同时读写同一个串口（一个后台线程读、主线程写），
而 pyserial 不是线程安全的。那些乱码是脚本自己造成的。

**规则**：验证脚本必须与被测程序同构。用单线程模型复现，才不会把工具的问题
当成被测对象的问题。

---

## 十、`/tmp` 会在重启时被清空

**现象**：隔夜后工程源码全部丢失。

**根因**：为规避构建目录的删除拦截，把整个工程放在了 `/tmp`，事后没迁回。

**规则**：源码一律放持久目录；`/tmp` 只放构建产物与缓存。
