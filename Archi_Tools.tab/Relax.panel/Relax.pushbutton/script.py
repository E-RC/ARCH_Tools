# -*- coding: utf-8 -*-
"""Relax — mini juego estilo Arkanoid con estetica neon y el logo ARCHIPLAN."""
from __future__ import division
import os
import json
import math
import random
import clr

clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")

from System.Diagnostics import Stopwatch
from System.Windows import (Window, WindowStyle, ResizeMode, Thickness,
                            HorizontalAlignment, VerticalAlignment,
                            WindowStartupLocation, Visibility, TextAlignment)
from System.Windows.Controls import Canvas, TextBlock
from System.Windows.Shapes import Rectangle, Ellipse
from System.Windows.Media import (SolidColorBrush, Color, Brushes,
                                  LinearGradientBrush, GradientStop, FontFamily,
                                  CompositionTarget, TranslateTransform)
from System.Windows import Point
from System.Windows.Media.Effects import DropShadowEffect
from System.Windows.Input import Key

# ─────────────────────────────────────────────────────────────────
# Configuracion general
# ─────────────────────────────────────────────────────────────────
from archi_common.paths import user_data_path

_CFG_PATH = user_data_path("Relax", "config.json")

W, H = 960, 640
HUD_H = 48
FIELD_MARGIN = 28
BRICK_TOP = HUD_H + 26

COLS, ROWS = 23, 13
CELL_W = (W - 2 * FIELD_MARGIN) / COLS
CELL_H = 19.0
BRICK_GAP = 4.0

PADDLE_W, PADDLE_W_BIG, PADDLE_H = 112.0, 168.0, 14.0
PADDLE_Y = H - 46.0
PADDLE_SPEED = 10.0
BALL_R = 7.0
BASE_SPEED = 5.6
POWERUP_CHANCE = 0.14
POWERUP_FALL = 2.6
EXPAND_FRAMES = 60 * 12   # ~12 segundos

TRAIL_DOTS = 7      # segmentos de la estela de la bola
TRAIL_SPACING = 3   # frames de historia entre cada segmento

TIME_WARN_S = 5 * 60    # el contador pasa a ambar (5 min)
TIME_ALERT_S = 10 * 60  # el contador pasa a rojo (10 min)

STATE_READY, STATE_PLAYING, STATE_PAUSED, STATE_OVER, STATE_WIN = range(5)

# Paleta neon
def _c(hexstr):
    hexstr = hexstr.lstrip("#")
    return Color.FromRgb(int(hexstr[0:2], 16), int(hexstr[2:4], 16), int(hexstr[4:6], 16))


def _mix(color, target, f):
    """Mezcla el color hacia un gris objetivo (0 = negro, 255 = blanco)."""
    return Color.FromRgb(int(color.R + (target - color.R) * f),
                         int(color.G + (target - color.G) * f),
                         int(color.B + (target - color.B) * f))

COL_BG      = _c("0B0F1E")
COL_FIELD   = _c("101731")
COL_RING    = _c("00E5FF")   # anillo del logo
COL_LETTER  = _c("E8F1FF")   # letra A
COL_SWOOSH  = _c("FF2B55")   # swoosh rojo Archiplan
COL_PADDLE  = _c("00E5FF")
COL_BALL    = _c("FFFFFF")
COL_TEXT    = _c("9AA8CC")

BRICK_STYLE = {
    "ring":   {"color": COL_RING,   "hp": 1, "score": 20},
    "letter": {"color": COL_LETTER, "hp": 1, "score": 30},
    "swoosh": {"color": COL_SWOOSH, "hp": 2, "score": 50},
}

POWERUPS = {
    "E": {"color": _c("00E5FF"), "label": "E"},   # paleta grande
    "M": {"color": _c("B388FF"), "label": "M"},   # multibola
    "L": {"color": _c("69F0AE"), "label": "+"},   # vida extra
    "S": {"color": _c("FFD740"), "label": "S"},   # bola lenta
}


def _load_cfg():
    try:
        if os.path.exists(_CFG_PATH):
            with open(_CFG_PATH, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_cfg(data):
    try:
        with open(_CFG_PATH, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────
# Patrones de ladrillos por nivel
# Nivel 1: logo ARCHIPLAN. Niveles siguientes: disposiciones clasicas
# que rotan y se endurecen progresivamente.
# ─────────────────────────────────────────────────────────────────
def _pattern_logo():
    """Logo ARCHIPLAN: anillo + letra A + swoosh rojo."""
    cells = {}
    for r in range(ROWS):
        for c in range(COLS):
            # coordenadas normalizadas [-1, 1] centradas en la grilla
            x = (c + 0.5) / COLS * 2.0 - 1.0
            y = (r + 0.5) / ROWS * 2.0 - 1.0
            d = math.sqrt(x * x + y * y)
            kind = None

            # anillo exterior
            if 0.80 <= d <= 1.0:
                kind = "ring"

            # letra A (dentro del anillo)
            if d < 0.80:
                apex_y, base_y = -0.52, 0.58
                if apex_y <= y <= base_y:
                    t = (y - apex_y) / (base_y - apex_y)
                    half = t * 0.44
                    if abs(abs(x) - half) <= 0.115:          # piernas
                        kind = "letter"
                    elif 0.16 <= y <= 0.32 and abs(x) < half:  # travesano
                        kind = "letter"

            # swoosh rojo: cinta diagonal que cruza el logo (encima de todo)
            fy = 0.22 - 0.25 * x
            if abs(y - fy) <= 0.13:
                kind = "swoosh"

            if kind:
                cells[(c, r)] = kind
    return cells


def _pattern_rows():
    """Filas horizontales clasicas."""
    cells = {}
    kinds = ["swoosh", "ring", "letter", "ring", "letter", "ring", "letter", "ring"]
    for r in range(8):
        for c in range(COLS):
            cells[(c, r)] = kinds[r]
    return cells


def _pattern_pyramid():
    """Piramide centrada."""
    cells = {}
    for r in range(10):
        half = int(round((r + 1) / 10.0 * (COLS / 2.0)))
        for c in range(COLS // 2 - half, COLS // 2 + half + 1):
            if 0 <= c < COLS:
                cells[(c, r)] = "ring" if r % 2 else "letter"
    return cells


def _pattern_checker():
    """Damero con bandas de color."""
    cells = {}
    for r in range(10):
        for c in range(COLS):
            if (c + r) % 2 == 0:
                if r < 2:
                    cells[(c, r)] = "swoosh"
                elif r < 6:
                    cells[(c, r)] = "ring"
                else:
                    cells[(c, r)] = "letter"
    return cells


def _pattern_diamond():
    """Diamante con nucleo resistente."""
    cells = {}
    for r in range(12):
        for c in range(COLS):
            d = abs(c - COLS // 2) / (COLS / 2.0) + abs(r - 5.5) / 6.0
            if d <= 1.0:
                if d < 0.35:
                    cells[(c, r)] = "swoosh"
                elif d < 0.7:
                    cells[(c, r)] = "letter"
                else:
                    cells[(c, r)] = "ring"
    return cells


def _pattern_zigzag():
    """Franjas diagonales."""
    cells = {}
    for r in range(9):
        for c in range(COLS):
            if (c + r * 2) % 8 < 5:
                stripe = (c + r * 2) // 8
                cells[(c, r)] = "ring" if stripe % 2 else "letter"
    return cells


def _pattern_fortress():
    """Muralla superior + torres con pasillos."""
    cells = {}
    for c in range(COLS):
        cells[(c, 0)] = "swoosh"
        cells[(c, 1)] = "ring"
    for c in range(COLS):
        if c % 4 != 3:
            for r in range(3, 9):
                cells[(c, r)] = "letter" if r % 2 else "ring"
    return cells


def build_pattern(level):
    """Devuelve dict {(col, fila): tipo} para el nivel pedido."""
    if level <= 1:
        return _pattern_logo()
    gens = [_pattern_rows, _pattern_pyramid, _pattern_checker,
            _pattern_diamond, _pattern_zigzag, _pattern_fortress]
    cells = gens[(level - 2) % len(gens)]()
    # dificultad: mas ladrillos rojos resistentes (2 golpes) a mayor nivel;
    # semilla fija por nivel para que cada nivel sea siempre igual
    rnd = random.Random(level * 7919)
    hard = min(0.08 + 0.06 * level, 0.55)
    for key in list(cells.keys()):
        if rnd.random() < hard:
            cells[key] = "swoosh"
    return cells


# ─────────────────────────────────────────────────────────────────
# Juego
# ─────────────────────────────────────────────────────────────────
class RelaxGame(object):
    def __init__(self):
        self.cfg = _load_cfg()
        self.high = self.cfg.get("highscore", 0)
        self.score = 0
        self.lives = 3
        self.level = 1
        self.state = STATE_READY
        self.paddle_w = PADDLE_W
        self.expand_left = 0
        self.left_down = False
        self.right_down = False
        self.bricks = []
        self.balls = []
        self.powerups = []

        self._build_window()
        self._new_level(reset_all=True)
        self._show_overlay("RELAX", "Pulsa ESPACIO para lanzar")

        # loop sincronizado con el render de WPF + movimiento por delta-time,
        # mucho mas fluido que un DispatcherTimer de intervalo fijo
        self._closed = False
        self._watch = Stopwatch.StartNew()
        self._last_t = 0.0
        self._shown_sec = -1
        CompositionTarget.Rendering += self._on_frame

    # ── UI ──────────────────────────────────────────────────────
    def _glow(self, color, radius=14, opacity=0.9):
        e = DropShadowEffect()
        e.Color = color
        e.BlurRadius = radius
        e.ShadowDepth = 0
        e.Opacity = opacity
        return e

    def _build_window(self):
        self.win = Window()
        self.win.Title = "Relax — Archiplan"
        self.win.Width = W + 16
        self.win.Height = H + 40
        self.win.ResizeMode = ResizeMode.NoResize
        self.win.WindowStartupLocation = WindowStartupLocation.CenterScreen
        self.win.Background = SolidColorBrush(COL_BG)

        self.canvas = Canvas()
        self.canvas.Width = W
        self.canvas.Height = H
        self.canvas.Background = SolidColorBrush(COL_BG)
        self.win.Content = self.canvas

        # campo de juego
        field = Rectangle()
        field.Width = W - 2 * (FIELD_MARGIN - 8)
        field.Height = H - HUD_H - 16
        field.RadiusX = 10
        field.RadiusY = 10
        field.Fill = SolidColorBrush(COL_FIELD)
        Canvas.SetLeft(field, FIELD_MARGIN - 8)
        Canvas.SetTop(field, HUD_H + 4)
        self.canvas.Children.Add(field)

        # HUD
        self.hud_title = self._text("R E L A X", 22, COL_RING, bold=True)
        self.hud_title.Effect = self._glow(COL_RING, 18)
        Canvas.SetLeft(self.hud_title, FIELD_MARGIN)
        Canvas.SetTop(self.hud_title, 10)

        self.hud_score = self._text("", 16, COL_LETTER, bold=True)
        Canvas.SetLeft(self.hud_score, 270)
        Canvas.SetTop(self.hud_score, 15)

        self.hud_high = self._text("", 16, COL_TEXT)
        Canvas.SetLeft(self.hud_high, 460)
        Canvas.SetTop(self.hud_high, 15)

        self.hud_level = self._text("", 16, COL_TEXT)
        Canvas.SetLeft(self.hud_level, 620)
        Canvas.SetTop(self.hud_level, 15)

        # contador de tiempo jugado: recordatorio de volver al trabajo
        self.hud_time = self._text("00:00", 16, COL_TEXT, bold=True)
        Canvas.SetLeft(self.hud_time, 748)
        Canvas.SetTop(self.hud_time, 15)
        self._time_warn_brush = SolidColorBrush(_c("FFD740"))
        self._time_alert_brush = SolidColorBrush(COL_SWOOSH)

        self.hud_lives = self._text("", 16, COL_SWOOSH, bold=True)
        self.hud_lives.Effect = self._glow(COL_SWOOSH, 10, 0.7)
        Canvas.SetLeft(self.hud_lives, 845)
        Canvas.SetTop(self.hud_lives, 15)

        for tb in (self.hud_title, self.hud_score, self.hud_high,
                   self.hud_level, self.hud_time, self.hud_lives):
            self.canvas.Children.Add(tb)

        # paleta
        self.paddle = Rectangle()
        self.paddle.Height = PADDLE_H
        self.paddle.RadiusX = 7
        self.paddle.RadiusY = 7
        self.paddle.Fill = self._neon_fill(COL_PADDLE)
        self.paddle.Effect = self._glow(COL_PADDLE, 16)
        self.canvas.Children.Add(self.paddle)
        self.paddle_x = W / 2.0 - PADDLE_W / 2.0
        self._place_paddle()

        # overlay central
        self.overlay_big = self._text("", 46, COL_SWOOSH, bold=True)
        self.overlay_big.Width = W
        self.overlay_big.TextAlignment = TextAlignment.Center
        self.overlay_big.Effect = self._glow(COL_SWOOSH, 24)
        Canvas.SetLeft(self.overlay_big, 0)
        Canvas.SetTop(self.overlay_big, H / 2 - 70)
        self.canvas.Children.Add(self.overlay_big)

        self.overlay_small = self._text("", 18, COL_LETTER)
        self.overlay_small.Width = W
        self.overlay_small.TextAlignment = TextAlignment.Center
        Canvas.SetLeft(self.overlay_small, 0)
        Canvas.SetTop(self.overlay_small, H / 2 + 2)
        self.canvas.Children.Add(self.overlay_small)

        # eventos
        self.win.PreviewKeyDown += self._on_key_down
        self.win.PreviewKeyUp += self._on_key_up
        self.canvas.MouseMove += self._on_mouse_move
        self.win.Closed += self._on_closed

    def _text(self, txt, size, color, bold=False):
        tb = TextBlock()
        tb.Text = txt
        tb.FontSize = size
        tb.FontFamily = FontFamily("Consolas")
        tb.Foreground = SolidColorBrush(color)
        if bold:
            from System.Windows import FontWeights
            tb.FontWeight = FontWeights.Bold
        return tb

    def _neon_fill(self, color):
        """Gradiente vertical: color brillante arriba, oscuro abajo."""
        dark = Color.FromRgb(int(color.R * 0.35), int(color.G * 0.35), int(color.B * 0.35))
        gb = LinearGradientBrush()
        gb.StartPoint = Point(0, 0)
        gb.EndPoint = Point(0, 1)
        gb.GradientStops.Add(GradientStop(color, 0.0))
        gb.GradientStops.Add(GradientStop(dark, 1.0))
        return gb

    def _brick_fill(self, color):
        """Volumen pseudo-3D: brillo arriba, color al medio, sombra abajo."""
        gb = LinearGradientBrush()
        gb.StartPoint = Point(0, 0)
        gb.EndPoint = Point(0, 1)
        gb.GradientStops.Add(GradientStop(_mix(color, 255, 0.55), 0.0))
        gb.GradientStops.Add(GradientStop(color, 0.45))
        gb.GradientStops.Add(GradientStop(_mix(color, 0, 0.55), 1.0))
        return gb

    # ── construccion de nivel ───────────────────────────────────
    def _new_level(self, reset_all=False):
        if reset_all:
            self.score = 0
            self.lives = 3
            self.level = 1
        for b in self.bricks:
            self.canvas.Children.Remove(b["rect"])
            self.canvas.Children.Remove(b["gloss"])
        for p in self.powerups:
            self.canvas.Children.Remove(p["el"])
        self.bricks = []
        self.powerups = []
        self._clear_balls()

        pattern = build_pattern(self.level)
        for (c, r), kind in pattern.items():
            style = BRICK_STYLE[kind]
            base = style["color"]
            rect = Rectangle()
            rect.Width = CELL_W - BRICK_GAP
            rect.Height = CELL_H - BRICK_GAP
            rect.RadiusX = 3
            rect.RadiusY = 3
            rect.Fill = self._brick_fill(base)
            rect.Stroke = SolidColorBrush(_mix(base, 0, 0.55))
            rect.StrokeThickness = 1.0
            x = FIELD_MARGIN + c * CELL_W + BRICK_GAP / 2
            y = BRICK_TOP + r * CELL_H + BRICK_GAP / 2
            Canvas.SetLeft(rect, x)
            Canvas.SetTop(rect, y)
            self.canvas.Children.Add(rect)
            # brillo superior: remata el efecto de volumen
            gloss = Rectangle()
            gloss.Width = rect.Width - 7
            gloss.Height = 3.0
            gloss.RadiusX = 1.5
            gloss.RadiusY = 1.5
            gloss.Fill = SolidColorBrush(Color.FromArgb(120, 255, 255, 255))
            Canvas.SetLeft(gloss, x + 3.5)
            Canvas.SetTop(gloss, y + 2.5)
            self.canvas.Children.Add(gloss)
            self.bricks.append({
                "x": x, "y": y,
                "w": rect.Width, "h": rect.Height,
                "kind": kind, "hp": style["hp"],
                "score": style["score"], "rect": rect,
                "gloss": gloss,
            })

        self.paddle_w = PADDLE_W
        self.expand_left = 0
        self._place_paddle()
        self._spawn_ball(on_paddle=True)
        self._update_hud()

    def _ball_speed(self):
        return min(BASE_SPEED + (self.level - 1) * 0.6, 10.0)

    def _spawn_ball(self, on_paddle=False, x=None, y=None, angle=None):
        # estela: puntos que se desvanecen siguiendo la trayectoria;
        # se agregan antes que la bola para quedar debajo de ella
        trail = []
        for i in range(TRAIL_DOTS):
            t = (i + 1.0) / TRAIL_DOTS
            r = BALL_R * (1.0 - 0.7 * t)
            dot = Ellipse()
            dot.Width = r * 2
            dot.Height = r * 2
            alpha = int(150 * (1.0 - t))
            dot.Fill = SolidColorBrush(Color.FromArgb(alpha, COL_RING.R,
                                                      COL_RING.G, COL_RING.B))
            dtr = TranslateTransform()
            dtr.X = -100
            dtr.Y = -100
            dot.RenderTransform = dtr
            Canvas.SetLeft(dot, -r)
            Canvas.SetTop(dot, -r)
            self.canvas.Children.Add(dot)
            trail.append((dot, dtr))

        el = Ellipse()
        el.Width = BALL_R * 2
        el.Height = BALL_R * 2
        el.Fill = SolidColorBrush(COL_BALL)
        el.Effect = self._glow(COL_BALL, 12)
        # posicion via transform (solo render, sin layout) para mayor fluidez
        tr = TranslateTransform()
        el.RenderTransform = tr
        Canvas.SetLeft(el, -BALL_R)
        Canvas.SetTop(el, -BALL_R)
        self.canvas.Children.Add(el)
        sp = self._ball_speed()
        if angle is None:
            angle = math.radians(random.uniform(-35, 35) - 90)
        ball = {
            "x": x if x is not None else self.paddle_x + self.paddle_w / 2,
            "y": y if y is not None else PADDLE_Y - BALL_R - 1,
            "vx": sp * math.cos(angle),
            "vy": sp * math.sin(angle),
            "glued": on_paddle,
            "el": el,
            "tr": tr,
            "trail": trail,
            "hist": [],
        }
        self.balls.append(ball)
        self._place_ball(ball)

    def _clear_balls(self):
        for b in self.balls:
            self.canvas.Children.Remove(b["el"])
            for dot, _dtr in b["trail"]:
                self.canvas.Children.Remove(dot)
        self.balls = []

    # ── colocacion visual ───────────────────────────────────────
    def _place_paddle(self):
        self.paddle.Width = self.paddle_w
        Canvas.SetLeft(self.paddle, self.paddle_x)
        Canvas.SetTop(self.paddle, PADDLE_Y)

    def _place_ball(self, ball):
        ball["tr"].X = ball["x"]
        ball["tr"].Y = ball["y"]

    def _update_hud(self):
        self.hud_score.Text = "SCORE {0:06d}".format(self.score)
        self.hud_high.Text = "HI {0:06d}".format(self.high)
        self.hud_level.Text = "NIVEL {0}".format(self.level)
        self.hud_lives.Text = u"♥ " * self.lives

    def _show_overlay(self, big, small):
        self.overlay_big.Text = big
        self.overlay_small.Text = small
        self.overlay_big.Visibility = Visibility.Visible
        self.overlay_small.Visibility = Visibility.Visible

    def _hide_overlay(self):
        self.overlay_big.Visibility = Visibility.Hidden
        self.overlay_small.Visibility = Visibility.Hidden

    # ── input ───────────────────────────────────────────────────
    def _on_mouse_move(self, sender, e):
        if self.state in (STATE_PLAYING, STATE_READY):
            mx = e.GetPosition(self.canvas).X
            self._move_paddle_to(mx - self.paddle_w / 2)

    def _on_key_down(self, sender, e):
        if e.Key == Key.Left:
            self.left_down = True
        elif e.Key == Key.Right:
            self.right_down = True
        elif e.Key == Key.Space:
            self._on_space()
        elif e.Key == Key.P:
            self._toggle_pause()
        elif e.Key == Key.Escape:
            self.win.Close()

    def _on_key_up(self, sender, e):
        if e.Key == Key.Left:
            self.left_down = False
        elif e.Key == Key.Right:
            self.right_down = False

    def _on_space(self):
        if self.state == STATE_READY:
            self.state = STATE_PLAYING
            self._hide_overlay()
            for b in self.balls:
                b["glued"] = False
        elif self.state == STATE_OVER:
            self._new_level(reset_all=True)
            self.state = STATE_READY
            self._show_overlay("RELAX", "Pulsa ESPACIO para lanzar")
        elif self.state == STATE_WIN:
            self.level += 1
            self._new_level()
            self.state = STATE_READY
            self._show_overlay("NIVEL {0}".format(self.level),
                               "Pulsa ESPACIO para lanzar")

    def _toggle_pause(self):
        if self.state == STATE_PLAYING:
            self.state = STATE_PAUSED
            self._show_overlay("PAUSA", "Pulsa P para continuar")
        elif self.state == STATE_PAUSED:
            self.state = STATE_PLAYING
            self._hide_overlay()

    def _move_paddle_to(self, x):
        self.paddle_x = max(FIELD_MARGIN,
                            min(W - FIELD_MARGIN - self.paddle_w, x))
        self._place_paddle()
        if self.state == STATE_READY:
            for b in self.balls:
                if b["glued"]:
                    b["x"] = self.paddle_x + self.paddle_w / 2
                    self._place_ball(b)

    # ── loop principal ──────────────────────────────────────────
    def _on_frame(self, sender, e):
        """Un paso de juego por frame de render, escalado por delta-time."""
        if self._closed:
            return
        now = self._watch.Elapsed.TotalSeconds
        dt = now - self._last_t
        self._last_t = now
        if dt <= 0:
            return

        # contador de tiempo jugado (se actualiza solo al cambiar de segundo)
        sec = int(now)
        if sec != self._shown_sec:
            self._shown_sec = sec
            m, s = divmod(sec, 60)
            self.hud_time.Text = "{0:02d}:{1:02d}".format(m, s)
            if sec >= TIME_ALERT_S:
                self.hud_time.Foreground = self._time_alert_brush
            elif sec >= TIME_WARN_S:
                self.hud_time.Foreground = self._time_warn_brush
        # step = 1.0 equivale a un frame clasico de 60 fps; se limita el dt
        # para no dar saltos enormes si Revit congela la ventana un instante
        step = min(dt, 0.05) * 60.0

        if self.state not in (STATE_PLAYING, STATE_READY):
            return

        if self.left_down:
            self._move_paddle_to(self.paddle_x - PADDLE_SPEED * step)
        if self.right_down:
            self._move_paddle_to(self.paddle_x + PADDLE_SPEED * step)

        if self.state != STATE_PLAYING:
            return

        if self.expand_left > 0:
            self.expand_left -= step
            if self.expand_left <= 0:
                self.expand_left = 0
                cx = self.paddle_x + self.paddle_w / 2
                self.paddle_w = PADDLE_W
                self._move_paddle_to(cx - self.paddle_w / 2)

        self._move_balls(step)
        self._move_powerups(step)

        if not self.bricks:
            self.state = STATE_WIN
            self._save_high()
            self._show_overlay("NIVEL COMPLETADO",
                               "Pulsa ESPACIO para el siguiente nivel")

    def _move_balls(self, step):
        dead = []
        for ball in self.balls:
            if ball["glued"]:
                continue

            # sub-pasos de max 1 frame-equivalente para no atravesar ladrillos
            remaining = step
            while remaining > 0:
                s = min(remaining, 1.0)
                remaining -= s
                ball["x"] += ball["vx"] * s
                ball["y"] += ball["vy"] * s

                # paredes
                if ball["x"] - BALL_R <= FIELD_MARGIN - 6:
                    ball["x"] = FIELD_MARGIN - 6 + BALL_R
                    ball["vx"] = abs(ball["vx"])
                elif ball["x"] + BALL_R >= W - FIELD_MARGIN + 6:
                    ball["x"] = W - FIELD_MARGIN + 6 - BALL_R
                    ball["vx"] = -abs(ball["vx"])
                if ball["y"] - BALL_R <= HUD_H + 8:
                    ball["y"] = HUD_H + 8 + BALL_R
                    ball["vy"] = abs(ball["vy"])

                # paleta
                if (ball["vy"] > 0 and
                        PADDLE_Y - BALL_R <= ball["y"] <= PADDLE_Y + PADDLE_H and
                        self.paddle_x - BALL_R <= ball["x"] <= self.paddle_x + self.paddle_w + BALL_R):
                    rel = (ball["x"] - (self.paddle_x + self.paddle_w / 2)) / (self.paddle_w / 2)
                    rel = max(-1.0, min(1.0, rel))
                    sp = math.sqrt(ball["vx"] ** 2 + ball["vy"] ** 2)
                    ball["vx"] = sp * rel * 0.8
                    ball["vy"] = -math.sqrt(max(sp * sp - ball["vx"] ** 2, 1.0))
                    ball["y"] = PADDLE_Y - BALL_R

                # ladrillos
                self._hit_bricks(ball)

                # fuera de pantalla
                if ball["y"] - BALL_R > H:
                    dead.append(ball)
                    break

            if ball not in dead:
                self._place_ball(ball)
                self._update_trail(ball)

        for ball in dead:
            self.canvas.Children.Remove(ball["el"])
            for dot, _dtr in ball["trail"]:
                self.canvas.Children.Remove(dot)
            self.balls.remove(ball)

        if not self.balls:
            self._lose_life()

    def _update_trail(self, ball):
        """Registra la posicion y reparte los puntos de la estela hacia atras."""
        hist = ball["hist"]
        hist.insert(0, (ball["x"], ball["y"]))
        maxlen = TRAIL_DOTS * TRAIL_SPACING + 1
        if len(hist) > maxlen:
            del hist[maxlen:]
        for i in range(len(ball["trail"])):
            dot, dtr = ball["trail"][i]
            idx = (i + 1) * TRAIL_SPACING
            if idx < len(hist):
                dtr.X = hist[idx][0]
                dtr.Y = hist[idx][1]

    def _hit_bricks(self, ball):
        for brick in self.bricks:
            bx, by, bw, bh = brick["x"], brick["y"], brick["w"], brick["h"]
            if (ball["x"] + BALL_R < bx or ball["x"] - BALL_R > bx + bw or
                    ball["y"] + BALL_R < by or ball["y"] - BALL_R > by + bh):
                continue

            # profundidad de penetracion por eje
            overlap_x = min(ball["x"] + BALL_R - bx, bx + bw - (ball["x"] - BALL_R))
            overlap_y = min(ball["y"] + BALL_R - by, by + bh - (ball["y"] - BALL_R))
            if overlap_x < overlap_y:
                ball["vx"] = -ball["vx"]
            else:
                ball["vy"] = -ball["vy"]

            brick["hp"] -= 1
            if brick["hp"] <= 0:
                self.canvas.Children.Remove(brick["rect"])
                self.canvas.Children.Remove(brick["gloss"])
                self.bricks.remove(brick)
                self.score += brick["score"] * self.level
                if random.random() < POWERUP_CHANCE:
                    self._spawn_powerup(bx + bw / 2, by + bh / 2)
            else:
                brick["rect"].Opacity = 0.5
                brick["gloss"].Opacity = 0.35
                self.score += 5
            self._update_hud()
            break  # un ladrillo por frame por bola

    def _lose_life(self):
        self.lives -= 1
        self._update_hud()
        if self.lives <= 0:
            self.state = STATE_OVER
            self._save_high()
            self._show_overlay("GAME OVER",
                               "Puntaje: {0}  —  ESPACIO para reiniciar".format(self.score))
        else:
            self.state = STATE_READY
            self._spawn_ball(on_paddle=True)
            self._show_overlay("", "Pulsa ESPACIO para lanzar")

    # ── power-ups ───────────────────────────────────────────────
    def _spawn_powerup(self, x, y):
        kind = random.choice(list(POWERUPS.keys()))
        style = POWERUPS[kind]
        el = TextBlock()
        el.Text = " {0} ".format(style["label"])
        el.FontSize = 15
        el.FontFamily = FontFamily("Consolas")
        from System.Windows import FontWeights
        el.FontWeight = FontWeights.Bold
        el.Foreground = SolidColorBrush(COL_BG)
        el.Background = SolidColorBrush(style["color"])
        el.Effect = self._glow(style["color"], 12)
        Canvas.SetLeft(el, x - 12)
        Canvas.SetTop(el, y)
        self.canvas.Children.Add(el)
        self.powerups.append({"x": x, "y": y, "kind": kind, "el": el})

    def _move_powerups(self, step):
        caught, gone = [], []
        for p in self.powerups:
            p["y"] += POWERUP_FALL * step
            Canvas.SetTop(p["el"], p["y"])
            if (PADDLE_Y - 12 <= p["y"] <= PADDLE_Y + PADDLE_H and
                    self.paddle_x - 14 <= p["x"] <= self.paddle_x + self.paddle_w + 14):
                caught.append(p)
            elif p["y"] > H:
                gone.append(p)
        for p in caught + gone:
            self.canvas.Children.Remove(p["el"])
            self.powerups.remove(p)
        for p in caught:
            self._apply_powerup(p["kind"])

    def _apply_powerup(self, kind):
        if kind == "E":
            cx = self.paddle_x + self.paddle_w / 2
            self.paddle_w = PADDLE_W_BIG
            self.expand_left = EXPAND_FRAMES
            self._move_paddle_to(cx - self.paddle_w / 2)
        elif kind == "L":
            self.lives = min(self.lives + 1, 6)
        elif kind == "S":
            for b in self.balls:
                sp = math.sqrt(b["vx"] ** 2 + b["vy"] ** 2)
                if sp > 3.5:
                    f = max(0.72, 3.5 / sp)
                    b["vx"] *= f
                    b["vy"] *= f
        elif kind == "M" and self.balls:
            src = self.balls[0]
            sp = math.sqrt(src["vx"] ** 2 + src["vy"] ** 2)
            base = math.atan2(src["vy"], src["vx"])
            for da in (-0.45, 0.45):
                self._spawn_ball(x=src["x"], y=src["y"],
                                 angle=base + da)
        self.score += 25
        self._update_hud()

    # ── cierre / persistencia ───────────────────────────────────
    def _save_high(self):
        if self.score > self.high:
            self.high = self.score
            self.cfg["highscore"] = self.high
            _save_cfg(self.cfg)
        self._update_hud()

    def _on_closed(self, sender, e):
        self._closed = True
        CompositionTarget.Rendering -= self._on_frame
        self._save_high()

    def run(self):
        self.win.ShowDialog()


if __name__ == "__main__":
    RelaxGame().run()
