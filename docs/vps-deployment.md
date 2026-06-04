# How to Deploy Rover on a VPS

This guide covers running **Rover** unattended on a Linux VPS with Chrome,
Selenium, Xvfb, and a persistent Chrome profile that has the SellerAmp
extension installed.

## Requirements

- Ubuntu (or Debian) Linux VPS
- Python 3.13+
- Google Chrome
- `Xvfb` and `x11vnc`
- Rover config files (+ `.env`)
- Chrome profile with SellerAmp extension installed

## Install System Packages

1. Install Xvfb, Chrome support packages, and basic utilities:

```bash
sudo apt update
sudo apt install -y wget curl ca-certificates gnupg unzip xvfb xauth x11vnc
```

2. Install Google Chrome:

```bash
cd /tmp
wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt install -y ./google-chrome-stable_current_amd64.deb
```

3. Verify the binaries:

```bash
which google-chrome
google-chrome --version
which Xvfb
which xvfb-run
```

## Install Rover

1. Create the Python environment and install dependencies:

```bash
cd /path/to/rover
python3.13 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

2. Copy config files and `.env`:

```bash
cp config/data.example.yaml config/data.yaml
cp config/scraper.example.yaml config/scraper.yaml
cp config/agent.example.yaml config/agent.yaml
cp config/email_report.example.yaml config/email_report.yaml
cp config/keyword_policy.example.yaml config/keyword_policy.yaml
cp .env.example .env
```

3. Fill in `.env` and run the health check:

```bash
.venv/bin/python scripts/doctor.py
```

> **IMPORTANT:** Set the scraper browser mode to `xvfb` in `scraper.yaml`.

## Create SellerAmp Chrome Profile

1. Verify Chrome can start inside `Xvfb`:

```bash
timeout 10s xvfb-run -a --server-args="-screen 0 1440x1200x24" \
  google-chrome --no-sandbox --disable-dev-shm-usage --disable-gpu \
  --user-data-dir=/tmp/chrome-test about:blank
echo $?
```

**Expected exit codes**:

- `124`: Chrome ran without crashing (good)
- `0`: Chrome started and exited on its own (also good)
- `99` or `2`: Chrome crashed immediately (bad)

2. Launch a Chrome instance on the VPS:

```bash
cd /path/to/rover
mkdir -p secrets/chrome-profile

Xvfb :99 -screen 0 1440x1200x24 -ac -nolisten tcp >/tmp/xvfb-rover.log 2>&1 &
export DISPLAY=:99

google-chrome \
  --no-sandbox \
  --disable-dev-shm-usage \
  --disable-gpu \
  --user-data-dir=/path/to/rover/secrets/chrome-profile \
  --window-size=1440,1200 \
  https://sas.selleramp.com/
```

3. Open a new terminal and SSH into the VPS again. Start VNC for that display:

```bash
x11vnc -display :99 -localhost -forever -shared -noxdamage -rfbport 5900
```

4. Create an SSH tunnel on your local machine:

```bash
ssh -N -L 5901:127.0.0.1:5900 user@server-hostname
```

5. Connect a local VNC client to:

```
127.0.0.1:5901
```

> I recommend [TigerVNC](https://tigervnc.org/). It works well with `x11vnc`.

6. Inside Chrome:
   - Install the SellerAmp extension.
   - Log in to SellerAmp.
   - Confirm the SellerAmp page and extension are working.
   - Close Chrome.

7. Stop `x11vbc` and `Xvfb`:

```bash
pkill x11vnc
pkill Xvfb
```

8. Make sure no Chrome process is locking the profile:

```bash
ps aux | grep -i chrome
```

## Run Rover Manually

To run the full pipeline:

```bash
cd /path/to/rover
xvfb-run -a --server-args="-screen 0 1440x1200x24" \
  /path/to/rover/.venv/bin/python scripts/run_pipeline.py
```

## Schedule Rover Runs

Add the following to your `crontab` to run Rover twice per day (at 6:00 AM and 8:00 PM):

```cron
0 6,20 * * * cd /path/to/rover && /usr/bin/xvfb-run -a --server-args="-screen 0 1440x1200x24" /path/to/rover/.venv/bin/python scripts/run_pipeline.py >> /path/to/rover/logs/pipeline.log 2>&1
```
