"""
Key Stack Overlay v7
────────────────────
• Semi-transparent overlay in the lower-left corner of the screen, up to 3 cards.
• Global keyboard capture with pynput: no need to click the overlay first.
• Ctrl + Shift + K toggles the overlay on/off.
• Enter commits the current typed text to a visible key card.
• Space continues the current text buffer.

Dependencies:
    pip install pynput pystray pillow
"""

import time
import threading
import tkinter as tk
import json
from pathlib import Path

try:
    from pynput import keyboard as pynput_kb
    HAS_PYNPUT = True
except ImportError:
    HAS_PYNPUT = False
    print("[WARN] pynput missing - pip install pynput")

try:
    import pystray
    from PIL import Image, ImageDraw, ImageFont
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False
    print("[WARN] pystray/Pillow missing - pip install pystray pillow")

# ── Settings ──────────────────────────────────────────
CONFIG_PATH = Path(__file__).with_name("config.json")
DEFAULT_TOGGLE_HOTKEY = ["ctrl", "shift", "k"]

def _load_config():
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print(f"[WARN] config.json load failed: {exc}")
        return {}

CONFIG = _load_config()

# User request:
# The default toggle hotkey is Ctrl + Shift + K.
# Set this to True if you want to read the hotkey from config.json again.
USE_CONFIG_HOTKEY = False
TOGGLE_HOTKEY = (
    CONFIG.get("toggle_hotkey", DEFAULT_TOGGLE_HOTKEY)
    if USE_CONFIG_HOTKEY
    else DEFAULT_TOGGLE_HOTKEY
)

OVERLAY_W   = 340
OVERLAY_X   = 24
BOTTOM_PAD  = 60
MAX_CARDS   = 3
CARD_H      = 62
CARD_PAD    = 6
LIFETIME_MS = 5000
TICK_MS     = 30

TRANSPARENT = "#010101"
CARD_BG     = "#12121f"
CARD_BORDER = "#e94560"
TEXT_COLOR  = "#f0f0f0"
PREVIEW_COL = "#aaaacc"
BAR_COLOR   = "#e94560"
BAR_BG      = "#252540"
FONT_KEY    = ("Courier New", 20, "bold")
FONT_PRE    = ("Courier New", 14)
FONT_STATUS = ("Courier New", 11, "bold")
BAR_H       = 4
CARD_ALPHA  = 0.82

# Status bar colors
STATUS_ON_BG   = "#1a2f1a"
STATUS_ON_FG   = "#4cff72"
STATUS_ON_DOT  = "#00ff44"
STATUS_OFF_BG  = "#2f1a1a"
STATUS_OFF_FG  = "#ff6060"
STATUS_OFF_DOT = "#ff2222"

# Popup notification colors
POPUP_ON_BG    = "#0d2b0d"
POPUP_ON_FG    = "#4cff72"
POPUP_ON_BDR   = "#00cc33"
POPUP_OFF_BG   = "#2b0d0d"
POPUP_OFF_FG   = "#ff6060"
POPUP_OFF_BDR  = "#cc0000"

INSTANT_KEYS = {
    "tab", "escape", "up", "down", "left", "right",
    "home", "end", "prior", "next",
    "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12",
}

# ── Utilities ─────────────────────────────────────────
def _blend(hex1, hex2, t):
    def p(h): return tuple(int(h[i:i+2], 16) for i in (1, 3, 5))
    r1,g1,b1 = p(hex1); r2,g2,b2 = p(hex2)
    def lerp(a,b): return int(a*t + b*(1-t))
    return f"#{lerp(r1,r2):02x}{lerp(g1,g2):02x}{lerp(b1,b2):02x}"

def _special_label(name):
    key = _normalize_key_name(name)
    table = {
        "tab": "⇥ Tab", "escape": "⎋ Esc",
        "up": "↑ Up", "down": "↓ Down",
        "left": "← Left", "right": "→ Right",
        "home": "⇱ Home", "end": "⇲ End",
        "prior": "⇞ PgUp", "next": "⇟ PgDn",
    }
    if key in table:
        return table[key]
    if key.startswith("f") and key[1:].isdigit():
        return key.upper()
    return ""

def _modifier_only(name):
    key = _normalize_key_name(name)
    return key in {
        "shift", "ctrl", "control", "alt", "alt_gr",
        "super", "super_l", "super_r", "cmd", "caps_lock", "num_lock", "scroll_lock",
    }

def _normalize_key_name(name):
    key = (name or "").lower().strip()
    if key.startswith("key."):
        key = key[4:]
    aliases = {
        "control": "ctrl", "control_l": "ctrl", "control_r": "ctrl",
        "ctrl_l": "ctrl", "ctrl_r": "ctrl",
        "alt_l": "alt", "alt_r": "alt", "alt_gr": "alt",
        "shift_l": "shift", "shift_r": "shift",
        "backspace": "backspace", "delete": "delete",
        "return": "enter", "esc": "escape",
    }
    return aliases.get(key, key)

def _normalize_hotkey(hotkey):
    if isinstance(hotkey, str):
        parts = hotkey.replace("+", " ").split()
    else:
        parts = list(hotkey or [])
    return {_normalize_key_name(str(part)) for part in parts if str(part).strip()}

def _format_hotkey(hotkey):
    if isinstance(hotkey, str):
        parts = hotkey.replace("+", " ").split()
    else:
        parts = list(hotkey or [])
    return " + ".join(str(part).upper() for part in parts) or "F8"

def _key_to_tokens(key):
    """Convert a pynput key object into normalized tokens for hotkey matching.

    When Ctrl/Shift is held, the character may arrive as a control character.
    The vk fallback restores keys such as Ctrl+Shift+K from vk=75.
    """
    tokens = set()
    try:
        char = getattr(key, "char", "") or ""
        name = getattr(key, "name", "") or ""
        vk = getattr(key, "vk", None)
    except Exception:
        char = ""
        name = str(key)
        vk = None

    if char and char.isprintable():
        tokens.add(_normalize_key_name(char.lower()))

    if name:
        tokens.add(_normalize_key_name(name))
    else:
        tokens.add(_normalize_key_name(str(key)))

    # Fallback for platforms where character keys arrive only as vk values
    if isinstance(vk, int):
        if 65 <= vk <= 90:       # A-Z
            tokens.add(chr(vk).lower())
        elif 48 <= vk <= 57:     # 0-9
            tokens.add(chr(vk))

    return {t for t in tokens if t}

# ── KeyCard ──────────────────────────────────────────
class KeyCard:
    FADE_STEPS = 20

    def __init__(self, canvas, label, on_done):
        self.canvas  = canvas
        self.label   = label
        self.on_done = on_done
        self.alive   = True
        self._born   = time.time()
        self.y       = 0
        cw = int(canvas.winfo_width()) or OVERLAY_W

        self._rect = canvas.create_rectangle(
            4, 0, cw-4, CARD_H, fill=CARD_BG, outline=CARD_BORDER, width=2)
        self._text = canvas.create_text(
            cw//2, CARD_H//2 - 4,
            text=label, fill=TEXT_COLOR, font=FONT_KEY, width=cw-20)
        self._bbg  = canvas.create_rectangle(
            8, CARD_H-BAR_H-5, cw-8, CARD_H-5, fill=BAR_BG, outline="")
        self._bar  = canvas.create_rectangle(
            8, CARD_H-BAR_H-5, cw-8, CARD_H-5, fill=BAR_COLOR, outline="")
        canvas.after(LIFETIME_MS, self._start_fade)

    def move_to(self, y):
        dy = y - self.y
        for it in (self._rect, self._text, self._bbg, self._bar):
            self.canvas.move(it, 0, dy)
        self.y = y

    def tick(self):
        if not self.alive: return
        elapsed = (time.time() - self._born) * 1000
        ratio   = max(0.0, 1.0 - elapsed / LIFETIME_MS)
        cw = int(self.canvas.winfo_width()) or OVERLAY_W
        bx2 = 8 + int((cw - 16) * ratio)
        by  = self.y + CARD_H - BAR_H - 5
        self.canvas.coords(self._bar, 8, by, max(8, bx2), by + BAR_H)

    def _start_fade(self):
        if self.alive: self._fade(self.FADE_STEPS)

    def _fade(self, step):
        if not self.alive: return
        if step <= 0:
            self._remove(); return
        t = step / self.FADE_STEPS
        c = self.canvas
        c.itemconfig(self._rect, fill=_blend(CARD_BG,    TRANSPARENT, t),
                                 outline=_blend(CARD_BORDER, TRANSPARENT, t))
        c.itemconfig(self._text, fill=_blend(TEXT_COLOR, TRANSPARENT, t))
        c.itemconfig(self._bar,  fill=_blend(BAR_COLOR,  TRANSPARENT, t))
        c.itemconfig(self._bbg,  fill=_blend(BAR_BG,     TRANSPARENT, t))
        self.canvas.after(16, lambda: self._fade(step - 1))

    def _remove(self):
        self.alive = False
        for it in (self._rect, self._text, self._bbg, self._bar):
            self.canvas.delete(it)
        self.on_done(self)

    def force_remove(self):
        self.alive = False
        for it in (self._rect, self._text, self._bbg, self._bar):
            self.canvas.delete(it)


# ── Center popup notification ─────────────────────────
class StatusPopup:
    """Small center-screen notification shown after toggling."""

    SHOW_MS  = 1200
    FADE_MS  = 300
    FADE_STEPS = 15

    def __init__(self, active: bool):
        self.win = tk.Toplevel()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", 0.0)

        if active:
            bg, fg, bdr = POPUP_ON_BG, POPUP_ON_FG, POPUP_ON_BDR
            icon  = "●"
            label = "KeyStack  ON"
            sub   = "Key display enabled"
        else:
            bg, fg, bdr = POPUP_OFF_BG, POPUP_OFF_FG, POPUP_OFF_BDR
            icon  = "●"
            label = "KeyStack  OFF"
            sub   = "Key display disabled"

        W, H = 320, 110
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        x  = (sw - W) // 2
        y  = (sh - H) // 2 - 40
        self.win.geometry(f"{W}x{H}+{x}+{y}")
        self.win.configure(bg=bdr)

        inner = tk.Frame(self.win, bg=bg, padx=16, pady=10)
        inner.pack(fill="both", expand=True, padx=2, pady=2)

        top = tk.Frame(inner, bg=bg)
        top.pack(fill="x")

        tk.Label(top, text=icon, font=("Segoe UI", 18), bg=bg, fg=bdr).pack(side="left")
        tk.Label(top, text=label, font=("Courier New", 18, "bold"),
                 bg=bg, fg=fg).pack(side="left", padx=8)

        hotkey_txt = f"[{_format_hotkey(TOGGLE_HOTKEY)}] to toggle"
        tk.Label(inner, text=sub, font=("Segoe UI", 10),
                 bg=bg, fg=fg).pack(anchor="w")
        tk.Label(inner, text=hotkey_txt, font=("Segoe UI", 9),
                 bg=bg, fg=bdr).pack(anchor="w")

        # Fade in
        self._fade_in(10)

    def _fade_in(self, step):
        if step < 0:
            self.win.after(self.SHOW_MS, lambda: self._fade_out(self.FADE_STEPS))
            return
        alpha = (10 - step) / 10 * 0.92
        try:
            self.win.attributes("-alpha", alpha)
            self.win.after(20, lambda: self._fade_in(step - 1))
        except Exception:
            pass

    def _fade_out(self, step):
        if step <= 0:
            try: self.win.destroy()
            except Exception: pass
            return
        alpha = step / self.FADE_STEPS * 0.92
        try:
            self.win.attributes("-alpha", alpha)
            self.win.after(self.FADE_MS // self.FADE_STEPS,
                           lambda: self._fade_out(step - 1))
        except Exception:
            pass


# ── Overlay window ────────────────────────────────────
class Overlay:
    STATUS_BAR_H = 22

    def __init__(self):
        self.root        = tk.Tk()
        self.cards       = []
        self.active      = True
        self._pressed    = set()
        self._buf        = []
        self._ctrl_held  = False
        self._alt_held   = False
        self._shift_held = False
        self._toggle_tokens = _normalize_hotkey(TOGGLE_HOTKEY)
        self._toggle_held   = False
        self._listener      = None

        r = self.root
        r.title("KeyStack")
        r.overrideredirect(True)

        sw = r.winfo_screenwidth()
        sh = r.winfo_screenheight()
        self._oh = (CARD_H + CARD_PAD) * MAX_CARDS + 40 + self.STATUS_BAR_H
        oy = sh - self._oh - BOTTOM_PAD
        r.geometry(f"{OVERLAY_W}x{self._oh}+{OVERLAY_X}+{oy}")

        r.configure(bg=TRANSPARENT)
        r.attributes("-transparentcolor", TRANSPARENT)
        r.attributes("-topmost", True)
        r.attributes("-alpha", CARD_ALPHA)

        # Make the overlay click-through on Windows
        try:
            import ctypes
            hwnd  = ctypes.windll.user32.GetParent(r.winfo_id())
            style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
            ctypes.windll.user32.SetWindowLongW(hwnd, -20, style | 0x80000 | 0x20)
        except Exception:
            pass

        self.canvas = tk.Canvas(r, bg=TRANSPARENT, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        # ── Status bar ──
        # Background rectangle
        self._status_bg = self.canvas.create_rectangle(
            0, 0, OVERLAY_W, self.STATUS_BAR_H,
            fill=STATUS_ON_BG, outline=STATUS_ON_DOT, width=1)
        # Blinking dot
        self._status_dot = self.canvas.create_oval(
            6, 5, 16, 15, fill=STATUS_ON_DOT, outline="")
        # Status text
        hotkey_txt = _format_hotkey(TOGGLE_HOTKEY)
        self._status_text = self.canvas.create_text(
            OVERLAY_W // 2, self.STATUS_BAR_H // 2,
            text=f"● KeyStack ON  [{hotkey_txt}]",
            fill=STATUS_ON_FG, font=FONT_STATUS)
        self._dot_visible = True
        self._blink_status()

        # Preview text
        self._preview = self.canvas.create_text(
            OVERLAY_W // 2, self._oh - 14,
            text="", fill=PREVIEW_COL, font=FONT_PRE, width=OVERLAY_W - 20)

        # Tkinter <Key> bindings require the overlay to have focus.
        # Use only the pynput global listener so typing works anywhere.
        if not HAS_PYNPUT:
            print("[ERROR] pynput is missing, so global keyboard capture is unavailable.")
            print("[ERROR] Run this command first: pip install pynput pystray pillow")

        self._tick()

    # ── Status bar update ─────────────────────────────────
    def _update_status_bar(self):
        hotkey_txt = _format_hotkey(TOGGLE_HOTKEY)
        if self.active:
            bg, dot, fg = STATUS_ON_BG, STATUS_ON_DOT, STATUS_ON_FG
            label = f"● KeyStack ON  [{hotkey_txt}]"
        else:
            bg, dot, fg = STATUS_OFF_BG, STATUS_OFF_DOT, STATUS_OFF_FG
            label = f"■ KeyStack OFF  [{hotkey_txt}]"

        self.canvas.itemconfig(self._status_bg, fill=bg, outline=dot)
        self.canvas.itemconfig(self._status_dot, fill=dot)
        self.canvas.itemconfig(self._status_text, text=label, fill=fg)

    def _blink_status(self):
        """Blink the status dot while the overlay is ON."""
        if not self.active:
            self.root.after(600, self._blink_status)
            return
        self._dot_visible = not self._dot_visible
        col = STATUS_ON_DOT if self._dot_visible else STATUS_ON_BG
        self.canvas.itemconfig(self._status_dot, fill=col)
        self.root.after(600, self._blink_status)

    # ── Preview ───────────────────────────────────────────
    def _update_preview(self):
        txt = "".join(self._buf)
        if txt:
            display = f"✏  {txt}"
            if len(display) > 30:
                display = "✏  …" + display[-26:]
        else:
            display = ""
        self.canvas.itemconfig(self._preview, text=display)

    # ── Tkinter fallback ──────────────────────────────────
    def _tk_key(self, event):
        if HAS_PYNPUT:
            return
        self._handle_key(event.keysym, event.char or "")

    # ── pynput callbacks ─────────────────────────────────
    def pynput_press(self, key):
        try:
            self._pressed.add(key)
            if self._is_toggle_combo():
                if not self._toggle_held:
                    self._toggle_held = True
                    self.root.after(0, self.toggle)
                return

            try:
                name = getattr(key, 'name', '') or ''
                char = getattr(key, 'char', '') or ''
            except Exception:
                name = str(key); char = ''

            nl = name.lower()
            if nl in ('ctrl_l','ctrl_r','control_l','control_r'): self._ctrl_held = True
            if nl in ('alt_l','alt_r','alt','alt_gr'):             self._alt_held  = True
            if nl in ('shift','shift_l','shift_r'):                self._shift_held = True

            self.root.after(0, lambda n=name, c=char: self._handle_key(n, c))
        except Exception as e:
            print(f"[WARN] pynput_press error: {e}")

    def pynput_release(self, key):
        try:
            self._pressed.discard(key)
            try:
                name = getattr(key, 'name', '') or ''
            except Exception:
                name = ''
            nl = name.lower()
            if nl in ('ctrl_l','ctrl_r','control_l','control_r'): self._ctrl_held  = False
            if nl in ('alt_l','alt_r','alt','alt_gr'):             self._alt_held   = False
            if nl in ('shift','shift_l','shift_r'):                self._shift_held = False
            if not self._is_toggle_combo():
                self._toggle_held = False
        except Exception as e:
            print(f"[WARN] pynput_release error: {e}")
    def _is_toggle_combo(self):
        pressed = set()
        for key in self._pressed:
            pressed.update(_key_to_tokens(key))
        return bool(self._toggle_tokens) and self._toggle_tokens.issubset(pressed)

    # ── Main key handling ─────────────────────────────────
    def _handle_key(self, name, char):
        key_name = _normalize_key_name(name)

        if not self.active or _modifier_only(key_name):
            return

        # Enter commits the current preview buffer to a visible card.
        # Backspace still edits the current preview buffer.
        if key_name == "enter" or char in ("\r", "\n"):
            self._flush_buffer()
            return

        if key_name == "backspace":
            if self._buf:
                self._buf.pop()
                self._update_preview()
            return

        if key_name == "delete":
            return

        # Ctrl shortcuts are displayed, except the toggle combo is already handled earlier.
        if self._ctrl_held:
            self._flush_buffer()
            label_key = (char if char and char.isprintable() else key_name).upper()
            label = f"Ctrl+{label_key}"
            if label != "Ctrl+":
                self._add_card(label)
            return

        if key_name == "space" or char == " ":
            self._buf.append(" ")
            self._update_preview()
            return

        if key_name in INSTANT_KEYS:
            self._flush_buffer()
            label = _special_label(key_name)
            if label:
                self._add_card(label)
            return

        if char and char.isprintable():
            self._buf.append(char)
            self._update_preview()
            return

        if key_name and not _modifier_only(key_name):
            self._flush_buffer()
            label = _special_label(key_name) or key_name[:12]
            self._add_card(label)

    def _flush_buffer(self):
        if self._buf:
            word = "".join(self._buf)
            self._buf.clear()
            self._update_preview()
            self._add_card(word)

    # ── Card management ───────────────────────────────────
    def _add_card(self, label):
        if not label.strip():
            return
        if len(self.cards) >= MAX_CARDS:
            old = self.cards.pop(0)
            old.force_remove()
        card = KeyCard(self.canvas, label, self._card_done)
        self.cards.append(card)
        self._layout()

    def _card_done(self, card):
        if card in self.cards:
            self.cards.remove(card)
        self._layout()

    def _layout(self):
        self.canvas.update_idletasks()
        h     = int(self.canvas.winfo_height()) or 300
        total = len(self.cards)
        step  = CARD_H + CARD_PAD
        base  = h - 36
        for i, card in enumerate(self.cards):
            y = base - (total - i) * step
            card.move_to(max(self.STATUS_BAR_H + 4, y))

    # ── Toggle on/off ─────────────────────────────────────
    def toggle(self):
        self.active = not self.active
        if self.active:
            self.root.deiconify()
            self.root.attributes("-topmost", True)
        else:
            self._buf.clear()
            self._update_preview()
            for card in self.cards:
                card.force_remove()
            self.cards.clear()

        # 1. Update overlay status bar
        self._update_status_bar()

        # 2. Show center popup notification
        StatusPopup(self.active)

        # 3. Update tray icon
        self._update_tray()

        status = "ON" if self.active else "OFF"
        print(f"KeyStack {status}", flush=True)

    # ── System tray icon ──────────────────────────────────
    def _make_tray_image(self):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d   = ImageDraw.Draw(img)

        if self.active:
            # ON: red background + white "K"
            bg_col   = (233, 69, 96, 255)
            ring_col = (255, 255, 255, 200)
            txt_col  = (255, 255, 255, 255)
            txt      = "K"
        else:
            # OFF: dark gray background + gray "K" + red X overlay
            bg_col   = (50, 50, 60, 255)
            ring_col = (120, 120, 140, 160)
            txt_col  = (130, 130, 150, 255)
            txt      = "K"

        # Background circle
        d.ellipse([2, 2, 62, 62], fill=bg_col)
        # Ring
        d.ellipse([4, 4, 60, 60], outline=ring_col, width=3)
        # Letter
        d.text((18, 12), txt, fill=txt_col)

        if not self.active:
            # Red X while OFF
            d.line([12, 12, 52, 52], fill=(220, 60, 60, 220), width=5)
            d.line([52, 12, 12, 52], fill=(220, 60, 60, 220), width=5)

        return img

    def _tray_title(self):
        status  = "🟢 ON" if self.active else "🔴 OFF"
        hotkey  = _format_hotkey(TOGGLE_HOTKEY)
        return f"KeyStack  |  {status}  |  Toggle: {hotkey}"

    def _update_tray(self):
        if not HAS_TRAY or not hasattr(self, '_tray'):
            return
        try:
            self._tray.icon  = self._make_tray_image()
            self._tray.title = self._tray_title()
            self._tray.update_menu()
        except Exception as e:
            print(f"[WARN] tray update error: {e}")

    def _tick(self):
        for card in list(self.cards):
            card.tick()
        self.root.after(TICK_MS, self._tick)

    # ── Initialize tray icon ──────────────────────────────
    def _make_tray_icon(self):
        def _on_toggle(icon, item):
            self.root.after(0, self.toggle)

        def _on_quit(icon, item):
            icon.stop()
            self.root.after(0, self._quit)

        menu = pystray.Menu(
            pystray.MenuItem(
                lambda _: ("✅ Showing  (click to turn off)" if self.active
                           else "❌ Hidden  (click to turn on)"),
                _on_toggle,
                default=True,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", _on_quit),
        )

        self._tray = pystray.Icon(
            "KeyStack",
            self._make_tray_image(),
            self._tray_title(),
            menu,
        )
        threading.Thread(target=self._tray.run, daemon=True).start()

    # ── Quit ──────────────────────────────────────────
    def _quit(self):
        try:
            if self._listener:
                self._listener.stop()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    # ── Run ───────────────────────────────────────────────
    def run(self):
        if HAS_TRAY:
            self.root.after(600, self._make_tray_icon)

        if HAS_PYNPUT:
            self._listener = pynput_kb.Listener(
                on_press=self.pynput_press,
                on_release=self.pynput_release,
                suppress=False,
            )
            self._listener.daemon = True
            self._listener.start()
        else:
            # Do not fall back to focus-based capture because that would require
            # clicking the overlay again. Stop here and show install instructions.
            try:
                import tkinter.messagebox as messagebox
                messagebox.showerror(
                    "KeyStack Error",
                    "pynput is not installed, so global keyboard capture cannot be used.\n\n"
                    "Run this command in your terminal:\n"
                    "pip install pynput pystray pillow"
                )
            except Exception:
                pass
            return

        self.root.mainloop()


if __name__ == "__main__":
    print("=" * 54)
    print("  Key Stack Overlay  v7")
    print("  Status: tray icon + popup notification + overlay status bar")
    print(f"  Toggle hotkey: {_format_hotkey(TOGGLE_HOTKEY)}")
    print("  Enter commits text  /  Backspace edits preview  /  Space keeps typing")
    if HAS_TRAY:
        print("  Tray icon: right-click → toggle / quit")
    print("=" * 54)
    Overlay().run()
    
