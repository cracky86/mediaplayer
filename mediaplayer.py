import sys
import time
import urllib.request
import io
import shutil
import termios
import tty
import threading
import signal
import select

import playerctl
import lyrics
import ansi
import screenbuffer

try:
  from PIL import Image
except ImportError:
  print("Pillow is required: pip install Pillow --break-system-packages")
  sys.exit(1)

# Layout settings
TOP_BAR = 1
STATUS_MESSAGE_DISPLAY = 2
INFO_SIZE = 20

# Art settings
CELL_ASPECT = 0.45
ART_ROWS_LANDSCAPE = 20

# Default color palette and icons
PANEL_BG = (12, 12, 18)
TEXT_PRI  = (220, 220, 235)
TEXT_SEC  = (120, 120, 140)
ACCENT    = (100, 220, 140)
ACCENT2   = (24, 24, 36)
DIM_LINE  = (35, 35, 50)

STATUS_ICONS = {"Playing": "\u25b6", "Paused": "\u23f8", "Stopped": "\u23f9"}    

# Load album art placeholder
try:
  ART_PLACEHOLDER = Image.open("missing.png").convert("RGB")
except:
  print("Missing placeholder image")
  sys.exit(1)

# Allow for skipping forwards and backwards
def seek_relative(secs, player=None):
  sign = "+" if secs >= 0 else "-"
  playerctl.playerctl("position", f"{abs(secs)}{sign}", player=player)

# Fetch image from an url
def fetch_image(url):
  if not url:
    return None
  try:
    if url.startswith("file://"):
      with open(url[7:], "rb") as f:
        return Image.open(io.BytesIO(f.read())).convert("RGB")
    req = urllib.request.Request(url, headers={"User-Agent": "mediaplayer/1.0"})
    with urllib.request.urlopen(req, timeout=5) as resp:
      return Image.open(io.BytesIO(resp.read())).convert("RGB")
  except Exception as err:
    return err

# Render album art as half block ASCII art
def render_art(album_art, target_rows=None, target_cols=None, get_resulting_size=False):
  original_width, original_height = album_art.size

  # Calculate the resulting size
  if target_cols is not None: # Scale to a fixed width
    resized_width = target_cols
    resized_height = int(resized_width / (original_width / original_height) * CELL_ASPECT * 2)
    resized_height = max(2, resized_height - resized_height % 2)
    target_rows = resized_height // 2
  else:
    if target_rows is None:
      target_rows = ART_ROWS_LANDSCAPE
    resized_height = target_rows * 2
    resized_width = int(original_width * resized_height / original_height)

  # If this flag is set to true, only output the resulting size
  if get_resulting_size:
    return [], resized_width, resized_height
    
  # Resize the image
  album_art = album_art.resize((resized_width, resized_height), Image.LANCZOS)
  album_art = album_art.load()

  lines = []
  for row in range(target_rows):
    line = ""
    top_y, bottom_y = row * 2, row * 2 + 1
    for x in range(resized_width):
      top_r, top_g, top_b = album_art[x, top_y]
      bottom_r, bottom_g, bottom_b = album_art[x, bottom_y]
      line += ansi.fg(top_r, top_g, top_b) + ansi.bg(bottom_r, bottom_g, bottom_b) + "\u2580"
    lines.append(line + ansi.RESET)
  return lines, resized_width, target_rows

# Show a progress bar
def bar(fraction, width, filled_color=(100, 220, 140), empty_color=(40, 40, 50)):
  fraction = max(0.0, min(1.0, fraction))
  n = int(fraction * width)
  return (
      ansi.fg(*filled_color) + "\u2500" * n
      + ansi.fg(*empty_color) + "\u2500" * (width - n)
      + ansi.RESET
  )

# Format time into a human readable format
def fmt_time(microseconds):
  try:
    s = int(float(microseconds)) // 1_000_000
  except (ValueError, TypeError):
    return "0:00"
  m, s = divmod(s, 60)
  return f"{m}:{s:02d}"

# Truncate a string if it's too long, as well as scroll it
def truncate(string, max_width, scroll_amount):
  original_string = string
  if len(string) <= max_width:
    scroll_amount = 0
    while len(string) < max_width:
      string += " "
  else:
    scroll_amount = scroll_amount % (len(string) + 1)
    if len(string) > max_width and scroll_amount > 0:
      string = string[scroll_amount:]
    if scroll_amount < 0:
      string = " " * abs(scroll_amount) + string

  if not len(original_string) < max_width:
    if len(string) < max_width:
      string += " | "
    i = 0
    while len(string) < max_width and not len(original_string) < max_width:
      string += original_string[i % len(original_string)]
      i += 1
  return string if len(string) <= max_width else string[:max_width]

# User interface
class PlayerUI:
  def __init__(self):
    self.player = None
    self.players = []
    self.meta = {}
    self.meta_prev = {}
    self.lyrics = None
    self.status_messages = ["", "", "Status messages will be displayed here"]

    self.draw_count = 0

    self.art_lines = []
    self.art_cols = 0
    self.art_rows = 0

    self.cached_art = ART_PLACEHOLDER
    self.last_art_url = ""

    self.running = True
    self.lock = threading.Lock()
    
    self.term_width, self.term_height = shutil.get_terminal_size()
    self.last_term_width, self.last_term_height = shutil.get_terminal_size()
    
    self.vol_until = 0.0

    self.screen = screenbuffer.ScreenBuffer()

    self.pos_us = 0.0
    self.pos_track = ""
    self.last_status = "Stopped"

  def print_status(self, message):
    if len(self.status_messages) > 0:
      if self.status_messages[-1] == message:
        return
    self.status_messages.append(message)

  # Update album art with the provided URL
  def update_art(self, url):
    url_changed = url != self.last_art_url
    layout_changed = (self.term_width, self.term_height) != (self.last_term_width, self.last_term_height)

    # Update album art only if the layout or URL changed
    if not url_changed and not layout_changed:
      return False

    # URL changed, force a full refresh and fetch the album art from there
    if url_changed:
      self.screen.invalidate()
      self.last_art_url = url
      self.cached_art = fetch_image(url)

    # Error occurred, push it onto the status messages list
    if type(self.cached_art) == str:
      self.print_status(f"Error fetching image: {self.cached_art}")
      self.cached_art = None

    art = self.cached_art
    if not art:
      art = ART_PLACEHOLDER

    # Obtain a column size that fits without pushing the info panel off screen
    target_cols = self.term_width
    while True:
      _, cols, rows = render_art(art, target_cols=target_cols, get_resulting_size=True)
      if self.term_height - rows - INFO_SIZE > 0:
        break
      target_cols -= 1
    
    # Render the album art
    lines, cols, rows = render_art(art, target_cols=target_cols)

    # Update stored art
    with self.lock:
      self.art_lines = lines
      self.art_cols  = cols
      self.art_rows  = rows
        
    return True

  def build_frame(self) -> dict[int, tuple[int, str]]:
    meta = self.meta
    status = meta.get("status", "Stopped")
    title = meta.get("title", "No media") or "No media"
    artist = meta.get("artist", "") or ""
    album = meta.get("album", "") or ""

    # Get the current position and length, and calculate the ratio between the 2 for the progress bar
    try:
      # Prevent position and progress bar from advancing if paused, fixes a browser side issue
      if status != "Playing":
        pos_us = self.pos_us
      else:
        pos_us = float(meta.get("position", "0")) * 1_000_000
        self.pos_us = pos_us
      len_us = float(meta.get("length", "0"))
      progress = pos_us / len_us if len_us > 0 else 0.0
    except (ValueError, ZeroDivisionError):
      pos_us, len_us, progress = 0.0, 0.0, 0.0

    # Get volume
    try:
      volume = float(meta.get("volume", "0"))
    except ValueError:
      volume = 0.0

    terminal_width, terminal_height = self.term_width, self.term_height
    panel_bg = ansi.bg(*PANEL_BG)
    frame: dict[int, tuple[int, str]] = {}

    # Top bar which shows the current player and controls
    player_label = f"  {self.player or 'No player'}"
    key_hints = "[ </> ] seek  [ -/+ ] vol  [ p ] pause  [ n/b ] skip  [ TAB ] player [ u ] update lyrics [ q ] quit"

    # Get the spacing between player label and hints, if there isn't enough space, show just the hints
    spaces = terminal_width - len(player_label) - len(key_hints) - 2
    if spaces >= 2:
      frame[TOP_BAR] = (1,
        ansi.bg(*ACCENT2) + ansi.fg(*TEXT_PRI) + ansi.bold()
        + player_label
        + " " * max(1, spaces)
        + key_hints + "  "
        + ansi.RESET
      )
    else:
      spaces = terminal_width - len(key_hints)
      frame[TOP_BAR] = (1,
        ansi.bg(*ACCENT2) + ansi.fg(*TEXT_PRI) + ansi.bold()
        + truncate(key_hints, terminal_width, self.draw_count // 3)
        + " " * max(0, spaces)
        + ansi.RESET
      )

    # Status messages
    status_line = STATUS_MESSAGE_DISPLAY
    max_width = max(10, terminal_width - 3)
    def status_row(content=""):
      nonlocal status_line
      frame[status_line] = (0, panel_bg + content + ansi.RESET)
      status_line += 1

    # Display the 3 most recent status messages
    for i, status_message in enumerate(self.status_messages):
      if i < len(self.status_messages) - 3:
        continue
      status_row(ansi.bg(*ACCENT2) + ansi.fg(*TEXT_PRI) + f"{i - 1}" + ansi.bg(*PANEL_BG) + ansi.fg(*TEXT_SEC) + ansi.bold() + truncate(f" {status_message}", max_width, self.draw_count // 3))
      
    # Album art (rows 3 to 3+art_rows-1)
    with self.lock:
      art = list(self.art_lines)
      art_cols = self.art_cols
      art_rows = self.art_rows

    ART_START = 6
    for i, line in enumerate(art):
      frame[ART_START + i] = (1, line)

    # Info panel
    icon = STATUS_ICONS.get(status, "?")
    icon_color = ACCENT if status == "Playing" else TEXT_SEC
    show_volume   = time.time() < self.vol_until
    volume_color  = ACCENT if show_volume else DIM_LINE
    
    # Get lyrics data
    position_seconds = pos_us / 1_000_000
    current_line_index = 0
    if self.lyrics:
      try:
        current_line_index = self.lyrics.index(lyrics.get_current_line(self.lyrics, position_seconds))
      except:
        pass
    
    info_col = 2
    max_width = max(10, terminal_width - 3)
    row = ART_START + art_rows + 1
    
    # Pad to terminal width
    def prow(content=""):
      nonlocal row
      frame[row] = (info_col, panel_bg + content + ansi.RESET)
      row += 1

    # Show title, artist, and album
    prow()
    prow(ansi.fg(*icon_color) + ansi.bold() + f"{icon}  {status}" + ansi.RESET)
    prow()
    prow(ansi.fg(*TEXT_PRI) + ansi.bold() + truncate(title, max_width, self.draw_count // 2))
    prow(ansi.fg(*ACCENT) + truncate(artist, max_width, self.draw_count // 2))
    prow(ansi.fg(*TEXT_SEC) + ansi.italic() + truncate(album, max_width, self.draw_count // 2))
    prow()

    # Display lyrics
    if self.lyrics:
      if current_line_index != 0:
        prow(ansi.fg(*TEXT_SEC) + ansi.italic() + truncate(self.lyrics[current_line_index - 1][1],  max_width, 0))
      else:
        prow()
      prow(ansi.fg(*ACCENT) + ansi.italic() + truncate(self.lyrics[current_line_index][1], max_width, 0))
      if current_line_index != len(self.lyrics) - 1:
        prow(ansi.fg(*TEXT_SEC) + ansi.italic() + truncate(self.lyrics[current_line_index + 1][1],  max_width, 0))
      else:
        prow()
    else:
      prow(ansi.fg(*TEXT_SEC) + " " * max_width)
      prow(ansi.fg(*TEXT_SEC) + ansi.italic() + truncate("Lyrics data unavailable",  max_width, 0))
      prow(ansi.fg(*TEXT_SEC) + " " * max_width)
    prow()
            
    bar_width   = max(8, terminal_width - 14)
    position_string = fmt_time(pos_us)
    length_string = fmt_time(len_us)
    prow(ansi.fg(*TEXT_SEC) + f"{position_string}  " + bar(progress, bar_width) + f"  {length_string}")
    prow()
    prow(ansi.fg(*TEXT_SEC) + "vol  "
         + bar(volume, 20, filled_color=volume_color, empty_color=DIM_LINE)
         + f"  {int(volume * 100):3d}%")
    prow()

    if row + len(self.players) + 1 < terminal_height:
      prow(ansi.fg(*TEXT_SEC) + ansi.dim_esc() + "Players:")
      for p in self.players:
        marker = ansi.fg(*ACCENT) + "\u25b6 " if p == self.player else ansi.fg(*TEXT_SEC) + "  "
        prow(marker + ansi.fg(*TEXT_PRI) + p + ansi.RESET)

    # Bottom status bar (last row)
    hint = f" TAB: switch player   {terminal_width}x{terminal_height}"
    frame[terminal_height] = (1, ansi.bg(*DIM_LINE) + ansi.fg(*TEXT_SEC) + hint + ansi.RESET)

    return frame

  def draw(self):
    self.term_width, self.term_height = shutil.get_terminal_size()
    frame = self.build_frame()
    self.screen.draw(frame)

  # input
  def handle_key(self, key):
    player = self.player
    if key in ("q", "Q", "\x03"):
      self.running = False
    elif key in (" ", "p"):
      playerctl.send_cmd("play-pause", player=player)
      self.screen.invalidate()
    elif key == "n":
      playerctl.send_cmd("next", player=player)
      self.screen.invalidate()
    elif key == "b":
      playerctl.send_cmd("previous", player=player)
      self.screen.invalidate()
    elif key == "\x1b[C":
      seek_relative(10, player=player)
      self.pos_track = ""   # force resync on next poll
    elif key == "\x1b[D":
      seek_relative(-10, player=player)
      self.pos_track = ""   # force resync on next poll
    elif key in ("+", "="):
      playerctl.set_volume(self.safe_vol() + 0.05)
      self.vol_until = time.time() + 2
    elif key in ("-", "_"):
      playerctl.set_volume(self.safe_vol() - 0.05)
      self.vol_until = time.time() + 2
    elif key == "\t":
      if self.players:
        idx = self.players.index(self.player) if self.player in self.players else -1
        self.player = self.players[(idx + 1) % len(self.players)]
    elif key == "u":
      self.print_status("Force updating lyrics")
      self.lyrics = None

  def safe_vol(self):
    try:
      return float(self.meta.get("volume", "0.5"))
    except ValueError:
      return 0.5

  def input_thread(self):
    fd  = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
      tty.setraw(fd)
      while self.running:
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            more = ""
            try:
              r, _, _ = select.select([sys.stdin], [], [], 0.05)
              if r:
                more = sys.stdin.read(2)
            except Exception:
              pass
            key = ch + more
        else:
          key = ch
        self.handle_key(key)
    finally:
      termios.tcsetattr(fd, termios.TCSADRAIN, old)

  def run(self):
    sys.stdout.write(ansi.ALT_ON + ansi.HIDE_CURSOR)
    sys.stdout.flush()

    prev_size = (self.term_width, self.term_height)
    self.last_term_width, self.last_term_height = prev_size

    # Force full redraw on resize
    def on_resize(sig, frame):
      self.term_width, self.term_height = shutil.get_terminal_size()
      self.screen.invalidate()
      self.draw()

    signal.signal(signal.SIGWINCH, on_resize)

    t = threading.Thread(target=self.input_thread, daemon=True)
    t.start()

    try:
      while self.running:
        self.term_width, self.term_height = shutil.get_terminal_size()

        # Detect resize that SIGWINCH missed (e.g. some terminal muxers)
        new_size = (self.term_width, self.term_height)
        
        if new_size != prev_size:
          self.screen.invalidate()
          prev_size = new_size

        # Get media players
        self.players = playerctl.get_players()
        if not self.player and self.players:
          self.player = self.players[0]
        elif self.player and self.player not in self.players:
          self.player = self.players[0] if self.players else None

        # Get metadata and the album art URL
        self.meta_prev = self.meta
        self.meta = playerctl.get_metadata(self.player)
        art_url = self.meta.get("art_url", "")

        # Update lyrics
        if not self.lyrics and self.draw_count % 100 == 0:
          title = self.meta.get("title", "") or ""
          artist = self.meta.get("artist", "") or ""
          album = self.meta.get("album", "") or ""
          length = self.meta.get("length", "") or ""

          try:
            self.print_status(f"Attempting to fetch lyrics for {title}")
            lyrics_info, lrc_file = lyrics.get_lyrics(title, artist, album, length)
            self.print_status(lyrics_info)
            self.lyrics = lyrics.convert_lrc(lrc_file)
          except Exception as err:
            self.print_status(f"LRCERROR: {err}")
            self.lyrics = None
                
        # If art changed, the new art lines will be diffed normally, no need to invalidate the whole screen.
        self.update_art(art_url)
        self.draw()
        self.draw_count += 1
        time.sleep(0.05)

    finally:
      sys.stdout.write(ansi.ALT_OFF + ansi.SHOW_CURSOR + ansi.RESET)
      sys.stdout.flush()
      
if __name__ == "__main__":
  if shutil.which("playerctl") is None:
    print("Error: playerctl not found.")
    print("  Install with:  sudo apt install playerctl")
    sys.exit(1)

  PlayerUI().run()
