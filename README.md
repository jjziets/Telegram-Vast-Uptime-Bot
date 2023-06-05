# Uptime monitor

This is a set of scripts for monitoring machine crashes. Run the client on your vast machine and the server on a remote one. You get notifications on Telegram if no heartbeats are sent within the timeout (default 12 seconds).

I recommend a $2.50 or $3.50 server from Vultr. Server location must be set to New Jersey for the cheap plans. Use my referral link for $100 credit.
https://www.vultr.com/?ref=8581277-6G
*based on leona / vast.ai-tools
## Setup Telegram bot

1. Search for the contact BotFather in app
2. Send /newbot as a message
3. Enter the details and then copy the token it gives you
4. Create a group chat with your bot and send "/start"

### Config setup
Create a file called ".env" in this directory. You can copy the same .env between server and client, only some are absolutely required though.
```bash
CHAT_ID=                  # SERVER only - See below for steps
TELEGRAM_TOKEN=<token>    # SERVER - Token as given in previous step
FAIL_TIMEOUT=5            # SERVER - If no pings in 5 seconds, send alert.
PING_INTERVAL=2           # CLIENT - How often to send pings
API_KEY=asecretkey        # SERVER+CLIENT - A random unique key to authenticate the client
SERVER_ADDR=192.168.1.150 # SERVER+CLIENT - Address the client will use to send pings
SERVER_PORT=5000          # SERVER+CLIENT - Port of the server.
```

If you get too many false notifications, try increase the FAIL_TIMEOUT.

## Server setup

Install dependencies
```bash
apt install python3 python3-pip
pip install -r requirements.txt
```

### Get chat id
After adding your telegram_token to your .env file and sending /start to your bot, run the below to get the chat_id and also add it to the .env file.
```bash
./run_server.sh chat_id
```
or for groups and channles: 
Log in with your account to Telegram web and select the Telegram group. Then, in the URL of your web browser you should see something similar to https://web.telegram.org/k/#-XXXXXXXXX. Then, the ID you need to use for the Telegram group is -XXXXXXXXX, where each X character represents a number. Remember to include the minus symbol preceding the numbers.

Finally, for private channels in Telegram, select the private channel in Telegram web. Then, in the URL of your web browser you should see something similar to https://web.telegram.org/k/#-YYYYYYYYYY. Here, the ID you need to use for the private channel is -100YYYYYYYYYY. That is, you need to include a 100 between the minus symbol and the YYYYYYYYYY numbers.
you will need to addd your bot to the group or channel 

### Run server
```bash
chmod +x run_server.sh
./run_server.sh
```

Start on boot (optional) change path to uptime-monitor location
```bash
(crontab -l; echo "@reboot screen -dmS uptime-server /root/uptime-monitor/run_server.sh") | crontab -
```

## Client setup

### Run client. Pass a name (no spaces) for your worker.
```bash
./run_client.sh <worker_name>
```

Start on boot (optional) change path and <worker_name>
```bash
(crontab -l; echo "@reboot screen -dmS uptime-client /root/uptime-monitor/run_client.sh <worker_name>") | crontab -
```
