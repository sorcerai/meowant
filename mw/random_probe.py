import time
import random
import sys
from datetime import datetime

class RandomProbe:
    """Randomly drops a snack from one of the feeders during waking hours to probe eating habits."""
    def __init__(self, feeder_devs, feeder_monitors, min_hours=2, max_hours=5, start_hour=8, end_hour=22):
        self.feeder_devs = feeder_devs
        self.feeder_monitors = feeder_monitors
        self.min_hours = min_hours
        self.max_hours = max_hours
        self.start_hour = start_hour
        self.end_hour = end_hour

    def run(self):
        while True:
            # Sleep for a random interval
            sleep_s = random.randint(int(self.min_hours * 3600), int(self.max_hours * 3600))
            time.sleep(sleep_s)
            
            now = datetime.now()
            # Only drop during waking hours to avoid startling the cats at night
            if not (self.start_hour <= now.hour <= self.end_hour):
                continue
                
            if not self.feeder_devs:
                continue
                
            # Pick a random feeder
            label = random.choice(list(self.feeder_devs.keys()))
            dev = self.feeder_devs[label]
            mon = self.feeder_monitors.get(label)
            
            st = dev.status()
            if st.get("online"):
                # Dispense 1 portion
                success = dev.feed(1)
                if success and mon:
                    mon.note_manual_feed()
                    print(f"[random-probe] Successfully dropped 1 portion to '{label}'.", file=sys.stderr)
            else:
                print(f"[random-probe] Feeder '{label}' offline. Probe skipped.", file=sys.stderr)
