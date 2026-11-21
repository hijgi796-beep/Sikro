"""يشغّل أكثر من بوت في نفس الخدمة. عدّل قائمة BOTS فقط."""
import os
import subprocess
import sys
import time

# (اسم الملف, المتغيرات الخاصة بهذا البوت: اسم_المتغير_داخل_البوت → اسم_المتغير_في_Railway)
BOTS = [
    ("bot.py",  {}),  # البوت الأول: يستخدم BOT_TOKEN و DATABASE_URL كما هي
    ("bot2.py", {"BOT_TOKEN": "BOT2_TOKEN", "DATABASE_URL": "DATABASE_URL2", "PORT": "PORT2"}),
]

def start(script, mapping):
    env = dict(os.environ)
    for inner, outer in mapping.items():
        if os.environ.get(outer): env[inner] = os.environ[outer]
        elif inner == "PORT": env["PORT"] = "8081"   # بوت ثانٍ بلا دومين خاص
        else: print(f"[run] missing {outer} for {script}", flush=True); return None
    return subprocess.Popen([sys.executable, script], env=env)

procs = {s: start(s, m) for s, m in BOTS}
while True:
    for (script, mapping) in BOTS:
        p = procs.get(script)
        if p is None or p.poll() is not None:
            print(f"[run] restarting {script}", flush=True)
            time.sleep(3)
            procs[script] = start(script, mapping)
    time.sleep(5)
