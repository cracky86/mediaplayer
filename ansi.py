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
