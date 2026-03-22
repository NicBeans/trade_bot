# OCI Free Tier Deployment

Deploy the trade bot to Oracle Cloud Infrastructure's Always Free ARM instance.

## What You Get (Free)

- 1 ARM VM (4 OCPU, 24GB RAM) — way more than needed
- 200GB block storage
- 10TB/month outbound data

## Steps

### 1. Create OCI Account

Sign up at cloud.oracle.com. Free tier requires a credit card but won't charge you for Always Free resources.

### 2. Create ARM VM

1. Go to **Compute > Instances > Create Instance**
2. Shape: **VM.Standard.A1.Flex** (ARM)
3. OCPU: **1** (save the rest for other projects)
4. Memory: **6 GB**
5. Image: **Ubuntu 22.04** (or Oracle Linux)
6. Add your SSH public key
7. Create

### 3. Open Ports

In the instance's subnet security list, add ingress rules:
- Port **8080** (dashboard) — restrict to your IP if possible
- Port **22** (SSH) — should already be open

### 4. Install Docker

```bash
ssh ubuntu@<your-instance-ip>

# Install Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# Log out and back in

# Install Docker Compose plugin
sudo apt install docker-compose-plugin
```

### 5. Deploy the Bot

```bash
# Clone your repo (or scp the files)
git clone <your-repo-url> trade_bot
cd trade_bot

# Configure
cp config/.env.example config/.env
nano config/.env
# Set your mainnet API keys, Discord webhook, TRADING_MODE=mainnet

# Build and start
docker compose up -d

# Check logs
docker compose logs -f bot
```

### 6. Auto-Restart on Boot

Docker's `restart: unless-stopped` handles process restarts. For VM reboots:

```bash
sudo systemctl enable docker
```

### 7. Monitoring

- Dashboard: `http://<your-instance-ip>:8080`
- Logs: `docker compose logs -f bot`
- Discord notifications will alert you on errors/crashes

### 8. Updates

```bash
cd trade_bot
git pull
docker compose build
docker compose up -d
```

## Security Notes

- Use mainnet API keys with **trading only** permissions (no withdraw)
- Restrict dashboard port to your IP in OCI security list
- Keep your `.env` file secure
- Consider setting up a reverse proxy (Caddy/nginx) with HTTPS if exposing the dashboard
