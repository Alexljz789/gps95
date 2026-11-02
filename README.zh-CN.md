# GPS95 中文简介

GPS95 是一款为 **ClockworkPi uConsole + HackerGadgets AIO v2** 设计的
GPS/GNSS 导航软件，界面采用 Windows 95 风格，适配 uConsole 的 1280×720
屏幕。它通过服务端瓦片缓存实现离线地图，并针对 AIO v2 上的
Ai-Thinker **GP-02（CASIC）** 模块修复了 gpsd / PyGPS 的经典兼容性问题。

## 主要功能

- **Windows 95 界面**：标题栏、菜单、工具栏、状态栏、按钮、对话框、
  像素字体与 98.css 风格。
- **实时地图**：Leaflet + Esri 街道图，显示当前位置、航向锥、轨迹、
  1σ / 95% 精度圈，支持跟随和居中。
- **离线地图**：服务端缓存瓦片，可显式缓存当前视野、限制缓存大小、
  离线浏览；没有瓦片时显示 `NO MAP DATA`。
- **卫星列表**：显示系统、PRN、系列、仰角、方位、信噪比、是否参与定位；
  另有天空图和信号柱状图。
- **导航数据**：纬度、经度、网格、速度、海拔、航向、HDOP、精度、
  数据源、卫星数、时间。
- **双模 GPS**：优先 GPSD；GPSD 不可用或数据异常时自动切换到 NMEA 桥；
  桥也不可用时直接读取串口。
- **多星座**：`PCAS04,7` 同时启用 GPS、北斗、GLONASS（GP-02 不支持
  Galileo，但解析器已预留 Galileo/QZSS/NavIC）。
- **附加功能**：GPX 轨迹导出、NMEA 监视、诊断日志、夜间配色、
  键盘快捷键、演示模式、低卫星数提示。

## 修复了什么问题

GP-02 并不是真正的 u-blox 模块。gpsd 的 u-blox 自动配置会把它切到
二进制模式，导致：

- `gpspipe -w` 有位置 TPV，但 `SKY` 没有卫星，`xgps/cgps` 看不到卫星；
- gpsd 停止后模块仍停留在二进制模式，PyGPS/PyGPSClient 直读串口时
  只看到乱码，必须手动“重置硬件”。

GPS95 的硬件桥接服务独占串口：

```text
GP-02 UART → gps95-gpsbridge（过滤二进制，必要时恢复 NMEA）
               ├─ /run/gps95/nmea（PTY，纯 NMEA）→ gpsd → xgps/cgps
               └─ 127.0.0.1:2958（TCP，纯 NMEA）→ 软件直连模式
```

关键点：

- `PCAS10,2` 恢复出厂 NMEA（含 GGA/GST/GSV）；
- `PCAS04,7` 启用 GPS + 北斗 + GLONASS；
- **不要**用 `PCAS03` 一次打开多个语句，实测会抑制 `$GNGGA`（丢失海拔）；
- gpsd 只看到纯 NMEA，因此能生成完整 `SKY`。

## 安装与运行

```bash
sudo apt update
sudo apt install -y python3 python3-serial python3-flask curl gpsd gpsd-clients chromium

# 在项目目录中
sudo bash scripts/install.sh

# 启动界面（桌面也有 GPS95 图标）
/usr/local/bin/gps95-ui

# 服务状态
/usr/local/sbin/gps95-ctl status
systemctl status gps95-gpsbridge gpsd gps95-web
```

访问地址：`http://127.0.0.1:8765`。

配置在 `/var/lib/gps95/config.json`，地图缓存在
`/var/lib/gps95/tiles`，日志在 `/var/log/gps95/`。

## 隐私说明

本仓库**不包含**设备实际位置、实景截图、真实坐标或个人信息。代码中的
地图初始位置与演示轨迹使用伦敦作为通用示例，仅用于地图初始化。
如果你在自己的设备上使用，位置数据只保存在本机，不会上传到本仓库。

## 文档

- 英文完整文档：[README.md](README.md)
- 安装脚本：[scripts/install.sh](scripts/install.sh)
- 卸载脚本：[scripts/uninstall.sh](scripts/uninstall.sh)
- 硬件桥：[app/bridge.py](app/bridge.py)
- NMEA 解析：[app/nmea.py](app/nmea.py)
- 地图缓存：[app/tiles.py](app/tiles.py)

## 许可

MIT License，详见 [LICENSE](LICENSE)。
