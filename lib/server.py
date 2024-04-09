from flask import jsonify, request, Flask, render_template
from queue import Queue
import threading  # Import the threading module
from threading import Timer, Event
import os,  time
from datetime import datetime, timedelta
from utilities import telegram_request
app = Flask(__name__)

timers = {}
pause_events = {}
# Create a message queue
last_seen = {}  # Dictionary to keep track of the last seen time for each worker
message_queue = Queue()

def message_sender():
    while True:
        # Retrieve the next message from the queue
        message = message_queue.get()
        try:
            # Attempt to send the message
            while True:
                response = telegram_request("/sendMessage?chat_id=" + os.getenv("CHAT_ID") + "&text=" + message)
                if response.get('error_code') == 429:
                    # If rate limited, wait and retry
                    retry_after = response.get('parameters', {}).get('retry_after', 1)
                    time.sleep(retry_after)
                else:
                    break
        finally:
            # Mark the message as processed
            message_queue.task_done()

# Start the message sender thread
threading.Thread(target=message_sender, daemon=True).start()

def missed_ping(worker):
    pause_event = pause_events.get(worker)
    if pause_event is not None:
        pause_event.wait()

    current_time = datetime.now()
    last_ping = last_seen.get(worker, current_time)
    # Only notify for missed ping if the last ping is beyond the FAIL_TIMEOUT
    if (current_time - last_ping) > timedelta(seconds=int(os.getenv("FAIL_TIMEOUT"))):
        print("Missed ping for", worker)
        message_queue.put(worker + " is down")
    else:
        print("False alarm for", worker)

    if worker in timers:
        del timers[worker]
    if worker in pause_events:
        del pause_events[worker]

@app.route('/ping/<worker_id>', methods=['GET'])
def app_stats(worker_id):
    api_key = request.args.get('api_key')

    if api_key != os.getenv("API_KEY"):
        return jsonify({
            "status": 0,
            "msg": "Invalid API key"
        })

    current_time = datetime.now()
    last_seen[worker_id] = current_time  # Update the last seen time

    if worker_id in timers:
        print("Cancelling timer for:", worker_id)
        timers[worker_id].cancel()

    print("Creating timer for:", worker_id)
    pause_event = Event()
    pause_event.set()
    pause_events[worker_id] = pause_event
    timers[worker_id] = Timer(int(os.getenv("FAIL_TIMEOUT")), missed_ping, [worker_id])
    timers[worker_id].start()

    return jsonify({
        "status": 1,
        "msg": "Heartbeat received"
    })

@app.route('/')
def index():
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    active_workers = sorted(list(timers.keys()))  # Assumes timers contains worker_ids that are up
    return render_template('index.html', current_time=current_time, active_workers=active_workers)

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=os.getenv("SERVER_PORT"))
