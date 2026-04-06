import json
import urllib.parse
import hashlib
import subprocess
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

try:
    from PIL import Image
except ImportError:
    print("Pillow is required: pip install Pillow --break-system-packages")
    sys.exit(1)

# Settings
CACHE_DIR = "lrc_cache/"
CELL_ASPECT = 0.45
ART_ROWS_LANDSCAPE = 20

# Default color palette and icons
PANEL_BG = (12, 12, 18)
TEXT_PRI  = (220, 220, 235)
TEXT_SEC  = (120, 120, 140)
ACCENT    = (100, 220, 140)
ACCENT2   = (80, 160, 220)
DIM_LINE  = (35, 35, 50)

STATUS_ICONS = {"Playing": "\u25b6", "Paused": "\u23f8", "Stopped": "\u23f9"}

# Some ANSI codes
ESC = "\033"
RESET = f"{ESC}[0m"
CLEAR = f"{ESC}[2J{ESC}[H"
HIDE_CURSOR = f"{ESC}[?25l"
SHOW_CURSOR = f"{ESC}[?25h"
ALT_ON = f"{ESC}[?1049h"
ALT_OFF = f"{ESC}[?1049l"

# ANSI helpers
def move(row, col): return f"{ESC}[{row};{col}H"
def fg(r, g, b): return f"{ESC}[38;2;{r};{g};{b}m"
def bg(r, g, b): return f"{ESC}[48;2;{r};{g};{b}m"
def bold(): return f"{ESC}[1m"
def dim_esc(): return f"{ESC}[2m"
def italic(): return f"{ESC}[3m"

    
# The ScreenBuffer class is used for partial updates of what's currently on screen
class ScreenBuffer:
  def __init__(self):
    self._prev: dict[int, tuple[int, str]] = {}
    self._dirty = True

  # Invalidate screen buffer, forcing a redraw
  def invalidate(self):
    self._dirty = True

  # Draw what's currently in the buffer
  def draw(self, frame: dict[int, tuple[int, str]]) -> None:
    out = []

    if self._dirty: # Redraw all
      out.append(CLEAR)
      for row, (col, content) in sorted(frame.items()):
        out.append(move(row, col) + content + RESET)

        # Erase any rows that are no longer part of the frame
        for row in self._prev:
          if row not in frame:
            out.append(move(row, 1) + f"{ESC}[K")
      self._dirty = False
    else: # Only update modified rows
      for row, (col, content) in frame.items():
        prev = self._prev.get(row)
        if prev is None or prev != (col, content):
          out.append(move(row, col) + content + RESET)
        # Erase empty rows
        for row in self._prev:
          if row not in frame:
            out.append(move(row, 1) + f"{ESC}[K")

    # Set the previous frame to the current one
    self._prev = dict(frame)

    # Display the buffer
    if out:
      sys.stdout.write("".join(out))
      sys.stdout.flush()

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
def render_art(album_art, target_rows=None, target_cols=None):
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
      line += fg(top_r, top_g, top_b) + bg(bottom_r, bottom_g, bottom_b) + "\u2580"
    lines.append(line + RESET)
  return lines, resized_width, target_rows

# Generate placeholder album art
def placeholder_art(rows, cols):
  lines = []
  for i in range(rows):
    lines.append(bg(i, i, i) + " " * cols + RESET)
  return lines, cols, rows

# Show a progress bar
def bar(fraction, width, filled_color=(100, 220, 140), empty_color=(40, 40, 50)):
  fraction = max(0.0, min(1.0, fraction))
  n = int(fraction * width)
  return (
      fg(*filled_color) + "\u2500" * n
      + fg(*empty_color) + "\u2500" * (width - n)
      + RESET
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

    self._cached_img   = None
    self._last_art_url = ""
    self._last_art_key = None   # (portrait, constraining_dim)

    self.running = True
    self.lock = threading.Lock()
    
    self.term_w, self.term_h = shutil.get_terminal_size()
    self._vol_until = 0.0

    self._screen = ScreenBuffer()

    self._pos_us     = 0.0
    self._pos_ts     = 0.0
    self._pos_track  = ""
    self._last_status = "Stopped"

  @property
  def portrait_mode(self):
    return self.term_h > self.term_w * CELL_ASPECT

  def print_status(self, message):
    if len(self.status_messages) > 0:
      if self.status_messages[-1] == message:
        return
    self.status_messages.append(message)

  def _art_key(self):
    if self.portrait_mode:
      return ("portrait", self.term_w)
    return ("landscape", ART_ROWS_LANDSCAPE)

  def update_art(self, url):
    key = self._art_key()
    url_changed = url != self._last_art_url
    layout_changed = key != self._last_art_key
    
    if not url_changed and not layout_changed:
      return False

    if url_changed:
      self._screen.invalidate()
      self._last_art_url = url
      self._cached_img = fetch_image(url)

    # Error occurred, push that onto the status messages list
    if type(self._cached_img) == str:
      self.print_status(f"Error fetching image: {self._cached_img}")
      self._cached_img = None

    img = self._cached_img
    portrait = key[0] == "portrait"
    dim = key[1]

    if portrait:
      if img:
        lines, cols, rows = render_art(img, target_cols=int(dim / 4 * 3))
      else:
        est_rows = max(4, int(int(dim / 4 * 3) * CELL_ASPECT))
        est_rows -= est_rows % 2
        lines, cols, rows = placeholder_art(est_rows, int(dim / 4 * 3))
    else:
      if img:
        lines, cols, rows = render_art(img, target_rows=dim)
      else:
        est_rows = max(4, int(dim * CELL_ASPECT))
        est_rows -= est_rows % 2
        lines, cols, rows = placeholder_art(est_rows, 40)

    self._last_art_key = key
    with self.lock:
      self.art_lines = lines
      self.art_cols  = cols
      self.art_rows  = rows
        
    return True

  def _build_frame(self) -> dict[int, tuple[int, str]]:
    meta = self.meta
    status = meta.get("status", "Stopped")
    title  = meta.get("title",  "No media") or "No media"
    artist = meta.get("artist", "") or ""
    album  = meta.get("album",  "") or ""

    try:
      pos_us = float(meta.get("position", "0")) * 1_000_000
      len_us = float(meta.get("length", "0"))
      progress = pos_us / len_us if len_us > 0 else 0.0
    except (ValueError, ZeroDivisionError):
      pos_us, len_us, progress = 0.0, 0.0, 0.0

    if status != "Playing":
      pos_us = self._pos_us
    else:
      self._pos_us = pos_us
      
    try:
      volume = float(meta.get("volume", "0"))
    except ValueError:
      volume = 0.0

    terminal_width, terminal_height = self.term_w, self.term_h
    panel_bg = bg(*PANEL_BG)
    frame: dict[int, tuple[int, str]] = {}

    # Top bar
    player_label = f"  {self.player or 'No player'}"
    if self.portrait_mode:
      key_hints = "[ </> ] seek  [ -/+ ] vol  [ p ] pause  [ n/b ] skip  [ q ] quit"
    else:
      key_hints = "[ </> ] seek  [ -/+ ] vol  [ p ] pause  [ n/b ] skip  [ TAB ] player  [ q ] quit"
    spaces = terminal_width - len(player_label) - len(key_hints) - 2
    if spaces >= 2:
      frame[1] = (1,
        bg(*ACCENT2) + fg(*PANEL_BG) + bold()
        + player_label
        + " " * max(1, spaces)
        + key_hints + "  "
        + RESET
      )
    else:
      spaces = terminal_width - len(key_hints)
      frame[1] = (1,
        bg(*ACCENT2) + fg(*PANEL_BG) + bold()
        + truncate(key_hints, terminal_width, self.draw_count // 3)
        + " " * max(0, spaces)
        + RESET
      )


    # Status messages
    status_line = 2
    max_width = max(10, terminal_width - 3)
    def status_row(content=""):
      nonlocal status_line
      frame[status_line] = (0, panel_bg + content + RESET)
      status_line += 1

    for status_message in self.status_messages[-1:-4:-1]:
      status_row(bg(*PANEL_BG) + fg(*ACCENT2) + bold() + truncate(status_message, max_width, self.draw_count // 3))
      
    # art (rows 3 to 3+art_rows-1)
    with self.lock:
      art      = list(self.art_lines)
      art_cols = self.art_cols
      art_rows = self.art_rows

    ART_START = 5
    for i, line in enumerate(art):
      frame[ART_START + i] = (1, line)

    # Info panel
    icon = STATUS_ICONS.get(status, "?")
    icon_color = ACCENT if status == "Playing" else TEXT_SEC
    show_volume   = time.time() < self._vol_until
    volume_color  = ACCENT if show_volume else DIM_LINE
    
    # Get lyrics data
    position_seconds = pos_us / 1_000_000
    current_line_index = 0
    if self.lyrics:
      try:
        current_line_index = self.lyrics.index(lyrics.get_current_line(self.lyrics, position_seconds))
      except:
        pass
    
    if self.portrait_mode:
      info_col = 2
      max_width = max(10, terminal_width - 3)
      row = ART_START + art_rows + 1

      # Pad to terminal width
      def prow(content=""):
        nonlocal row
        frame[row] = (info_col, panel_bg + content + RESET)
        row += 1

      # Show title, artist, and album
      prow(fg(*icon_color) + bold() + f"{icon}  {status}" + RESET)
      prow()
      prow(fg(*TEXT_PRI) + bold() + truncate(title, max_width, self.draw_count // 2))
      prow(fg(*ACCENT) + truncate(artist, max_width, self.draw_count // 2))
      prow(fg(*TEXT_SEC) + italic() + truncate(album, max_width, self.draw_count // 2))
      prow()

      # Display lyrics
      if self.lyrics:
        if current_line_index != 0:
          prow(fg(*TEXT_SEC) + italic() + truncate(self.lyrics[current_line_index - 1][1],  max_width, 0))
        else:
          prow()
        prow(fg(*ACCENT) + italic() + truncate(self.lyrics[current_line_index][1], max_width, 0))
        if current_line_index != len(self.lyrics) - 1:
          prow(fg(*TEXT_SEC) + italic() + truncate(self.lyrics[current_line_index + 1][1],  max_width, 0))
        else:
          prow()
      else:
        prow(fg(*TEXT_SEC) + " " * max_width)
        prow(fg(*TEXT_SEC) + italic() + truncate("Lyrics data unavailable",  max_width, 0))
        prow(fg(*TEXT_SEC) + " " * max_width)
      prow()
            
      bar_width   = max(8, terminal_width - 14)
      position_string = fmt_time(pos_us)
      length_string = fmt_time(len_us)
      prow(fg(*TEXT_SEC) + f"{position_string}  " + bar(progress, bar_width) + f"  {length_string}")
      prow()
      prow(fg(*TEXT_SEC) + "vol  "
           + bar(volume, 20, filled_color=volume_color, empty_color=DIM_LINE)
           + f"  {int(volume * 100):3d}%")
      prow()

      if row + len(self.players) + 1 < terminal_height:
        prow(fg(*TEXT_SEC) + dim_esc() + "Players:")
        for p in self.players:
          marker = fg(*ACCENT) + "\u25b6 " if p == self.player else fg(*TEXT_SEC) + "  "
          prow(marker + fg(*TEXT_PRI) + p + RESET)
    else:
      info_col = art_cols + 3
      max_width = max(10, terminal_width - info_col - 1)
      row = ART_START

      def lrow(content=""):
        nonlocal row
        frame[row] = (0, frame[row][1] + panel_bg + content + RESET)
        row += 1

      lrow(fg(*icon_color) + bold() + f"{icon}  {status}" + RESET)
      lrow()
      lrow(fg(*TEXT_PRI) + bold()   + truncate(title,  max_width, self.draw_count // 2))
      lrow(fg(*ACCENT)              + truncate(artist, max_width, self.draw_count // 2))
      lrow(fg(*TEXT_SEC) + italic() + truncate(album,  max_width, self.draw_count // 2))
      lrow()

      # Display lyrics
      if self.lyrics:
        if current_line_index != 0:
          lrow(fg(*TEXT_SEC) + italic() + truncate(self.lyrics[current_line_index - 1][1],  max_width, 0))
        else:
          lrow()
        lrow(fg(*ACCENT) + italic() + truncate(self.lyrics[current_line_index][1], max_width, 0))
        if current_line_index != len(self.lyrics) - 1:
          lrow(fg(*TEXT_SEC) + italic() + truncate(self.lyrics[current_line_index + 1][1],  max_width, 0))
        else:
          lrow()
      else:
        lrow()
        lrow(fg(*TEXT_SEC) + italic() + truncate("Lyrics data unavailable",  max_width, 0))
        lrow()
      lrow()      
      
      bar_w   = min(max_width, 40)
      pos_str = fmt_time(pos_us)
      len_str = fmt_time(len_us)
      lrow(fg(*TEXT_SEC) + f"{pos_str}  " + bar(progress, bar_w - 12) + f"  {len_str}")
      lrow()
      lrow(fg(*TEXT_SEC) + "vol  "
           + bar(volume, 20, filled_color=volume_color, empty_color=DIM_LINE)
           + f"  {int(volume * 100):3d}%")
      lrow()
      lrow(fg(*TEXT_SEC) + dim_esc() + "Players:")
      for p in self.players:
        marker = fg(*ACCENT) + "\u25b6 " if p == self.player else fg(*TEXT_SEC) + "  "
        lrow(marker + fg(*TEXT_PRI) + p + RESET)

    # ── bottom status bar (last row) ───────────────────────────────────

    mode_tag = "portrait" if self.portrait_mode else "landscape"
    hint = f" TAB: switch player   {terminal_width}x{terminal_height} [{mode_tag}] "
    frame[terminal_height] = (1, bg(*DIM_LINE) + fg(*TEXT_SEC) + hint + RESET)

    return frame

  def draw(self):
    self.term_w, self.term_h = shutil.get_terminal_size()
    frame = self._build_frame()
    self._screen.draw(frame)

  # input
  def handle_key(self, key):
    player = self.player
    if key in ("q", "Q", "\x03"):
      self.running = False
    elif key in (" ", "p"):
      playerctl.send_cmd("play-pause", player=player)
      self._screen.invalidate()
    elif key == "n":
      playerctl.send_cmd("next", player=player)
      self._screen.invalidate()
    elif key == "b":
      playerctl.send_cmd("previous", player=player)
      self._screen.invalidate()
    elif key == "\x1b[C":
      seek_relative(10, player=player)
      self._pos_track = ""   # force resync on next poll
    elif key == "\x1b[D":
      seek_relative(-10, player=player)
      self._pos_track = ""   # force resync on next poll
    elif key in ("+", "="):
      playerctl.set_volume(self._safe_vol() + 0.05)
      self._vol_until = time.time() + 2
    elif key in ("-", "_"):
      playerctl.set_volume(self._safe_vol() - 0.05)
      self._vol_until = time.time() + 2
    elif key == "\t":
      if self.players:
        idx = self.players.index(self.player) if self.player in self.players else -1
        self.player = self.players[(idx + 1) % len(self.players)]
    elif key == "u":
      self.lyrics = None

  def _safe_vol(self):
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
    sys.stdout.write(ALT_ON + HIDE_CURSOR)
    sys.stdout.flush()

    prev_size = (self.term_w, self.term_h)

    # Force full redraw on resize
    def on_resize(sig, frame):
      self.term_w, self.term_h = shutil.get_terminal_size()
      self._last_art_key = None
      self._screen.invalidate()
      self.draw()

    signal.signal(signal.SIGWINCH, on_resize)

    t = threading.Thread(target=self.input_thread, daemon=True)
    t.start()

    try:
      while self.running:
        self.term_w, self.term_h = shutil.get_terminal_size()

        # Detect resize that SIGWINCH missed (e.g. some terminal muxers)
        new_size = (self.term_w, self.term_h)
        if new_size != prev_size:
          self._last_art_key = None
          self._screen.invalidate()
          prev_size = new_size

        self.players = playerctl.get_players()
        if not self.player and self.players:
          self.player = self.players[0]
        elif self.player and self.player not in self.players:
          self.player = self.players[0] if self.players else None

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
            lrc_file = lyrics.get_lyrics(title, artist, album, length)
            if lrc_file[0:8] == "LRCERROR":
              self.print_status(lrc_file)
            self.lyrics = lyrics.convert_lrc(lrc_file)
          except Exception as err:
            self.print_status(f"LRCEROR: {err}")
            self.lyrics = None
                
        # If art changed, the new art lines will be diffed normally;
        # no need to invalidate the whole screen.
        self.update_art(art_url)
        self.draw()
        self.draw_count += 1
        time.sleep(0.05)

    finally:
      sys.stdout.write(ALT_OFF + SHOW_CURSOR + RESET)
      sys.stdout.flush()
      
if __name__ == "__main__":
  if shutil.which("playerctl") is None:
    print("Error: playerctl not found.")
    print("  Install with:  sudo apt install playerctl")
    sys.exit(1)

  PlayerUI().run()
