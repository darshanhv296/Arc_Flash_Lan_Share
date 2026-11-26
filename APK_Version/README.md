
# ⚡ ARC FLASH — LAN File Sharing

<p align="center">
  <img src="PC_Version/static/brand-icon.png" alt="ARC FLASH logo" width="150">
</p>

<h3 align="center">Fast. Local. Direct.</h3>

<p align="center">
  High-speed file sharing between Android and Windows PCs over your local network.
</p>

<p align="center">
  <a href="https://drive.google.com/drive/folders/1rBTvIBJs3fQL1FKlETxc2HRzwO1P19cW?usp=drive_link">DOWNLOAD ARC FLASH — APK & WINDOWS EXE</a>
</p>

---

## 🚀 Overview

**ARC FLASH** is a local-network file-sharing system for Android and Windows PC, designed to transfer files directly between compatible devices connected to the same Wi-Fi network or mobile hotspot.

The project includes an Android app and a PC companion. Devices discover compatible peers on the local network and establish a transfer session to send files and folders. ARC FLASH is designed to operate without cloud file hosting or a required internet connection when devices can communicate on the same LAN.

- ⚡ Direct LAN transfers
- 📱 Android APK and 🖥️ Windows PC companion
- 📡 Local device discovery
- 🔗 QR-assisted connection workflow
- 📁 File and folder transfer workflows
- 📊 Transfer progress and speed monitoring
- 🔄 Transfer controls
- 📴 Local-network operation without cloud upload
- 🎨 Interface theme options on Android and PC, where supported

> **Download the ready-to-use applications:** Use the single download link at the top of this README to access the latest release containing the Android APK and Windows EXE.

---

## 📥 Download ARC FLASH

The latest Android APK and Windows executable are distributed through the project's GitHub Releases page.

Use the **Download ARC FLASH** link at the top of this README to open the latest release.

### Android

1. Open the global download link at the top of this README.
2. Download the Android APK from the release assets.
3. Open the APK on your Android device and follow Android's installation prompts. You may need to allow installation from that source.
4. Launch ARC FLASH and connect to the same Wi-Fi or hotspot as the other device.

### Windows PC

1. Open the global download link at the top of this README.
2. Download the Windows executable from the release assets.
3. Run the downloaded executable.
4. If Windows Firewall asks, allow local network access only on networks you trust and intend to use for file sharing.
5. Connect the PC and Android/other PC to the same local network.

> If a release does not yet contain an APK or EXE asset, check back after the maintainer publishes the builds.

---

## ✨ Features

### 📂 File and folder sharing

- Transfer single files and multiple files.
- Share folders using the supported folder-transfer workflows.
- PC folder-transfer options include ZIP archive transfer and folder-tree transfer.
- Folder-tree workflows are designed to preserve supported relative folder structure.

### 📡 LAN device discovery

ARC FLASH uses local-network discovery to help identify compatible devices. Discovery can be affected by router configuration, guest-network isolation, firewall rules, VPNs, and other network restrictions.

### 🔗 QR-assisted connection

QR workflows help simplify connection or pairing.

On the PC version, the Show QR workflow generates a pairing payload for a compatible Android client. The Android version includes QR scan/connect and QR generation workflows.

### 📊 Transfer dashboard

The applications provide transfer status information, with supported views showing details such as:

- File name
- Transfer progress
- Transfer status
- Transfer speed
- File size and related transfer information

### 🔄 Transfer controls

Controls available in the interface can include:

- Pause and resume
- Stop active transfer activity
- Cancel an individual transfer
- Retry a failed transfer, where available
- Refresh discovery
- Clear transfer entries/history

The exact controls and availability depend on the platform and transfer state.

### 🎨 Interface themes

The Android README describes Dark, Light and Warm themes. The PC UI supports its available theme options, including system/light/dark selection as implemented.

### 📴 Offline/local operation

ARC FLASH is designed to send files directly over a local network. It does not require cloud file hosting for its intended LAN transfer workflow. Internet availability is not required when the local network provides device connectivity.

---

## 🔒 Privacy and connection model

ARC FLASH is designed around local device-to-device communication and user-controlled transfers.

- File transfers are intended to occur between compatible devices on the local network.
- The application is not designed as a public cloud file-sharing service.
- Devices use connection/session workflows before transfer, as supported by the implementation.
- Received files are saved locally to the selected or configured destination.

**Network safety:** Use ARC FLASH on networks you trust. Avoid exposing the application to the public internet or forwarding its ports through your router. Network discovery visibility is not a substitute for securing the local network.

---

## ⚡ Performance

Transfer performance depends on the network band, signal strength, access point/router, interference, device hardware, storage speed, file sizes, and other activity on the network.

ARC FLASH uses direct local-network communication and optimized transfer processing in its implementations. Actual throughput varies; no fixed speed is guaranteed.

---

# 📱 Android Version

## Android highlights

The Android application provides the mobile side of ARC FLASH:

- LAN file sharing
- Automatic device discovery
- QR connection and QR generation workflows
- Session-oriented connection/transfer workflow
- File and folder sharing
- Background transfer service
- Real-time progress information
- Duplicate-transfer prevention mechanisms as implemented
- Dark, Light and Warm themes
- Storage/location controls

### Android installation

1. Download the Android APK from the global download link at the top of this README.
2. Install the APK on your Android device.
3. Grant the required permissions when prompted.
4. Connect to the same Wi-Fi or hotspot as the target device.
5. Open ARC FLASH, discover/connect to the target device, and initiate a transfer.

### Android project structure

The Android project structure documented for the application is:

```text
APK_Version/
├── app/
│   ├── src/
│   │   └── main/
│   │       ├── java/com/example/filesharing/
│   │       │   ├── MainActivity.kt
│   │       │   ├── FileSharingViewModel.kt
│   │       │   ├── TransferService.kt
│   │       │   ├── ProgressRequestBody.kt
│   │       │   └── ...other Kotlin files
│   │       ├── res/
│   │       │   ├── drawable/
│   │       │   ├── mipmap-anydpi-v26/
│   │       │   ├── mipmap-mdpi/
│   │       │   ├── mipmap-hdpi/
│   │       │   ├── mipmap-xhdpi/
│   │       │   ├── mipmap-xxhdpi/
│   │       │   ├── mipmap-xxxhdpi/
│   │       │   ├── values/
│   │       │   └── xml/
│   │       └── AndroidManifest.xml
│   │       ├── build.gradle.kts
│   │       └── proguard-rules.pro
│   ├── gradle/
│   │   └── wrapper/
│   ├── releases/
│   │   └── app-release.apk
│   ├── assets/
│   │   ├── home_screen.png
│   │   ├── qr_connect.png
│   │   ├── receive_screen.png
│   │   ├── send_screen_1.png
│   │   ├── send_screen_2.png
│   │   └── settings_screen.png
│   ├── build.gradle.kts
│   ├── settings.gradle.kts
│   ├── gradle.properties
│   ├── gradlew
│   ├── gradlew.bat
│   └── README.md
```

> The structure above describes the Android project layout documented for the app. The combined repository layout may differ depending on how the maintainer organizes the platform folders.

---

# 🖥️ PC Version (Windows)

## PC highlights

ARC FLASH PC is the companion application for Windows, supporting compatible PC ↔ PC and Android ↔ PC LAN transfer workflows.

- Python-based PC transfer service
- Local desktop web interface
- Desktop window via `pywebview` when available, with browser fallback
- LAN discovery using UDP broadcast/listener
- Optional mDNS discovery
- QR-assisted pairing payload generation
- AF01 binary transfer protocol
- HTTP/1.1 and local interface service
- File and folder transfer workflows
- Folder-to-ZIP and folder-tree workflows
- Transfer status and management
- Configurable received-file destination

## PC version requirements

### Prebuilt executable

- Windows compatible with the published release build
- Local Wi-Fi, hotspot, or LAN connectivity
- Windows Firewall permission for the intended trusted network, when needed

### Run from source

- Python 3.9 or newer
- `zeroconf` is optional (for mDNS discovery)

## Run the PC version from source

### 1. Install Python

Install Python 3.9 or newer and confirm it is available in PowerShell:

```powershell
python --version
```

### 2. Download the source

Download or clone the repository and open PowerShell in the PC source directory, where `app.py` is located.

### 3. Optional mDNS package

UDP discovery is built in. mDNS is optional:

```powershell
python -m pip install zeroconf
```

### 4. Launch ARC FLASH PC

```powershell
python app.py
```

The service starts and the UI opens. If the desktop WebView module is not available, the application falls back to the default browser.

## PC command-line options

| Option | Description |
|---|---|
| `--port PORT` | Select the preferred TCP port (default: `53317`). |
| `--save-dir PATH` | Choose the destination folder for received files. |
| `--name NAME` | Set the display name advertised to nearby devices. |
| `--no-ui` | Start the service without opening the interface. |

### Examples

Default launch:

```powershell
python app.py
```

Custom device name:

```powershell
python app.py --name "My PC"
```

Custom receive directory:

```powershell
python app.py --save-dir "D:\ARC-FLASH-Received"
```

Preferred TCP port:

```powershell
python app.py --port 53317
```

Run without UI:

```powershell
python app.py --no-ui
```

Combined:

```powershell
python app.py --name "Office PC" --save-dir "D:\Shared Files" --port 53317
```

When using `--no-ui`, stop the process with `Ctrl+C`.

## PC configuration and storage

The PC version creates or loads a local configuration for its device identity, display name, receive location and theme.

- When running from source, configuration is stored in the source directory.
- When running as a packaged executable, configuration is stored in the user's application-data directory.
- The default receive folder is `C:\ARC-FLASH\ArcFlash`.
- The receive location and supported settings can be changed through the UI.

## PC network ports

The PC version's documented protocol constants include:

| Purpose | Port |
|---|---|
| Preferred TCP transfer/UI service | `53317` |
| UDP LAN discovery | `45890` |
| TCP fallback | `53318` through `53327` may be attempted if the preferred port is unavailable |

The TCP service supports the AF01 binary transfer protocol and HTTP-based interface/workflows.

### Windows Firewall

If discovery or transfer fails:

1. Confirm the devices are on the same local network.
2. Check the Windows network profile and use Private only for a trusted network.
3. Allow ARC FLASH through Windows Firewall for that trusted network if required.
4. Check for access-point isolation or guest-network restrictions.
5. Consider whether a VPN is interfering with local network connectivity.

Do not use router port forwarding to expose ARC FLASH to the internet.

---

## 🔄 Typical connection workflow

```text
Connect both devices to the same LAN/Wi-Fi/hotspot
                      ↓
             Open ARC FLASH apps
                      ↓
         Discover device or use QR workflow
                      ↓
               Connect / pair
                      ↓
             Select files or folder
                      ↓
             Transfer and monitor
                      ↓
                 Completed
```

---

## 🧰 Troubleshooting

### Devices cannot find each other

- Ensure both devices are on the same Wi-Fi, LAN or hotspot.
- Check whether guest Wi-Fi or AP/client isolation blocks device communication.
- Check firewall permissions on Windows.
- Refresh the device discovery list or restart the apps.
- Verify that local network connectivity is working.

### QR connection fails

- Ensure you are using a compatible ARC FLASH version.
- Refresh or regenerate the QR code if the pairing payload has expired.
- Check network connectivity and firewall rules.

### Transfers are slow

- Try a stronger Wi-Fi signal.
- Use a less congested network.
- Check device and disk performance.
- Avoid simultaneous heavy network usage.
- Test the devices on a different local network if necessary.

### PC receives files in the wrong location

- Open PC Settings and verify the receive destination.
- Confirm that the drive exists and has sufficient free space.
- Check local folder permissions.

### PC application does not open its desktop window

- The source version can use `pywebview` when installed.
- If unavailable, the application can open the UI in the default browser.
- Launch from PowerShell to see diagnostic messages.

### PC application cannot start

- Confirm Python 3.9+ for source execution.
- Check the terminal output.
- Try another preferred port using `--port`.
- Check whether another instance is running.

---

## 🧠 PC architecture and source files

The PC implementation is organized around these source components:

| File | Purpose |
|---|---|
| `app.py` | Application entry point, arguments, configuration and service/UI startup |
| `arc_protocol.py` | AF01 framing, protocol constants, file metadata, control frames and helper logic |
| `discovery.py` | UDP discovery, peer filtering and QR payload generation |
| `transfer_engine.py` | Transfer manager, transfer processing, service/API, file sending and receiving |
| `static/index.html` | Interface structure |
| `static/styles.css` | Interface styling |
| `static/app.js` | UI logic |
| `static/qrcode.js` | QR rendering support |
| `static/brand-icon.png` | Branding asset |

## AF01 transfer protocol

The PC implementation uses the AF01 binary transfer protocol, designed for compatibility with the Android counterpart.

The protocol implementation includes:

- AF01 session preface
- Framed messages
- File metadata and data frames
- Acknowledgement and completion messages
- Transfer control frames
- Chunk processing
- CRC32 integrity verification
- Path confinement and safe destination handling
- Transfer-management processing

The implementation's tuning and transfer behavior are described in the source code; actual throughput depends on hardware and network conditions.

---

## 🗂️ Combined project layout

Example platform-separated layout for the repository:

```text
ARC-FLASH/
├── APK_Version/
│   └── Android project files...
├── PC_Version/
│   ├── app.py
│   ├── arc_protocol.py
│   ├── discovery.py
│   ├── transfer_engine.py
│   ├── requirements.txt
│   └── static/
│       ├── index.html
│       ├── app.js
│       ├── styles.css
│       ├── qrcode.js
│       └── brand-icon.png
└── README.md
```

This is an illustrative organization for the repository; actual folder names can be adapted to the repository contents.

---

## 🆚 ARC FLASH and other sharing apps

ARC FLASH focuses on direct local transfers, a lightweight interface, and user-controlled sharing. Feature comparisons with other applications depend on their versions, devices, settings and network conditions. This README does not claim universal speed or security superiority over other apps.

---

## 🛠️ Future improvements

Potential improvements and additions may include:

- Further transfer throughput optimizations
- More platform support
- Expanded connection options
- Additional transfer-management features

---

## 👤 Author

**Darshan H V**

**ARC FLASH — LAN File Sharing**

Built with a focus on local connectivity, performance, transfer visibility, and user control.

---

## 📜 License and usage

Refer to the project's repository or release notes for the maintainer's current licensing and usage terms.

---

<p align="center">
  <b>⚡ ARC FLASH</b><br>
  Fast. Local. Direct.
</p>
