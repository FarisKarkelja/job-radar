"""Slanje notifikacija preko ntfy (https://ntfy.sh)."""
import logging
import os
import time

import requests

log = logging.getLogger("radar.notify")

SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
TOPIC = os.environ.get("NTFY_TOPIC", "").strip()

sent_count = 0
outbox = []  # za izvještaj kad nema topica (lokalni test)


def send(title: str, message: str, *, priority: int = 3, tags=None, click: str = None,
         actions=None) -> bool:
    global sent_count
    payload = {
        "topic": TOPIC,
        "title": title[:250],
        "message": message[:3800],
        "priority": int(priority),
        "tags": tags or [],
    }
    if click:
        payload["click"] = click
        payload["actions"] = actions or [{"action": "view", "label": "Otvori", "url": click}]
    if not TOPIC:
        outbox.append(payload)
        log.info("[DRY RUN] %s | %s", title, message.replace("\n", " / ")[:200])
        return True
    for attempt in range(4):
        try:
            r = requests.post(SERVER, json=payload, timeout=20)
            if r.status_code == 429:
                wait = 10 * (attempt + 1)
                log.warning("ntfy rate limit, čekam %ss", wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            sent_count += 1
            time.sleep(1.2)  # ne gušiti ntfy.sh
            return True
        except requests.RequestException as e:
            log.warning("ntfy greška: %s", e)
            time.sleep(3)
    return False
