import subprocess
import threading
import json
from datetime import datetime

def json_to_netscape(json_file="cookies.json", txt_file="cookies.txt"):
    with open(json_file, "r", encoding="utf-8") as f:
        cookies = json.load(f)

    lines = ["# Netscape HTTP Cookie File\n"]
    for c in cookies:
        domain = c.get("domain", "")
        flag = "TRUE" if domain.startswith(".") else "FALSE"
        path = c.get("path", "/")
        secure = "TRUE" if c.get("secure", False) else "FALSE"
        expiry = str(int(c.get("expires", 0))) if c.get("expires") else "0"
        name = c.get("name", "")
        value = c.get("value", "")

        line = f"{domain}\t{flag}\t{path}\t{secure}\t{expiry}\t{name}\t{value}\n"
        lines.append(line)

    with open(txt_file, "w", encoding="utf-8") as f:
        f.writelines(lines)

    print(f"✅ Converted {json_file} → {txt_file}")

if __name__ == "__main__":
    json_to_netscape()
    # # Start Flask in a thread
    # threading.Thread(target=run_flask).start()

    # # Start each bot in a subprocess/thread
    # threading.Thread(target=run_bot, args=("edb.py",)).start()
    # # threading.Thread(target=run_bot, args=("login.py",)).start()

    # # 🛡️ Keep main thread alive forever
    # while True:
    #     time.sleep(60)

