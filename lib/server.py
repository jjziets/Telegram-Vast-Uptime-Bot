from flask import jsonify, request, Flask
from threading import Timer, Event
import os
from utilities import telegram_request
app = Flask(__name__)

timers = {}
pause_events = {}

def missed_ping(worker):
    pause_event = pause_events.get(worker)
    if pause_event is not None:
        pause_event.wait()

    del timers[worker]
    print("Missed ping for", worker)
    
    while True:
        response = telegram_request("/sendMessage?chat_id=" + os.getenv("CHAT_ID") + "&text=" + worker + " is down")
        if response.get('error_code') == 429:
            retry_after = response.get('parameters', {}).get('retry_after', 0) + 1
            print(f"Pausing all timers for {retry_after}s...")
            
            for w, e in pause_events.items():
                e.clear()

            time.sleep(retry_after)

            for w, e in pause_events.items():
                e.set()
        else:
            break

@app.route('/ping/<worker_id>', methods=['GET'])
def app_stats(worker_id):
    api_key = request.args.get('api_key')

    if api_key != os.getenv("API_KEY"):
        return jsonify({
            "status": 0,
            "msg": "Invalid API key"
        })

    if worker_id in timers:
        print("Cancelling timer for:", worker_id)
        timers[worker_id].cancel()
    else:
        telegram_request("/sendMessage?chat_id=" + os.getenv("CHAT_ID") + "&text=" + worker_id + " is up")

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

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=os.getenv("SERVER_PORT"))
