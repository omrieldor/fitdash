# Railway → Oracle Cloud Migration Guide

Everything you need to do, in order. I already created all the scripts — you just need to click through Oracle's console and run ONE command on the server.

---

## STEP 1: Create Oracle Cloud Account (5 min)

1. Go to **https://www.oracle.com/cloud/free/**
2. Click **"Start for free"**
3. Fill in your details — use your real name and email
4. **Credit card required** for verification — you will NOT be charged. Oracle's Always Free tier is truly free forever
5. Choose **Home Region** → pick the closest to Israel (e.g., `me-jeddah-1` or `eu-frankfurt-1`)
6. Wait for account activation email (usually instant, can take up to 30 min)

---

## STEP 2: Create a VM Instance (5 min)

1. Log into **https://cloud.oracle.com**
2. Click the hamburger menu (☰) → **Compute** → **Instances**
3. Click **"Create Instance"**
4. Configure:
   - **Name:** `eldorado`
   - **Image:** Click **"Edit"** → select **Canonical Ubuntu 22.04** (or 24.04)
   - **Shape:** Click **"Change Shape"** → **Ampere** → **VM.Standard.A1.Flex**
     - Set **OCPUs: 1**, **Memory: 6 GB** (well within free limits)
   - **Networking:** Leave defaults (it creates a VCN for you)
   - **SSH Keys:** Select **"Generate a key pair for me"** → click **"Save Private Key"** and **"Save Public Key"**
     - **SAVE THESE FILES!** You need the private key to connect. Put it somewhere safe (e.g., `C:\Users\omrie\.ssh\oracle-key.key`)
5. Click **"Create"**
6. Wait ~1 min for it to say **"Running"**
7. Copy the **Public IP Address** shown on the instance page — you'll need it

---

## STEP 3: Open Port 80 and 443 (2 min)

1. On your instance page, click the **Subnet** link (under "Primary VNIC")
2. Click the **Security List** (default security list)
3. Click **"Add Ingress Rules"** and add TWO rules:

| Source CIDR    | Protocol | Dest Port | Description |
|----------------|----------|-----------|-------------|
| `0.0.0.0/0`   | TCP      | 80        | HTTP        |
| `0.0.0.0/0`   | TCP      | 443       | HTTPS       |

4. Click **"Add Ingress Rules"**

---

## STEP 4: Connect & Deploy (3 min)

Open **PowerShell** on your PC and run:

```powershell
ssh -i "C:\Users\omrie\.ssh\oracle-key.key" ubuntu@YOUR_PUBLIC_IP
```

(Replace `YOUR_PUBLIC_IP` with the IP from Step 2)

If it asks about fingerprint, type `yes`.

Once connected, run this ONE command:

```bash
curl -sL https://raw.githubusercontent.com/omrieldor/fitdash/main/deploy/setup-server.sh | bash
```

**OR** if the deploy folder isn't pushed to GitHub yet, paste the commands manually:

```bash
git clone https://github.com/omrieldor/fitdash.git /home/ubuntu/fitdash
cd /home/ubuntu/fitdash/deploy
bash setup-server.sh
```

The script does everything: installs packages, clones repo, sets up Python, creates the service, configures nginx.

When it finishes, it prints your URL: **http://YOUR_PUBLIC_IP**

---

## STEP 5: Verify It Works

Open your browser and go to `http://YOUR_PUBLIC_IP`. You should see the Path to Eldorado login page.

---

## STEP 6 (Optional): Custom Domain + SSL

If you want `https://yourdomain.com` instead of a raw IP:

1. Get a free domain from **DuckDNS** (https://www.duckdns.org) or use your own
2. Point the domain's A record to your Oracle VM's public IP
3. SSH into your server and run:
   ```bash
   cd /home/ubuntu/fitdash/deploy
   bash add-ssl.sh yourdomain.com
   ```

---

## After Migration: How to Update Your App

Whenever you push code changes to GitHub:

```bash
ssh -i "C:\Users\omrie\.ssh\oracle-key.key" ubuntu@YOUR_PUBLIC_IP
bash /home/ubuntu/fitdash/deploy/update-app.sh
```

## Backup Your Database

```bash
bash /home/ubuntu/fitdash/deploy/backup-db.sh
```

---

## What You Can Shut Down After Migration

- **Railway:** Delete the project at https://railway.app to free it up
- Your GitHub repo stays the same — Oracle pulls from it

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Can't SSH in | Check security list has port 22 open (it is by default) |
| Site not loading | `sudo systemctl status eldorado` to check app, `sudo systemctl status nginx` to check proxy |
| App crashes | `sudo journalctl -u eldorado -n 50` to see logs |
| Need to restart | `sudo systemctl restart eldorado` |
| DB disappeared | It shouldn't! But check `/home/ubuntu/fitdash/instance/dashboard.db` |
