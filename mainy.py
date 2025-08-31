import subprocess
import threading
import time
from flask import Flask, send_from_directory

# ----------- Flask App ----------- #
app = Flask("")

@app.route("/")
def home():
    return send_from_directory("static", "bot_status.html")

def run_flask():
    app.run(host="0.0.0.0", port=8080)

# ----------- Run Bots as Subprocesses ----------- #
def run_bot(file):
    subprocess.Popen(["python3", file])

# ----------- Main ----------- #
if __name__ == "__main__":
    # Start Flask in a thread
    threading.Thread(target=run_flask).start()

    # Start each bot in a subprocess/thread
    threading.Thread(target=run_bot, args=("epic_yt_downloader.py",)).start()
    threading.Thread(target=run_bot, args=("login.py",)).start()

    # 🛡️ Keep main thread alive forever
    while True:
        time.sleep(60)
