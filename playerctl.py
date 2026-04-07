import subprocess

def get_volume():
  r = subprocess.run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"], capture_output=True, text=True)
  elements = r.stdout.split(" ")
  volume = "0"
  for element in elements:
    if "%" in element:
      volume = f"{float(element[0:-1:]) / 100}"
      break
  return volume

# playerctl wrapper
def playerctl(*args, player=None):
    cmd = ["playerctl"]
    if player:
        cmd += ["-p", player]
    cmd += list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
        return r.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""

# pactl wrapper for volume adjustment
def pactl(*args):
    cmd = ["pactl"]
    cmd += list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
        return r.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""

      
def get_players():
    out = playerctl("-l")
    return [p for p in out.splitlines() if p] if out else []

def get_metadata(player=None):
    fields = {
        "title":   "xesam:title",
        "artist":  "xesam:artist",
        "album":   "xesam:album",
        "art_url": "mpris:artUrl",
        "length":  "mpris:length",
    }
    meta = {}
    for key, field in fields.items():
        meta[key] = playerctl("metadata", field, player=player) or ""
    meta["status"]   = playerctl("status",   player=player) or "Stopped"
    meta["position"] = playerctl("position", player=player) or "0"
    meta["volume"]   = get_volume() or "0.0"
    return meta

def send_cmd(cmd, player=None):    playerctl(cmd, player=player)
def set_volume(vol):  pactl("set-sink-volume", "@DEFAULT_SINK@", f"{int(max(0, min(1, vol)) * 100)}%")
