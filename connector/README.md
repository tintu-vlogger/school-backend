# Attendance connector

A small program for one computer in your office. It reads punches from attendance machines and
from attendance software on your office network, and sends them to the HR system over the
internet. Use it when the HR system can't reach your machines or database directly, which is
the usual case: they sit inside the office network.

It works with:

- **ZKTeco, eSSL and other machines that use the ZKTeco protocol** (network port 4370).
- **The database of your attendance software**: Matrix COSEC, eSSL eTimeTrackLite, ZKTeco
  ZKBioTime, ZKTime / ZKAccess (Access file), or any SQL Server, MySQL, MariaDB, PostgreSQL,
  Oracle or MS Access database that has a punch table.

If your machine can send punches to the internet by itself ("ADMS", "cloud server" or "push"
in its menu), you don't need this program: choose **ADMS cloud push** on the Devices page.

## What you need

- A Windows PC (or a Linux machine) that stays on during office hours and can reach the
  machines or the database. The server where eTimeTrackLite or COSEC is installed is a good choice.
- Internet access from that PC. Nothing needs to be opened from the internet into your office.

## Set it up

1. **In the HR system**, open **Time, Devices**, add the device (or open it) and under
   *How punches arrive* choose **Database read** or **LAN pull (ZKTeco)**. Fill in the
   details, test them, and choose **Runs on: office connector**. Save.
2. Open the device again and click **Connector settings file**. Save the file as
   `connector.json`. Keep it private: it contains the device key.
3. **On the office PC**, put `connector.exe` and `connector.json` in one folder, for example
   `C:\AttendanceConnector`.
4. Check that it works: open a command window in that folder and run

   ```
   connector.exe test
   ```

   It connects to each device or database and shows a few punches. Nothing is sent yet.
5. Make it start with Windows: right-click `install-windows.bat` and choose
   **Run as administrator**. It runs in the background from then on, even when nobody is
   signed in.
6. Back in the HR system, the device shows **Connector online** within a minute or two,
   and new punches appear in the **Sync log**.

More than one device? Download the settings file for each one, then add them to the first
file with `connector.exe add connector-2.json`.

## How it behaves

- It reads each device every few minutes (the interval set in the HR system) and only takes
  punches newer than the last one it read.
- Punches are first saved on the PC (in `connector-data.db`) and then sent. If the internet
  is down, they wait and are sent when it is back. Nothing is lost when the PC restarts.
- Every minute it tells the HR system it is alive, how many punches are waiting and whether
  something went wrong. You see this on the Devices page.
- When you change the device's settings in the HR system, the connector picks them up by itself.
- It only reads. It never changes the attendance software's data or the machine's records.
  For a database, give it a user that can only read.

## Other commands

| Command | What it does |
|---|---|
| `connector.exe status` | Punches waiting to be sent and how far each device has been read |
| `connector.exe once` | Read and send once, then stop |
| `connector.exe run` | Keep running (what the start-up task does) |
| `uninstall-windows.bat` | Stop it and remove it from Windows start-up |

The log is in `connector.log` in the same folder.

## When something is wrong

- **"The device key was refused"**: a new settings file was made for the device, or the device
  was switched off in the HR system. Download the settings file again and replace `connector.json`.
- **"Couldn't connect to the device"**: check the machine's IP address and that the PC can reach
  it (`ping 192.168.1.201`). The communication key must match the one in the machine's menu.
- **"needs the 'pymssql' package"** (or another name): this copy of the program was built
  without that database driver. Use the full `connector.exe`, or install the package (below).
- **MS Access files**: the PC needs the free *Microsoft Access Database Engine*, in the same
  32-bit or 64-bit version as the connector.

## For IT: running from source or building the .exe

Python 3.10 or later:

```
pip install -r requirements.txt          # add the drivers your office needs
python connector.py test
python connector.py run
```

Build a single `connector.exe` on Windows:

```
pip install -r requirements.txt pyodbc pyinstaller
pyinstaller connector.spec
```

The program is written to `dist\connector.exe`. On Linux, `attendance-connector.service` runs it
with systemd.

What it sends: `POST <server>/attendance/devices/push` with the header `X-Device-Key` and
`{"punches": [{"device_user_id", "timestamp", "direction", "terminal"}]}`; its settings come
from `GET <server>/attendance/connector/config` and its status goes to
`POST <server>/attendance/connector/heartbeat`.
