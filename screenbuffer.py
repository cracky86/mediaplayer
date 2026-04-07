import sys

import ansi

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
      out.append(ansi.CLEAR)
      for row, (col, content) in sorted(frame.items()):
        out.append(ansi.move(row, col) + content + ansi.RESET)

        # Erase any rows that are no longer part of the frame
        for row in self._prev:
          if row not in frame:
            out.append(ansi.move(row, 1) + f"{ansi.ESC}[K")
            
      self._dirty = False
    else: # Only update modified rows
      for row, (col, content) in frame.items():
        prev = self._prev.get(row)
        
        if prev is None or prev != (col, content):
          out.append(ansi.move(row, col) + content + ansi.RESET)
          
        # Erase empty rows
        for row in self._prev:
          if row not in frame:
            out.append(ansi.move(row, 1) + f"{ansi.ESC}[K")

    # Set the previous frame to the current one
    self._prev = dict(frame)

    # Display the buffer
    if out:
      sys.stdout.write("".join(out))
      sys.stdout.flush()
