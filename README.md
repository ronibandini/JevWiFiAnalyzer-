# JEV WiFi Analyzer

JEV WiFi Analyzer is a compact 2.4 GHz WiFi site-survey instrument built with a ZimaBoard 2, an Alfa AWUS036H USB WiFi adapter, Python, Scapy and Jev.

The device passively monitors WiFi activity, converts captured 802.11 frames into structured RF telemetry and sends each measurement window to Jev through the OpenRouter Decisions API.

Jev evaluates the telemetry and returns structured decisions about coverage, congestion and possible network optimization.

<img width="2710" height="1544" alt="Run1" src="https://github.com/user-attachments/assets/f3e6b2ed-4cad-4519-8eef-729590cc82c6" />


## Features

- Passive WiFi monitoring
- 2.4 GHz channel scanning
- Frames/sec measurement
- Access point detection
- RSSI statistics
- Observed client counting
- Configurable channels and measurement windows

## Hardware

- ZimaBoard 2 https://shop.zimaspace.com?sca_ref=12384820.5O0Okp5fhsjM&utm_source=roni-bandini&utm_medium=affiliate&utm_campaign=zimaspace_affiliate&utm_content=Roni-Bandini
- Alfa AWUS036H
- 16 GB USB flash drive
- HDMI monitor
- USB keyboard
- Ethernet cable

## Installation

### Ubuntu

Download the [Ubuntu Desktop ISO](https://ubuntu.com/download/desktop) and create a bootable USB drive with [Balena Etcher](https://etcher.balena.io/).

Connect the USB drive, HDMI monitor, keyboard and Ethernet cable to the ZimaBoard 2. Power it on, press `F11` during startup, select the USB drive and install Ubuntu.

### Dependencies

Open a terminal with `CTRL + ALT + T`:

```bash
sudo apt update
sudo apt install python3-pip python3-scapy
pip install requests --break-system-packages
```

## Alfa AWUS036H

Connect the Alfa adapter and check that Ubuntu detects it:

```bash
ip link
iw dev
lsusb
```

A typical installation may show:

```text
Interface: wlx018c0cb28c7b1
Chipset: Realtek RTL8187
Current mode: managed
MAC: 01:c0:ca:19:23:b1
```

The interface name and MAC address will differ on each system. The analyzer automatically switches the interface to monitor mode before starting the survey.

## OpenRouter API key

Create an API key at:

https://openrouter.ai/workspaces/default/keys

Configure inside scan.py


## Running the analyzer

Scan channels 1-13 with:

```bash
sudo -E python3 scan.py \
  -c 1,2,3,4,5,6,7,8,9,10,11,12,13 \
  -t 1 \
  -w 30
```

Options:

| Parameter | Description |
|---|---|
| `-c` | Channels to scan |
| `-t` | Seconds spent on each channel |
| `-w` | Measurement window in seconds |
| `-i` | WiFi interface |
| `-m` | Jev model |
| `--exportDir` | JSON export directory |

The analyzer sequentially hops between channels; it does not monitor all channels simultaneously.

Press `Q` to quit.

## How it works

The Alfa adapter captures 802.11 frames while the analyzer moves through the selected 2.4 GHz channels. Scapy processes the frames and Python aggregates the measurements locally.

For every channel, the analyzer calculates:

```text
channel
frames
dwellSeconds
framesPerSecond
accessPoints
averageRssi
clients
```

Each detected access point also has a summary containing:

```text
BSSID
SSID
channel
beacon count
beacons/sec
RSSI mean/min/max
active clients
```

## Jev integration

Scan creates a structured state containing:

```text
channelsScanned
channelMetrics
accessPointsDetected
measurementWindowSeconds
```

This information is sent to Jev AI through OpenRouter using ~typesafe/jev-latest

The architecture is:

```text
Raw WiFi frames
      |
      v
Python / Scapy
      |
      +-- frame counts
      +-- frames/sec
      +-- AP detection
      +-- RSSI statistics
      +-- observed clients
      |
      v
Structured RF telemetry
      |
      v
Jev
      |
      +-- coverageQuality
      +-- congestionLevel
      +-- optimizationAction
```

Python performs the deterministic measurement and aggregation. Jev interprets the resulting telemetry and makes bounded decisions.

## What is Jev?

[Jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev) is a decision model from TypeSafe designed for structured decision-making.

Instead of asking a generative model to produce a textual report and then parsing that text, the application provides a state and typed questions. Jev returns structured answers such as scores and choices.

In this project, Python handles:

```text
capture
count
calculate
aggregate
```

Jev handles:

```text
score
classify
choose
```

## Jev decisions

### Coverage quality

```text
coverageQuality -> score
```

The criteria range from very poor/dead zone to excellent/strong signal.

### Congestion

```text
congestionLevel -> choice
```

Possible results:

```text
low
moderate
high
critical
```

### Optimization

```text
optimizationAction -> choice
```

Possible results:

```text
none
powerAdjustment
channelPlan
hardwareAddition
```

The three decisions are sent together in the same Jev request.

## Measurement windows and exports

At the end of each measurement window, the analyzer aggregates the current data, sends the structured state to Jev, saves the results and starts a new window.

The default export directory is:

```text
surveyExports/
```

Each JSON export contains:

```text
timestamp
windowSeconds
channelsScanned
channelMetrics
accessPoints
jevEvaluations
```

## Dashboard

The terminal dashboard displays:

- Current channel
- Channel activity
- Frames/sec
- Access points
- Average RSSI
- Observed clients
- Jev coverage score
- Jev congestion level
- Jev optimization action
- Measurement progress
- Accumulated Jev cost

## Applications

### WiFi troubleshooting

Identify channels with high activity or many nearby access points.

### Channel planning

Compare channel activity before selecting a channel for an access point.

### Coverage analysis

Take measurements at different locations and compare observed RSSI.

### Change detection

Run repeated surveys and compare the resulting JSON measurements.

### IoT monitoring

Observe WiFi activity in environments with many connected devices.

### Continuous monitoring

The ZimaBoard can operate as a dedicated WiFi measurement system and periodically record the local environment.

## Limitations

This project is a WiFi activity analyzer, not a general-purpose spectrum analyzer.

RSSI is measured at the Alfa adapter and represents the signal observed at the sensor location.

The project uses sequential channel hopping, so channels are not monitored simultaneously.

## Demo

https://www.youtube.com/watch?v=VluRIPXSZjU

## References

- Jev / TypeSafe: https://typesafe.ai/blog/introducing-system-one-models-and-jev
- What Is Jev? — OpenRouter: https://openrouter.ai/blog/insights/what-is-jev/
- How to Use Jev — OpenRouter: https://openrouter.ai/blog/tutorials/how-to-use-jev/
- OpenRouter API Keys: https://openrouter.ai/workspaces/default/keys
- ZimaBoard 2: https://shop.zimaspace.com?sca_ref=12384820.5O0Okp5fhsjM&utm_source=roni-bandini&utm_medium=affiliate&utm_campaign=zimaspace_affiliate&utm_content=Roni-Bandini
- Ubuntu Desktop: https://ubuntu.com/download/desktop
- Balena Etcher: https://etcher.balena.io/

## Author

**Roni Bandini**

- GitHub: https://github.com/ronibandini
- Contracultura Maker: https://github.com/ronibandini/contracultura-maker
- Medium: https://bandini.medium.com/

## License

GPL
