import subprocess
import sys
import os

# Запускаем api.py и bot.py одновременно
api = subprocess.Popen([sys.executable, "api.py"])
bot = subprocess.Popen([sys.executable, "bot.py"])

# Ждём оба процесса
api.wait()
bot.wait()